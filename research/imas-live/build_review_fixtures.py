from pathlib import Path
import json
from bs4 import BeautifulSoup

root = Path(__file__).parent
target = root.parents[1] / 'astrbot_plugin_imas_live' / 'tests' / 'fixtures'
target.mkdir(exist_ok=True)
for name in ('gakuen_final_ticket.html', 'sidem_ticket.html', 'shiny_single_page.html'):
    soup = BeautifulSoup((root / 'sources' / name).read_text(encoding='utf-8'), 'html.parser')
    blocks = []
    for node in soup.select('section.p-ticket__group, dl.ticketList, dl.c-dl'):
        parent = node.find_parent('dl', class_='accordionList') or node.find_parent('details')
        if parent:
            blocks.append(str(parent))
        elif node.name == 'section':
            blocks.append(str(node))
        else:
            heading = node.find_previous(['h2', 'h3', 'h4'])
            blocks.append(str(heading or '') + str(node))
    if name == 'shiny_single_page.html':
        for dt in soup.find_all('dt'):
            if dt.get_text(' ', strip=True).startswith('日程'):
                blocks.insert(0, '<dl>' + str(dt) + str(dt.find_next_sibling('dd')) + '</dl>')
        for h in soup.find_all(['h2', 'h3', 'h4']):
            image = h.find('img', alt=True)
            label = h.get_text(' ') + (image['alt'] if image else '')
            block = h.find_next_sibling('div')
            if 'DAY' in label.upper() or (block and 'p-information__cast' in (block.get('class') or [])):
                blocks.append(str(h))
                if block and 'p-information__cast' in (block.get('class') or []):
                    blocks.append(str(block))
    trimmed = BeautifulSoup('\n'.join(dict.fromkeys(blocks)), 'html.parser')
    for node in trimmed.select('script,style,link,iframe'):
        node.decompose()
    for node in trimmed.find_all('img'):
        if not node.find_parent(['h2', 'h3', 'h4']):
            node.decompose()
        else:
            node.attrs = {'alt': node.get('alt', '')}
    (target / name).write_text(str(trimmed), encoding='utf-8')
source = json.loads((root / 'sources' / 'live.json').read_text(encoding='utf-8'))
item = source['data']['article_list'][0]
keys = ('_id', 'title', 'brand', 'article_type', 'event_url', 'event_dspdate', 'event_place', 'updated')
(target / 'live.json').write_text(json.dumps({'data': {'article_list': [{k: item.get(k) for k in keys}]}}, ensure_ascii=False, indent=2), encoding='utf-8')
