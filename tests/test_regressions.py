import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from imas_live.cms import CmsArticle, SourceUnavailable
from imas_live.parsing import parse_ticket_page, schedule_performances, japan_datetime, parse_information
from imas_live.service import ImasLiveService
from test_calendar import seed


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_follows_information_and_keeps_both_date_paragraphs(self):
        root = 'https://idolmaster-official.jp/live_event/test/'
        pages = {root: '<a href="information/">概要</a>', root + 'information/':
                 '<h3>公演日時</h3><p>2026年9月12日(土) 17:30開演</p>'
                 '<p>2026年9月13日(日) 17:30開演</p><h3>別イベント</h3><p>2026年9月14日</p>'}
        with tempfile.TemporaryDirectory() as directory:
            service = ImasLiveService(Path(directory))
            service.client.fetch_html = AsyncMock(side_effect=lambda url: pages[url])
            parsed = await service._collect_special(CmsArticle('1', 'TEST LIVE', root, ['SIDEM'], None, None, None, {}))
            self.assertEqual([p.date for p in parsed.performances], ['2026-09-12', '2026-09-13'])
            self.assertEqual(service.client.fetch_html.await_count, 2)
            await service.close()

    async def test_failed_page_rotates_without_refreshing_verified_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImasLiveService(Path(directory))
            for i in range(2):
                service.db.upsert_event({'id': str(i), 'title': 'TEST LIVE', 'url': f'https://idolmaster-official.jp/live_event/{i}/', 'brands': []})
            first = service.db.fetchable_events(1)[0]
            service.db.source_error(first['id'], first['official_url'], 'offline')
            self.assertNotEqual(service.db.fetchable_events(1)[0]['id'], first['id'])
            with service.db._connect() as db:
                db.execute("UPDATE sources SET fetched_at='2020-01-01T00:00:00+00:00'")
            service.db.source_error(first['id'], first['official_url'], 'offline again')
            with service.db._connect() as db:
                self.assertEqual(db.execute('SELECT fetched_at FROM sources').fetchone()[0], '2020-01-01T00:00:00+00:00')
            await service.close()

    async def test_directory_saves_every_event_before_bounded_special_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImasLiveService(Path(directory), {'max_special_pages': 1})
            articles = [CmsArticle(str(i), 'TEST LIVE', f'https://idolmaster-official.jp/live_event/test{i}/', ['GAKUEN'], '2026年9月12日(土)・13日(日)', 'Hall', None, {}) for i in range(15)]
            service.client.live_articles = AsyncMock(return_value=articles)
            service.client.fetch_html = AsyncMock(return_value='<h3>公演日時</h3><p>2026年9月12日(土) 17:00開演</p>')
            result = await service.sync()
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(len(service.db.list_events(limit=100)), 15)
            self.assertEqual(service.client.fetch_html.await_count, 1)
            self.assertTrue(service.directory_ready.is_set())
            self.assertTrue(service.db.meta('last_directory_sync'))
            await service.close()

    async def test_first_directory_failure_never_marks_success(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImasLiveService(Path(directory))
            service.client.live_articles = AsyncMock(side_effect=SourceUnavailable('offline'))
            result = await service.sync()
            self.assertEqual(result['status'], 'failed')
            self.assertIsNone(service.db.meta('last_directory_sync'))
            self.assertIn('尚未同步', (await service.calendar_entries())[3])
            await service.close()

    async def test_failed_source_cannot_borrow_freshness_from_other_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImasLiveService(Path(directory), {'white_umos': ['aiocqhttp:GroupMessage:42'], 'freshness_hours': 24})
            seed(service)
            service.db.source_error('cms-calendar', 'https://example.test/ticket', 'offline')
            current = datetime.fromisoformat('2026-09-07T22:59:00+08:00')
            self.assertEqual(await service.claim_due_reminders(current), [])
            await service.close()

    async def test_stuck_inflight_can_recover_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImasLiveService(Path(directory))
            self.assertTrue(service.db.claim_delivery('k', 'umo', 'payload'))
            await service.close()
            service = ImasLiveService(Path(directory))
            self.assertTrue(service.db.claim_delivery('k', 'umo', 'payload'))
            service.db.finish_delivery('k', True)
            self.assertFalse(service.db.claim_delivery('k', 'umo', 'payload'))
            await service.close()

    def test_date_list_is_not_a_continuous_range(self):
        rows = schedule_performances('2026年9月12日(土)・13日(日)', 'https://example.test')
        self.assertEqual([row.date for row in rows], ['2026-09-12', '2026-09-13'])
        self.assertEqual(schedule_performances('2026年9月12日(土)～13日(日)', 'https://example.test'), [])
        self.assertEqual(japan_datetime('2026年12月31日 24:00'), '2027-01-01T00:00+09:00')

    def test_schedule_includes_day_subheadings_and_updated_date_label(self):
        html = '<h2>開催日時</h2><h3>DAY 1</h3><p>2026年9月19日 開演17:30</p><h3>DAY 2</h3><p>2026年9月20日 開演16:30</p><h2>その他</h2><p>2026年9月21日</p>'
        self.assertEqual([p.date for p in parse_information(html, 'https://example.test')], ['2026-09-19', '2026-09-20'])
        html = '<dl><dt>開催日 2026.03.27 Update</dt><dd>2026年9月22日(火・祝) 開演18:30<br>2026年9月23日(水・祝) 開演16:30</dd></dl>'
        self.assertEqual([p.date for p in parse_information(html, 'https://example.test')], ['2026-09-22', '2026-09-23'])

    def test_ticketcol_seat_subheading_does_not_hide_sale_method(self):
        html = '<h2>一般販売(先着) 2026.8.21 UPDATE!</h2><h3>受付対象席種</h3><div class="ticketCol"><dl><dt>受付期間</dt><dd>2026年8月30日12:00～9月12日23:59</dd></dl></div>'
        row = parse_ticket_page(html, 'https://example.test').ticket_rounds[0]
        self.assertEqual(row.name, '一般販売(先着)')
        self.assertEqual(row.sale_method, 'first_come')

    def test_reception_identity_survives_deadline_and_update_badge_changes(self):
        html = '<section class="p-ticket__group"><h2>会員先行 2026.09.01 Update</h2><dl><dt>受付期間</dt><dd>2026年9月1日 12:00～9月10日 23:59</dd><dt>受付URL</dt><dd><a href="https://asobiticket2.asobistore.jp/receptions/test">申込</a></dd></dl></section>'
        old = parse_ticket_page(html, 'https://example.test').ticket_rounds[0]
        new = parse_ticket_page(html.replace('09.01 Update', '09.02 Update').replace('9月10日', '9月11日'), 'https://example.test').ticket_rounds[0]
        self.assertEqual(old.stable_key, new.stable_key)
        self.assertNotEqual(old.application_end, new.application_end)
