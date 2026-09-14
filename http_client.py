
import hashlib
import json
import threading
import tempfile
import time
import urllib.error
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

HOSTS = {'api.oqee.net', 'awk.epgsky.com', 'apid.sky.it', 'ottcache.dof6.com',
         'meogouser.apps.meo.pt', 'www.tvpassport.com', 'mi.tv', 'www.raiplay.it',
         'api.prod.sngtv.magentatv.de', 'cdn.pt.vtv.vodafone.com'}
HOSTS.add('raw.githubusercontent.com')
MAX_BYTES = 12 * 1024 * 1024


def validate_url(url):
    u = urlsplit(url)
    if u.scheme != 'https' or u.hostname not in HOSTS or u.username or u.password or u.port not in (443, None):
        raise ValueError('metadata_host_not_allowed')


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, validator=validate_url):
        self.validator = validator
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as file:
        temporary = Path(file.name)
        file.write(data)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Client:
    def __init__(self, directory, clock=time.time, min_interval=0.35, validator=validate_url, max_bytes=MAX_BYTES):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.stats = dict(requests=0, cacheHits=0, bytes=0)
        self.lock = threading.Lock()
        self.min_interval = min_interval
        self.validator = validator
        self.max_bytes = max_bytes
        self.host_locks = {}
        self.last_request = {}

    def host_gate(self, host):

        with self.lock:
            return self.host_locks.setdefault(host, threading.Lock())

    def host_state(self, host):
        return self.directory / ('host-' + host + '.json')

    def get(self, url, headers=None, ttl=900, body=None, opener=None, cacheable=True):
        self.validator(url)
        now = self.clock()

        key_headers = {k: v for k, v in (headers or {}).items() if k.lower() != 'x_csrftoken'}
        key = hashlib.sha256((url + json.dumps(key_headers, sort_keys=True)).encode() + (body or b'')).hexdigest()
        meta_file = self.directory / (key + '.json')
        body_file = self.directory / (key + '.body')
        try:
            meta = json.loads(meta_file.read_text())
        except (ValueError, OSError):
            meta = {}
        if (cacheable and 0 <= now - meta.get('fetchedAt', 0) < ttl and body_file.exists()
                and body_file.stat().st_size <= self.max_bytes):
            with self.lock:
                self.stats['cacheHits'] += 1
            return body_file.read_bytes()
        if now < meta.get('retryAt', 0):
            raise ValueError('source_backoff')
        host = urlsplit(url).hostname
        gate = self.host_gate(host)
        retry_after = 0
        gate.acquire()
        try:
            try:
                host_meta = json.loads(self.host_state(host).read_text())
            except (OSError, ValueError):
                host_meta = {}
            if self.clock() < host_meta.get('retryAt', 0):
                raise ValueError('host_backoff')
            delay = self.min_interval - (time.monotonic() - self.last_request.get(host, 0))
            if delay > 0:
                time.sleep(delay)
            self.last_request[host] = time.monotonic()
            req = urllib.request.Request(url, data=body, headers={
                'User-Agent': 'TitanProgrammePilot/0.1 (public TV guide metadata)',
                'Accept': 'application/json,text/html;q=0.9', **(headers or {})})
            with self.lock:
                self.stats['requests'] += 1
            with (opener or urllib.request.build_opener(SafeRedirect(self.validator))).open(req, timeout=15) as response:
                if int(response.headers.get('Content-Length', 0)) > self.max_bytes:
                    raise ValueError('metadata_too_large')
                data = response.read(self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    raise ValueError('metadata_too_large')
            if cacheable:
                atomic_write(body_file, data)

            atomic_write(meta_file, json.dumps(dict(fetchedAt=now)).encode())
            with self.lock:
                self.stats['bytes'] += len(data)
            return data
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                value = error.headers.get('Retry-After', '')
                try:
                    retry_after = float(value)
                except ValueError:
                    try:
                        retry_after = parsedate_to_datetime(value).replace(tzinfo=timezone.utc).timestamp() - now
                    except (ValueError, TypeError, OverflowError):
                        pass
            failures = min(meta.get('failures', 0) + 1, 6)
            meta.update(failures=failures, retryAt=now + min(86400, max(60 * 2 ** (failures - 1), retry_after)))
            atomic_write(meta_file, json.dumps(meta).encode())
            if isinstance(error, urllib.error.HTTPError) and error.code in (429, 503):
                atomic_write(self.host_state(host), json.dumps({'retryAt': meta['retryAt']}).encode())
            raise
        finally:
            gate.release()

    def json(self, url, **kwargs):
        return json.loads(self.get(url, **kwargs))

    def prune(self):
        for path in self.directory.iterdir():
            if path.is_file() and self.clock() - path.stat().st_mtime > 3 * 86400:
                path.unlink(missing_ok=True)
