
import re
import json
import urllib.error
import urllib.request
import http.cookiejar
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin

from model import compact_events, event, utc_seconds
from http_client import SafeRedirect


class PassportHTML(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.zone = None
        self.in_selector = False
        self.rows = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select' and attrs.get('id') == 'timezone_selector':
            self.in_selector = True
        if tag == 'option' and self.in_selector and 'selected' in attrs:
            self.zone = attrs.get('value')
        if 'data-st' in attrs and 'data-duration' in attrs:
            self.rows.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'select':
            self.in_selector = False


def passport_events(text):
    page = PassportHTML(text)
    if not page.zone:
        raise ValueError('tvpassport_timezone_missing')
    result = []
    for row in page.rows:
        try:
            start = utc_seconds(row['data-st'], page.zone)
            duration = int(row['data-duration']) * 60
        except (ValueError, KeyError):
            continue
        title = row.get('data-showname', '')
        if title in ('Movie', 'Cinéma'):
            title = row.get('data-episodetitle') or title
        picture = row.get('data-showpicture')
        image = urljoin('https://cdn.tvpassport.com/image/show/960x540/', picture) if picture else None
        result.append(event(title, start, start + duration, image, 'tvpassport'))
    return result, page.zone


class MiHTML(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.in_list = False
        self.row = None
        self.capture = None
        self.rows = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get('class') or '').split()
        if tag == 'ul' and 'broadcasts' in classes:
            self.in_list = True
        if tag == 'li' and self.in_list:
            self.row = dict(time='', title='', image=None)
        if self.row is None:
            return
        if tag == 'h2':
            self.capture = 'title'
        if tag == 'span' and 'time' in classes:
            self.capture = 'time'
        if 'image' in classes:
            match = re.search(r'background-image\s*:\s*url\([\s\x27\x22]*(.*?)[\s\x27\x22]*\)', attrs.get('style', ''))
            if match:
                self.row['image'] = match[1]

    def handle_data(self, data):
        if self.row is not None and self.capture:
            self.row[self.capture] += data

    def handle_endtag(self, tag):
        if tag in ('h2', 'span'):
            self.capture = None
        if tag == 'li' and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        if tag == 'ul':
            self.in_list = False


def mitv_starts(text, date):
    result = []
    previous = None
    day = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
    for row in MiHTML(text).rows:
        try:
            h, m = map(int, row['time'].strip().split(':'))
            start = day.replace(hour=h, minute=m)
        except ValueError:
            continue
        if previous and start < previous:
            day += timedelta(days=1)
            start += timedelta(days=1)
        previous = start
        result.append(dict(title=row['title'], start=int(start.timestamp()), image=row['image']))
    return result


def mitv_events(starts):

    items = sorted({(r['start'], r['title']): r for r in starts}.values(), key=lambda r: r['start'])
    return [event(r['title'], r['start'], following['start'], r['image'], 'mitv')
            for r, following in zip(items, items[1:])]


def sky_events(data, sid):
    result = []
    for schedule in data.get('schedule', []):
        if str(schedule.get('sid')) != sid:
            continue
        for item in schedule.get('events', []):
            uuid = item.get('programmeuuid')
            image = f'https://images.metadata.sky.com/pd-image/{uuid}/16-9/640' if uuid else None
            try:
                start, duration = int(item['st']), int(item['d'])
            except (ValueError, KeyError, TypeError):
                continue
            result.append(event(item.get('t'), start, start + duration, image, 'sky'))
    return result


def movistar_events(data, sid):
    result = []
    if not isinstance(data, list):
        raise ValueError('movistar_invalid_schedule')
    for item in data:
        if item.get('Canal', {}).get('CodCadenaTv') != sid:
            continue
        pictures = {p.get('id'): p.get('uri') for p in item.get('Imagenes', [])}

        image = pictures.get('watch2tgr-end') or pictures.get('horizontal')
        try:
            start, end = int(item['FechaHoraInicio']) // 1000, int(item['FechaHoraFin']) // 1000
        except (ValueError, KeyError, TypeError):
            continue
        result.append(event(item.get('Titulo'), start, end, image, 'movistar'))
    return result


def skyit_events(data, sid):
    result = []
    for item in data.get('events', []):
        if str(item.get('channel', {}).get('id')) != sid:
            continue
        images = {p.get('key'): (p.get('img') or {}).get('url')
                  for p in item.get('content', {}).get('imagesMap', [])}


        image = next((images[key] for key in ('scene', 'scene_key_art', 'background', 'cover', 'cover_clean')
                      if images.get(key) and not re.search(r'/uuid/[A-Za-z]+_(?:Scene|Cover|Background)_[^/?]+\.png(?:\?|$)', images[key])), None)
        try:
            start, end = utc_seconds(item['starttime']), utc_seconds(item['endtime'])
        except (ValueError, KeyError):
            continue
        result.append(event(item.get('eventTitle'), start, end,
                            urljoin('https://guidatv.sky.it', image) if image else None, 'skyit'))
    return result


def rai_events(data, date):
    result = []
    for item in data.get('events', []):
        try:
            local_date = datetime.strptime(item['date'], '%d/%m/%Y').date() if item.get('date') else date
            start = utc_seconds(f'{local_date}T{item["hour"]}', 'Europe/Rome')
            duration = int(re.fullmatch(r'(\d+)\s*(?:min)?', str(item['duration_in_minutes'])).group(1)) * 60
        except (ValueError, KeyError, AttributeError):
            continue
        image = urljoin('https://www.raiplay.it', item['image']) if item.get('image') else None
        if image and 'placeholder' in image.lower():
            image = None
        result.append(event(item.get('name'), start, start + duration, image, 'rai'))
    return result


def magenta_events(data, sid):
    result = []
    for item in data.get('playbilllist', []):
        if str(item.get('channelid')) != sid:
            continue
        try:
            start, end = (utc_seconds(item[key].replace(' UTC', '')) for key in ['starttime', 'endtime'])
        except (ValueError, KeyError):
            continue
        images = []
        for p in item.get('pictures', []):
            try:
                width, height = map(int, p['resolution'])
            except (ValueError, KeyError, TypeError):
                continue
            if height > 0 and 1.7 < width / height < 1.85:
                images.append((width, p.get('href')))
        image = sorted(images, key=lambda i: i[0])[0][1] if images else None
        if image and image.startswith('http://ngiss.t-online.de/'):
            image = 'https://' + image.removeprefix('http://')
        result.append(event(item.get('name'), start, end, image, 'magenta'))
    return result


def vodafone_events(data):
    result = []
    for item in data.get('result', {}).get('objects', []):
        pictures = {p.get('imageTypeName'): p.get('url') for p in item.get('images', [])}

        image = pictures.get('bg') or pictures.get('cc')
        if image and re.fullmatch(r'https://3038\.images-vfp2\.ott\.kaltura\.com/Service\.svc/GetImage/p/3038/entry_id/[a-zA-Z0-9_-]+/version/\d+', image):
            image += '/width/640/height/360'
        result.append(event(item.get('name'), item.get('startDate'), item.get('endDate'), image, 'vodafone'))
    return result


def collect_group(client, channels, now):
    source, country = channels[0]['source'], channels[0]['country']
    day = datetime.fromtimestamp(now, timezone.utc).date()
    dates = [day, day + timedelta(days=1)]
    rows = {c['id']: [] for c in channels}
    warnings = {c['id']: [] for c in channels}
    def fetch(url, channel=None, as_json=True, affected=None, **kwargs):
        try:
            kwargs.setdefault('ttl', 900 if source in ('oqee', 'sky') else 1800)
            data = client.get(url, **kwargs)
            return json.loads(data) if as_json else data.decode('utf-8')
        except Exception as error:
            message = f'HTTP {error.code}' if isinstance(error, urllib.error.HTTPError) else type(error).__name__
            for c in (affected or ([channel] if channel else channels)):
                warnings[c['id']].append(message)
            return None
    timing = 'source-format-checked'
    basis = ''
    if source == 'oqee':

        for hour in (now // 3600 * 3600, (now // 3600 + 1) * 3600):
            guide = fetch(f'https://api.oqee.net/api/v1/epg/all/{hour}')
            if guide is None:
                continue
            if not guide.get('success'):
                raise ValueError('oqee_unsuccessful')
            for channel in channels:
                for item in guide['result']['entries'].get(channel['sourceId'], []):
                    live = item.get('live') or {}
                    rows[channel['id']].append(event(live.get('title'), live.get('start'), live.get('end'),
                                                     (item.get('pictures') or {}).get('main'), source))
        basis = 'UTC epoch seconds'
    elif source == 'sky':

        for offset in range(0, len(channels), 20):
            batch = channels[offset:offset + 20]
            ids = ','.join(c['sourceId'] for c in batch)
            for date in dates:
                data = fetch(f'https://awk.epgsky.com/hawk/linear/schedule/{date:%Y%m%d}/{ids}',
                             headers={'X-SkyOTT-Territory': country}, affected=batch)
                if data is None:
                    continue
                if not isinstance(data.get('schedule'), list):
                    for c in batch:
                        warnings[c['id']].append('sky_invalid_schedule')
                    continue
                for channel in batch:
                    rows[channel['id']].extend(sky_events(data, channel['sourceId']))
        basis = 'UTC epoch seconds + duration'
    elif source == 'magenta':

        base = 'https://api.prod.sngtv.magentatv.de/EPG/JSON/'
        opener = urllib.request.build_opener(SafeRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        auth = fetch(base + 'Authenticate?SID=firstup&T=Windows_chrome_118',
                     body=json.dumps(dict(terminalid='00:00:00:00:00:00', mac='00:00:00:00:00:00',
                         terminaltype='WEBTV', utcEnable=1, timezone='Etc/GMT0', userType=3, terminalvendor='Unknown')).encode(),
                     headers={'Content-Type': 'application/json'}, opener=opener, cacheable=False)
        if not auth or not auth.get('csrfToken'):
            raise ValueError('magenta_visitor_session_unavailable')
        for channel in channels:
            payload = dict(count=-1, isFillProgram=1, offset=0,
                           properties=[dict(include='endtime,id,name,starttime,channelid,pictures', name='playbill')],
                           type=2, begintime=day.strftime('%Y%m%d000000'),
                           endtime=(day + timedelta(days=2)).strftime('%Y%m%d000000'), channelid=channel['sourceId'])
            data = fetch(base + 'PlayBillList', channel, body=json.dumps(payload).encode(), opener=opener,
                         headers={'Content-Type': 'application/json', 'X_CSRFTOKEN': auth['csrfToken']})
            if data is not None:
                if not isinstance(data.get('playbilllist'), list):
                    warnings[channel['id']].append('magenta_schedule_unavailable')
                    continue
                rows[channel['id']].extend(magenta_events(data, channel['sourceId']))
        basis = 'Visitor guide requests UTC; timestamps include explicit UTC+00:00'
    elif source == 'rai':
        for channel in channels:
            for date in dates:
                data = fetch(f'https://www.raiplay.it/palinsesto/app/{channel["sourceId"]}/{date:%d-%m-%Y}.json', channel)
                if data is not None:
                    if not isinstance(data.get('events'), list):
                        warnings[channel['id']].append('rai_schedule_unavailable')
                        continue
                    rows[channel['id']].extend(rai_events(data, date))
        basis = 'Explicit programme date and local time Europe/Rome → UTC'
    elif source == 'skyit':
        for offset in range(0, len(channels), 8):
            batch = channels[offset:offset + 8]
            query = urlencode({'from': f'{day}T00:00:00Z', 'to': f'{day + timedelta(days=2)}T00:00:00Z',
                               'pageSize': 999, 'pageNum': 0, 'env': 'DTH',
                               'channels': ','.join(c['sourceId'] for c in batch)})
            data = fetch('https://apid.sky.it/gtv/v1/events?' + query, affected=batch)
            if data is not None:
                if not isinstance(data.get('events'), list) or data.get('total', 0) > 999:
                    for channel in batch:
                        warnings[channel['id']].append('skyit_invalid_or_truncated_schedule')
                    continue
                for channel in batch:
                    rows[channel['id']].extend(skyit_events(data, channel['sourceId']))
        basis = 'ISO 8601 with explicit UTC offset'
    elif source == 'movistar':
        for channel in channels:
            for date in dates:
                query = urlencode(dict(channel=channel['sourceId'], **{
                    'from': f'{date}T00:00:00', 'span': 1, 'version': 8, 'mdrm': 'true',
                    'tlsstream': 'true', 'demarcation': 18}))
                data = fetch('https://ottcache.dof6.com/movistarplus/webplayer/OTT/epg?' + query, channel)
                if data is None:
                    continue
                try:
                    rows[channel['id']].extend(movistar_events(data, channel['sourceId']))
                except ValueError:
                    warnings[channel['id']].append('movistar_invalid_schedule')
        basis = 'UTC epoch milliseconds → seconds'
    elif source == 'vodafone':


        window_start = now // 21600 * 21600 - 21600
        windows = [datetime.fromtimestamp(window_start + i * 21600, timezone.utc) for i in range(4)]
        for channel in channels:
            for date in windows:
                period = ('00-06', '06-12', '12-18', '18-00')[date.hour // 6]
                url = f'https://cdn.pt.vtv.vodafone.com/epg/{channel["sourceId"]}/{date:%Y/%m/%d}/{period}'
                data = fetch(url, channel, headers={'Origin': 'https://www.vodafone.pt', 'Referer': 'https://www.vodafone.pt/'})
                if data is not None:
                    if not isinstance(data.get('result', {}).get('objects'), list):
                        warnings[channel['id']].append('vodafone_invalid_schedule')
                        continue
                    rows[channel['id']].extend(vodafone_events(data))
        basis = 'UTC epoch seconds; separate channel endpoints'
    elif source == 'meo':
        timing = 'pending'
        basis = 'Naive StartDate/EndDate: UTC hypothesis; do not activate before verification'
        for channel in channels:
            for date in dates:
                query = urlencode(dict(callLetter=channel['sourceId'], date=str(date), userAgent='IPTV_OFR_GTV'))
                data = fetch('https://meogouser.apps.meo.pt/Services/GridTv/GridTv.svc/GetLiveChannelProgramsByDate?' + query, channel,
                                   headers={'Origin': 'https://www.meo.pt', 'Referer': 'https://www.meo.pt/'})
                if data is None:
                    continue
                if not isinstance(data.get('Result'), list):
                    raise ValueError('meo_invalid_schedule')
                for item in data['Result']:
                    if item.get('CallLetter') != channel['sourceId']:
                        continue
                    try:
                        start, end = (utc_seconds(item[key], 'UTC') for key in ['StartDate', 'EndDate'])
                    except (ValueError, KeyError):
                        continue
                    query = urlencode(dict(chCallLetter=channel['sourceId'], progTitle=item.get('Title', ''),
                                           profile='16_9', profileFallback='false', noFallback='true',
                                           appSource='PC_CHROME_PWA', width=640))
                    image = 'https://cdn-er-images.online.meo.pt/eemstb/ImageHandler.ashx?' + query
                    rows[channel['id']].append(event(item.get('Title'), start, end, image, source))
    elif source == 'tvpassport':
        zones = set()
        for channel in channels:

            for date in (day, day - timedelta(days=1), day + timedelta(days=1)):
                if date < day and any(e and e['start'] <= now < e['end'] for e in rows[channel['id']]):
                    continue
                url = f'https://www.tvpassport.com/tv-listings/stations/{channel["sourceId"]}/{date}'
                html = fetch(url, channel, as_json=False)
                if html is None:
                    continue
                try:
                    events, zone = passport_events(html)
                except ValueError:
                    warnings[channel['id']].append('tvpassport_timezone_missing')
                    continue
                zones.add(zone)
                rows[channel['id']].extend(events)
        basis = 'Selected page timezone → UTC: ' + ', '.join(sorted(zones))
    elif source == 'mitv':
        for channel in channels:
            starts = []
            for date in dates:

                text = fetch(f'https://mi.tv/{country.lower()}/async/channel/{channel["sourceId"]}/{date}/0', channel, as_json=False)
                if text is None:
                    continue
                if 'broadcasts' not in text:
                    warnings[channel['id']].append('mitv_schedule_missing')
                    continue
                starts.extend(mitv_starts(text, str(date)))
            rows[channel['id']].extend(mitv_events(starts))
        basis = 'UTC request offset=0; end from next programme start'
    else:
        raise ValueError('unsupported_source')
    return [dict(c, timingStatus=timing, timeBasis=basis,
                 programmes=compact_events(rows[c['id']], now), fetchedAt=now,
                 sourceErrors=warnings[c['id']]) for c in channels]
