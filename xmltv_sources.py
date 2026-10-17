#!/usr/bin/env python3
"""Public XMLTV guides with programme pictures, downloaded at most twice a day.

Each guide is fetched with a conditional request, then reduced once to the programmes that carry
a picture inside the publication window. Later collections reuse that reduced copy until the
guide is due again, so the 30-minute schedule never re-downloads or re-parses the large files.
"""

import gzip
import io
import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from http_client import SafeRedirect, atomic_write

GUIDE_HOSTS = {'xmltvfr.fr', 'epgshare01.online', 'i.mjh.nz'}
REFRESH_SECONDS = 12 * 3600
WINDOW_BEFORE = 3 * 3600
WINDOW_AFTER = 48 * 3600
MAX_GUIDE_BYTES = 400 * 1024 * 1024
MAX_TITLE = 200
MAX_IMAGE_URL = 1024
MAX_NAMES = 12

# epgshare01 files where most programmes carry a picture (measured 2026-10-17). The others are
# left out: without pictures they add size, not artwork.
EPGSHARE_FILES = (
    'AE1', 'AL1', 'AT1', 'AU1', 'BA1', 'BE2', 'CA2', 'CH1', 'DE1', 'DK1', 'ES1', 'FI1', 'FR1',
    'GR1', 'HK1', 'HR1', 'IE1', 'IL1', 'IN1', 'IN4', 'IT1', 'JP1', 'LT1', 'LV1', 'MT1', 'MY1',
    'NL1', 'NO1', 'PLEX1', 'PT1', 'RO2', 'RS1', 'SE1', 'SK1', 'TR3', 'UK1', 'US_LOCALS1',
    'US_SPORTS1', 'US2', 'UY1',
)
# Files whose channels belong to one platform rather than one country.
EPGSHARE_PLATFORM_COUNTRY = {'PLEX1': 'US', 'US_LOCALS1': 'US', 'US_SPORTS1': 'US', 'US2': 'US', 'UK1': 'GB'}
MJH_REGIONS = {
    'PlutoTV': ('ar', 'br', 'ca', 'cl', 'de', 'dk', 'es', 'fr', 'gb', 'it', 'mx', 'no', 'se', 'us'),
    'SamsungTVPlus': ('at', 'ca', 'ch', 'de', 'es', 'fr', 'gb', 'in', 'it', 'kr', 'us'),
}


def guides():
    """(name, url, rank, country) for every guide. Lower rank wins when two describe one channel."""
    result = [('xmltvfr', 'https://xmltvfr.fr/xmltv/xmltv.xml.gz', 1, None)]
    for code in EPGSHARE_FILES:
        country = EPGSHARE_PLATFORM_COUNTRY.get(code, code[:2])
        result.append((f'epgshare-{code.lower()}', f'https://epgshare01.online/epgshare01/epg_ripper_{code}.xml.gz', 2, country))
    for platform, regions in MJH_REGIONS.items():
        for region in regions:
            result.append((f'{platform.lower()}-{region}', f'https://i.mjh.nz/{platform}/{region}.xml.gz', 3, region.upper()))
    result.append(('roku-us', 'https://i.mjh.nz/Roku/all.xml.gz', 3, 'US'))
    result.append(('dstv-za', 'https://i.mjh.nz/DStv/za.xml.gz', 3, 'ZA'))
    result.append(('skygo-nz', 'https://i.mjh.nz/SkyGo/epg.xml.gz', 3, 'NZ'))
    return result


# i.mjh.nz answers with a redirect to the files of its own GitHub repository.
MIRROR_HOSTS = {'github.com', 'raw.githubusercontent.com'}
MIRROR_PATH = '/matthuisman/i.mjh.nz/'


def validate_guide_url(url):
    u = urlsplit(url)
    allowed = u.hostname in GUIDE_HOSTS or (u.hostname in MIRROR_HOSTS and u.path.startswith(MIRROR_PATH))
    if u.scheme != 'https' or not allowed or u.username or u.password or u.port not in (None, 443):
        raise ValueError('guide_host_not_allowed')


def xmltv_time(value):
    value = (value or '').strip()
    try:
        return int(datetime.strptime(value[:20].strip(), '%Y%m%d%H%M%S %z').timestamp())
    except ValueError:
        return 0


def picture(element):
    for icon in element.findall('icon'):
        src = (icon.get('src') or '').strip()
        if clean_image(src):
            return clean_image(src)
    for image in element.findall('image'):
        if clean_image((image.text or '').strip()):
            return clean_image(image.text.strip())
    return None


def clean_image(value):
    if not value or len(value) > MAX_IMAGE_URL or any(ord(c) < 32 or c in ' "<>\\' for c in value):
        return None
    u = urlsplit(value)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
        return None
    return value


def country_of(channel_id, fallback):
    channel_id = channel_id.split('@', 1)[0]
    suffix = channel_id.rsplit('.', 1)[-1] if '.' in channel_id else ''
    if re.fullmatch(r'[A-Za-z]{2}', suffix):
        code = suffix.upper()
        return 'GB' if code == 'UK' else code
    return fallback


def reduce_guide(stream, anchor, fallback_country):
    """Channels with at least one illustrated programme inside [anchor-3h, anchor+48h]."""
    low, high = anchor - WINDOW_BEFORE, anchor + WINDOW_AFTER
    names, events = {}, {}
    for _, element in ET.iterparse(stream):
        if element.tag == 'channel':
            channel_id = (element.get('id') or '').strip()
            if channel_id:
                names[channel_id] = [n.text.strip() for n in element.findall('display-name') if n.text and n.text.strip()][:MAX_NAMES]
            element.clear()
        elif element.tag == 'programme':
            start, end = xmltv_time(element.get('start')), xmltv_time(element.get('stop'))
            if start and low < end and start < high and 0 < end - start <= 86400:
                image = picture(element)
                title = ' '.join((element.findtext('title') or '').split())[:MAX_TITLE]
                if image and title:
                    events.setdefault(element.get('channel') or '', {})[(start, end)] = [start, end, title, image]
            element.clear()
    channels = []
    for channel_id, programmes in events.items():
        if channel_id:
            channels.append(dict(id=channel_id, names=names.get(channel_id) or [channel_id],
                                 country=country_of(channel_id, fallback_country),
                                 programmes=sorted(programmes.values())))
    return channels


class GuideStore:
    """Conditional downloads into `.cache/xmltv`, reduced copies into `output/xmltv`."""

    def __init__(self, cache, output, clock=time.time, opener=None, pause=1.0):
        self.cache, self.output = Path(cache) / 'xmltv', Path(output) / 'xmltv'
        self.clock, self.pause = clock, pause
        self.opener = opener or urllib.request.build_opener(SafeRedirect(validate_guide_url))
        self.stats = dict(downloaded=0, notModified=0, reused=0, failed=0, bytes=0)

    def meta(self, name):
        try:
            return json.loads((self.cache / f'{name}.json').read_text())
        except (OSError, ValueError):
            return {}

    def due(self, name, now):
        meta = self.meta(name)
        return not (0 <= now - meta.get('checkedAt', 0) < REFRESH_SECONDS) and now >= meta.get('retryAt', 0)

    def download(self, name, url, now, conditional):
        """True when a new body was stored; False when the reduction already describes the guide."""
        validate_guide_url(url)
        meta = self.meta(name)
        body = self.cache / f'{name}.xml.gz'
        headers = {'User-Agent': 'TitanProgrammePilot/0.2 (daily TV guide pictures)'}
        if conditional and meta.get('etag'):
            headers['If-None-Match'] = meta['etag']
        if conditional and meta.get('lastModified'):
            headers['If-Modified-Since'] = meta['lastModified']
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers), timeout=120) as response:
                if int(response.headers.get('Content-Length') or 0) > MAX_GUIDE_BYTES:
                    raise ValueError('guide_too_large')
                data = response.read(MAX_GUIDE_BYTES + 1)
                if len(data) > MAX_GUIDE_BYTES:
                    raise ValueError('guide_too_large')
                etag, modified = response.headers.get('ETag'), response.headers.get('Last-Modified')
        except urllib.error.HTTPError as error:
            if error.code == 304 and conditional:
                meta['checkedAt'] = now
                atomic_write(self.cache / f'{name}.json', json.dumps(meta).encode())
                self.stats['notModified'] += 1
                return False
            raise
        atomic_write(body, data)
        atomic_write(self.cache / f'{name}.json', json.dumps(dict(etag=etag, lastModified=modified, checkedAt=now)).encode())
        self.stats['downloaded'] += 1
        self.stats['bytes'] += len(data)
        return True

    def reduced(self, name, url, rank, country, now):
        """Summary {name, rank, anchor, path} of one guide's reduction, refreshed when the guide is due.

        The reduction itself stays on disk: the 30-minute collections only read these summaries.
        """
        target, summary_file = self.output / f'{name}.json', self.output / f'{name}.summary.json'
        try:
            previous = json.loads(summary_file.read_bytes()) if target.exists() else None
        except (OSError, ValueError):
            previous = None
        fresh_window = previous and now < previous['anchor'] + REFRESH_SECONDS + 6 * 3600
        if previous and fresh_window and not self.due(name, now):
            self.stats['reused'] += 1
            return previous
        try:
            # The window must move forward: an old reduction needs the guide again, not a 304.
            changed = self.download(name, url, now, conditional=bool(previous and fresh_window))
            time.sleep(self.pause)
        except Exception as error:
            meta = self.meta(name)
            failures = min(meta.get('failures', 0) + 1, 6)
            meta.update(failures=failures, retryAt=now + min(REFRESH_SECONDS, 900 * 2 ** (failures - 1)), error=type(error).__name__)
            atomic_write(self.cache / f'{name}.json', json.dumps(meta).encode())
            self.stats['failed'] += 1
            # Keep serving the previous reduction while it still covers the coming hours.
            return previous if previous and now < previous['anchor'] + WINDOW_AFTER - 6 * 3600 else None
        if previous and not changed and fresh_window:
            return previous
        body = self.cache / f'{name}.xml.gz'
        raw = body.read_bytes()
        stream = gzip.GzipFile(fileobj=io.BytesIO(raw)) if raw[:2] == b'\x1f\x8b' else io.BytesIO(raw)
        try:
            channels = reduce_guide(stream, now, country)
        except (ET.ParseError, EOFError, OSError):
            self.stats['failed'] += 1
            return previous
        finally:
            body.unlink(missing_ok=True)
        atomic_write(target, json.dumps(channels, ensure_ascii=False, separators=(',', ':')).encode())
        summary = dict(name=name, rank=rank, anchor=now, path=str(target), channels=len(channels))
        atomic_write(summary_file, json.dumps(summary).encode())
        return summary

    def reduced_local(self, name, path, rank, now):
        """Summary of a guide produced on this machine (iptv-org grabbers), reduced when it changed."""
        path = Path(path)
        target, summary_file = self.output / f'{name}.json', self.output / f'{name}.summary.json'
        stat = path.stat()
        source = f'{stat.st_size}:{int(stat.st_mtime)}'
        try:
            previous = json.loads(summary_file.read_bytes()) if target.exists() else None
        except (OSError, ValueError):
            previous = None
        if previous and previous.get('source') == source and now < previous['anchor'] + REFRESH_SECONDS + 6 * 3600:
            self.stats['reused'] += 1
            return previous
        raw = path.read_bytes()
        stream = gzip.GzipFile(fileobj=io.BytesIO(raw)) if raw[:2] == b'\x1f\x8b' else io.BytesIO(raw)
        try:
            channels = reduce_guide(stream, now, None)
        except (ET.ParseError, EOFError, OSError):
            self.stats['failed'] += 1
            return previous
        atomic_write(target, json.dumps(channels, ensure_ascii=False, separators=(',', ':')).encode())
        summary = dict(name=name, rank=rank, anchor=now, path=str(target), channels=len(channels), source=source)
        atomic_write(summary_file, json.dumps(summary).encode())
        return summary

    def collect(self, now=None, selection=None, local=None):
        """Summaries of every guide still usable; see [reduced]. [local]: folder of grabbed guides."""
        now = int(self.clock() if now is None else now)
        reductions = []
        for name, url, rank, country in guides():
            if selection and name not in selection:
                continue
            reduced = self.reduced(name, url, rank, country, now)
            if reduced:
                reductions.append(reduced)
        for path in sorted(Path(local).glob('*.xml*')) if local and Path(local).is_dir() else []:
            # The grabbed guide comes from third-party code: plain files only, never a link elsewhere.
            if path.is_symlink() or not path.is_file():
                continue
            name = 'iptvorg-' + path.name.split('.', 1)[0]
            reduced = self.reduced_local(name, path, 4, now)
            if reduced:
                reductions.append(reduced)
        return reductions


def load_channels(summary):
    return json.loads(Path(summary['path']).read_bytes())
