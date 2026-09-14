import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_catalogue import assemble
from collect import collect, collect_country
from http_client import Client
from model import current, resolve_channel
from sources import collect_group
from test_pilot import NOW, Response, ROOT


def channel(id, source='sky', country='GB', xmltv=''):
    return dict(id=id, country=country, source=source, sourceId=id, name=id,
                aliases=[], feed=id, timezone='Europe/London', xmltvId=xmltv)


class ExpansionTests(unittest.TestCase):
    def test_sky_batches_and_failure_scope(self):
        class FakeClient:
            calls = []
            def get(self, url, **kwargs):
                ids = url.rsplit('/', 1)[1].split(',')
                self.calls.append(ids)
                if '20' in ids:
                    raise HTTPError(url, 503, 'Unavailable', {}, None)
                return json.dumps(dict(schedule=[dict(sid=sid, events=[dict(t='Show', st=NOW-60, d=3600)]) for sid in ids])).encode()
        client = FakeClient()
        rows = collect_group(client, [channel(str(i)) for i in range(45)], NOW)
        self.assertEqual([len(c) for c in client.calls], [20, 20, 20, 20, 5, 5])
        self.assertEqual(sum(bool(c['sourceErrors']) for c in rows), 20)
        self.assertEqual(sum(current(c['programmes'], NOW) is not None for c in rows), 25)

    def test_429_blocks_other_urls_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder, patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError('https://mi.tv/a', 429, 'Limited', {'Retry-After': '300'}, None)
            with self.assertRaises(HTTPError):
                Client(folder, clock=lambda:NOW).get('https://mi.tv/a')
            with self.assertRaisesRegex(ValueError, 'host_backoff'):
                Client(folder, clock=lambda:NOW+10).get('https://mi.tv/b')
            self.assertEqual(opener.return_value.open.call_count, 1)
            opener.return_value.open.side_effect = None
            opener.return_value.open.return_value = Response(b'{}')
            self.assertEqual(Client(folder, clock=lambda:NOW+301).get('https://mi.tv/c'), b'{}')

    def test_deduplication_never_merges_different_regions(self):
        east = channel('east', country='US', xmltv='HBO.us@East')
        east.update(name='HBO East', feed='East')
        same = dict(east, id='east-hd', sourceId='east-hd', name='HBO HD East')
        west = dict(east, id='west', sourceId='west', name='HBO West', xmltvId='HBO.us@West', feed='West')
        rows, stats = assemble({'US':[east, same, west]}, [east], limits={'US':10})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['id'], 'east')
        self.assertEqual({c['feed'] for c in rows}, {'East','West'})
        rebuilt, _ = assemble({'US':[same, west, east]}, [east], rows, limits={'US':10})
        self.assertEqual({c['id'] for c in rows}, {c['id'] for c in rebuilt})

    def test_expanded_catalogue_keeps_pilot_and_ambiguous_names_fail(self):
        rows=json.loads((ROOT/'catalogue.json').read_text())
        seed=json.loads((ROOT/'catalogue-seed.json').read_text())
        self.assertGreater(len(rows), 500)
        self.assertEqual(len({c['id'] for c in rows}), len(rows))
        self.assertTrue({c['id'] for c in seed}.issubset({c['id'] for c in rows}))
        self.assertIsNone(resolve_channel(rows, 'US', 'HBO'))
        self.assertIsNone(resolve_channel(rows, 'BR', 'GLOBO'))
        self.assertNotEqual(resolve_channel(rows, 'FR', 'TF1')['id'], resolve_channel(rows, 'FR', 'TF1 UHD')['id'])

    def test_partial_failure_preserves_only_fresh_same_feed(self):
        base = channel('a')
        old = dict(base, programmes=[dict(title='Show', start=NOW-60, end=NOW+7200, imageURL=None)],
                   fetchedAt=NOW-3500, timingStatus='source-format-checked', timeBasis='UTC')
        failed = dict(base, programmes=[], fetchedAt=NOW, sourceErrors=['HTTP 503'], timingStatus='source-format-checked', timeBasis='UTC')
        with patch('collect.collect_group', return_value=[failed]):
            rows = collect_country(None, [base], dict(schemaVersion=1, channels=[old]), NOW)
            self.assertEqual(rows[0]['validUntil'], NOW+100)
            self.assertTrue(rows[0]['cachedAfterError'])
            expired = collect_country(None, [base], dict(schemaVersion=1, channels=[old]), NOW+101)
            self.assertEqual(expired[0]['programmes'], [])
            switched = collect_country(None, [base], dict(schemaVersion=1, channels=[dict(old, sourceId='another-feed')]), NOW)
            self.assertEqual(switched[0]['programmes'], [])

    def test_partial_country_update_keeps_other_manifest_entries_and_sources(self):
        catalogue=[channel('f1','oqee','FR'),channel('f2','rai','FR'),channel('u1','sky','GB')]
        def fake(client, rows, now):
            return [dict(c, programmes=[dict(title='Show', start=NOW-60,end=NOW+3600,imageURL=None)],
                         fetchedAt=now, sourceErrors=[], timingStatus='source-format-checked', timeBasis='UTC') for c in rows]
        with tempfile.TemporaryDirectory() as folder, patch('collect.collect_group', side_effect=fake):
            out=Path(folder)/'output'
            first=collect(out, Path(folder)/'cache', countries=['FR','GB'], now=NOW, catalogue=catalogue)
            second=collect(out, Path(folder)/'cache', countries=['FR'], now=NOW+5, catalogue=catalogue)
            self.assertEqual([r['country'] for r in second['countries']],['FR','GB'])
            self.assertEqual(second['countries'][1]['generatedAt'],NOW)
            self.assertEqual(len(json.loads((out/'fr.json').read_text())['channels']),2)
            self.assertEqual(second['countries'][0]['source'],'oqee, rai')


if __name__ == '__main__':
    unittest.main()
