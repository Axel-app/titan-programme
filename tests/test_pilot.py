import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from http_client import Client, MAX_BYTES, validate_url
from model import current, event, image_url, name_key, resolve_channel, utc_seconds, compact_events
from sources import collect_group, mitv_events, mitv_starts, movistar_events, passport_events, sky_events, rai_events, magenta_events, vodafone_events
from datetime import date
from serve import accepts_gzip

ROOT = Path(__file__).resolve().parents[1]
NOW = utc_seconds('2026-09-10T13:00:00Z')


class IdentityAndTimeTests(unittest.TestCase):
    def test_duplicate_hour_does_not_remove_available_image(self):
        a = event('A', NOW, NOW + 60, 'https://img1.oqee.net/h300', 'oqee')
        merged = compact_events([a, dict(a, imageURL=None)], NOW)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['imageURL'], a['imageURL'])

    def test_boundary_and_overlap(self):
        a = event('A', NOW - 60, NOW, None, 'sky')
        b = event('B', NOW, NOW + 60, None, 'sky')
        self.assertEqual(current([a, b], NOW), b)
        self.assertIsNone(current([a], NOW))
        self.assertIsNone(current([b, dict(b, title='Other')], NOW))
        self.assertEqual(current([b, b], NOW), b)

    def test_timezone_and_dst(self):
        self.assertEqual(utc_seconds('2026-09-10T15:00:00', 'Europe/Paris'), NOW)
        self.assertEqual(utc_seconds('2026-09-10T09:00:00', 'America/New_York'), NOW)
        self.assertEqual(utc_seconds('2026-09-10T10:00:00', 'America/Sao_Paulo'), NOW)
        self.assertEqual(utc_seconds('2026-01-10T13:00:00', 'Europe/Lisbon'), utc_seconds('2026-01-10T13:00:00Z'))
        for value in ['2026-03-29T02:30:00', '2026-10-25T02:30:00']:
            with self.assertRaises(ValueError):
                utc_seconds(value, 'Europe/Paris')
        with self.assertRaises(ValueError):
            utc_seconds('2026-09-10T13:00:00')

    def test_channel_aliases_keep_feed_and_country(self):
        catalogue = json.loads((ROOT / 'catalogue.json').read_text())
        match = resolve_channel(catalogue, 'FR', '|FR| CANAL+ BOX OFFICE FHD+')
        self.assertEqual(match['sourceId'], '1164')
        for country, name in [('US', 'HBO'), ('FR', '|US| TF1'), ('BR', 'GLOBO'), ('GB', 'BBC One'), ('FR', 'TF1 +1')]:
            self.assertIsNone(resolve_channel(catalogue, country, name), (country, name))
        self.assertNotEqual(resolve_channel(catalogue, 'FR', 'TF1 UHD')['id'], resolve_channel(catalogue, 'FR', 'TF1')['id'])
        self.assertNotEqual(resolve_channel(catalogue, 'US', 'HBO East HD')['id'], resolve_channel(catalogue, 'US', 'HBO West HD')['id'])
        self.assertNotEqual(resolve_channel(catalogue, 'BR', 'GLOBO SP HD')['id'], resolve_channel(catalogue, 'BR', 'GLOBO RJ HD')['id'])
        self.assertNotEqual(name_key('CGTN Русский'), name_key('CGTN'))

    def test_safe_image_hosts(self):
        for value in ['http://img1.oqee.net/test', 'https://oqee.net.evil.test/x', 'https://user@img1.oqee.net/x', 'https://127.0.0.1/x', 'https://img1.oqee.net:123/x', 'javascript:alert(1)']:
            self.assertIsNone(image_url(value, 'oqee'), value)
        self.assertEqual(image_url('https://img1.oqee.net/h%d', 'oqee'), 'https://img1.oqee.net/h300')
        self.assertIsNone(event('', NOW, NOW+60, None, 'sky'))
        self.assertIsNone(event('A', NOW, NOW, None, 'sky'))


class SourceTests(unittest.TestCase):
    def test_vodafone_uses_epoch_and_available_background_image(self):
        data = dict(result=dict(objects=[dict(name='Show', startDate=NOW, endDate=NOW+60, images=[
            dict(imageTypeName='bg', url='https://3038.images-vfp2.ott.kaltura.com/image')])]))
        e = vodafone_events(data)[0]
        self.assertEqual(e['start'], NOW)
        self.assertEqual(e['imageURL'], 'https://3038.images-vfp2.ott.kaltura.com/image')
        data['result']['objects'][0]['endDate'] = NOW-60
        self.assertIsNone(vodafone_events(data)[0])

    def test_sky_uses_exact_sid_and_epoch(self):
        data = dict(schedule=[dict(sid='a', events=[dict(t='Show', st=NOW, d=60, programmeuuid='abc')]), dict(sid='b', events=[dict(t='Wrong', st=NOW, d=60)])])
        events = sky_events(data, 'a')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['start'], NOW)
        self.assertIn('/abc/16-9/640', events[0]['imageURL'])

    def test_rai_local_time_and_explicit_duration(self):
        data = dict(events=[dict(date='10/09/2026', hour='15:00', duration_in_minutes='30 min', name='Show', image='/dl/image.jpg')])
        e = rai_events(data, date(2026, 9, 10))[0]
        self.assertEqual((e['start'], e['end']), (NOW, NOW + 1800))
        self.assertEqual(e['imageURL'], 'https://www.raiplay.it/dl/image.jpg')

    def test_magenta_explicit_utc_and_landscape(self):
        data = dict(playbilllist=[dict(channelid='373', name='Show', starttime='2026-09-10 13:00:00 UTC+00:00',
                                      endtime='2026-09-10 13:30:00 UTC+00:00', pictures=[
            dict(resolution=['1440','1080'], href='http://ngiss.t-online.de/portrait'),
            dict(resolution=['1920','1080'], href='http://ngiss.t-online.de/wide')])])
        e = magenta_events(data, '373')[0]
        self.assertEqual(e['start'], NOW)
        self.assertEqual(e['imageURL'], 'https://ngiss.t-online.de/wide')
        self.assertEqual(magenta_events(data, '408'), [])

    def test_movistar_exact_feed_and_landscape_without_details(self):
        data = [dict(Canal={'CodCadenaTv': 'TVE'}, Titulo='Show', FechaHoraInicio=str(NOW*1000), FechaHoraFin=str((NOW+60)*1000),
                     Ficha='https://unwanted.invalid/details', Imagenes=[dict(id='horizontal', uri='https://estatico.emisiondof6.com/landscape')])]
        self.assertEqual(movistar_events(data, 'TVE')[0]['start'], NOW)
        self.assertEqual(movistar_events(data, 'TVE')[0]['imageURL'], 'https://estatico.emisiondof6.com/landscape')
        self.assertEqual(movistar_events(data, 'T5'), [])

    def test_american_guide_uses_selected_paris_timezone(self):
        html = '<select id="timezone_selector"><option value="America/New_York">Eastern</option><option value="Europe/Paris" selected>Paris</option></select><div data-st="2026-09-10 15:00:00" data-duration="30" data-showName="Sports" data-showPicture="v2/p.jpg"></div>'
        events, zone = passport_events(html)
        self.assertEqual(zone, 'Europe/Paris')
        self.assertEqual(events[0]['start'], NOW)
        self.assertEqual(events[0]['end'], NOW + 1800)
        with self.assertRaises(ValueError):
            passport_events('<div data-st="2026-09-10 15:00:00" data-duration="30"></div>')

    def test_brazil_midnight_and_no_invented_last_end(self):
        html = '<ul class="broadcasts time24"><li><div class="image" style="background-image:url(\'https://cdn.mitvstatic.com/a.jpg\')"></div><span class="time">23:30</span><h2>A</h2></li><li><span class="time">00:20</span><h2>B</h2></li></ul>'
        starts = mitv_starts(html, '2026-09-10')
        self.assertEqual(starts[1]['start'], utc_seconds('2026-09-11T00:20:00Z'))
        events = mitv_events(starts)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['end']-events[0]['start'], 50*60)
        self.assertEqual(events[0]['imageURL'], 'https://cdn.mitvstatic.com/a.jpg')

    def test_failed_channel_does_not_hide_other_channels_or_invent_time_confidence(self):
        class PartialClient:
            def get(self, url, **kwargs):
                if 'callLetter=RTP1' in url:
                    raise HTTPError(url, 410, 'Gone', {}, None)
                return json.dumps({'Result': [dict(CallLetter='SIC', Title='Show', StartDate='2026-09-10T12:30:00', EndDate='2026-09-10T14:00:00')]}).encode()
        channels = [dict(c, source='meo', sourceId={'3028':'RTP1','2670':'SIC','2671':'TVI'}[c['sourceId']]) for c in json.loads((ROOT/'catalogue-seed.json').read_text()) if c['country']=='PT'][:2]
        result = collect_group(PartialClient(), channels, NOW)
        self.assertEqual(result[0]['programmes'], [])
        self.assertEqual(result[0]['sourceErrors'], ['HTTP 410', 'HTTP 410'])
        self.assertIsNotNone(current(result[1]['programmes'], NOW))
        self.assertEqual(result[1]['timingStatus'], 'pending')

    def test_tomorrow_failure_preserves_current_sky_programme(self):
        class PartialClient:
            def get(self, url, **kwargs):
                if '20260911' in url:
                    raise OSError('offline')
                return json.dumps(dict(schedule=[dict(sid='826', events=[dict(t='Current', st=NOW-10, d=100)])])).encode()
        channels = [dict(c, source='sky', sourceId='826') for c in json.loads((ROOT/'catalogue.json').read_text()) if c['country']=='DE']
        result = collect_group(PartialClient(), channels, NOW)
        self.assertEqual(current(result[0]['programmes'], NOW)['title'], 'Current')
        self.assertTrue(result[0]['sourceErrors'])


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


class CacheTests(unittest.TestCase):
    def test_gzip_quality_is_respected(self):
        self.assertTrue(accepts_gzip('br, gzip;q=0.8'))
        self.assertTrue(accepts_gzip('gzip, deflate'))
        self.assertFalse(accepts_gzip('gzip;q=0, identity'))
        self.assertFalse(accepts_gzip('gzip;q=invalid'))

    def test_visitor_auth_body_is_not_cached(self):
        with tempfile.TemporaryDirectory() as path, patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = Response(b'{"csrfToken":"temporary-visitor-value"}')
            client = Client(path)
            client.get('https://api.prod.sngtv.magentatv.de/EPG/JSON/Authenticate', body=b'{}', cacheable=False)
            self.assertEqual(list(Path(path).glob('*.body')), [])

    def test_cache_and_retry_after_survive_client_restart(self):
        clock = [1000]
        with tempfile.TemporaryDirectory() as path, patch('urllib.request.build_opener') as opener:
            client = Client(path, clock=lambda: clock[0])
            url = 'https://api.oqee.net/example'
            opener.return_value.open.return_value = Response(b'{}')
            self.assertEqual(client.get(url), b'{}')
            restarted = Client(path, clock=lambda: clock[0])
            self.assertEqual(restarted.get(url), b'{}')
            self.assertEqual(opener.return_value.open.call_count, 1)
            clock[0] += 901
            opener.return_value.open.side_effect = HTTPError(url, 429, 'Limit', {'Retry-After': '300'}, None)
            with self.assertRaises(HTTPError):
                restarted.get(url)
            fresh = Client(path, clock=lambda: clock[0])
            with self.assertRaisesRegex(ValueError, 'source_backoff'):
                fresh.get(url)
            self.assertEqual(opener.return_value.open.call_count, 2)

    def test_byte_limit_and_host_allowlist(self):
        with tempfile.TemporaryDirectory() as path, patch('urllib.request.build_opener') as opener:
            client = Client(path)
            opener.return_value.open.return_value = Response(b'', {'Content-Length': str(MAX_BYTES+1)})
            with self.assertRaisesRegex(ValueError, 'metadata_too_large'):
                client.get('https://api.oqee.net/large')
        for url in ['http://api.oqee.net/x', 'https://api.oqee.net.evil.invalid/x', 'https://127.0.0.1/x']:
            with self.assertRaises(ValueError):
                validate_url(url)


if __name__ == '__main__':
    unittest.main()
