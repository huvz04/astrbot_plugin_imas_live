import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / 'astrbot_plugin_imas_live'))
from imas_live.service import ImasLiveService
from imas_live.render import CalendarRenderer


async def main():
    service = ImasLiveService(root / 'research/imas-live/release-smoke', {'max_special_pages': 12})
    try:
        if '--retry-known' in sys.argv:
            original = service.db.fetchable_events
            service.db.fetchable_events = lambda limit: [row for row in original(100) if row['title'] in (
                'THE IDOLM@STER MILLION LIVE! 14thLIVE', '学園アイドルマスター LIVE TOUR -標- 岩手公演')]
            print('SYNC', await service.sync(False), flush=True)
        elif '--cached' not in sys.argv:
            print('SYNC', await service.sync(), flush=True)
        entries, start, end, status = await service.calendar_entries()
        print('CALENDAR', len(entries), status, flush=True)
        for row in entries:
            print(row['display_date'], row['kind'], row['title'], row['subtitle'], flush=True)
        with service.db._connect() as db:
            print('EVENTS', db.execute('SELECT COUNT(*) FROM events').fetchone()[0])
            for row in db.execute("SELECT e.title,s.error FROM sources s JOIN events e ON e.id=s.event_id WHERE s.quality='stale'"):
                print('UNVERIFIED', tuple(row), flush=True)
        preview = root / 'astrbot_plugin_imas_live/previews'
        renderer = CalendarRenderer(service.data_dir / 'rendered')
        now = datetime.now(start.tzinfo)
        for index, path in enumerate(renderer.render_calendar(entries, start.date(), end.date(), now, status)):
            dest = preview / ('normal-calendar.png' if index == 0 else f'normal-calendar-{index+1}.png')
            shutil.copyfile(path, dest)
            print('PREVIEW', dest, flush=True)
        samples = [{**row, 'remaining_minutes': 60} for row in entries if row['kind'] == 'deadline'][:2]
        if samples:
            simulated = datetime.fromisoformat(samples[0]['display_date'] + 'T21:59:00').replace(tzinfo=now.tzinfo)
            shutil.copyfile(renderer.render_reminder(samples[:1], simulated), preview / 'deadline-reminder.png')
        shutil.copyfile(renderer.render_calendar([], start.date(), end.date(), now, '尚未同步')[0], preview / 'empty-calendar.png')
        long_rows = [{'kind': 'deadline', 'display_date': (start + timedelta(days=i)).date().isoformat(),
                     'title': '长标题示例：THE IDOLM@STER 合同公演与跨月抽选截止日期完整展示测试',
                     'subtitle': '一般会員2次先行｜截止：2026/10/01 22:59 北京时间 / 10/01 23:59 JST',
                     'brands': ['CINDERELLAGIRLS', 'MILLIONLIVE'], 'url': 'https://example.test'} for i in (20, 21, 22, 23, 24, 25, 26)]
        for index, path in enumerate(renderer.render_calendar(long_rows, start.date(), end.date(), now, '排版示例・虚构活动')):
            shutil.copyfile(path, preview / ('long-cross-month.png' if index == 0 else f'long-cross-month-{index+1}.png'))
    finally:
        await service.close()

asyncio.run(main())
