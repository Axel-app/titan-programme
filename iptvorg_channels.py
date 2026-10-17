#!/usr/bin/env python3
"""Channel list for the iptv-org/epg grabbers: only what the public guides do not illustrate yet.

The sites below return a picture per programme, need no request per programme and are not
geo-blocked (audit of iptv-org/epg 2026-10-16). A channel already present in the schema 2
directories (same guide id, or same name in the same edition) is left out, so the daily grab only
asks the broadcasters' sites for what is still missing.
"""

import argparse
from pathlib import Path
import json
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from build_v2 import channel_name_key, edition, id_key

IPTVORG_SITES = (
    '9tv.co.il', 'abc.net.au', 'airtelxstream.in', 'allente.dk', 'allente.fi', 'allente.no', 'allente.se',
    'app.tvufop.com.br', 'artonline.tv', 'cableplus.com.uy', 'clarotvmais.com.br', 'cosmotetv.gr', 'ctc.ru', 'dazn.com',
    'derana.lk', 'dishtv.in', 'distro.tv', 'dna.fi', 'dtv8.net', 'elcinema.com', 'energeek.cl', 'entertainment.ie',
    'epg.telemach.ba', 'epg.tmacaraibes.com', 'freetv.tv', 'galamtv.kz', 'gatotv.com', 'go3.ee', 'guidatv.sky.it',
    'i24news.tv', 'iltalehti.fi', 'ionplustv.com', 'jiotv.com', 'm.tving.com', 'mako.co.il', 'maxstream.tv',
    'maxtvgo.mk', 'mediaklikk.hu', 'melita.com', 'meo.pt', 'mewatch.sg', 'mi.tv', 'mon-programme-tv.be', 'mtel.ba',
    'mts.rs', 'musor.tv', 'nostv.pt', 'nzxmltv.com', 'pickx.be', 'plex.tv', 'pluto.tv', 'programacion.tcc.com.uy',
    'programetv.ro', 'programme-tv.net', 'raiplay.it', 'rikstv.no', 'roya-tv.com', 'rthk.hk', 'rtp.pt', 'rts.ch',
    'ruv.is', 's.mxtv.jp', 'sat.tv', 'shahid.mbc.net', 'skyperfectv.co.jp', 'sporttv.pt', 'taiwanplus.com',
    'tapdmv.com', 'tataplay.com', 'teliatv.ee', 'telsu.fi', 'tv.blue.ch', 'tv.lv', 'tv.movistar.com.pe', 'tv.post.lu',
    'tv.sfr.fr', 'tv.trueid.net', 'tv2go.t-2.net', 'tvgids.nl', 'tvguide.myjcom.jp', 'tvheute.at', 'tvmi.mt',
    'tvmusor.hu', 'tvpassport.com', 'tvplus.com.tr', 'vidio.com', 'virginmediatelevision.ie', 'vivoplay.com.br',
    'vrt.be', 'vtm.be', 'watch.sportsnet.ca', 'watchyour.tv', 'web.magentatv.de', 'winplay.co',
    'worldfishingnetwork.com', 'www.magenta.tv', 'www.tv-tokyo.co.jp', 'xem.kplus.vn', 'yes.co.il', 'zap2it.com',
    'zee5.com',
)


def covered(v2_folder):
    """Guide ids and (name, edition) pairs the schema 2 directories already describe."""
    ids, names = set(), set()
    for path in Path(v2_folder).glob('*/channels.json'):
        directory = json.loads(path.read_bytes())
        for channel in directory['channels']:
            ids.update(channel['ids'])
            names.update((channel_name_key(n), directory['edition']) for n in channel['names'])
    return ids, names


def missing_channels(epg_sites, v2_folder):
    """<channel> elements of the allowed sites for services the public guides miss, one per guide id."""
    ids, names = covered(v2_folder)
    picked = {}
    for site in IPTVORG_SITES:
        for path in sorted(Path(epg_sites, site).glob('*.channels.xml')):
            for channel in ET.parse(path).getroot().findall('channel'):
                key = id_key(channel.get('xmltv_id'))
                if not key or key in picked or key in ids:
                    continue
                ed = edition(key.rsplit('.', 1)[-1])
                if ed and (channel_name_key(channel.text or ''), ed) not in names:
                    picked[key] = channel
    return list(picked.values())


def write_channels(channels, output):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<channels>']
    for c in channels:
        attributes = ' '.join(f'{name}={quoteattr(c.get(name))}' for name in ('site', 'site_id', 'lang', 'xmltv_id') if c.get(name))
        lines.append(f'  <channel {attributes}>{escape(c.text or "")}</channel>')
    lines.append('</channels>')
    Path(output).write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--epg', type=Path, required=True, help='iptv-org/epg checkout')
    parser.add_argument('--v2', type=Path, default=Path('output/v2'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    channels = missing_channels(args.epg / 'sites', args.v2)
    write_channels(channels, args.output)
    print(f'{len(channels)} channels to grab from {len({c.get("site") for c in channels})} sites')
