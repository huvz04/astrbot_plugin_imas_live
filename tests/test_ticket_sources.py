"""Official 2026-10 page shapes and complete lottery-list behavior."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from imas_live.cms import CmsArticle, OfficialCmsClient, SourceUnavailable
from imas_live.models import Evidence, Performance, TicketRound
from imas_live.parsing import parse_information, parse_ticket_page, parse_venue
from imas_live.service import ImasLiveService

JST = ZoneInfo('Asia/Tokyo')
NOW = datetime(2026, 10, 8, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
ROOT = 'https://idolmaster-official.jp/live_event/example/'


class NewOfficialShapes(unittest.TestCase):
    def test_missing_start_clock_is_not_replaced_with_the_deadline(self):
        html = '<section class="p-ticket__group"><h2>先行抽選</h2><dl><dt>受付期間</dt><dd>2026年10月1日～10月11日23:59</dd></dl></section>'
        row = parse_ticket_page(html, ROOT).ticket_rounds[0]
        self.assertIsNone(row.application_start)
        self.assertEqual(row.application_end, '2026-10-11T23:59+09:00')

    def test_wrapped_orchestra_heading_and_unclosed_venue_do_not_swallow_other_fields(self):
        html = ('<div class="ribbonWrapper"><h2>開催日時</h2></div>'
                '<p>2027 年 1 月 11 日(月・祝) 開場17:00 / 開演18:00<br>2027 年 1 月 12 日(火) 開演19:00</p>'
                '<div class="ribbonWrapper"><h2>開催場所</h2></div><p>パシフィコ横浜</p>')
        self.assertEqual([p.date for p in parse_information(html, ROOT)], ['2027-01-11', '2027-01-12'])
        self.assertEqual(parse_venue(html), 'パシフィコ横浜')
        broken = '<dl><dt>会場</dt><dd>幕張イベントホール<br><a href="https://venue.test">https://venue.test</a><dt>ARTIST</dt><dd>Jupiter</dd></dl>'
        self.assertEqual(parse_venue(broken), '幕張イベントホール')

    def test_noctchill_plain_dl_and_apai_new_accordion(self):
        # Reduced semantic fragments from official pages checked 2026-10-08.
        for wrapper in (
            '<dl class="accordionList js-anime"><dt>アソビストアプレミアム会員先行</dt><dd class="show frameCol">{}</dd></dl>',
            '<div class="p-ticket__accordion c-round-accordion__item"><button>アソビストアプレミアム会員先行</button>{}</div>',
        ):
            html = wrapper.format('<dl class="p-ticket__detailList c-detail-list"><dt>受付期間</dt>'
                '<dd>2026年9月28日(月)21:30 ～ 10月18日(日)23:59</dd><dt>受付URL</dt>'
                '<dd><a href="https://asobiticket2.asobistore.jp/receptions/apai">申込</a></dd>'
                '<dt>当落発表</dt><dd>2026年10月24日13:00</dd></dl>')
            with self.subTest(wrapper=wrapper):
                rows = parse_ticket_page(html, ROOT).ticket_rounds
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0].application_start, rows[0].application_end),
                                 ('2026-09-28T21:30+09:00', '2026-10-18T23:59+09:00'))
                self.assertEqual(rows[0].sale_method, 'lottery')
                self.assertEqual(rows[0].name, 'アソビストアプレミアム会員先行')

    def test_radiant_fields_in_separate_dl_are_one_reception(self):
        html = ('<dl class="accordionList"><dt>アソビストアプレミアム会員先行</dt><dd class="open">'
                '<div class="ticketList"><dl><dt>受付期間</dt><dd>2026年9月26日20:00～10月18日23:59</dd></dl>'
                '<dl><dt>受付URL</dt><dd><a href="https://asobiticket2.asobistore.jp/receptions/radiant">申込</a></dd></dl>'
                '<dl><dt>入金期間</dt><dd>2026年10月24日13:00～10月28日23:59</dd></dl></div></dd></dl>')
        rows = parse_ticket_page(html, ROOT).ticket_rounds
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].application_end, '2026-10-18T23:59+09:00')
        self.assertEqual(rows[0].payment_end, '2026-10-28T23:59+09:00')

    def test_sparkle_div_rows_preserve_both_seat_receptions(self):
        html = ('<dl class="c-accordion"><dt>アソビストアプレミアム会員先行</dt><dd><dl class="c-table-list-a">'
                '<div><dt><p>受付期間</p></dt><dd>2026年9月12日21:00～10月12日23:59</dd></div>'
                '<div><dt>受付URL</dt><dd><a href="https://asobiticket2.asobistore.jp/receptions/vip">VIP席</a>'
                '<a href="https://asobiticket2.asobistore.jp/receptions/normal">通常席</a></dd></div></dl></dd></dl>')
        rows = parse_ticket_page(html, ROOT).ticket_rounds
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.url.rsplit('/', 1)[-1] for r in rows}, {'vip', 'normal'})
        self.assertTrue(all(r.application_end == '2026-10-12T23:59+09:00' for r in rows))

    def test_xr_cms_components_do_not_use_payment_or_historical_dates(self):
        def field(label, value):
            return '<div data-type="component-livetext_v2">' + label + '</div><div data-type="component-text">' + value + '</div>'
        html = field('開催日時', '2026年12月12日(土) 開演12:00<br>2026年12月13日(日) 開演16:00<br>※2024年1月1日～2月1日の公演')
        html += '<div data-type="component-livetext_v2"><h5>ASOBI STORE 一般会員先行（抽選）</h5></div>'
        html += field('受付期間', '2026年10月3日(土)19:00 ～ 11月1日(日)23:59')
        html += field('受付URL', '<a href="https://asobiticket2.asobistore.jp/receptions/xr">申込</a>')
        html += field('入金期間', '2026年11月14日13:00～11月18日23:59')
        rows = parse_ticket_page(html, ROOT).ticket_rounds
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].application_end, '2026-11-01T23:59+09:00')
        self.assertEqual(rows[0].payment_end, '2026-11-18T23:59+09:00')
        self.assertEqual([p.date for p in parse_information(html, ROOT)], ['2026-12-12', '2026-12-13'])


class TicketSourceWorkflow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ImasLiveService(Path(self.temp.name))

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def seed(self, event_id, tickets, day='2027-02-13'):
        source = ROOT.replace('example', event_id)
        self.service.db.upsert_event({'id': event_id, 'title': event_id + ' LIVE', 'brands': [], 'url': source})
        self.service.db.save_parsed(event_id, source, event_id, 'test', tickets,
            [Performance('p', day, '开演 19:00 JST', 'Hall', evidence=Evidence(source, 'official', 'test'))], [], [])
        with self.service.db._connect() as db:
            db.execute('UPDATE sources SET fetched_at=? WHERE event_id=?', (NOW.astimezone(timezone.utc).isoformat(), event_id))

    def ticket(self, key, start, end, scope='onsite', method='lottery'):
        return TicketRound(key, key, scope, method, start.isoformat(), end.isoformat(), evidence=Evidence(ROOT, 'official', 'test'))

    async def test_all_rounds_grouped_far_future_live_and_no_false_deadline_alerts(self):
        rows = [self.ticket('open', NOW-timedelta(days=1), NOW+timedelta(days=5)),
                self.ticket('future', NOW+timedelta(days=2), NOW+timedelta(days=3)),
                self.ticket('ended', NOW-timedelta(days=2), NOW-timedelta(hours=1)),
                self.ticket('stream', NOW-timedelta(days=1), NOW+timedelta(days=1), scope='streaming'),
                self.ticket('first-come', NOW-timedelta(days=1), NOW+timedelta(days=1), method='first_come')]
        self.seed('feb-live', rows)
        self.seed('another', [self.ticket('urgent', NOW-timedelta(days=1), NOW+timedelta(hours=1))])
        entries, *_ = await self.service.ticket_entries(NOW)
        self.assertEqual([e['ticket_status'] for e in entries], ['urgent', 'open', 'upcoming', 'ended'])
        self.assertEqual([e['event_id'] for e in entries], ['another', 'feb-live', 'feb-live', 'feb-live'])
        self.assertIn('2027/02/13', entries[1]['subtitle'])
        self.service.set_subscription('ticket', 'test:GroupMessage:42', True)
        with self.service.db._connect() as db:
            db.execute("UPDATE ticket_group_subscriptions SET created_at='2000-01-01T00:00:00+00:00',updated_at='2000-01-01T00:00:00+00:00'")
        claimed = await self.service.claim_due_reminders(NOW)
        self.assertEqual(len(claimed), 1)
        self.assertIn('urgent', claimed[0]['subtitle'])
        self.service.db.source_error('another', ROOT.replace('example', 'another'), 'offline')
        await self.service.finish_reminders(claimed, False)
        self.assertEqual(await self.service.claim_due_reminders(NOW), [])
        stale, *_ = await self.service.ticket_entries(NOW)
        self.assertIn('待核验', next(e for e in stale if e['event_id'] == 'another')['status_label'])

    async def test_news_discovers_missing_event_prioritizes_and_retains_public_number(self):
        news = {'_id': 99, 'path': '01_19969', 'title': 'noctchill LIVE チケット先行', 'updated': 123, 'brand': []}
        self.service.client.recent_news = AsyncMock(return_value=[news])
        self.service.client.article_content = AsyncMock(return_value='<a href="/live_event/283_noctchill/">イベントサイト</a>')
        await self.service._discover_news_sources()
        found = self.service.db.fetchable_events(1)[0]
        self.assertEqual(found['official_url'], 'https://idolmaster-official.jp/live_event/283_noctchill/')
        number = self.service.db.detail(found['id'])['event']['public_number']
        self.assertTrue(found['hint_pending'])
        await self.service._discover_news_sources()
        self.service.client.article_content.assert_awaited_once()
        self.service.client.live_articles = AsyncMock(return_value=[CmsArticle('999', 'noctchill LIVE', found['official_url'], [], '2027年3月27日', None, None, {})])
        self.service._refresh_article = AsyncMock(return_value=False)
        await self.service.sync()
        self.assertIsNone(self.service.db.detail('999'))
        self.assertEqual(self.service.db.detail(found['id'])['event']['public_number'], number)

    async def test_failed_initial_news_is_retried_without_historical_discovery_alert(self):
        item = {'_id': 1, 'path': '01_old', 'title': 'old LIVE チケット', 'updated': 123}
        self.service.client.recent_news = AsyncMock(return_value=[item])
        self.service.client.article_content = AsyncMock(side_effect=SourceUnavailable('offline'))
        await self.service._discover_news_sources()
        self.assertIsNone(self.service.db.meta('news_seen:1'))
        self.service.client.article_content = AsyncMock(return_value='<a href="/live_event/old/">公式</a>')
        await self.service._discover_news_sources()
        with self.service.db._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ticket_new_event_discoveries').fetchone()[0], 0)
        self.assertEqual(self.service.db.meta('news_seen:1'), '123')

    async def test_dynamic_route_uses_confirmed_article_get_body(self):
        client = self.service.client
        client.token = AsyncMock(return_value='anonymous')
        client._get = AsyncMock(return_value={'data': {'article': {'content': '<h5>公演日時</h5>'}}})
        html = await client.event_html('https://idolmaster-official.jp/live_events/xr_revival2026')
        self.assertIn('公演日時', html)
        path, params = client._get.call_args.args
        self.assertEqual(path, 'idolmaster/Article/get')
        self.assertEqual(json.loads(params['data']), {'page': 'xr_revival2026', 'article_type': 'detail_page'})

    async def test_queue_prioritizes_active_and_dynamic_pages_over_old_undated_failures(self):
        for index in range(20):
            self.service.db.upsert_event({'id': 'old'+str(index), 'title': 'old LIVE', 'brands': [], 'url': ROOT.replace('example', 'old'+str(index))})
        self.seed('feb', [self.ticket('open', NOW-timedelta(days=1), NOW+timedelta(days=3))])
        self.service.db.upsert_event({'id': 'xr', 'title': 'xR LIVE', 'brands': [], 'url': 'https://idolmaster-official.jp/live_events/xr_revival2026', 'updated': str(int(NOW.timestamp()))})
        ids = [r['id'] for r in self.service.db.fetchable_events(3)]
        self.assertIn('feb', ids)
        self.assertIn('xr', ids)
        self.assertEqual(sum(i.startswith('old') for i in ids), 1)

    async def test_directory_failure_still_refreshes_cached_sources_without_false_success(self):
        self.seed('cached', [])
        self.service.client.live_articles = AsyncMock(side_effect=SourceUnavailable('offline'))
        self.service._refresh_article = AsyncMock(return_value=False)
        result = await self.service.sync()
        self.assertEqual(result['status'], 'failed')
        self.service._refresh_article.assert_awaited_once()
        self.assertIsNone(self.service.db.meta('last_directory_sync'))
        self.service._refresh_article = AsyncMock(side_effect=SourceUnavailable('offline'))
        self.service.db.set_meta('last_successful_sync', 'previous')
        await self.service.sync(False)
        self.assertEqual(self.service.db.meta('last_successful_sync'), 'previous')

    async def test_unchanged_hash_reverifies_source_and_diagnostics_use_utc(self):
        self.seed('cached', [])
        source = ROOT.replace('example', 'cached')
        self.service.db.source_error('cached', source, 'offline')
        self.assertEqual(self.service.db.sync_diagnostics(current=NOW)['pending_verification'], 1)
        self.assertFalse(self.service.db.save_parsed('cached', source, 'cached', 'test', [], [], [], []))
        self.assertEqual(self.service.db.sync_diagnostics(current=NOW)['pending_verification'], 0)

    async def test_recheck_budget_is_per_event_and_refreshes_open_far_from_deadline(self):
        tickets = [self.ticket(str(i), NOW-timedelta(days=1), NOW+timedelta(days=30)) for i in range(6)]
        self.seed('many', tickets)
        self.seed('other', tickets[:1])
        with self.service.db._connect() as db:
            db.execute("UPDATE sources SET fetched_at='2026-10-01T00:00:00+00:00',attempted_at='2026-10-01T00:00:00+00:00'")
        self.service._refresh_article = AsyncMock(return_value=False)
        self.assertEqual(await self.service.refresh_due_ticket_sources(NOW), {'refreshed': 2, 'failed': 0})
        self.service._refresh_article.reset_mock()
        self.assertEqual(await self.service.refresh_open_ticket_sources(NOW), {'refreshed': 2, 'failed': 0})
