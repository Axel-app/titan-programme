import gzip
import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serve import Handler


class HttpTransportTests(unittest.TestCase):
    def test_countries_and_304_share_a_persistent_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'output').mkdir()
            (root / 'output/fr.json').write_text('{"country":"FR"}')
            (root / 'output/gb.json').write_text('{"country":"GB"}')
            with patch('serve.ROOT', root):
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                try:
                    connection.request('GET', '/v1/fr.json', headers={'Accept-Encoding': 'gzip'})
                    response = connection.getresponse()
                    self.assertEqual(response.version, 11)
                    self.assertEqual(response.status, 200)
                    etag = response.getheader('ETag')
                    self.assertEqual(gzip.decompress(response.read()), b'{"country":"FR"}')
                    socket = connection.sock
                    self.assertIsNotNone(socket)

                    connection.request('GET', '/v1/gb.json')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b'{"country":"GB"}')
                    self.assertIs(connection.sock, socket)

                    connection.request('GET', '/v1/fr.json', headers={
                        'Accept-Encoding': 'gzip', 'If-None-Match': etag,
                    })
                    response = connection.getresponse()
                    self.assertEqual(response.status, 304)
                    self.assertEqual(response.read(), b'')
                    self.assertIs(connection.sock, socket)

                    connection.request('GET', '/v1/fr.json')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b'{"country":"FR"}')
                    self.assertIs(connection.sock, socket)
                finally:
                    connection.close()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)
