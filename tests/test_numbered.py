"""Numbered admission slips share ticket subscriptions, without general goods spam."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from imas_live.cms import CmsArticle, SourceUnavailable
from imas_live.models import Evidence, Performance, TicketRound
from imas_live.parsing import parse_ticket_news, parse_ticket_page
from imas_live.service import ImasLiveService

NOW = datetime(2026, 10, 21, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
ROOT = 'https://idolmaster-official.jp/live_event/gkmas_livetour_shirube/'
FIXTURES = Path(__file__).parent/'fixtures'


class NumberedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = patch('imas_live.database.now', return_value=NOW.astimezone(timezone.utc).isoformat())
        self.mock_now = self.clock.start()
        self.service = ImasLiveService(Path(self.temp.name))
        self.service.db.upsert_event({'id': 'final', 'title': '学園 LIVE ファイナル', 'url': ROOT, 'brands': ['GAKUEN']})
        self.service.set_subscription('ticket', 'test:GroupMessage:ticket', True)
        self.service.set_subscription('live', 'test:GroupMessage:live-only', True)
        with self.service.db._connect() as db:
            db.execute("UPDATE ticket_group_subscriptions SET updated_at='2026-10-01T00:00:00+00:00',created_at='2026-10-01T00:00:00+00:00'")

    async def asyncTearDown(self):
        await self.service.close()
        self.clock.stop()
        self.temp.cleanup()

    def ticket(self, key, scope='numbered', method='unknown', end=NOW+timedelta(hours=24)):
        return TicketRound(key, '物販整理券 '+key if scope == 'numbered' else key, scope, method,
            (NOW-timedelta(days=1)).isoformat(), end.isoformat(), url='https://apply.test/'+key,
            evidence=Evidence(ROOT, 'official reception', 'test'))

    def save(self, rows, digest='new'):
        self.service.db.save_parsed('final', ROOT, digest, 'test', rows,
            [Performance('day', '2026-11-07', '开演 17:00 JST', 'Hall')], [], [])

    def test_official_goods_and_animate_slips_keep_their_own_links_and_no_false_first_come(self):
        html = (FIXTURES/'shiny_single_page.html').read_text(encoding='utf-8')
        rows = [r for r in parse_ticket_page(html, ROOT).ticket_rounds if r.ticket_scope == 'numbered']
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.sale_method for r in rows}, {'lottery'})
        self.assertTrue(rows[0].url.startswith('https://asobiticket2.asobistore.jp/receptions/'))
        self.assertEqual(rows[1].url, 'https://www.club-animate.jp/entries/fair_entry/8269')
        rows = parse_ticket_page((FIXTURES/'gakuen_numbered.html').read_text(encoding='utf-8'), ROOT).ticket_rounds
        self.assertEqual((rows[0].ticket_scope, rows[0].sale_method), ('numbered', 'unknown'))
        # A products page mentioning slips in unrelated copy isn't itself a slip reception.
        html = '<h3>グッズ事前販売</h3><dl><dt>販売期間</dt><dd>2026年10月20日12:00～10月30日23:59</dd></dl>'
        self.assertEqual(parse_ticket_page(html, ROOT).ticket_rounds[0].ticket_scope, 'goods')

    async def test_added_lottery_first_come_and_unknown_slips_notify_and_remind_without_goods_or_streams(self):
        self.save([self.ticket('old', scope='onsite', method='lottery')], 'baseline')
        slips = [self.ticket(method, method=method) for method in ('lottery', 'first_come', 'unknown')]
        self.save([*slips, self.ticket('products', scope='goods', method='lottery'), self.ticket('stream', scope='streaming', method='lottery')])
        due = await self.service.claim_new_ticket_announcements(NOW)
        self.assertEqual(len(due), 3)
        self.assertEqual({r['umo'] for r in due}, {'test:GroupMessage:ticket'})
        self.assertTrue(all('新增整理券' in r['status_label'] for r in due))
        entries, *_ = await self.service.ticket_entries(NOW)
        self.assertEqual(len(entries), 3)
        self.assertTrue(all('整理券' in r['status_label'] for r in entries))
        reminders = await self.service.claim_due_reminders(NOW)
        self.assertEqual(len(reminders), 3)
        await self.service.finish_reminders(due+reminders, True)
        self.save(slips)
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])
        self.assertEqual(await self.service.claim_due_reminders(NOW), [])

    async def test_first_coverage_is_quiet_and_stale_expired_and_future_slips_stay_out_of_default(self):
        opened = self.ticket('active')
        expired = self.ticket('ended', end=NOW-timedelta(hours=1))
        future = self.ticket('future')
        future.application_start = (NOW+timedelta(hours=1)).isoformat()
        self.save([opened, expired, future])
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])
        self.assertEqual([r['round_name'] for r in (await self.service.ticket_entries(NOW))[0]], [opened.name])
        number = self.service.db.detail('final')['event']['public_number']
        detail = await self.service.ticket_detail(number, NOW)
        self.assertEqual(len(detail['tickets']), 3)
        self.assertTrue(all(r['url'] is None for r in detail['tickets'] if r['ticket_status'] == 'ended'))
        self.service.db.source_error('final', ROOT, 'offline')
        self.assertEqual((await self.service.ticket_entries(NOW))[0], [])
        self.assertEqual(await self.service.claim_due_reminders(NOW), [])

    async def test_goods_navigation_respects_city_and_never_fetches_images_or_external_forms(self):
        pages = {
            ROOT: '<a href="information/final.php">ファイナル</a><a href="goods/">GOODS</a>',
            ROOT+'information/final.php': '<dl><dt>公演日時</dt><dd>2026年11月7日 開演17:00</dd></dl>',
            ROOT+'goods/': '<meta http-equiv="refresh" content="0; url=./iwate.php">',
            ROOT+'goods/iwate.php': (FIXTURES/'gakuen_numbered.html').read_text(encoding='utf-8')+'<a href="final.php">ファイナル</a>',
            ROOT+'goods/final.php': '<h3>グッズ整理券</h3><dl><dt>受付期間</dt><dd>2026年10月20日13:00～10月30日23:59</dd>'
                '<dt>受付URL</dt><dd><a href="https://www.club-animate.jp/entries/fair_entry/9999">申込</a></dd></dl>'
                '<a href="../assets/images/goods/large.jpg">商品画像</a>',
        }
        self.service.client.event_html = AsyncMock(side_effect=lambda url: pages[url])
        result = await self.service._collect_special(CmsArticle('final', '学園 LIVE ファイナル', ROOT, [], None, None, None, {}))
        self.assertEqual(len(result.ticket_rounds), 1)
        self.assertEqual(result.ticket_rounds[0].url, 'https://www.club-animate.jp/entries/fair_entry/9999')
        self.assertEqual(result.ticket_rounds[0].performance_keys, [result.performances[0].stable_key])
        self.assertEqual({c.args[0] for c in self.service.client.event_html.await_args_list}, set(pages))

    async def test_optional_goods_failure_does_not_break_performances_or_live_tickets(self):
        async def page(url):
            if '/goods/' in url:
                raise SourceUnavailable('goods offline')
            return '<dl><dt>公演日時</dt><dd>2026年11月7日 開演17:00</dd></dl><a href="goods/">GOODS</a>'
        self.service.client.event_html = AsyncMock(side_effect=page)
        result = await self.service._collect_special(CmsArticle('final', '学園 LIVE', ROOT.replace('gkmas_livetour_shirube', 'example'), [], None, None, None, {}))
        self.assertEqual(len(result.performances), 1)
        self.assertTrue(any('整理券来源暂不可用' in note for note in result.review_notes))

    async def test_numbered_news_is_detected_and_old_marker_revisited_without_new_event(self):
        reception = 'https://asobiticket2.asobistore.jp/receptions/slips'
        html = '<h4>物販整理券 抽選受付</h4><div class="c-txt"><b>〇受付期間</b><br>2026年10月20日13:00～10月30日23:59<br>'
        html += '<a href="'+reception+'">申込</a></div><a href="'+ROOT+'">特設サイト</a>'
        parsed = parse_ticket_news(html, ROOT+'news')
        self.assertEqual((len(parsed), parsed[0].ticket_scope), (1, 'numbered'))
        self.assertEqual(parse_ticket_news(html.replace('物販整理券 抽選受付', 'グッズ事前販売'), ROOT), [])
        item = {'_id': 999, 'path': '01_slips', 'title': '整理券受付のお知らせ', 'updated': (NOW-timedelta(hours=12)).timestamp()}
        self.service.db.set_meta('news_discovery_baseline', (NOW-timedelta(days=10)).isoformat())
        self.service.db.set_meta('news_seen:999', str(item['updated']))
        self.service.db.set_meta('news_ticket_seen_v2:999', str(item['updated']))
        self.service.client.recent_news = AsyncMock(return_value=[item])
        self.service.client.article_content = AsyncMock(return_value=html)
        await self.service._discover_news_sources()
        await self.service._discover_news_sources()
        self.service.client.article_content.assert_awaited_once()
        self.assertEqual(len(self.service.db.list_events()), 1)
        self.assertEqual(len(await self.service.claim_new_ticket_announcements(NOW)), 1)

    async def test_upgrade_migrates_old_goods_scope_and_recovers_recent_missed_slip_once(self):
        self.mock_now.return_value = (NOW-timedelta(days=3)).astimezone(timezone.utc).isoformat()
        self.save([], 'old-source')
        self.mock_now.return_value = NOW.astimezone(timezone.utc).isoformat()
        old = self.ticket('missed')
        old.ticket_scope = 'goods'
        self.save([old])
        with self.service.db._connect() as db:
            db.execute("DELETE FROM meta WHERE key='numbered_notice_recovery_v1'")
        await self.service.close()
        self.service = ImasLiveService(Path(self.temp.name))
        self.assertEqual(self.service.db.detail('final')['tickets'][0]['ticket_scope'], 'numbered')
        due = await self.service.claim_new_ticket_announcements(NOW)
        self.assertEqual(len(due), 1)
        await self.service.finish_reminders(due, True)
        await self.service.close()
        self.service = ImasLiveService(Path(self.temp.name))
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])
