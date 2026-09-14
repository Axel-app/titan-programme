
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

IMAGE_HOSTS = {
    'oqee': ('oqee.net',),
    'sky': ('images.metadata.sky.com',),
    'skyit': ('guidatv.sky.it',),
    'rai': ('www.raiplay.it',),
    'magenta': ('ngiss.t-online.de',),
    'vodafone': ('3038.images-vfp2.ott.kaltura.com',),
    'movistar': ('estatico.emisiondof6.com',),
    'meo': ('cdn-er-images.online.meo.pt',),
    'tvpassport': ('cdn.tvpassport.com',),
    'mitv': ('cdn.mitvstatic.com',),
    'shahid': ('shahid.mbc.net',),
    'tvplus': ('izmaottvsc14.tvplus.com.tr', 'gbzottvsc13.tvplus.com.tr'),
    'onet': ('cdn.programtv.onet.pl',),
    'rts': ('kingfisher.rts.ch', 'img.rts.ch', 'il.srgssr.ch'),
}


def image_url(value, source):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    value = value.replace('%d', '300')
    try:
        url = urlsplit(value)
        allowed = IMAGE_HOSTS[source]
        ports = (None, 443, 33207) if source == 'tvplus' else (None, 443)
        if (url.scheme != 'https' or url.username or url.password or url.port not in ports
                or not any(url.hostname == h or (source == 'oqee' and url.hostname
                           and url.hostname.endswith('.' + h)) for h in allowed)):
            return None
        if any(ord(c) < 32 for c in value):
            return None
        return value
    except (ValueError, KeyError):
        return None


def utc_seconds(value, zone=None):

    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo:
        return int(dt.timestamp())
    if zone is None:
        raise ValueError('timezone_missing')
    tz = ZoneInfo(zone)
    candidates = set()
    for fold in (0, 1):
        aware = dt.replace(tzinfo=tz, fold=fold)
        if aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == dt:
            candidates.add(int(aware.timestamp()))
    if len(candidates) != 1:
        raise ValueError('timezone_ambiguous_or_nonexistent')
    return candidates.pop()


def event(title, start, end, image, source):
    title = ' '.join(str(title or '').split())[:300]
    try:
        start, end = int(start), int(end)
    except (ValueError, TypeError, OverflowError):
        return None
    if not title or start <= 0 or not 0 < end - start <= 86400:
        return None
    return dict(title=title, start=start, end=end, imageURL=image_url(image, source))


def compact_events(events, now):
    unique = {}
    for e in events:
        if e and e['end'] > now - 3600 and e['start'] < now + 30 * 3600:
            key = (e['start'], e['end'], e['title'])
            if key not in unique or e.get('imageURL'):
                unique[key] = e
    return sorted(unique.values(), key=lambda e: (e['start'], e['end'], e['title']))


def current(events, now):
    candidates = [e for e in events if e['start'] <= now < e['end']]

    if len({(e['start'], e['end'], e['title']) for e in candidates}) != 1:
        return None
    return next((e for e in candidates if e.get('imageURL')), candidates[0])


def name_key(value):
    value = ''.join(c for c in unicodedata.normalize('NFKD', value.lower())
                    if not unicodedata.combining(c))
    value = re.sub(r'\b(sd|hd|fhd|hevc|h265|h264)\+(?![\w+])', r'\1', value)
    value = re.sub(r'\b(sd|hd|fhd|hevc|h265|h264|720p|1080p)\b', '', value)

    value = re.sub(r'\buhd\b', '4k', value)
    return ''.join(c for c in value.replace('+', 'plus') if c.isalnum())


def resolve_channel(catalogue, country, name):
    country = country.upper()

    tokens = {'GB': ['GB', 'UK'], 'US': ['US', 'USA']}.get(country, [country])
    name = re.sub(r'^\s*(?:\[(?:' + '|'.join(tokens) + r')\]|\|(?:' +
                  '|'.join(tokens) + r')\||(?:' + '|'.join(tokens) + r')\s*[:|])\s*',
                  '', name, flags=re.I)
    key = name_key(name)
    matches = [c for c in catalogue if c['country'] == country
               and any(name_key(alias) == key for alias in [c['name']] + c['aliases'])]
    return matches[0] if len(matches) == 1 else None
