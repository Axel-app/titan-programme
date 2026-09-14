#!/usr/bin/env python3

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import gzip
import json
from pathlib import Path
import time
import urllib.error

from http_client import Client, atomic_write
from model import current
from sources import collect_group

ROOT = Path(__file__).resolve().parent
REFRESH_SECONDS = 1800
COUNTRIES = dict(FR='France', GB='Royaume-Uni', DE='Allemagne', IT='Italie',
                 ES='Espagne', PT='Portugal', US='États-Unis', BR='Brésil', CA='Canada', MX='Mexique',
                 TR='Turquie', PL='Pologne', CH='Suisse', MENA='Moyen-Orient et Afrique du Nord')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def load_json(path, default):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return default


def collect_country(client, catalogue, old, now):
    groups = defaultdict(list)
    for c in catalogue:
        groups[c['source']].append(c)
    previous = {c['id']: c for c in old.get('channels', [])} if old.get('schemaVersion') == 1 else {}
    result = []
    for source, rows in groups.items():
        try:
            fresh = collect_group(client, rows, now)
        except Exception as exc:
            error = f'HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__ + ': ' + str(exc)[:120]
            fresh = [dict(c, timingStatus='pending', programmes=[], fetchedAt=0, sourceErrors=[error],
                          timeBasis='Source unavailable') for c in rows]
        for c in fresh:
            cached = previous.get(c['id'])

            if c['sourceErrors'] and not c['programmes'] and cached and cached.get('source') == source and cached.get('sourceId') == c['sourceId']:
                fetched = cached.get('fetchedAt', 0)
                if cached.get('programmes') and 0 <= now - fetched < 3600:
                    c = dict(c, programmes=cached['programmes'], fetchedAt=fetched,
                             timingStatus=cached['timingStatus'], timeBasis=cached['timeBasis'], cachedAfterError=True)
            c['validUntil'] = c['fetchedAt'] + 3600 if c['fetchedAt'] else 0
            result.append(c)
    by_id = {c['id']: c for c in result}
    return [by_id[c['id']] for c in catalogue]


def _collect(output, cache, countries, now, catalogue, refresh_seconds):
    started = time.monotonic()
    client = Client(cache)
    selected = countries or list(COUNTRIES)
    groups = {country: [c for c in catalogue if c['country'] == country] for country in selected}
    old_manifest = load_json(output / 'manifest.json', {})

    reports = [r for r in old_manifest.get('countries', []) if r['country'] not in selected]
    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs = {executor.submit(collect_country, client, rows,
                    load_json(output / f'{country.lower()}.json', {}), now): country for country, rows in groups.items()}
        for job in as_completed(jobs):
            country = jobs[job]
            channels = job.result()
            errors = sorted({e for c in channels for e in c['sourceErrors']})
            status = 'partial' if errors else 'ok'
            if errors and not any(c['programmes'] for c in channels):
                status = 'unavailable'
            path = output / f'{country.lower()}.json'
            shard = dict(schemaVersion=1, country=country, generatedAt=now, validUntil=now + 3600,
                         refreshAfter=now + refresh_seconds, channels=channels)
            raw = encode(shard)
            zipped = gzip.compress(raw, mtime=0)
            atomic_write(path, raw)
            atomic_write(path.with_suffix('.json.gz'), zipped)
            live = [current(c['programmes'], now) if c['validUntil'] > now and c['timingStatus'] != 'pending' else None for c in channels]
            report = dict(country=country, label=COUNTRIES[country], source=', '.join(sorted({c['source'] for c in channels})),
                generatedAt=now, refreshAfter=now + refresh_seconds, status=status, error=', '.join(errors) or None,
                channels=len(channels), programmes=sum(len(c['programmes']) for c in channels),
                current=sum(e is not None for e in live), currentImageURLs=sum(bool(e and e.get('imageURL')) for e in live),
                channelsWithErrors=sum(bool(c['sourceErrors']) for c in channels),
                cachedChannels=sum(bool(c.get('cachedAfterError')) for c in channels),
                timingReady=all(c['timingStatus'] != 'pending' for c in channels), jsonBytes=len(raw),
                gzipBytes=len(zipped), path=f'/v1/{country.lower()}.json')
            reports.append(report)
            print(f'{country} {status}: {report["currentImageURLs"]}/{len(channels)} current image URLs, {len(zipped)} bytes gzip', flush=True)
    reports.sort(key=lambda r: list(COUNTRIES).index(r['country']))
    manifest = dict(schemaVersion=1, generatedAt=now,
        refreshAfter=min((r.get('refreshAfter', old_manifest.get('refreshAfter', now)) for r in reports), default=now + refresh_seconds),
        countries=reports, collection=dict(client.stats, elapsedSeconds=round(time.monotonic() - started, 2), countries=selected),
        caveat='Bounded selection of channels and editions. Image URLs do not prove decoding, per-episode accuracy or video synchronisation.')
    atomic_write(output / 'manifest.json', encode(manifest))
    client.prune()
    return manifest


def collect(output=ROOT / 'output', cache=ROOT / '.cache', countries=None, now=None, catalogue=None, refresh_seconds=REFRESH_SECONDS):
    if refresh_seconds not in (900, 1800):
        raise ValueError("refresh_must_be_15_or_30_minutes")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)

    with (output / '.collection.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('collection_already_running')
        return _collect(output, cache, countries, int(now if now is not None else time.time()),
                        catalogue if catalogue is not None else json.loads((ROOT / 'catalogue.json').read_text()), refresh_seconds)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--countries', nargs='+', choices=COUNTRIES)
    parser.add_argument('--output', type=Path, default=ROOT / 'output')
    parser.add_argument('--refresh-minutes', type=int, choices=(15, 30), default=30)
    args = parser.parse_args()
    collect(output=args.output, countries=args.countries, refresh_seconds=args.refresh_minutes * 60)
