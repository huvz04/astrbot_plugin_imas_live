"""Ticket rounds outlive applications, not the final LIVE session."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from imas_live.cms import CmsArticle
from imas_live.database import Database
from imas_live.models import Evidence, Performance, TicketRound
from imas_live.parsing import parse_ticket_news, stable
from imas_live.service import ImasLiveService

NOW = datetime(2026, 10, 9, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
ROOT = 'https://idolmaster-official.jp/live_event/deremilli_clashmatch/'
NEWS = 'https://idolmaster-official.jp/news/01_20072.html'
RECEPTION = 'https://asobiticket2.asobistore.jp/receptions/eea32b55-afb5-4ff8-be1b-f9ed1bf22008'
NEWS_HTML = ('<h4>現地チケット情報</h4><h5>アソビストア一般会員先行</h5><div class="c-txt">'
             '<strong>✦ 受付URL</strong><br><a href="'+RECEPTION+'">申込</a><br>'
             '<strong>✦ 受付期間</strong><br>2026年10月7日(水)12:00～2026年11月1日(日)23:59<br>'
             '<strong>✦ 入金期間</strong><br>2026年11月14日13:00～2026年11月18日23:59</div>'
             '<a href="'+ROOT+'">イベントサイトはこちら</a>')


class TicketLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ImasLiveService(Path(self.temp.name))

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def round(self, key, start=NOW-timedelta(days=3), end=NOW-timedelta(days=1), method='lottery', bindings=()):
        return TicketRound(key, key, 'onsite', method, start.isoformat(), end.isoformat(),
                           evidence=Evidence(ROOT+'ticket/', 'official', 'test'), performance_keys=list(bindings))

    def event(self, event_id, performances, rounds):
        source = ROOT if event_id == 'clash' else ROOT.replace('deremilli_clashmatch', event_id)
        self.service.db.upsert_event({'id': event_id, 'title': event_id, 'url': source, 'brands': []})
        self.service.db.save_parsed(event_id, source, 'initial', 'test', rounds, performances, [], [])
        return source

    def performance(self, key, day, clock='开演 17:00 JST'):
        return Performance(key, day, clock, 'IGアリーナ', evidence=Evidence(ROOT, 'official', 'test'))

    async def test_sort_by_live_start_not_application_deadline_or_status(self):
        self.event('earlier', [self.performance('p', '2026-12-01')], [self.round('premium-ended')])
        self.event('later', [self.performance('p', '2027-02-27')], [self.round('open', end=NOW+timedelta(hours=1))])
        entries, *_ = await self.service.ticket_entries(NOW)
        self.assertEqual([e['event_id'] for e in entries], ['earlier', 'later'])
        self.assertEqual([e['ticket_status'] for e in entries], ['ended', 'urgent'])

    async def test_multiday_day1_passed_day2_kept_and_explicit_binding_filtered(self):
        self.event('tour', [self.performance('d1', '2026-10-08'), self.performance('d2', '2026-10-09')],
                   [self.round('all-days'), self.round('day1-only', bindings=['d1']), self.round('day2', bindings=['d2'])])
        entries, *_ = await self.service.ticket_entries(NOW)
        self.assertEqual({e['subtitle'].splitlines()[0] for e in entries}, {'轮次：all-days', '轮次：day2'})
        self.assertEqual((await self.service.ticket_entries(NOW.replace(hour=16)))[0], [])
        # The historical natural-month LIVE calendar retains its old behavior.
        live, *_ = await self.service.calendar_entries(NOW, 10)
        self.assertEqual({r['display_date'] for r in live}, {'2026-10-08', '2026-10-09'})

    async def test_date_only_retains_whole_japanese_date_and_unknown_stays_visible(self):
        self.event('date-only', [self.performance('p', '2026-10-09', '演出日（场次待细分）')], [self.round('old')])
        self.assertEqual(len((await self.service.ticket_entries(NOW.replace(hour=22, minute=59)))[0]), 1)
        self.assertEqual((await self.service.ticket_entries(NOW.replace(hour=23, minute=0)))[0], [])
        self.event('unknown', [self.performance('old', '2020-01-01'), Performance('unknown', None, None, None)], [self.round('unknown-round')])
        self.assertEqual([e['event_id'] for e in (await self.service.ticket_entries(NOW.replace(hour=23)))[0]], ['unknown'])

    async def test_new_general_never_replaces_premium_and_missing_old_round_is_not_fresh(self):
        self.event('clash', [self.performance('d1', '2027-02-27'), self.performance('d2', '2027-02-28')], [self.round('premium')])
        general = self.round('general', end=NOW+timedelta(days=20))
        resale = self.round('resale', end=NOW+timedelta(hours=1), method='resale')
        self.service.db.save_parsed('clash', ROOT, 'next', 'test', [general, resale], [], [], [])
        self.assertEqual(len(self.service.db.detail('clash')['tickets']), 3)
        entries, *_ = await self.service.ticket_entries(NOW)
        self.assertEqual(len(entries), 3)
        premium = next(e for e in entries if e['subtitle'].startswith('轮次：premium'))
        self.assertIn('已结束', premium['status_label'])
        self.assertIn('待核验', premium['status_label'])
        self.assertIn('转售', next(e for e in entries if e['subtitle'].startswith('轮次：resale'))['status_label'])
        self.service.set_subscription('ticket', 'test:GroupMessage:42', True)
        with self.service.db._connect() as db:
            db.execute("UPDATE ticket_group_subscriptions SET created_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(await self.service.claim_due_reminders(NOW), [])  # resale is not a lottery alert

    async def test_future_live_with_only_ended_premium_is_rechecked_in_both_paths(self):
        self.event('clash', [self.performance('d1', '2027-02-27'), self.performance('d2', '2027-02-28')], [self.round('premium')])
        self.service.db.source_error('clash', ROOT, 'offline')
        with self.service.db._connect() as db:
            db.execute("UPDATE sources SET fetched_at='2026-09-13T00:00:00+00:00',attempted_at='2026-09-13T00:00:00+00:00'")
        self.service._refresh_article = AsyncMock(return_value=False)
        self.assertEqual(await self.service.refresh_open_ticket_sources(NOW), {'refreshed': 1, 'failed': 0})
        self.assertEqual(await self.service.refresh_due_ticket_sources(NOW), {'refreshed': 1, 'failed': 0})
        self.assertEqual(self.service._refresh_article.await_count, 2)
        with self.service.db._connect() as db:
            db.execute('UPDATE sources SET attempted_at=?', (NOW.astimezone(timezone.utc).isoformat(),))
        self.assertEqual(await self.service.refresh_due_ticket_sources(NOW), {'refreshed': 0, 'failed': 0})

    async def test_news_general_independent_freshness_dedupe_and_root_refresh(self):
        self.event('clash', [self.performance('d1', '2027-02-27')], [self.round('premium')])
        self.service.db.source_error('clash', ROOT, 'offline')
        before = self.service.db.source_candidates()[0]['fetched_at']
        self.service.client.recent_news = AsyncMock(return_value=[{'_id': 23731, 'path': '01_20072', 'title': 'CLASH LIVE 一般会員先行', 'updated': 1791342902}])
        self.service.client.article_content = AsyncMock(return_value=NEWS_HTML)
        await self.service._discover_news_sources()
        await self.service._discover_news_sources()
        rows = self.service.db.ticket_query_rows()
        self.assertEqual(len(rows), 2)
        general = next(r for r in rows if r['url'] == RECEPTION)
        self.assertEqual(general['verified_source_url'], NEWS)
        self.assertEqual(general['source_quality'], 'verified')
        self.assertEqual(general['application_end'], '2026-11-01T23:59+09:00')
        self.assertEqual(general['payment_end'], '2026-11-18T23:59+09:00')
        self.assertEqual(next(r for r in rows if r['name'] == 'premium')['source_quality'], 'stale')
        self.assertEqual(self.service.db.source_candidates()[0]['fetched_at'], before)
        round_ = parse_ticket_news(NEWS_HTML, NEWS)[0]
        self.service.db.save_parsed('clash', ROOT, 'updated-root', 'test', [round_], [], [], [])
        self.assertEqual(len(self.service.db.detail('clash')['tickets']), 2)
        general = next(r for r in self.service.db.ticket_query_rows() if r['url'] == RECEPTION)
        self.assertEqual(general['verified_source_url'], ROOT)
        self.assertEqual(general['id'], 'clash:'+stable(RECEPTION))
        entries, *_ = await self.service.ticket_entries(NOW)
        self.assertEqual(next(e for e in entries if e['url'] == RECEPTION)['ticket_status'], 'open')

    async def test_tour_information_merge_city_binding_and_cast_never_discards_clocks(self):
        root = ROOT.replace('deremilli_clashmatch', 'tour')
        pages = {
            root: '<a href="information/nagano.php">情報</a><a href="information/makuhari.php">情報</a>'
                  '<a href="ticket/nagano.php">チケット</a><a href="cast/">出演者</a>',
            root+'information/nagano.php': '<dl><dt>公演日時</dt><dd>2026年10月8日 開演17:00<br>'
                '2026年10月9日 開演17:00</dd><dt>会場</dt><dd>長野ホール</dd></dl>',
            root+'information/makuhari.php': '<dl><dt>公演日時</dt><dd>2026年12月26日 開演18:00<br>'
                '2026年12月27日 開演18:00</dd><dt>会場</dt><dd>幕張ホール</dd></dl>',
            root+'ticket/nagano.php': '<section class="p-ticket__group"><h2>先行抽選</h2><dl>'
                '<dt>受付期間</dt><dd>2026年9月1日12:00～9月30日23:59</dd>'
                '<dt>受付URL</dt><dd><a href="'+RECEPTION+'">申込</a></dd></dl></section>',
            root+'cast/': '<h2>DAY2 CAST 2026年10月9日</h2><p>声優名（役名）</p>',
        }
        self.service.client.event_html = AsyncMock(side_effect=lambda url: pages[url])
        article = CmsArticle('tour', 'tour', root, [], None, None, None, {})
        parsed = await self.service._collect_special(article)
        self.assertEqual(len(parsed.performances), 4)
        self.assertTrue(all('开演' in p.session_label for p in parsed.performances))
        self.assertEqual(len(parsed.ticket_rounds[0].performance_keys), 2)
        cast_key = parsed.cast[0].performance_key
        self.assertEqual(next(p.date for p in parsed.performances if p.stable_key == cast_key), '2026-10-09')
        self.event('tour', parsed.performances, parsed.ticket_rounds)
        entries, *_ = await self.service.ticket_entries(NOW)
        self.assertIn('長野ホール', entries[0]['subtitle'])
        self.assertNotIn('幕張ホール', entries[0]['subtitle'])
        self.assertEqual((await self.service.ticket_entries(NOW.replace(hour=16)))[0], [])

    async def test_upgrade_migrates_old_rounds_and_stale_active_round_never_notifies(self):
        self.event('clash', [self.performance('d1', '2027-02-27')],
                   [self.round('missing-active', end=NOW+timedelta(hours=1))])
        with self.service.db._connect() as db:
            for column in ('verified_source_url', 'verified_at', 'verification_quality', 'performance_keys_json'):
                db.execute('ALTER TABLE ticket_rounds DROP COLUMN '+column)
        self.service.db = Database(self.service.db.path)
        legacy = self.service.db.ticket_query_rows()[0]
        self.assertEqual(legacy['verified_source_url'], ROOT)
        self.service.db.save_parsed('clash', ROOT, 'page-no-longer-lists-round', 'test', [], [], [], [])
        entries, *_ = await self.service.ticket_entries(NOW)
        self.assertEqual(entries[0]['ticket_status'], 'stale')
        self.service.set_subscription('ticket', 'test:GroupMessage:42', True)
        with self.service.db._connect() as db:
            db.execute("UPDATE ticket_group_subscriptions SET created_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(await self.service.claim_due_reminders(NOW), [])
        self.service.db = Database(self.service.db.path)
        self.assertEqual(self.service.db.ticket_query_rows()[0]['source_quality'], 'stale')

    async def test_home_schedule_clock_survives_day_level_cast(self):
        html = '<dl><dt>公演日時</dt><dd>2026年10月9日 開演17:00</dd></dl>'
        html += '<h2>出演者 DAY1 CAST 2026年10月9日</h2><p>声優名（役名）</p>'
        self.service.client.event_html = AsyncMock(return_value=html)
        parsed = await self.service._collect_special(CmsArticle('clash', 'CLASH', ROOT, [], None, None, None, {}))
        self.assertEqual(len(parsed.performances), 1)
        self.assertEqual(parsed.performances[0].session_label, '开演 17:00 JST')
        self.assertEqual(parsed.cast[0].performance_key, parsed.performances[0].stable_key)

    def test_news_does_not_turn_payment_or_streaming_into_an_application(self):
        self.assertEqual(parse_ticket_news(NEWS_HTML.replace('受付期間', '支払期間'), NEWS), [])
        self.assertEqual(parse_ticket_news(NEWS_HTML.replace('現地チケット情報', '配信チケット情報'), NEWS), [])
