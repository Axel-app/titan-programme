import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, urlunsplit
import http.cookiejar
import urllib.request

from http_client import SafeRedirect
from model import event, utc_seconds


class StructuredHTML(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.blocks, self.image, self.parts, self.capture = [], None, [], False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.capture, self.parts = True, []
        if tag == 'meta' and attrs.get('property') == 'og:image':
            self.image = attrs.get('content')

    def handle_data(self, value):
        if self.capture:
            self.parts.append(value)

    def handle_endtag(self, tag):
        if tag == 'script' and self.capture:
            try:
                self.blocks.append(json.loads(''.join(self.parts)))
            except ValueError:
                pass
            self.capture = False


def onet_starts(text):
    result = []
    for block in StructuredHTML(text).blocks:
        for item in block if isinstance(block, list) else [block]:
            if not isinstance(item, dict):
                continue
            for row in item.get('itemListElement', []):
                e = row.get('item', {})
                if e.get('@type') != 'BroadcastEvent':
                    continue
                try:
                    result.append(dict(title=e['name'], start=utc_seconds(e['startDate']), url=e['url']))
                except (KeyError, ValueError, TypeError):
                    pass
    return result


def shahid_events(data, sid):
    result = []
    for channel in data.get('items', []):
        if str(channel.get('channelId')) != str(sid):
            continue
        for e in channel.get('items', []):
            if e.get('emptySlot'):
                continue
            try:
                start, end = utc_seconds(e['actualFrom']), utc_seconds(e['actualTo'])
            except (KeyError, TypeError, ValueError):
                continue
            image = e.get('productPoster')
            if image:
                u = urlsplit(image)

                image = image.replace('{width}', '640').replace('{height}', '360').replace('{croppingPoint}', 'center')
                if '{' in image or '}' in image:
                    image = urlunsplit((u.scheme, u.netloc, u.path, '', ''))
            result.append(event(e.get('title'), start, end, image, 'shahid'))
    return result


def tvplus_events(data, sid):
    result = []
    for e in data.get('playbilllist', []):
        if str(e.get('channelid', sid)) != str(sid):
            continue
        try:
            start, end = (utc_seconds(e[k].replace(' UTC', '')) for k in ('starttime', 'endtime'))
        except (KeyError, TypeError, ValueError):
            continue
        result.append(event(e.get('name'), start, end, (e.get('picture') or {}).get('still'), 'tvplus'))
    return result


def rts_events(data, sid):
    result = []
    for channel in data.get('data', []):
        if channel.get('channel', {}).get('id') != sid:
            continue
        for e in channel.get('programList', []):
            try:
                start, end = utc_seconds(e['startTime']), utc_seconds(e['endTime'])
            except (KeyError, TypeError, ValueError):
                continue
            image = None if e.get('imageIsFallbackUrl') else e.get('imageUrl')
            result.append(event(e.get('title'), start, end, image, 'rts'))
    return result


def collect_additional(source, channels, now, fetch, rows, warnings):
    day = datetime.fromtimestamp(now, timezone.utc).date()
    if source == 'shahid':
        for offset in range(0, len(channels), 12):
            batch = channels[offset:offset + 12]
            query = urlencode(dict(csvChannelIds=','.join(c['sourceId'] for c in batch),
                **{'from': f'{day}T00:00:00.000Z', 'to': f'{day + timedelta(days=2)}T00:00:00.000Z',
                   'country': 'SA', 'language': 'ar', 'Accept-Language': 'ar'}))
            data = fetch('https://api2.shahid.net/proxy/v2.1/shahid-epg-api/?' + query, affected=batch)
            if data is not None:
                if not isinstance(data.get('items'), list):
                    for c in batch:
                        warnings[c['id']].append('shahid_invalid_schedule')
                    continue
                for c in batch:
                    rows[c['id']].extend(shahid_events(data, c['sourceId']))
        return 'Explicit ISO UTC timestamps; empty slots excluded'
    if source == 'rts':
        for date in (day, day + timedelta(days=1)):
            data = fetch(f'https://www.rts.ch/play/v3/api/rts/production/tv-program-guide?date={date}')
            if data is not None:
                if not isinstance(data.get('data'), list) or data.get('failedIlRequests'):
                    for c in channels:
                        warnings[c['id']].append('rts_incomplete_schedule')
                for c in channels:
                    rows[c['id']].extend(rts_events(data, c['sourceId']))

        missing = [c for c in channels if not any(e and e['start'] <= now < e['end'] for e in rows[c['id']])]
        if missing:
            data = fetch(f'https://www.rts.ch/play/v3/api/rts/production/tv-program-guide?date={day - timedelta(days=1)}', affected=missing)
            if data is not None:
                for c in missing:
                    rows[c['id']].extend(rts_events(data, c['sourceId']))
        return 'Explicit ISO offsets, including Europe/Zurich DST'
    if source == 'tvplus':
        base = 'https://izmaottvsc14.tvplus.com.tr:33207/EPG/JSON/'
        opener = urllib.request.build_opener(SafeRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        headers = {'Content-Type': 'application/json'}
        data = fetch(base + 'Authenticate', body=json.dumps(dict(terminaltype='webtv', terminalvendor='Mozilla/5.0',
            osversion='Win32', userType='3', utcEnable='1', timezone='Europe/Istanbul')).encode(),
            headers=headers, opener=opener, cacheable=False)
        if not data or data.get('retcode') not in (None, 0, '0'):
            raise ValueError('tvplus_visitor_unavailable')
        for c in channels:
            payload = dict(type='2', channelid=c['sourceId'], begintime=f'{day:%Y%m%d}000000',
                           endtime=f'{day + timedelta(days=2):%Y%m%d}000000', isFillProgram=1)
            data = fetch(base + 'PlayBillList', c, body=json.dumps(payload).encode(), headers=headers, opener=opener)
            if data is not None:
                if not isinstance(data.get('playbilllist'), list):
                    warnings[c['id']].append('tvplus_invalid_schedule')
                    continue
                rows[c['id']].extend(tvplus_events(data, c['sourceId']))
        return 'Explicit UTC offset in start/end; visitor session only'
    if source == 'onet':
        for c in channels:
            starts = []

            for offset in (0, 1):
                text = fetch(f'https://programtv.onet.pl/program-tv/{c["sourceId"]}?dzien={offset}', c, as_json=False)
                if text:
                    starts.extend(onet_starts(text))
            starts = sorted({(e['start'], e['title']): e for e in starts}.values(), key=lambda e:e['start'])
            if not starts:
                warnings[c['id']].append('onet_schedule_missing')
            near = 0
            for e, following in zip(starts, starts[1:]):
                image = None
                if following['start'] > now and e['start'] < now + 6 * 3600 and near < 4:
                    near += 1
                    u = urlsplit(e['url'])
                    if u.scheme == 'https' and u.netloc == 'programtv.onet.pl' and u.path.startswith('/tv/'):

                        detail = fetch(e['url'], c, as_json=False, ttl=86400)
                        if detail:
                            image = StructuredHTML(detail).image
                rows[c['id']].append(event(e['title'], e['start'], following['start'], image, 'onet'))
        return 'JSON-LD explicit UTC offsets; end from next start, bounded cached image enrichment'
    raise ValueError('unsupported_source')
