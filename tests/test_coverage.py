import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_catalogue import assemble, feed_aliases, same_feed_key
from sources import collect_group, mitv_starts, skyit_events

NOW = 1789401600

def channel(id, xmltv, name):
    return dict(id=id, country='GB', source='sky', sourceId=id, name=name, feed=xmltv.partition('@')[2], xmltvId=xmltv, aliases=[])

class CoverageTests(unittest.TestCase):
    def test_hd_sd_same_feed_merge_names_without_merging_regions_or_delays(self):
        rows = [channel('1','SkySportsCricket.uk@SD','SkySp Cricket'),channel('2','SkySportsCricket.uk@HD','SkySpCricket HD'),channel('3','BBCOne.uk@London','BBC One London'),channel('4','BBCOne.uk@Scotland','BBC One Scotland'),channel('5','SkySportsCricket.uk@Plus1','Sky Sports Cricket +1')]
        result,_=assemble({'GB':rows},[],limits={'GB':20})
        self.assertEqual(len(result),4)
        cricket=next(x for x in result if x['sourceId']=='1')
        self.assertIn('SkySportsCricket',cricket['aliases'])
        self.assertIn('SkySpCricket HD',cricket['aliases'])
        self.assertNotEqual(same_feed_key(rows[2]),same_feed_key(rows[3]))
        self.assertNotEqual(same_feed_key(rows[0]),same_feed_key(rows[4]))

    def test_regional_feeds_never_gain_a_generic_brand_alias(self):
        for edition in ('East','West','London','Scotland','Plus1'):
            item=channel('a','HBO.us@'+edition,'HBO '+edition)
            self.assertNotIn('HBO',feed_aliases(item))

    def test_sky_italy_bounds_batches_and_scopes_truncated_responses(self):
        class Client:
            calls=[]
            def get(self,url,**kwargs):
                from urllib.parse import parse_qs,urlsplit
                ids=parse_qs(urlsplit(url).query)['channels'][0].split(',')
                self.calls.append(ids)
                if '8' in ids:return json.dumps(dict(events=[],total=1000)).encode()
                return json.dumps(dict(total=len(ids),events=[dict(channel=dict(id=i),eventTitle='Live',starttime='2026-09-14T15:00:00Z',endtime='2026-09-14T19:00:00Z',content=dict(imagesMap=[])) for i in ids])).encode()
        client=Client();rows=[dict(channel(str(i),'','Italian '+str(i)),country='IT',source='skyit') for i in range(18)]
        result=collect_group(client,rows,NOW)
        self.assertEqual([len(x) for x in client.calls],[8,8,2])
        self.assertEqual(sum(bool(x['sourceErrors']) for x in result),8)
        self.assertEqual(sum(bool(x['programmes']) for x in result),10)

    def test_north_american_current_programme_after_utc_midnight_uses_previous_date(self):
        from datetime import datetime, timezone
        from model import current
        now=int(datetime(2026,9,15,1,tzinfo=timezone.utc).timestamp())
        class Client:
            calls=[]
            def get(self,url,**kwargs):
                self.calls.append(url)
                date=url.rsplit('/',1)[-1]
                return ('<select id="timezone_selector"><option selected value="America/New_York"></option></select><div data-st="'+date+' 20:30:00" data-duration="120" data-showname="Evening"></div>').encode()
        client=Client();row=dict(channel('47','','RDS'),country='CA',source='tvpassport')
        result=collect_group(client,[row],now)[0]
        self.assertEqual([url.rsplit('/',1)[-1] for url in client.calls],['2026-09-15','2026-09-14','2026-09-16'])
        self.assertEqual(current(result['programmes'],now)['title'],'Evening')

    def test_italian_cards_prefer_landscape_artwork_to_the_vertical_poster(self):
        item=dict(channel=dict(id=1),eventTitle='Show',starttime='2026-09-14T15:00:00Z',endtime='2026-09-14T19:00:00Z',content=dict(imagesMap=[dict(key='cover',img=dict(url='/poster.jpg')),dict(key='scene_key_art',img=dict(url='/wide.jpg'))]))
        self.assertEqual(skyit_events(dict(events=[item]),'1')[0]['imageURL'],'https://guidatv.sky.it/wide.jpg')
        item['content']['imagesMap'].append(dict(key='scene',img=dict(url='/scene.jpg')))
        self.assertEqual(skyit_events(dict(events=[item]),'1')[0]['imageURL'],'https://guidatv.sky.it/scene.jpg')
        item['content']['imagesMap']=[dict(key='scene',img=dict(url='/uuid/News_Scene_QP6ke0FVO.png'))]
        self.assertIsNone(skyit_events(dict(events=[item]),'1')[0]['imageURL'])

    def test_mexico_uses_its_own_utc_schedule_and_keeps_midnight_boundary(self):
        class Client:
            calls=[]
            def get(self,url,**kwargs):
                self.calls.append(url)
                return b'<ul class="broadcasts"><li><span class="time">23:30</span><h2>A</h2></li><li><span class="time">00:30</span><h2>B</h2></li></ul>'
        client=Client();row=dict(channel('5','','Canal 5'),country='MX',source='mitv')
        result=collect_group(client,[row],NOW)[0]
        self.assertEqual(len(client.calls),2)
        self.assertTrue(all('/mx/async/channel/' in x and x.endswith('/0') for x in client.calls))
        self.assertTrue(result['programmes'])
        self.assertEqual(result['programmes'][0]['end']-result['programmes'][0]['start'],3600)

if __name__=='__main__':unittest.main()
