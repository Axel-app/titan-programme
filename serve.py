#!/usr/bin/env python3

import argparse
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import urlsplit

from collect import ROOT, COUNTRIES, collect, encode


def accepts_gzip(header):
    for part in header.split(','):
        coding, *parameters = part.strip().lower().split(';')
        if coding != 'gzip':
            continue
        quality = 1.0
        for parameter in parameters:
            key, _, value = parameter.strip().partition('=')
            if key == 'q':
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0
        return 0 < quality <= 1
    return False


class Handler(BaseHTTPRequestHandler):


    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        url = urlsplit(self.path)
        routes = {'/v1/manifest.json': ('output/manifest.json', 'application/json')}
        routes.update({f'/v1/{c.lower()}.json': (f'output/{c.lower()}.json', 'application/json') for c in COUNTRIES})
        if url.path not in routes:
            self.send_error(404)
            return
        file, mime = routes[url.path]
        try:
            data = (ROOT / file).read_bytes()
        except OSError:
            self.send_error(503, 'First collection is still running')
            return
        self.respond(data, mime, 'public, max-age=30' if file.startswith('output/') else 'no-cache')

    def respond(self, data, mime, cache):
        use_gzip = accepts_gzip(self.headers.get('Accept-Encoding', ''))
        if use_gzip:
            data = gzip.compress(data, mtime=0)
        etag = '"' + hashlib.sha256(data).hexdigest()[:24] + '"'
        status = 304 if self.headers.get('If-None-Match') == etag else 200
        self.send_response(status)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Cache-Control', cache)
        self.send_header('ETag', etag)
        self.send_header('Vary', 'Accept-Encoding')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' https://*.oqee.net https://images.metadata.sky.com https://guidatv.sky.it https://www.raiplay.it https://ngiss.t-online.de https://3038.images-vfp2.ott.kaltura.com https://estatico.emisiondof6.com https://cdn-er-images.online.meo.pt https://cdn.tvpassport.com https://cdn.mitvstatic.com; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if use_gzip:
            self.send_header('Content-Encoding', 'gzip')

        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if status == 200:
            self.wfile.write(data)

    def log_message(self, fmt, *args):

        if args and str(args[1]) not in ('200', '304'):
            super().log_message(fmt, *args)


def refresh_loop():
    while True:
        try:
            path = ROOT / 'output/manifest.json'
            manifest = json.loads(path.read_bytes()) if path.exists() else {}
            wait = max(0, min(1800, manifest.get('refreshAfter', 0) - time.time()))
            if wait:
                time.sleep(wait)
            collect()
        except Exception as error:
            print('Collection error:', type(error).__name__, flush=True)
            time.sleep(60)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--no-refresh', action='store_true')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    if not args.no_refresh:
        threading.Thread(target=refresh_loop, daemon=True).start()
    print(f'Titan Programme pilot: http://127.0.0.1:{args.port}', flush=True)
    server.serve_forever()
