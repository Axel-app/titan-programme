#!/usr/bin/env python3
"""Schema 2 publication: public guides (when due) + operator shards from collect.py."""

import argparse
import json
from pathlib import Path
import time

from build_v2 import build
from collect import COUNTRIES, ROOT
from xmltv_sources import GuideStore


def collect_v2(output=ROOT / 'output', cache=ROOT / '.cache', grabbed=ROOT / 'grabbed', now=None):
    output = Path(output)
    now = int(time.time() if now is None else now)
    store = GuideStore(cache, output)
    summaries = store.collect(now=now, local=grabbed)
    shards = []
    for country in COUNTRIES:
        try:
            shards.append(json.loads((output / f'{country.lower()}.json').read_bytes()))
        except (OSError, ValueError):
            pass
    manifest = build(output, summaries, shards, now)
    print(f"Guides {len(summaries)} {store.stats}; v2 editions {len(manifest['editions'])}, "
          f"{sum(e['channels'] for e in manifest['editions'].values())} channels", flush=True)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'output')
    parser.add_argument('--grabbed', type=Path, default=ROOT / 'grabbed', help='guides from the iptv-org grabbers')
    args = parser.parse_args()
    collect_v2(output=args.output, grabbed=args.grabbed)
