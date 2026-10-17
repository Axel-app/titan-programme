import gzip
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_v2 import build, build_day, edition, entries, id_key
from collect import COUNTRIES
from export_static import export_static
from test_pilot import NOW
from xmltv_sources import GuideStore, REFRESH_SECONDS, reduce_guide, validate_guide_url
from iptvorg_channels import missing_channels, write_channels
from sources import NEAR_TTL, OQEE_HOURS_AHEAD, SLOW_TTL, collect_group


def stamp(seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y%m%d%H%M%S +0000')


def guide(*programmes, channels=(('TF1.fr', 'TF1'),)):
    body = ''.join(f'<channel id="{i}"><display-name>{n}</display-name></channel>' for i, n in channels)
    for channel, start, end, title, image in programmes:
        icon = f'<icon src="{image}"/>' if image else ''
        body += f'<programme channel="{channel}" start="{stamp(start)}" stop="{stamp(end)}"><title>{title}</title>{icon}</programme>'
    return f'<?xml version="1.0"?><tv>{body}</tv>'.encode()


class FakeResponse(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, answers):
        self.answers, self.requests = list(answers), []

    def open(self, request, timeout=None):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def reduction(name, rank, channels, folder, anchor=NOW):
    path = Path(folder) / f'{name}.json'
    path.write_text(json.dumps(channels))
    return dict(name=name, rank=rank, anchor=anchor, path=str(path), channels=len(channels))


class IdentityTests(unittest.TestCase):
    def test_ids_from_different_guides_meet(self):
        self.assertEqual(id_key('France.24.Arabic.ae'), 'france24arabic.ae')
        self.assertEqual(id_key('France24Arabic.ae@SD'), 'france24arabic.ae')
        self.assertEqual(id_key('Téva.fr'), 'teva.fr')
        self.assertEqual(id_key('673247127d5da5000817b4d6'), '')
        self.assertEqual(id_key(''), '')

    def test_editions(self):
        self.assertEqual(edition('UK'), 'GB')
        self.assertEqual(edition('ae'), 'MENA')
        self.assertEqual(edition('MA'), 'MENA')
        self.assertEqual(edition('fr'), 'FR')
        self.assertIsNone(edition('frdrom'))

    def test_guide_hosts(self):
        validate_guide_url('https://xmltvfr.fr/xmltv/xmltv.xml.gz')
        validate_guide_url('https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/PlutoTV/fr.xml.gz')
        for url in ('http://xmltvfr.fr/x.gz', 'https://evil.example/x.gz',
                    'https://raw.githubusercontent.com/someone/else/x.gz', 'https://user:pw@xmltvfr.fr/x'):
            with self.assertRaises(ValueError):
                validate_guide_url(url)


class ReduceTests(unittest.TestCase):
    def test_only_illustrated_programmes_inside_the_window(self):
        data = guide(
            ('TF1.fr', NOW - 1800, NOW + 1800, 'Journal', 'https://img.example/1.jpg'),
            ('TF1.fr', NOW + 1800, NOW + 3600, 'Sans image', None),
            ('TF1.fr', NOW - 10 * 3600, NOW - 9 * 3600, 'Trop tôt', 'https://img.example/2.jpg'),
            ('TF1.fr', NOW + 60 * 3600, NOW + 61 * 3600, 'Trop tard', 'https://img.example/3.jpg'),
            ('TF1.fr', NOW, NOW + 60, 'Image douteuse', 'javascript:alert(1)'),
        )
        channels = reduce_guide(io.BytesIO(data), NOW, None)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]['country'], 'FR')
        self.assertEqual(channels[0]['names'], ['TF1'])
        self.assertEqual(channels[0]['programmes'], [[NOW - 1800, NOW + 1800, 'Journal', 'https://img.example/1.jpg']])

    def test_country_comes_from_the_file_when_the_id_has_none(self):
        data = guide(('abc', NOW, NOW + 60, 'Show', 'https://img.example/x.jpg'), channels=(('abc', 'Pluto Movies'),))
        self.assertEqual(reduce_guide(io.BytesIO(data), NOW, 'US')[0]['country'], 'US')


class MergeTests(unittest.TestCase):
    def test_same_service_is_merged_and_keeps_the_widest_guide(self):
        with tempfile.TemporaryDirectory() as folder:
            short = [[NOW, NOW + 3600, 'A', 'https://img.example/a.jpg']]
            wide = short + [[NOW + 3600, NOW + 7200, 'B', 'https://img.example/b.jpg']]
            reductions = [
                reduction('xmltvfr', 1, [dict(id='TF1.fr', names=['TF1'], country='FR', programmes=short)], folder),
                reduction('epgshare-fr1', 2, [dict(id='TF1.HD.fr', names=['TF1 HD'], country='FR', programmes=wide),
                                              dict(id='France.3.fr', names=['France 3'], country='FR', programmes=short)], folder),
            ]
            day = build_day(entries(reductions, []))['FR']
            tf1 = next(c for c in day if 'TF1' in c['names'])
            self.assertEqual(tf1['k'], 'tf1.fr')
            self.assertEqual(tf1['programmes'], wide)
            self.assertEqual(len(day), 2)

    def test_a_name_shared_inside_one_guide_identifies_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            p = [[NOW, NOW + 3600, 'A', 'https://img.example/a.jpg']]
            reductions = [
                reduction('epgshare-fr1', 2, [dict(id='France3.Alpes.fr', names=['France 3'], country='FR', programmes=p),
                                              dict(id='France3.Bretagne.fr', names=['France 3'], country='FR', programmes=p)], folder),
                reduction('xmltvfr', 1, [dict(id='France3.fr', names=['France 3'], country='FR', programmes=p)], folder),
            ]
            self.assertEqual(len(build_day(entries(reductions, []))['FR']), 3)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        p = [[NOW - 600, NOW + 3000, 'Matin', 'https://img.example/m.jpg']]
        self.reductions = [reduction('xmltvfr', 1, [dict(id='TF1.fr', names=['TF1'], country='FR', programmes=p)], self.root)]
        self.shards = [dict(schemaVersion=1, country='FR', channels=[dict(
            id='fr.tf1', country='FR', source='oqee', sourceId='1', name='TF1', aliases=['TF1 HD'],
            timingStatus='source-format-checked', fetchedAt=NOW, validUntil=NOW + 3600,
            programmes=[dict(title='Direct', start=NOW - 300, end=NOW + 900, imageURL='https://oqee.net/x.jpg')])])]

    def tearDown(self):
        self.folder.cleanup()

    def test_day_buckets_stay_byte_identical_until_a_guide_changes(self):
        output = self.root / 'output'
        first = build(output, self.reductions, self.shards, NOW)
        day = (output / 'v2/fr/day-0.json').read_bytes()
        self.shards[0]['channels'][0]['fetchedAt'] = NOW + 1800
        self.shards[0]['channels'][0]['programmes'][0]['title'] = 'Direct corrigé'
        second = build(output, self.reductions, self.shards, NOW + 1800)
        self.assertEqual((output / 'v2/fr/day-0.json').read_bytes(), day)
        self.assertEqual(first['editions']['FR']['day'], second['editions']['FR']['day'])
        self.assertNotEqual(first['editions']['FR']['live'], second['editions']['FR']['live'])
        live = json.loads((output / 'v2/fr/live.json').read_bytes())
        self.assertEqual(live['channels']['tf1.fr'][0][2], 'Direct corrigé')
        directory = json.loads((output / 'v2/fr/channels.json').read_bytes())
        self.assertEqual(directory['channels'][0]['ids'], ['tf1.fr'])
        self.assertIn('TF1 HD', directory['channels'][0]['names'])

    def test_an_edition_keeps_its_day_while_another_country_guide_moves(self):
        output = self.root / 'output'
        kr = [[NOW - 600, NOW + 5 * 3600, '뉴스', 'https://img.example/k.jpg']]
        samsung = reduction('samsungtvplus-kr', 3, [dict(id='KR1300003KW', names=['STN'], country='KR', programmes=kr)], self.root)
        first = build(output, self.reductions + [samsung], self.shards, NOW)
        fr_day = (output / 'v2/fr/day-0.json').read_bytes()
        # The operator now describes TF1 longer than the guide: a rebuilt FR day would change.
        channel = self.shards[0]['channels'][0]
        channel.update(fetchedAt=NOW + 7200, programmes=[dict(title='Direct corrigé', start=NOW + 7000, end=NOW + 20000, imageURL='https://oqee.net/y.jpg')])
        kr = [[NOW + 7000, NOW + 7 * 3600, '드라마', 'https://img.example/d.jpg']]
        samsung = reduction('samsungtvplus-kr', 3, [dict(id='KR1300003KW', names=['STN'], country='KR', programmes=kr)], self.root, anchor=NOW + 7200)
        second = build(output, self.reductions + [samsung], self.shards, NOW + 7200)
        self.assertEqual((output / 'v2/fr/day-0.json').read_bytes(), fr_day)
        self.assertEqual(second['editions']['FR']['day'], first['editions']['FR']['day'])
        self.assertEqual(second['editions']['FR']['builtAt'], NOW)
        self.assertNotEqual(second['editions']['KR']['day'], first['editions']['KR']['day'])
        self.assertEqual(second['editions']['KR']['builtAt'], NOW + 7200)
        self.assertNotIn('inputs', second['editions']['KR'])
        # The kept edition still gets its live corrections.
        live = json.loads((output / 'v2/fr/live.json').read_bytes())
        self.assertEqual(live['channels']['tf1.fr'][0][2], 'Direct corrigé')
        # After 13 hours the edition is built again, with the operator programmes of that moment.
        channel['fetchedAt'] = NOW + 13 * 3600 + 60
        third = build(output, self.reductions + [samsung], self.shards, NOW + 13 * 3600 + 60)
        self.assertEqual(third['editions']['FR']['builtAt'], NOW + 13 * 3600 + 60)
        self.assertNotEqual((output / 'v2/fr/day-0.json').read_bytes(), fr_day)

    def test_a_state_written_before_the_edition_inputs_rebuilds_every_edition(self):
        output = self.root / 'output'
        build(output, self.reductions, self.shards, NOW)
        state_file = output / 'v2/state.json'
        state = json.loads(state_file.read_bytes())
        for report in state['editions'].values():
            report.pop('inputs')
        state['signature'] = 'older'
        state_file.write_text(json.dumps(state))
        self.assertEqual(build(output, self.reductions, self.shards, NOW + 1800)['editions']['FR']['builtAt'], NOW + 1800)

    def test_stale_operator_data_is_not_published_live(self):
        self.shards[0]['channels'][0]['fetchedAt'] = NOW - 7200
        manifest = build(self.root / 'output', self.reductions, self.shards, NOW)
        self.assertEqual(manifest['editions']['FR']['liveChannels'], 0)


class StoreTests(unittest.TestCase):
    def test_conditional_downloads_twice_a_day_and_previous_copy_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            data = gzip.compress(guide(('TF1.fr', NOW, NOW + 3600, 'A', 'https://img.example/a.jpg')))
            opener = FakeOpener([FakeResponse(data, {'ETag': '"v1"', 'Content-Length': str(len(data))})])
            store = GuideStore(Path(folder) / 'cache', Path(folder) / 'out', opener=opener, pause=0)
            first = store.reduced('xmltvfr', 'https://xmltvfr.fr/xmltv/xmltv.xml.gz', 1, None, NOW)
            self.assertEqual(first['channels'], 1)
            self.assertFalse((Path(folder) / 'cache/xmltv/xmltvfr.xml.gz').exists())
            self.assertEqual(store.reduced('xmltvfr', 'https://xmltvfr.fr/xmltv/xmltv.xml.gz', 1, None, NOW + 3600), first)
            self.assertEqual(len(opener.requests), 1)
            opener.answers.append(HTTPError('u', 304, 'Not Modified', {}, None))
            again = store.reduced('xmltvfr', 'https://xmltvfr.fr/xmltv/xmltv.xml.gz', 1, None, NOW + REFRESH_SECONDS + 60)
            self.assertEqual(again, first)
            self.assertEqual(opener.requests[-1].get_header('If-none-match'), '"v1"')
            opener.answers.append(OSError('down'))
            later = store.reduced('xmltvfr', 'https://xmltvfr.fr/xmltv/xmltv.xml.gz', 1, None, NOW + 2 * REFRESH_SECONDS + 120)
            self.assertEqual(later, first)


    SAMSUNG = 'https://i.mjh.nz/SamsungTVPlus/kr.xml.gz'

    def samsung(self, opener, folder, now, end):
        data = gzip.compress(guide(('KR1300003KW', now - 600, end, '뉴스', 'https://img.example/k.jpg'), channels=(('KR1300003KW', 'STN'),)))
        opener.answers.append(FakeResponse(data, {'ETag': f'"{now}"'}))

    def test_a_guide_ending_within_4_hours_is_asked_again_after_2_hours(self):
        with tempfile.TemporaryDirectory() as folder:
            opener = FakeOpener([])
            store = GuideStore(Path(folder) / 'cache', Path(folder) / 'out', opener=opener, pause=0)
            self.samsung(opener, folder, NOW, NOW + 5 * 3600)
            first = store.reduced('samsungtvplus-kr', self.SAMSUNG, 3, 'KR', NOW)
            self.assertEqual(first['horizon'], NOW + 5 * 3600)
            # 3.5 hours left, but asked less than 2 hours ago.
            self.assertEqual(store.reduced('samsungtvplus-kr', self.SAMSUNG, 3, 'KR', NOW + 5400), first)
            self.assertEqual(len(opener.requests), 1)
            later = NOW + 2 * 3600 + 60
            self.samsung(opener, folder, later, later + 5 * 3600)
            second = store.reduced('samsungtvplus-kr', self.SAMSUNG, 3, 'KR', later)
            self.assertEqual(opener.requests[-1].get_header('If-none-match'), f'"{NOW}"')
            self.assertEqual((second['anchor'], second['horizon']), (later, later + 5 * 3600))
            # Unchanged guide: the next question waits 2 more hours, it does not repeat every 30 minutes.
            opener.answers.append(HTTPError('u', 304, 'Not Modified', {}, None))
            self.assertEqual(store.reduced('samsungtvplus-kr', self.SAMSUNG, 3, 'KR', later + 2 * 3600 + 60), second)
            self.assertEqual(store.reduced('samsungtvplus-kr', self.SAMSUNG, 3, 'KR', later + 2 * 3600 + 1860), second)
            self.assertEqual(len(opener.requests), 3)

    def test_a_guide_two_days_ahead_and_an_older_summary_stay_on_12_hours(self):
        with tempfile.TemporaryDirectory() as folder:
            opener = FakeOpener([])
            store = GuideStore(Path(folder) / 'cache', Path(folder) / 'out', opener=opener, pause=0)
            self.samsung(opener, folder, NOW, NOW + 40 * 3600)
            first = store.reduced('samsungtvplus-kr', self.SAMSUNG, 3, 'KR', NOW)
            self.assertEqual(store.reduced('samsungtvplus-kr', self.SAMSUNG, 3, 'KR', NOW + 11 * 3600), first)
            self.assertEqual(len(opener.requests), 1)
            # A summary written before the horizon existed, for a guide that ends soon.
            self.samsung(opener, folder, NOW + 11 * 3600 + 1, NOW + 13 * 3600)
            store.reduced('samsungtvplus-kr', self.SAMSUNG, 3, 'KR', NOW + 12 * 3600 + 60)
            summary_file = Path(folder) / 'out/xmltv/samsungtvplus-kr.summary.json'
            summary = json.loads(summary_file.read_text())
            summary.pop('horizon')
            summary_file.write_text(json.dumps(summary))
            self.assertEqual(store.reduced('samsungtvplus-kr', self.SAMSUNG, 3, 'KR', NOW + 16 * 3600), summary)
            self.assertEqual(len(opener.requests), 2)


class CadenceTests(unittest.TestCase):
    class Recorder:
        def __init__(self, body):
            self.body, self.calls = body, []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs.get('ttl')))
            return json.dumps(self.body).encode()

    def test_oqee_hours_on_air_every_30_minutes_the_rest_every_6_hours(self):
        client = self.Recorder(dict(success=True, result=dict(entries={})))
        collect_group(client, [dict(id='fr.tf1', source='oqee', sourceId='1', country='FR', name='TF1', aliases=[])], NOW)
        self.assertEqual(len(client.calls), OQEE_HOURS_AHEAD)
        self.assertEqual([ttl for _, ttl in client.calls[:2]], [NEAR_TTL, NEAR_TTL])
        self.assertTrue(all(ttl == SLOW_TTL for _, ttl in client.calls[2:]))

    def test_day_ahead_guides_are_asked_every_6_hours(self):
        client = self.Recorder(dict(schedule=[]))
        collect_group(client, [dict(id='gb.bbc1', source='sky', sourceId='2002', country='GB', name='BBC One', aliases=[])], NOW)
        self.assertTrue(client.calls and all(ttl == SLOW_TTL for _, ttl in client.calls))


class GrabbedGuideTests(unittest.TestCase):
    def test_only_channels_the_public_guides_miss_are_grabbed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            site = root / 'sites/tv.blue.ch'
            site.mkdir(parents=True)
            (site / 'tv.blue.ch.channels.xml').write_text('''<?xml version="1.0" encoding="UTF-8"?><channels>
              <channel site="tv.blue.ch" site_id="1" lang="fr" xmltv_id="TF1.fr@SD">TF1</channel>
              <channel site="tv.blue.ch" site_id="2" lang="fr" xmltv_id="TF1Bis.fr@SD">TF1 HD</channel>
              <channel site="tv.blue.ch" site_id="3" lang="de" xmltv_id="SRF1.ch@HD">SRF 1</channel>
              <channel site="tv.blue.ch" site_id="4" lang="de" xmltv_id="">No id</channel>
              <channel site="tv.blue.ch" site_id="5" lang="de" xmltv_id="SRF1.ch@SD">SRF 1</channel></channels>''')
            (root / 'unknown.site').mkdir()
            v2 = root / 'v2/fr'
            v2.mkdir(parents=True)
            (v2 / 'channels.json').write_text(json.dumps(dict(edition='FR', channels=[dict(k='tf1.fr', ids=['tf1.fr'], names=['TF1'], b=0)])))
            channels = missing_channels(root / 'sites', root / 'v2')
            self.assertEqual([c.get('xmltv_id') for c in channels], ['SRF1.ch@HD'])
            write_channels(channels, root / 'out.xml')
            self.assertIn('site_id="3"', (root / 'out.xml').read_text())

    def test_grabbed_guide_is_reduced_once_and_keeps_its_country(self):
        with tempfile.TemporaryDirectory() as folder:
            grabbed = Path(folder) / 'grabbed'
            grabbed.mkdir()
            data = guide(('SRF1.ch@HD', NOW, NOW + 3600, 'Tagesschau Spezial', 'https://img.example/s.jpg'), channels=(('SRF1.ch@HD', 'SRF 1'),))
            (grabbed / 'iptvorg.xml.gz').write_bytes(gzip.compress(data))
            store = GuideStore(Path(folder) / 'cache', Path(folder) / 'out', opener=FakeOpener([]), pause=0)
            first = store.collect(now=NOW, selection={'none'}, local=grabbed)
            self.assertEqual([s['name'] for s in first], ['iptvorg-iptvorg'])
            channels = json.loads(Path(first[0]['path']).read_bytes())
            self.assertEqual(channels[0]['country'], 'CH')
            self.assertEqual(store.collect(now=NOW + 600, selection={'none'}, local=grabbed), first)
            self.assertEqual(store.stats['reused'], 1)


class ExportTests(unittest.TestCase):
    def fixture(self, root):
        source = root / 'output'
        source.mkdir()
        for country in COUNTRIES:
            c = dict(id=country, country=country, source='sky', sourceId='1', name=country, aliases=[],
                     timingStatus='source-format-checked', fetchedAt=NOW, validUntil=NOW + 3600,
                     programmes=[dict(title='Show', start=NOW - 60, end=NOW + 3000, imageURL=None)])
            (source / f'{country.lower()}.json').write_text(json.dumps(dict(
                schemaVersion=1, country=country, generatedAt=NOW, refreshAfter=NOW + 1800, validUntil=NOW + 3600, channels=[c])))
        return source

    def test_schema_2_is_published_next_to_schema_1(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.fixture(root)
            p = [[NOW - 600, NOW + 3000, 'Matin', 'https://img.example/m.jpg']]
            build(source, [reduction('xmltvfr', 1, [dict(id='TF1.fr', names=['TF1'], country='FR', programmes=p)], root)], [], NOW)
            files = export_static(source, root / 'dist', NOW + 60)
            self.assertIn('v2/manifest.json', files)
            self.assertIn('v2/fr/day-0.json', files)
            self.assertIn('v1/fr.json', files)
            self.assertIn(b'/v2/*', files['_headers'])
            self.assertNotIn('v2/state.json', files)

    def test_schema_1_keeps_only_two_hours_of_oqee(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.fixture(root)
            shard = json.loads((source / 'fr.json').read_text())
            shard['channels'][0].update(source='oqee', programmes=[
                dict(title='Maintenant', start=NOW - 60, end=NOW + 3000, imageURL=None),
                dict(title='Ce soir', start=NOW + 5 * 3600, end=NOW + 6 * 3600, imageURL=None)])
            (source / 'fr.json').write_text(json.dumps(shard))
            files = export_static(source, root / 'dist', NOW + 60)
            titles = [e['title'] for e in json.loads(files['v1/fr.json'])['channels'][0]['programmes']]
            self.assertEqual(titles, ['Maintenant'])
            self.assertEqual(len(json.loads((source / 'fr.json').read_text())['channels'][0]['programmes']), 2)

    def test_broken_schema_2_never_blocks_schema_1(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.fixture(root)
            (source / 'v2').mkdir()
            (source / 'v2/manifest.json').write_text('{"schemaVersion": 2, "editions": {"FR": {"buckets": 3}}}')
            files = export_static(source, root / 'dist', NOW + 60)
            self.assertIn('v1/fr.json', files)
            self.assertFalse(any(k.startswith('v2/') for k in files))


if __name__ == '__main__':
    unittest.main()
