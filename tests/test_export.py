import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collect import COUNTRIES, collect
from export_static import export_static
from unittest.mock import patch
from test_pilot import NOW


class ExportTests(unittest.TestCase):
    def fixture(self, folder):
        source = Path(folder) / 'output'
        source.mkdir()
        for country in COUNTRIES:
            c = dict(id=country, country=country, source='sky', sourceId='1', name=country,
                     aliases=[], timingStatus='source-format-checked', fetchedAt=NOW,
                     validUntil=NOW+3600, sourceErrors=['private diagnostic'], secret='must not export',
                     programmes=[dict(title='Show', start=NOW-60, end=NOW+3000, imageURL=None, debug='discard')])
            shard = dict(schemaVersion=1, country=country, generatedAt=NOW,
                         refreshAfter=NOW+1800, validUntil=NOW+3600, channels=[c], debug='discard')
            (source / f'{country.lower()}.json').write_text(json.dumps(shard))
        (source / '.private').write_text('must not export')
        return source

    def test_only_public_contract_is_exported_and_timestamps_stay_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.fixture(folder)
            dest = Path(folder) / 'dist'
            files = export_static(source, dest, NOW+120)
            self.assertEqual(len(files), len(COUNTRIES) + 2)
            public = json.loads((dest / 'v1/fr.json').read_bytes())
            self.assertEqual(public['generatedAt'], NOW)
            self.assertEqual(public['refreshAfter'], NOW+1800)
            self.assertEqual(public['channels'][0]['validUntil'], NOW+3600)
            self.assertNotIn(b'private', b''.join(files.values()))
            self.assertNotIn(b'discard', b''.join(files.values()))

    def test_stale_collection_and_invalid_country_do_not_replace_previous_export(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.fixture(folder)
            dest = Path(folder) / 'dist'
            first = export_static(source, dest, NOW)
            with self.assertRaisesRegex(ValueError, 'no_fresh'):
                export_static(source, dest, NOW+3601)
            shard = json.loads((source / 'fr.json').read_text()); shard['country'] = 'US'
            (source / 'fr.json').write_text(json.dumps(shard))
            with self.assertRaisesRegex(ValueError, 'invalid_envelope'):
                export_static(source, dest, NOW)
            self.assertEqual((dest / 'v1/fr.json').read_bytes(), first['v1/fr.json'])

    def test_refuses_destination_containing_source_or_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.fixture(folder)
            with self.assertRaisesRegex(ValueError, 'non_public'):
                export_static(source, source, NOW)

    def test_collection_cadence_changes_refresh_without_extending_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            base = dict(id='fr.test', country='FR', source='oqee', sourceId='1', name='Test', aliases=[])
            def fake(client, rows, now):
                return [dict(c, programmes=[], fetchedAt=now, sourceErrors=[],
                             timingStatus='source-format-checked', timeBasis='UTC') for c in rows]
            with patch('collect.collect_group', side_effect=fake):
                for interval in (900, 1800):
                    collect(Path(folder)/'output', Path(folder)/'cache', ['FR'], NOW, [base], interval)
                    shard = json.loads((Path(folder)/'output/fr.json').read_bytes())
                    self.assertEqual(shard['refreshAfter'], NOW+interval)
                    self.assertEqual(shard['validUntil'], NOW+3600)
