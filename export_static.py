#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
import time

from collect import COUNTRIES, ROOT, encode
from http_client import atomic_write

CHANNEL_FIELDS = ('id', 'country', 'source', 'sourceId', 'name', 'aliases',
                  'timingStatus', 'fetchedAt', 'validUntil')
EVENT_FIELDS = ('title', 'start', 'end', 'imageURL')
ENVELOPE_FIELDS = ('schemaVersion', 'country', 'generatedAt', 'refreshAfter', 'validUntil')
HEADERS = b'''/v1/*
  Cache-Control: public, max-age=60, must-revalidate
  Content-Type: application/json; charset=utf-8
  X-Content-Type-Options: nosniff
  Access-Control-Allow-Origin: *
  Referrer-Policy: no-referrer
'''


def export_static(source=ROOT / 'output', destination=ROOT / 'dist', now=None):
    source, destination = Path(source), Path(destination)
    now = int(time.time() if now is None else now)
    files, reports = {}, []
    healthy = 0
    for country in COUNTRIES:
        shard = json.loads((source / f'{country.lower()}.json').read_bytes())
        if (shard['schemaVersion'] != 1 or shard['country'] != country
                or not 0 < shard['generatedAt'] <= now + 300
                or not shard['generatedAt'] < shard['validUntil'] <= shard['generatedAt'] + 3600):
            raise ValueError(f'invalid_envelope_{country}')
        channels = shard['channels']
        if not 0 < len(channels) <= 2000 or len({c['id'] for c in channels}) != len(channels):
            raise ValueError(f'invalid_channels_{country}')
        public = {key: shard[key] for key in ENVELOPE_FIELDS}
        public['channels'] = []
        usable = 0
        for channel in channels:
            if channel['country'] != country or len(channel['programmes']) > 2000:
                raise ValueError(f'invalid_channel_{country}')
            clean = {key: channel[key] for key in CHANNEL_FIELDS}
            clean['programmes'] = [{key: event.get(key) for key in EVENT_FIELDS} for event in channel['programmes']]
            public['channels'].append(clean)
            if (min(channel['validUntil'], shard['validUntil']) > now
                    and channel['timingStatus'] == 'source-format-checked'
                    and any(e['start'] <= now < e['end'] for e in channel['programmes'])):
                usable += 1
        data = encode(public)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError(f'client_size_limit_{country}')
        path = f'v1/{country.lower()}.json'
        files[path] = data
        reports.append(dict(country=country, path='/' + path, channels=len(channels),
                            generatedAt=shard['generatedAt'], refreshAfter=shard['refreshAfter'],
                            validUntil=shard['validUntil'], usableNow=usable, jsonBytes=len(data)))
        healthy += usable


    if healthy == 0:
        raise ValueError('no_fresh_current_programmes')
    files['v1/manifest.json'] = encode(dict(schemaVersion=1, generatedAt=max(r['generatedAt'] for r in reports),
        refreshAfter=min(r['refreshAfter'] for r in reports), countries=reports))
    files['_headers'] = HEADERS


    if destination.exists():
        for path in destination.rglob('*'):
            if path.is_symlink() or (path.is_file() and path.relative_to(destination).as_posix() not in files):
                raise ValueError('destination_contains_non_public_files')
    for relative, data in files.items():
        atomic_write(destination / relative, data)
    print(f'Export: {len(files)-1} JSON files, {sum(len(v) for k,v in files.items() if k.endswith(".json"))} bytes, {healthy} current programmes')
    return files


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=ROOT / 'output')
    parser.add_argument('--destination', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    export_static(args.source, args.destination)
