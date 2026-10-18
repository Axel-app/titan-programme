#!/usr/bin/env python3
"""Schema 2: one channel directory per edition, illustrated programmes for today and tomorrow in
stable buckets, and a small live file refreshed from the operator collectors.

Day buckets only change when a public guide of their edition was refreshed (about twice a day,
more often where Samsung TV Plus feeds the edition), so TVs revalidate them with an ETag instead of
downloading them again. The live file carries the operator programmes
of the coming hours, which correct the day buckets on services whose guide changes at short notice.
"""

import hashlib
import json
import math
import re
import unicodedata
import zlib
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from http_client import atomic_write
from model import name_key
from xmltv_sources import load_channels

SCHEMA = 2
BUCKET_BYTES = 512 * 1024
MAX_BUCKETS = 256
LIVE_BEFORE, LIVE_AFTER = 3600, 6 * 3600
MENA = {'AE', 'SA', 'EG', 'QA', 'KW', 'BH', 'OM', 'JO', 'LB', 'SY', 'IQ', 'MA', 'DZ', 'TN', 'LY', 'PS', 'YE', 'SD'}


def edition(country):
    country = (country or '').upper()
    if country == 'UK':
        return 'GB'
    if country in MENA or country == 'MENA':
        return 'MENA'
    return country if re.fullmatch(r'[A-Z]{2}', country) else None


def id_key(value):
    """'France.24.Arabic.ae', 'France24Arabic.ae@SD' → 'france24arabic.ae'; other ids → ''."""
    value = (value or '').split('@', 1)[0].strip()
    match = re.fullmatch(r'(.+)\.([A-Za-z]{2})', value)
    if not match:
        return ''
    stem = ''.join(c for c in unicodedata.normalize('NFKD', match[1].lower()) if c.isalnum() and not unicodedata.combining(c))
    return f'{stem}.{match[2].lower()}' if stem else ''


def channel_name_key(value):
    value = re.sub(r'^\s*[\[(|]?\s*[A-Za-z]{2,3}\s*[\])|:]\s*', '', value or '')
    return name_key(value)


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def coverage(programmes):
    return sum(end - start for start, end, *_ in programmes)


class Union:
    def __init__(self):
        self.parent = {}

    def find(self, item):
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def join(self, a, b):
        self.parent[self.find(a)] = self.find(b)


# Picture services that resize on request: cards get about their own size (480x270) instead of
# originals of 1920x1080 or more (Pluto's bare pictures weigh 1 MB, SFR's about 200 KB).
SMALL_PICTURES = {
    'images.pluto.tv': dict(fit='fill', fill='blur', fm='jpg', q='75', w='480', h='270'),
    'dtil.tmsimg.com': dict(w='480', h='270'),
    'static-cdn.tv.sfr.net': dict(w='480'),
}


def small_picture(url):
    parts = urlsplit(url or '')
    size = SMALL_PICTURES.get((parts.hostname or '').lower())
    if not size or parts.username or parts.password or parts.port:
        return url
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(size)
    return urlunsplit(parts._replace(scheme='https', query=urlencode(query)))


# Pictures that are the provider's own "no picture" icon (measured on every edition 2026-10-18).
# Genre photos and channel visuals stay: only these say nothing about the programme, which then
# gets the channel logo or its TMDB picture in the apps.
NO_PICTURE = {
    'mediasb2c.mts.rs/medias/Left-Side-155x88-.jpg',  # MTS: grey "picture" icon
    'dtil.tmsimg.com/tvbanners/generic/generic_tvbanners_h12.png',  # Gracenote: drawn TV set
    'vimg.ipko.tv/mtcms/dev/0b/f/f/0bff2b48-588e-4a3a-b69d-943d1ea62a11.jpeg',  # ipko: "…" bubble
    'epg-garcenote-assets.s3.amazonaws.com/dishtvd2h/resources/assets/thumbnails/programs/default/default_880x660.png',  # Dish: "TV Program"
}
# Image proxies that no longer answer, in front of the provider's own link (which does).
DEAD_PROXIES = ('http://epg.bnet.hr/images/', 'https://epg.bnet.hr/images/')
# Changes with the rules above, so that every edition is rebuilt with them at once.
PICTURE_RULES = 1


def useful_picture(url):
    """[url], the link behind a dead proxy, or '' when it never shows a picture of the programme."""
    url = url or ''
    for proxy in DEAD_PROXIES:
        if url.startswith(proxy) and url[len(proxy):].startswith(('http://', 'https://')):
            url = url[len(proxy):]
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
    except ValueError:
        return ''
    # 'https://sat.tvhttps://www.sat.tv/…': two links glued together, which never load (and lead
    # to 40-pixel icons).
    if '.' not in host or host.endswith(('http', 'https')):
        return ''
    return '' if host + parts.path in NO_PICTURE else url


def card_pictures(programmes):
    """Programmes with the picture their card shows; those left without one are dropped."""
    result = []
    for p in programmes:
        picture = small_picture(useful_picture(p[3]))
        if picture:
            result.append([*p[:3], picture, *p[4:]])
    return result


def entries(reductions, operator_shards):
    """Every source channel as (edition, rank, source, id, names, programmes)."""
    result = []
    for reduction in reductions:
        for c in load_channels(reduction):
            ed = edition(c['country'])
            if ed:
                result.append(dict(edition=ed, rank=reduction['rank'], source=reduction['name'], id=c['id'],
                                   names=c['names'], programmes=card_pictures(c['programmes'])))
    for shard in operator_shards:
        ed = edition(shard['country'])
        for c in shard['channels']:
            if c.get('timingStatus') != 'source-format-checked':
                continue
            programmes = card_pictures([e['start'], e['end'], e['title'], e['imageURL']] for e in c['programmes'] if e.get('imageURL'))
            result.append(dict(edition=ed, rank=0, source='titan-' + c['source'], id=c.get('xmltvId') or '',
                               names=[c['name']] + c.get('aliases', []), programmes=programmes, operator=c['id']))
    return result


def canonical(items):
    """Merge the descriptions of one service: same normalised id, or same name met only once per source."""
    union = Union()
    ids, names = {}, {}
    per_source = {}
    for i, item in enumerate(items):
        keys = {channel_name_key(n) for n in item['names']} - {''}
        item['nameKeys'] = keys
        for key in keys:
            per_source.setdefault((item['source'], key), []).append(i)
    for i, item in enumerate(items):
        key = id_key(item['id'])
        if key:
            union.join(i, ids.setdefault(key, i))
        for name in item['nameKeys']:
            # A name shared by two channels of the same guide (regional editions…) identifies neither.
            if len(per_source[(item['source'], name)]) == 1:
                union.join(i, names.setdefault(name, i))
    groups = {}
    for i in range(len(items)):
        groups.setdefault(union.find(i), []).append(items[i])
    return list(groups.values())


def describe(group):
    ordered = sorted(group, key=lambda item: (item['rank'], item['source'], item['id']))
    keys = []
    for item in ordered:
        key = id_key(item['id'])
        if key and key not in keys:
            keys.append(key)
    names = []
    for item in ordered:
        for name in item['names']:
            if name not in names:
                names.append(name)
    best = max(ordered, key=lambda item: (coverage(item['programmes']), -item['rank']))
    first = ordered[0]
    k = keys[0] if keys else f"{first['source']}:{first['id'] or channel_name_key(first['names'][0])}"
    operators = sorted({item['operator'] for item in ordered if item.get('operator')})
    return dict(k=k, ids=keys[:10], names=names[:12], source=best['source'], programmes=best['programmes'], operators=operators)


def build_day(items):
    """{edition: [channel]} from every source; channels without an illustrated programme are dropped."""
    by_edition = {}
    for item in items:
        by_edition.setdefault(item['edition'], []).append(item)
    result = {}
    for ed, rows in sorted(by_edition.items()):
        channels = [describe(group) for group in canonical(rows)]
        channels = [c for c in channels if c['programmes']]
        seen = set()
        unique = []
        for c in sorted(channels, key=lambda c: c['k']):
            if c['k'] not in seen:
                seen.add(c['k'])
                unique.append(c)
        result[ed] = unique
    return result


def buckets_for(channels):
    size = sum(len(encode(c['programmes'])) for c in channels)
    count = max(1, min(MAX_BUCKETS, math.ceil(size / BUCKET_BYTES)))
    return count, {c['k']: zlib.crc32(c['k'].encode()) % count for c in channels}


def guide_inputs(items, reductions):
    """{edition: digest of the public guides (name, anchor) it is built from}."""
    anchors = {r['name']: r['anchor'] for r in reductions}
    inputs = {}
    for item in items:
        if item['source'] in anchors:
            inputs.setdefault(item['edition'], set()).add((item['source'], anchors[item['source']]))
    return {ed: hashlib.sha256(encode([PICTURE_RULES, sorted(pairs)])).hexdigest()[:16] for ed, pairs in inputs.items()}


def write_day(output, day, now, inputs=None):
    """Directory + buckets per edition. Returns {edition: report}."""
    reports = {}
    for ed, channels in day.items():
        count, bucket_of = buckets_for(channels)
        folder = output / 'v2' / ed.lower()
        directory = dict(schemaVersion=SCHEMA, edition=ed, buckets=count,
                         channels=[dict(k=c['k'], ids=c['ids'], names=c['names'], b=bucket_of[c['k']]) for c in channels])
        atomic_write(folder / 'channels.json', encode(directory))
        start = min((p[0] for c in channels for p in c['programmes']), default=now)
        end = max((p[1] for c in channels for p in c['programmes']), default=now)
        digests = []
        for b in range(count):
            content = {c['k']: c['programmes'] for c in channels if bucket_of[c['k']] == b}
            data = encode(dict(schemaVersion=SCHEMA, edition=ed, bucket=b, channels=content))
            atomic_write(folder / f'day-{b}.json', data)
            digests.append(hashlib.sha256(data).hexdigest()[:16])
        for stale in folder.glob('day-*.json'):
            if int(stale.stem.split('-')[1]) >= count:
                stale.unlink()
        reports[ed] = dict(channels=len(channels), buckets=count, builtAt=now, start=start, end=end,
                           day=hashlib.sha256(''.join(digests).encode()).hexdigest()[:16],
                           sources=sorted({c['source'].split('-')[0] for c in channels}),
                           inputs=(inputs or {}).get(ed, ''))
    return reports


def operator_targets(day):
    """Operator channel id → [edition, day key], saved with the day so live runs need no rebuild."""
    return {operator: [ed, c['k']] for ed, channels in day.items() for c in channels for operator in c['operators']}


def write_live(output, editions, by_operator, operator_shards, now):
    """Operator programmes of the coming hours, keyed like the day buckets."""
    live = {}
    for shard in operator_shards:
        for c in shard['channels']:
            target = by_operator.get(c['id'])
            if not target or c.get('timingStatus') != 'source-format-checked' or not now - 3600 <= c.get('fetchedAt', 0) <= now + 300:
                continue
            programmes = card_pictures([e['start'], e['end'], e['title'], e['imageURL']] for e in c['programmes']
                                       if e.get('imageURL') and e['end'] > now - LIVE_BEFORE and e['start'] < now + LIVE_AFTER)
            if programmes:
                live.setdefault(target[0], {})[target[1]] = programmes
    versions = {}
    for ed in editions:
        data = encode(dict(schemaVersion=SCHEMA, edition=ed, generatedAt=now, validUntil=now + 3600,
                           channels=dict(sorted(live.get(ed, {}).items()))))
        atomic_write(output / 'v2' / ed.lower() / 'live.json', data)
        versions[ed] = dict(live=hashlib.sha256(data).hexdigest()[:16], liveChannels=len(live.get(ed, {})))
    return versions


def build(output, reductions, operator_shards, now, force=False):
    """Day buckets of an edition only when one of its public guides changed (or after 13 hours); live
    and manifest every run."""
    output = Path(output)
    state_file = output / 'v2' / 'state.json'
    try:
        state = json.loads(state_file.read_bytes())
    except (OSError, ValueError):
        state = {}
    signature = hashlib.sha256(encode(sorted((r['name'], r['anchor']) for r in reductions))).hexdigest()[:16]
    if force or state.get('signature') != signature or not 0 <= now - state.get('builtAt', 0) < 13 * 3600 or not state.get('editions'):
        items = entries(reductions, operator_shards)
        inputs = guide_inputs(items, reductions)
        day = build_day(items)
        previous, operators = state.get('editions', {}), state.get('operators', {})
        # An edition none of whose guides moved keeps its files: a Samsung TV Plus refresh every
        # 2 hours must not make every other country download its day again.
        kept = {ed for ed in day if not force and ed in previous and previous[ed].get('inputs') == inputs.get(ed, '')
                and 0 <= now - previous[ed].get('builtAt', 0) < 13 * 3600 and (output / 'v2' / ed.lower() / 'channels.json').is_file()}
        rebuilt = {ed: channels for ed, channels in day.items() if ed not in kept}
        editions = dict(write_day(output, rebuilt, now, inputs), **{ed: previous[ed] for ed in kept})
        operators = dict({op: target for op, target in operators.items() if target[0] in kept}, **operator_targets(rebuilt))
        state = dict(signature=signature, builtAt=min(report['builtAt'] for report in editions.values()) if editions else now,
                     editions=dict(sorted(editions.items())), operators=operators)
        atomic_write(state_file, encode(state))
    editions = state['editions']
    live = write_live(output, editions, state.get('operators', {}), operator_shards, now)
    manifest = dict(schemaVersion=SCHEMA, generatedAt=now, liveRefreshAfter=now + 1800,
                    editions={ed: dict({k: v for k, v in report.items() if k != 'inputs'}, **live.get(ed, dict(live='', liveChannels=0)))
                              for ed, report in sorted(editions.items())})
    atomic_write(output / 'v2' / 'manifest.json', encode(manifest))
    return manifest
