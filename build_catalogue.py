#!/usr/bin/env python3

import argparse
from collections import Counter, defaultdict
import hashlib
import http.cookiejar
import json
from pathlib import Path
import re
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET

from http_client import Client, SafeRedirect, atomic_write

ROOT = Path(__file__).resolve().parent
LIMITS = dict(FR=450, GB=180, DE=140, IT=180, ES=90, PT=120, US=120, BR=140, CA=140, MX=60)
ZONES = dict(FR='Europe/Paris', GB='Europe/London', DE='Europe/Berlin', IT='Europe/Rome',
             ES='Europe/Madrid', PT='Europe/Lisbon', US='America/New_York', BR='America/Sao_Paulo', CA='America/Toronto', MX='America/Mexico_City')
GITHUB = 'https://raw.githubusercontent.com/iptv-org/epg/master/sites/'
REFERENCES = {
    'skyit-channels.json': 'https://apid.sky.it/gtv/v1/channels?env=DTH',
    'mi.tv_mx.channels.xml': GITHUB + 'mi.tv/mi.tv_mx.channels.xml',
    'oqee-plan.response': 'https://api.oqee.net/api/v6/service_plan',
    'sky.com.channels.xml': GITHUB + 'sky.com/sky.com.channels.xml',
    'rai-channels.response': GITHUB + 'raiplay.it/raiplay.it.channels.xml',
    'vodafone-channels.response': GITHUB + 'vodafone.pt/vodafone.pt.channels.xml',
    'movistarplus.es.channels.xml': GITHUB + 'movistarplus.es/movistarplus.es.channels.xml',
    'tvpassport.com.channels.xml': GITHUB + 'tvpassport.com/tvpassport.com.channels.xml',
    'br-sitemap-xml.response': GITHUB + 'mi.tv/mi.tv_br.channels.xml',
    'magenta-channels.response': 'https://api.prod.sngtv.magentatv.de/EPG/JSON/AllChannel',
}
US_BRANDS = '''ABC CBS NBC Fox ABCNewsLive AMC CartoonNetwork CBSSportsNetworkUSA CNBC CNN
DiscoveryChannel DisneyChannel DisneyJunior ESPN ESPN2 ESPNews ESPNU FoodNetwork FoxBusinessNetwork
FoxNewsChannel FoxSports1 FX FXX HBO HBO2 HGTV History HLN MLBNetwork MSNBC NationalGeographic
NationalGeographicWild NBATV NFLNetwork Nickelodeon ParamountNetwork PBS Showtime TBS TNT USA TVLand
Bravo ComedyCentral GolfChannel AnimalPlanet TravelChannel TLC AandE BET Cinemax Starz MTV MTV2 VH1 HallmarkChannel HallmarkMystery HallmarkDrama IFC TennisChannel AmericanHeroesChannel CMT BETHer QVC OutsideTV NFLRedZone SportsNetNewYork Syfy truTV Lifetime LifetimeMovies Oxygen Reelz SundanceTV TurnerClassicMovies CookingChannel Science InvestigationDiscovery NewsNation Newsmax TheWeatherChannel'''.split()
GB_BRANDS = '''BBCOne BBCTwo BBCThree BBCFour BBCNews BBCParliament BBCAlba BBCScotland ITV1 ITV2 ITV3 ITV4
Channel4 Channel5 E4 More4 Film4 4seven 5USA 5STAR 5Action 5SELECT SkyNews SkyArts SkyAtlantic SkyOne
SkyComedy SkyCrime SkyDocumentaries SkyHistory SkyNature SkyWitness SkySciFi SkyKids
SkyCinemaPremiere SkyCinemaAction SkyCinemaComedy SkyCinemaDrama SkyCinemaFamily SkyCinemaGreats
SkyCinemaHits SkyCinemaSciFiHorror SkyCinemaSelect SkyCinemaThriller SkySportsMainEvent
SkySportsPremierLeague SkySportsFootball SkySportsF1 SkySportsCricket SkySportsGolf SkySportsTennis
SkySportsNFL SkySportsNews SkySportsMix SkySportsRacing TNTSports1 TNTSports2 TNTSports3 TNTSports4
DiscoveryChannel AnimalPlanet NationalGeographic NationalGeographicWild TLC ComedyCentral
CartoonNetwork CBBC CBeebies Nickelodeon NickJr Nicktoons Boomerang Cartoonito BabyTV
FoodNetwork Quest QuestRed Really UAlibi UDave UDrama UGold UYesterday GBNews S4C STV UTV'''.split()
ES_PRIORITY = '''TVE LA2 A3 T5 SEXTA C4 24H TDEP ANT VAMOSD MPLUS MLIGA CHAPIO CPDEP GOLF+ MVF1
M1SD M2SD DAZNLI ESP ESP2 MV1 CPACCI CPCOLE CPCOME DCESP MCLAS MINDI MORIG MDOC
AXN FOXGE TNT SCI-FI PCM TCM CL13 COSMO SET NEOX NOVA FDFIC ENERGY DIVINI MEGA ATRESS
DCR DCRMAX NATGEO NATGW DKISS CLANTV BOING NICK NICKJR PLAYDC DWSP ANTV TELMIN TVC TVG ETB ARAGON'''.split()
PT_PRIORITY = '''3028 2825 2670 2671 7187 2935 5678 3414 8711 2729 2730 2731 2811 2719 7251 8835 2930
8829 8830 8831 8832 8833 3405 5585 3004 2675 5043 2712 2832 3024 2831 3026 2713 2700 2833
2715 2893 2847 2717 2845 2914 2741 2846 7185 8006 7522 8645 2937 2936 2915 7084 8424 2727'''.split()
BR_PRIORITY = '''globo-sp globo-rj record sbt band globo-brasilia globo-nordeste-hd globo-rbs-tv-poa
globo-tv-bahia globo-belem-liberal globo-amazonas record-rj band-bahia band-curitiba
globo-news cnn-brasil band-news bandsports sportv sportv2 sportv3 espn espn-2 espn-plus
premiere-clubes premiere-2 premiere-3 premiere-4 premiere-5 premiere-6 premiere-7 premiere-8
hbo hbo-2 hbo-plus hbo-family hbo-signature hbo-mundi hbo-pop hbo-xtreme
telecine-premium telecine-action telecine-touch telecine-pipoca telecine-cult telecine-fun
gnt viva multishow bis-hd canal-brasil futura cultura discovery discovery-kids
discovery-home-health discovery-turbo animal-planet national-geographic history h2
cartoon gloob gloobinho nickelodeon nick-jr axn sony warner tnt tnt-series space cinemax'''.split()


def slug(value):
    value = ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '-', value.replace('+', ' plus ')).strip('-')


def refresh_references(directory):
    client = Client(ROOT / '.cache' / 'discovery')
    for filename, url in REFERENCES.items():
        if filename == 'magenta-channels.response':
            opener = urllib.request.build_opener(SafeRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            base = url.rsplit('/', 1)[0] + '/'
            auth = client.json(base + 'Authenticate?SID=firstup&T=Windows_chrome_118',
                body=json.dumps(dict(terminalid='00:00:00:00:00:00', mac='00:00:00:00:00:00',
                    terminaltype='WEBTV', utcEnable=1, timezone='Etc/GMT0', userType=3, terminalvendor='Unknown')).encode(),
                headers={'Content-Type': 'application/json'}, opener=opener, cacheable=False)
            data = client.get(url, body=json.dumps(dict(channelNamespace=2,
                filterlist=[dict(key='IsHide', value='-1')], metaDataVer='Channel/1.1', returnSatChannel=0,
                properties=[dict(include='/channellist/logicalChannel/contentId,/channellist/logicalChannel/name',
                                 name='logicalChannel')])).encode(),
                opener=opener, headers={'Content-Type': 'application/json', 'X_CSRFTOKEN': auth['csrfToken']}, ttl=86400)
        else:
            data = client.get(url, ttl=86400)
        atomic_write(directory / filename, data)


def candidates(directory):
    result = defaultdict(list)
    def add(country, source, sid, name, xmltv='', feed=None):
        result[country].append(dict(country=country, source=source, sourceId=str(sid), name=name.strip(),
            xmltvId=xmltv, feed=feed or xmltv.partition('@')[2] or name.strip(),
            timezone=ZONES[country], aliases=[]))
    plan = json.loads((directory / 'oqee-plan.response').read_text())['result']
    lineup = []
    for entry in plan['channel_list']:
        if entry['type'] in ('channel', 'regional_channel'):
            lineup.append(str(entry['channel_id']))
            lineup.extend(map(str, entry.get('regional_channel', {}).values()))
    for sid in dict.fromkeys(lineup):
        c = plan['channels'].get(sid, {})
        if c.get('name') and c.get('category_id') in ('generalist', 'local', 'entertainment', 'sport', 'cinema', 'kids', 'news', 'music') and not c.get('adult'):
            add('FR', 'oqee', sid, c['name'])
    for c in json.loads((directory / 'magenta-channels.response').read_text())['channellist']:
        add('DE', 'magenta', c['contentId'], c['name'])
    specifications = [('GB', 'sky', 'sky.com.channels.xml'), ('IT', 'rai', 'rai-channels.response'),
        ('ES', 'movistar', 'movistarplus.es.channels.xml'), ('PT', 'vodafone', 'vodafone-channels.response'),
        ('US', 'tvpassport', 'tvpassport.com.channels.xml'), ('BR', 'mitv', 'br-sitemap-xml.response'),
        ('CA', 'tvpassport', 'tvpassport.com.channels.xml'), ('MX', 'mitv', 'mi.tv_mx.channels.xml')]
    for country, source, filename in specifications:
        nodes = list(ET.parse(directory / filename).getroot())
        source_ids = {n.get('site_id', '') for n in nodes}
        for node in nodes:
            sid, xmltv, name = node.get('site_id', ''), node.get('xmltv_id', ''), node.text or ''
            if country == 'GB':
                if not sid.startswith('GB#') or '.uk@' not in xmltv:
                    continue

                if re.search(r'radio|bbc r\d|\bfm\b|talksport|heart|capital|smooth|absolute|gold radio|adult|babes|babenation|tvx|uhd|4k|mosaic', name, re.I) or 'Ireland' in xmltv.partition('@')[2].replace('NorthernIreland', ''):
                    continue
                sid = sid.split('#', 1)[1]
            elif country == 'IT' and (not xmltv or not sid.startswith('rai-')):
                continue
            elif country == 'PT' and '.pt@' not in xmltv:
                continue
            elif country == 'US' and xmltv.partition('.us@')[0] not in US_BRANDS:
                continue
            elif country == 'CA' and ((not re.search(r'\.ca(?:@|$)', xmltv) and sid not in ('evasion/343', 'zeste/7508')) or re.search(r'penthouse|red hot|playboy|hustler|adult', name, re.I)):
                continue
            elif country == 'MX':
                if not xmltv or not sid.startswith('mx#'):
                    continue
                sid = sid.split('#', 1)[1]
            elif country == 'BR':
                if not sid.startswith('br#') or (xmltv and not ('.br@' in xmltv or xmltv.endswith('@Brazil'))) or re.search(r'sexy|playboy|hustler|adult|venus|priv[eé]|sextreme|hot$', name, re.I):
                    continue
                if sid.endswith('-hd') and sid[:-3] in source_ids:
                    continue
                sid = sid.split('#', 1)[1]

                if sid == 'globo-hd':
                    continue
            add(country, source, sid, name, xmltv)
    for c in json.loads((directory / 'skyit-channels.json').read_text()).get('channels', []):
        name = c.get('name', '').strip()
        if not name or re.match(r'^rai(?:\s|$)', name, re.I) or c.get('category', {}).get('name') in ('Adulti', 'Radio'):
            continue

        number = c.get('number', 0)
        if 251 <= number <= 259 and not re.search(r'\d', name):
            name += ' ' + str(number)
        add('IT', 'skyit', c['id'], name, 'skyit.' + str(c['id']))
    return result


def same_feed_key(channel):
    xmltv = channel.get('xmltvId', '')
    if not xmltv or xmltv.startswith('skyit.'):
        return channel['source'], channel['sourceId']
    brand, _, edition = xmltv.partition('@')
    edition = re.sub(r'(?:HD|SD)$', '', edition)
    return channel['source'], brand, edition


def feed_aliases(channel):
    xmltv = channel.get('xmltvId', '')
    if not xmltv or xmltv.startswith('skyit.'):
        return []
    brand, _, edition = xmltv.partition('@')
    brand = brand.rsplit('.', 1)[0]
    edition = re.sub(r'(?:HD|SD)$', '', edition)
    legacy = {'UGold': 'Gold', 'UDave': 'Dave', 'UAlibi': 'Alibi', 'UDrama': 'Drama', 'UYesterday': 'Yesterday'}.get(brand) if channel['country'] == 'GB' else None
    if not edition or edition in ('UK', 'Brazil', 'Mexico'):
        return [brand] + ([legacy] if legacy else [])
    if edition in ('East', 'West'):
        return [brand + ' ' + edition]
    return [brand + ' ' + edition] + ([legacy + ' ' + edition] if legacy else [])


def assemble(groups, seed, previous=(), limits=LIMITS):
    existing = {(c['country'], c['source'], c['sourceId']): c for c in list(previous) + list(seed)}
    output, stats, used_ids = [], {}, set()
    for country, limit in limits.items():
        raw = groups[country]
        pilot = [c for c in seed if c['country'] == country]
        source_lookup = {(c['source'], c['sourceId']): c for c in raw}
        rows = [dict(source_lookup.get((c['source'], c['sourceId']), {}), **c) for c in pilot]
        rest = list(raw)
        if country in ('US', 'GB'):
            brands = US_BRANDS if country == 'US' else GB_BRANDS
            per_brand = defaultdict(list)
            for c in rest:
                per_brand[c['xmltvId'].partition('.')[0]].append(c)
            order = sorted(per_brand, key=lambda b: (brands.index(b) if b in brands else 999, b))
            for variants in per_brand.values():
                variants.sort(key=lambda c: (c['feed'] not in ('East', 'UKHD', 'UK', 'HD', 'SD'), 'Plus' in c['feed'], c['sourceId']))

                seen_feeds = set()
                variants[:] = [c for c in variants if not (c['xmltvId'] in seen_feeds or seen_feeds.add(c['xmltvId']))]

            rest = [per_brand[b][i] for i in range(max(map(len, per_brand.values()), default=0)) for b in order if i < len(per_brand[b])]

            rest.sort(key=lambda c: c['xmltvId'].partition('.')[0] not in brands)
        elif country in ('ES', 'PT'):
            priority = ES_PRIORITY if country == 'ES' else PT_PRIORITY
            rest.sort(key=lambda c: (priority.index(c['sourceId']) if c['sourceId'] in priority else 999, c['name']))
        elif country == 'BR':
            rest.sort(key=lambda c: (BR_PRIORITY.index(c['sourceId']) if c['sourceId'] in BR_PRIORITY else 999, c['name']))


        seen = {}
        selected = []
        for c in rows + rest:
            key = same_feed_key(c)
            if key in seen:
                retained = seen[key]
                retained['aliases'] = list(dict.fromkeys(retained.get('aliases', []) + [c['name']] + c.get('aliases', []) + feed_aliases(c)))
                continue
            retained = dict(c, aliases=list(dict.fromkeys(c.get('aliases', []) + feed_aliases(c))))
            seen[key] = retained
            selected.append(retained)
        stats[country] = dict(eligibleReferences=len(raw), distinctFeeds=len(selected), limit=limit,
                              selected=min(len(selected), limit))
        for c in selected[:limit]:
            old = existing.get((country, c['source'], c['sourceId']))
            if old:
                c.update(id=old['id'], name=old['name'], feed=old['feed'], aliases=list(dict.fromkeys(old['aliases'] + c['aliases'])))
            else:
                identity = c.get('xmltvId') or c['name']
                c['id'] = country.lower() + '.' + slug(identity)
                if c['id'] in used_ids:
                    c['id'] += '.' + hashlib.sha256(c['sourceId'].encode()).hexdigest()[:8]
            if c['id'] in used_ids:
                raise ValueError('duplicate_canonical_id: ' + c['id'])
            used_ids.add(c['id'])
            c['identityStatus'] = 'pilot-verified' if c['id'] in {s['id'] for s in seed} else 'source-listed'
            aliases = {'FR': {'fr.histoire-tv': ['Histoire'], 'fr.bein-sports-1': ['beIN SPORT 1'], 'fr.bein-sports-2': ['beIN SPORT 2'], 'fr.bein-sports-3': ['beIN SPORT 3']}, 'CA': {}}.get(country, {}).get(c['id'], [])
            if country == 'CA' and c.get('xmltvId', '').startswith('NBATVCanada.ca@'):
                aliases += ['NBA TV']
            if country == 'PT' and c['sourceId'] == '3026':
                aliases += ['Hollywood']
            if country == 'PT' and c['sourceId'] == '2854':
                aliases += ['TV Record']
            c['aliases'] = list(dict.fromkeys(c['aliases'] + aliases))
            output.append(c)
    return output, stats


def build(directory):
    seed = json.loads((ROOT / 'catalogue-seed.json').read_text())
    previous = json.loads((ROOT / 'catalogue.json').read_text()) if (ROOT / 'catalogue.json').exists() else []
    rows, stats = assemble(candidates(directory), seed, previous)
    provenance = [dict(file=f, url=url, sha256=hashlib.sha256((directory / f).read_bytes()).hexdigest(),
                       snapshotAt=int((directory / f).stat().st_mtime)) for f, url in REFERENCES.items()]
    report = dict(generatedAt=int(time.time()), channels=len(rows), countries=stats, references=provenance,
        scope='Bounded test selection, not a complete national inventory. Country is the guide market; foreign brands may be distributed there.',
        identity='New aliases are source-listed and require playlist validation. Identical XMLTV feed IDs are deduplicated; regions and time shifts remain separate.')
    atomic_write(ROOT / 'catalogue.json', (json.dumps(rows, ensure_ascii=False, indent=2) + '\n').encode())
    atomic_write(ROOT / 'catalogue-report.json', (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode())
    print(json.dumps(dict(channels=len(rows), byCountry=dict(Counter(c['country'] for c in rows))), ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--reference-dir', type=Path, default=ROOT / '.cache' / 'catalogues')
    parser.add_argument('--refresh', action='store_true', help='Refresh the public lineup snapshots before building')
    args = parser.parse_args()
    if args.refresh:
        refresh_references(args.reference_dir)
    build(args.reference_dir)
