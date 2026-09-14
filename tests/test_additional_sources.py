import json
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from additional_sources import onet_starts, rts_events, shahid_events, tvplus_events
from http_client import validate_url
from model import image_url
from sources import collect_group

NOW = int(datetime(2026, 9, 14, 18, tzinfo=timezone.utc).timestamp())


class AdditionalSourceTests(unittest.TestCase):
    def test_shahid_excludes_empty_slots_and_keeps_actual_broadcast_window(self):
        broadcast = dict(title='Show', actualFrom='2026-09-14T20:30:00+03:00', actualTo='2026-09-14T22:00:00+03:00',
                         productPoster='https://shahid.mbc.net/mediaObject/a?width={width}&height={height}&version=2')
        data = dict(items=[dict(channelId=1, items=[broadcast, dict(broadcast, emptySlot=True)]), dict(channelId=2, items=[broadcast])])
        rows = shahid_events(data, '1')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['start'], NOW - 1800)
        self.assertEqual(rows[0]['end'], NOW + 3600)
        self.assertIn('width=640', rows[0]['imageURL'])
        self.assertIn('version=2', rows[0]['imageURL'])

    def test_turkish_explicit_offset_and_channel_identity(self):
        e = dict(channelid='144', name='Show', starttime='2026-09-14 20:30:00 UTC+03:00',
                 endtime='2026-09-14 22:00:00 UTC+03:00', picture=dict(still='https://izmaottvsc14.tvplus.com.tr:33207/a.jpg'))
        data = dict(playbilllist=[e, dict(e, channelid='88')])
        rows = tvplus_events(data, '144')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['start'], NOW - 1800)
        self.assertIsNotNone(rows[0]['imageURL'])

    def test_rts_keeps_channel_and_dst_offsets_and_rejects_logo_placeholders(self):
        e = dict(title='News', startTime='2026-10-25T02:15:00+02:00', endTime='2026-10-25T02:45:00+01:00',
                 imageUrl='https://img.rts.ch/a.image', imageIsFallbackUrl=True)
        data = dict(data=[dict(channel=dict(id='one'), programList=[e]), dict(channel=dict(id='two'), programList=[dict(e, title='Other')])])
        rows = rts_events(data, 'one')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['end'] - rows[0]['start'], 5400)
        self.assertIsNone(rows[0]['imageURL'])

    def test_rts_overnight_uses_previous_broadcast_day_only_when_needed(self):
        class Client:
            def __init__(self): self.urls = []
            def get(self, url, **kwargs):
                self.urls.append(url)
                events = [dict(title='Night', startTime='2026-09-13T23:00:00Z', endTime='2026-09-14T04:00:00Z', imageIsFallbackUrl=True)] if url.endswith('2026-09-13') else []
                return json.dumps(dict(data=[dict(channel=dict(id='one'), programList=events)])).encode()
        client = Client()
        row = dict(id='ch.rts1', country='CH', source='rts', sourceId='one', name='RTS Un')
        midnight = int(datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp())
        rows = collect_group(client, [row], midnight)[0]['programmes']
        self.assertEqual(len(client.urls), 3)
        self.assertTrue(any(e['start'] <= midnight < e['end'] for e in rows))

    def test_polish_schedule_uses_iso_dates_not_host_timezone(self):
        block = dict(itemListElement=[dict(item=dict(**{'@type':'BroadcastEvent'}, name='A', startDate='2026-09-14T20:00:00+02:00', url='https://programtv.onet.pl/tv/a'))])
        rows = onet_starts('<script type="application/ld+json">' + json.dumps(block) + '</script>')
        self.assertEqual(rows[0]['start'], NOW)

    def test_polish_enrichment_is_bounded_and_never_invents_last_end(self):
        class Client:
            def __init__(self): self.details = []
            def get(self, url, **kwargs):
                if '/program-tv/' in url:
                    block = dict(itemListElement=[dict(item=dict(**{'@type':'BroadcastEvent'}, name=str(i),
                        startDate=f'2026-09-14T{18+i//2:02d}:{30*(i%2):02d}:00Z', url=f'https://programtv.onet.pl/tv/{i}')) for i in range(10)])
                    return ('<script type="application/ld+json">' + json.dumps(block) + '</script>').encode()
                self.details.append((url, kwargs['ttl']))
                return b'<meta property="og:image" content="https://cdn.programtv.onet.pl/a.jpg">'
        client = Client()
        row = dict(id='pl.a', country='PL', source='onet', sourceId='a', name='A')
        result = collect_group(client, [row], NOW)[0]['programmes']
        self.assertEqual(len(client.details), 4)
        self.assertTrue(all(ttl == 86400 for url, ttl in client.details))
        self.assertEqual(len(result), 9)
        self.assertEqual(sum(bool(e['imageURL']) for e in result), 4)
        self.assertTrue(all(e['end'] - e['start'] == 1800 for e in result))

    def test_source_image_paths_encode_spaces_without_double_encoding(self):
        raw = 'https://shahid.mbc.net/mediaObject/Bab Alhara/épisode%201.jpg?width=640&title=a b'
        encoded = image_url(raw, 'shahid')
        self.assertEqual(encoded, 'https://shahid.mbc.net/mediaObject/Bab%20Alhara/%C3%A9pisode%201.jpg?width=640&title=a%20b')
        self.assertEqual(image_url(encoded, 'shahid'), encoded)
        self.assertIsNone(image_url(raw + chr(10), 'shahid'))

    def test_new_hosts_remain_source_and_port_specific(self):
        validate_url('https://izmaottvsc14.tvplus.com.tr:33207/EPG/JSON/PlayBillList')
        for url in ('https://api2.shahid.net:33207/a', 'https://izmaottvsc14.tvplus.com.tr.evil.test:33207/a', 'https://user@www.rts.ch/a'):
            with self.assertRaises(ValueError): validate_url(url)
        self.assertIsNone(image_url('https://img.rts.ch:33207/a', 'rts'))
        self.assertIsNone(image_url('https://shahid.mbc.net.evil.test/a', 'shahid'))
        self.assertIsNone(image_url('https://user@izmaottvsc14.tvplus.com.tr:33207/a', 'tvplus'))

if __name__ == '__main__': unittest.main()
