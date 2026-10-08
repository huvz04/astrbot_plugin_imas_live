"""Read-only live audit in a temporary DB; no subscriptions or messages.

Run: python -X utf8 scripts/audit_official_sources.py
Generated evidence and one PNG go to ignored previews/_generated/official-audit.
"""

import asyncio
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from imas_live.cms import CmsArticle
from imas_live.render import CalendarRenderer
from imas_live.service import ImasLiveService


async def run():
    output = Path(__file__).resolve().parents[1] / 'previews' / '_generated' / 'official-audit'
    output.mkdir(parents=True, exist_ok=True)
    current = datetime.now(ZoneInfo('Asia/Shanghai'))
    with tempfile.TemporaryDirectory() as directory:
        service = ImasLiveService(Path(directory), {'max_special_pages': 12})
        try:
            result = await service.sync()
            print('Directory/discovery:', json.dumps(result, ensure_ascii=False), flush=True)
            # Cover the user's February report and the changed October layouts
            # explicitly even when a bounded first sync has more candidates.
            slugs = {'cg_apai', 'deremilli_clashmatch', '283_noctchill', 'shinycolors_orchestra_RC',
                     'sidem_shinycolors_ss', 'cg_15th_orchestra', 'sidem_pm2027', 'xr_revival2026', 'IUOAFA'}
            verified = []
            for row in service.db.list_events(limit=10000):
                if not row['official_url'] or row['official_url'].strip('/').rsplit('/', 1)[-1] not in slugs:
                    continue
                article = CmsArticle(row['id'], row['title'], row['official_url'], json.loads(row['brands_json']),
                                     row['event_display'], row['venue'], row['source_updated'], {})
                try:
                    await service._refresh_article(article)
                    detail = service.db.detail(row['id'])
                    item = {'title': row['title'], 'url': row['official_url'], 'performances': detail['performances'],
                            'tickets': detail['tickets']}
                    verified.append(item)
                    print(row['official_url'], 'performances', len(detail['performances']), 'tickets', len(detail['tickets']), flush=True)
                except Exception as exc:
                    service.db.source_error(row['id'], row['official_url'], str(exc))
                    verified.append({'title': row['title'], 'url': row['official_url'], 'error': str(exc)})
                    print(row['official_url'], 'FAILED', str(exc), flush=True)
            entries, start, end, status = await service.ticket_entries(current)
            renderer = CalendarRenderer(output)
            source_times = [datetime.fromisoformat(row['source_fetched_at']) for row in entries if row.get('source_fetched_at')]
            image = renderer.render_ticket(entries, start.date(), end.date(), current, status, min(source_times, default=None))
            counts = {}
            for item in entries:
                counts[item['ticket_status']] = counts.get(item['ticket_status'], 0) + 1
            report = {'checked_at_beijing': current.isoformat(), 'sync': result, 'verified_sources': verified,
                      'listed_rounds': len(entries), 'listed_events': len({r['event_id'] for r in entries}),
                      'states': counts, 'entries': entries, 'image': str(image)}
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
            print('RESULT', len(entries), 'rounds', report['listed_events'], 'events', counts, flush=True)
            print('REPORT', output / 'report.json', '\nIMAGE', image, flush=True)
        finally:
            await service.close()


if __name__ == '__main__':
    asyncio.run(run())
