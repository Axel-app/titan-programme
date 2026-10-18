#!/usr/bin/env python3
"""Small pictures of the programmes on air, published next to the guide.

The guides point at the broadcasters' own pictures, often 1920x1080 or larger (Samsung TV Plus
sends PNGs of up to 2.4 MB, Roku up to 9 MB) for a card of about 500x280 on screen. For the
programmes of the daily guide on air now or within AHEAD, each run keeps a 480x270 JPEG of
every heavy picture at t/<2 first characters>/<sha256 of the picture address>.jpg: the apps
derive that address from the guide's link and fall back to the link itself when it is missing
(light pictures, later programmes, more pictures than the Worker can hold).
Only new pictures are fetched; the folder lives in the run cache with the rest of `output`.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit
import warnings

from collect import ROOT
from http_client import SafeRedirect, atomic_write

WIDTH, HEIGHT = 480, 270
# Runs come every 30 minutes: an hour ahead, a programme has its thumbnail before it starts.
AHEAD = 3600
# Static assets: 20,000 files per Worker version on the free plan, guide files included
# (about 650 today). The programmes on air come first; the next ones take what room is left.
MAX_FILES = 17500
NEW_PER_RUN = 6000
RETRY_AFTER = 6 * 3600
# Already about card size: the original is as quick as a thumbnail, no file spent on it.
LIGHT_BYTES = 60 * 1024
LIGHT_SIDE = 1000
LIGHT_FOR = 24 * 3600
MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000
TIMEOUT = 15
WORKERS = 32
QUALITY = 78
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'


def key(url):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


def relative_path(url):
    """Public path of a picture's thumbnail, the one the apps derive from the guide's link."""
    digest = key(url)
    return f't/{digest[:2]}/{digest}.jpg'


def wanted_pictures(v2, now, limit=None):
    """Picture links of the daily guide on air now or within AHEAD: those on air first, then the soonest."""
    v2 = Path(v2)
    manifest = json.loads((v2 / 'manifest.json').read_bytes())
    starts = {}
    for edition, report in manifest['editions'].items():
        folder = v2 / edition.lower()
        names = ['live.json'] + [f'day-{b}.json' for b in range(report.get('buckets', 0))]
        for name in names:
            try:
                channels = json.loads((folder / name).read_bytes())['channels']
            except (OSError, ValueError, KeyError):
                continue
            for programmes in channels.values():
                for start, end, _title, image in programmes:
                    if image and end > now and start < now + AHEAD and urlsplit(image).scheme in ('http', 'https'):
                        on_air = max(start, now)
                        if on_air < starts.get(image, on_air + 1):
                            starts[image] = on_air
    ordered = sorted(starts, key=lambda image: (starts[image], image))
    return ordered if limit is None else ordered[:limit]


def public_address(url):
    """Only pictures on the public internet: never the runner's own network or cloud metadata."""
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('picture_address_not_allowed')
    for *_, address in socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)):
        if not ipaddress.ip_address(address[0]).is_global:
            raise ValueError('picture_address_not_public')


def download(url, timeout=TIMEOUT):
    public_address(url)
    opener = urllib.request.build_opener(SafeRedirect(public_address))
    # Links with accents (Magio, TV Spielfilm) are sent escaped, as browsers do.
    address = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
    request = urllib.request.Request(address, headers={'User-Agent': USER_AGENT, 'Accept': 'image/*'})
    with opener.open(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('picture_too_large')
    return data


def is_light(data):
    """A picture already about card size, which the apps load as quickly as a thumbnail."""
    if len(data) > LIGHT_BYTES:
        return False
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        return max(image.size) <= LIGHT_SIDE


def thumbnail(data):
    """480x270 JPEG: wide pictures cropped to 16:9, posters and squares whole over their own blur."""
    from PIL import Image, ImageFilter, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            source.draft('RGB', (WIDTH * 2, HEIGHT * 2))
            image = ImageOps.exif_transpose(source)
            image.load()
    if image.mode in ('RGBA', 'LA', 'P', 'PA'):
        image = image.convert('RGBA')
        flat = Image.new('RGB', image.size, (0, 0, 0))
        flat.paste(image, mask=image.getchannel('A'))
        image = flat
    else:
        image = image.convert('RGB')
    if not image.width or not image.height:
        raise ValueError('empty_picture')
    size = (WIDTH, HEIGHT)
    if 1.5 <= image.width / image.height <= 2.1:
        result = ImageOps.fit(image, size, Image.Resampling.LANCZOS)
    else:
        result = ImageOps.fit(image, size, Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(18))
        result = Image.blend(result, Image.new('RGB', size, (0, 0, 0)), 0.35)
        whole = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
        result.paste(whole, ((WIDTH - whole.width) // 2, (HEIGHT - whole.height) // 2))
    out = io.BytesIO()
    result.save(out, 'JPEG', quality=QUALITY, optimize=True, progressive=True)
    return out.getvalue()


def update(output=ROOT / 'output', now=None, fetch=download, new_per_run=NEW_PER_RUN, limit=MAX_FILES):
    """Adds the missing thumbnails, drops those whose programme ended. Never raises on a picture.

    thumbs/index.json lists the published ones, those on air first: the export keeps that order
    when the guide leaves less room than expected.
    """
    output = Path(output)
    now = int(time.time() if now is None else now)
    folder = output / 'thumbs'
    state_file = folder / 'state.json'
    try:
        state = json.loads(state_file.read_bytes())
        retry = {k: v for k, v in state.get('retry', {}).items() if v > now}
        light = {k: v for k, v in state.get('light', {}).items() if v > now}
    except (OSError, ValueError, AttributeError):
        retry, light = {}, {}
    wanted = [url for url in wanted_pictures(output / 'v2', now) if key(url) not in light]
    wanted = wanted[:limit]
    missing = [url for url in wanted if not (folder / relative_path(url)).is_file() and key(url) not in retry]
    todo = missing[:new_per_run]

    def make(url):
        try:
            data = fetch(url)
            if is_light(data):
                return url, 'light'
            atomic_write(folder / relative_path(url), thumbnail(data))
            return url, None
        except Exception as error:  # a broken or refused picture keeps its original link
            return url, type(error).__name__

    made, lights, failures = 0, 0, {}
    with ThreadPoolExecutor(WORKERS) as pool:
        for url, outcome in pool.map(make, todo):
            if outcome == 'light':
                light[key(url)] = now + LIGHT_FOR
                lights += 1
            elif outcome:
                retry[key(url)] = now + RETRY_AFTER
                failures[outcome] = failures.get(outcome, 0) + 1
            else:
                made += 1
    published = [relative_path(url) for url in wanted if key(url) not in light and (folder / relative_path(url)).is_file()]
    keep = set(published)
    dropped = 0
    for path in folder.glob('t/*/*.jpg'):
        if path.relative_to(folder).as_posix() not in keep:
            path.unlink()
            dropped += 1
    atomic_write(folder / 'index.json', json.dumps(published, separators=(',', ':')).encode())
    atomic_write(state_file, json.dumps(dict(retry=retry, light=light), separators=(',', ':')).encode())
    print(f'Thumbnails: {len(wanted)} heavy or unknown pictures on air, {made} made, {lights} already light, '
          f'{sum(failures.values())} failed {failures}, {len(missing) - len(todo)} left for the next runs, '
          f'{len(published)} published, {dropped} dropped', flush=True)
    return dict(wanted=len(wanted), made=made, light=lights, failed=sum(failures.values()),
                published=len(published), dropped=dropped)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'output')
    args = parser.parse_args()
    update(args.output)
