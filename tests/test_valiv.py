"""vα-liv sources verified on 2026-10-18, including the JS ticket feed."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from imas_live.cms import SourceUnavailable, event_brands, event_root
from imas_live.database import Database
from imas_live.parsing import parse_information, parse_ticket_news, parse_ticket_page
from imas_live.service import ImasLiveService

FIXTURES = Path(__file__).parent / 'fixtures'
ROOT = 'https://idolmaster-official.jp/va-liv/event/3rd_live/'
JSON_URL = 'https://idolmaster-official.jp/cdn/mcms/va-liv/3rd_live/data.json'
NEWS = 'https://idolmaster-official.jp/news/01_20181.html'
RECEPTION = 'https://asobiticket2.asobistore.jp/receptions/a911b189-ae4a-4bc6-9f15-729201d4ff7c'
LETORA = 'https://idolmaster-official.jp/live_events/va-liv_letora_bd2026'
NOW = datetime(2026, 10, 18, 12, tzinfo=ZoneInfo('Asia/Shanghai'))


def fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class ValivParserTests(unittest.TestCase):
    def test_verified_event_routes_brand_and_exact_identity(self):
        self.assertEqual(event_root(ROOT.rstrip('/')+'#ticket'), ROOT)
        self.assertEqual(event_root(ROOT+'information'), ROOT)
        self.assertEqual(Database._identity_url(ROOT), ROOT.rstrip('/'))
        self.assertEqual(event_brands(['OTHER'], ROOT), ['VALIV'])
        self.assertEqual(event_brands(['OTHER'], LETORA), ['VALIV'])
        for bad in ('https://idolmaster-official.jp.evil.test/va-liv/event/3rd_live',
                    'http://idolmaster-official.jp/va-liv/event/3rd_live',
                    'https://idolmaster-official.jp/va-liv/event/',
                    'https://idolmaster-official.jp/va-liv/event/../news'):
            self.assertIsNone(event_root(bad))
        self.assertEqual(event_brands(['OTHER'], 'https://elsewhere.test/va-liv/event/3rd_live'), ['OTHER'])

    def test_all_six_explicit_sessions_use_start_not_doors_and_keep_cities(self):
        html = fixture('valiv_3rd.html')
        rows = parse_information(html, ROOT)
        self.assertEqual([p.date for p in rows], [
            '2027-01-30', '2027-01-31', '2027-01-31', '2027-03-21', '2027-03-21', '2027-03-22'])
        self.assertEqual([p.session_label.rsplit('开演 ', 1)[1] for p in rows], [
            '17:00 JST', '14:00 JST', '18:00 JST', '14:30 JST', '18:30 JST', '16:30 JST'])
        self.assertEqual([p.venue for p in rows], ['愛知県芸術劇場 大ホール']*3+['日本工学院アリーナ']*3)
        self.assertEqual(len({p.stable_key for p in rows}), 6)
        # The year must be in each published day; unrelated pages cannot use this layout.
        self.assertEqual(parse_information(html.replace('2027年', ''), ROOT), [])
        self.assertEqual(parse_information(html, 'https://example.test/event'), [])

    def test_news_onsite_heading_and_circle_field_labels(self):
        html = fixture('valiv_3rd_news.html')
        rows = parse_ticket_news(html, NEWS)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].url, rows[0].application_start, rows[0].application_end),
                         (RECEPTION, '2026-10-17T21:30+09:00', '2026-11-15T23:59+09:00'))
        self.assertEqual(rows[0].payment_end, '2026-12-02T23:59+09:00')
        self.assertEqual(parse_ticket_news(html.replace('現地チケット', '配信チケット'), NEWS), [])
        self.assertEqual(parse_ticket_news(html.replace('先行抽選申し込み受付', '一般販売(先着)'), NEWS), [])

    def test_livepocket_shared_url_is_two_rounds_and_recovers_old_ids(self):
        html = fixture('valiv_letora_ticket.html')
        rows = [t for t in parse_ticket_page(html, LETORA).ticket_rounds if t.sale_method == 'lottery']
        old = [t for t in parse_ticket_page(html.replace('https://livepocket.jp/e/va-liv_letora_bd2026', ''), LETORA).ticket_rounds
               if t.sale_method == 'lottery']
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r.stable_key for r in rows}), 2)
        self.assertEqual([r.stable_key for r in rows], [r.stable_key for r in old])
        self.assertTrue(all(r.url == 'https://livepocket.jp/e/va-liv_letora_bd2026' for r in rows))
        self.assertTrue(all(r.application_start is None for r in rows))  # no clock published
        self.assertEqual([r.application_end for r in rows], ['2026-10-30T23:59+09:00', '2026-10-11T23:59+09:00'])


class ValivWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = patch('imas_live.service.datetime', FrozenDateTime)
        self.clock.start()
        self.db_clock = patch('imas_live.database.now', return_value=NOW.astimezone(timezone.utc).isoformat(timespec='seconds'))
        self.db_clock.start()
        self.temp = tempfile.TemporaryDirectory()
        self.service = ImasLiveService(Path(self.temp.name))
        self.service._cache_event_cover = AsyncMock()
        self.service.client.fetch_html = AsyncMock(side_effect=lambda url: {
            ROOT: fixture('valiv_3rd.html'), JSON_URL: fixture('valiv_3rd_ticket.json')}[url])
        self.service.client.recent_news = AsyncMock(return_value=[{
            '_id': 23859, 'path': '01_20181', 'title': 'ヴイアラ 3rdLIVE 現地チケット先行',
            'updated': (NOW-timedelta(hours=15)).timestamp(), 'brand': [{'code': 'OTHER'}]}])
        self.service.client.article_content = AsyncMock(return_value=fixture('valiv_3rd_news.html'))
        self.service.set_subscription('ticket', 'test:GroupMessage:ticket', True)
        self.service.set_subscription('live', 'test:GroupMessage:live', True)
        with self.service.db._connect() as db:
            db.execute("UPDATE ticket_group_subscriptions SET updated_at='2000-01-01T00:00:00+00:00'")

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()
        self.db_clock.stop()
        self.clock.stop()

    def prior_installation(self, *, legacy_age_hours=15):
        self.service.db.set_meta('baseline_complete', '1')
        self.service.db.set_meta('news_discovery_baseline', (NOW-timedelta(days=20)).isoformat())
        item = self.service.client.recent_news.return_value[0]
        item['updated'] = (NOW-timedelta(hours=legacy_age_hours)).timestamp()
        for key in ('news_seen:23859', 'news_ticket_seen:23859'):
            self.service.db.set_meta(key, str(item['updated']))

    async def test_public_json_is_required_and_empty_ssr_is_not_no_ticket(self):
        html = await self.service.client.event_html(ROOT)
        self.assertEqual([c.args[0] for c in self.service.client.fetch_html.await_args_list], [ROOT, JSON_URL])
        rows = parse_ticket_page(html, ROOT).ticket_rounds
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].url, RECEPTION)
        self.assertEqual(rows[0].application_start, '2026-10-17T21:30+09:00')
        self.assertEqual(rows[0].application_end, '2026-11-15T23:59+09:00')
        self.assertEqual(len(parse_information(html, ROOT)), 6)
        self.assertEqual(rows[0].stable_key, parse_ticket_news(fixture('valiv_3rd_news.html'), NEWS)[0].stable_key)

    async def test_bad_json_keeps_verified_database_facts(self):
        self.service.db.upsert_event({'id': 'third', 'title': '3rdLIVE', 'brands': ['VALIV'], 'url': ROOT})
        article = self.service.client._article({'_id': 'third', 'title': '3rdLIVE', 'brand': [], 'event_url': ROOT})
        await self.service._refresh_article(article)
        before = self.service.db.detail('third')
        for malformed in ('not json', '{}', '{"ticket":[{}]}', '{"ticket":null}'):
            self.service.client.fetch_html.side_effect = lambda url: fixture('valiv_3rd.html') if url == ROOT else malformed
            with self.assertRaises(SourceUnavailable):
                await self.service._refresh_article(article)
            self.assertEqual(self.service.db.detail('third')['tickets'], before['tickets'])
            self.assertEqual(self.service.db.detail('third')['performances'], before['performances'])

    async def test_partial_sync_revisits_missed_news_verifies_same_cycle_and_announces_once(self):
        self.prior_installation()
        self.service.client.live_articles = AsyncMock(side_effect=AssertionError('partial sync must not fetch directory'))
        result = await self.service.sync(full_directory=False)
        self.assertEqual((result['events'], result['failed_pages']), (1, 0))
        events = self.service.db.list_events()
        self.assertEqual(len(events), 1)
        detail = self.service.db.detail(events[0]['id'])
        self.assertEqual(json.loads(detail['event']['brands_json']), ['VALIV'])
        self.assertEqual(len(detail['performances']), 6)
        self.assertEqual(len(detail['tickets']), 1)
        self.assertEqual(detail['tickets'][0]['verified_source_url'], ROOT)
        due = await self.service.claim_new_ticket_announcements(NOW)
        self.assertEqual([(d['umo'], d['url']) for d in due], [('test:GroupMessage:ticket', RECEPTION)])
        await self.service.finish_reminders(due, True)
        await self.service.sync(full_directory=False)
        self.service.client.article_content.assert_awaited_once()
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])
        # Recreating the service preserves dedupe and the routing migration baseline.
        await self.service.close()
        self.service = ImasLiveService(Path(self.temp.name))
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])

    async def test_legacy_news_older_than_72_hours_is_collected_without_backlog(self):
        self.prior_installation(legacy_age_hours=100)
        await self.service.sync(full_directory=False)
        self.assertEqual(len(self.service.db.ticket_query_rows()), 1)
        self.assertEqual(self.service.db.new_ticket_round_rows(), [])
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])

    async def test_first_installation_news_baseline_stays_quiet(self):
        await self.service._discover_news_sources()
        article = self.service.db.fetchable_events(1)[0]
        self.assertEqual(article['official_url'], ROOT)
        from imas_live.cms import CmsArticle
        await self.service._refresh_article(CmsArticle(article['id'], article['title'], ROOT, ['VALIV'], None, None, None, {}))
        self.assertEqual(len(self.service.db.detail(article['id'])['performances']), 6)
        self.assertEqual(self.service.db.new_ticket_round_rows(), [])
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])

    async def test_other_special_pages_do_not_fetch_the_valiv_json(self):
        other = 'https://idolmaster-official.jp/live_event/other/'
        self.service.client.fetch_html.side_effect = None
        self.service.client.fetch_html.return_value = '<h1>Other LIVE</h1>'
        self.assertEqual(await self.service.client.event_html(other), '<h1>Other LIVE</h1>')
        self.service.client.fetch_html.assert_awaited_once_with(other)

    async def test_later_livepocket_round_is_new_but_streaming_and_old_round_are_not(self):
        html = fixture('valiv_letora_ticket.html')
        self.service.db.upsert_event({'id': 'letora', 'title': 'レトラ BIRTHDAY ONLINE LIVE 2026', 'url': LETORA, 'brands': ['VALIV']})
        old = [t for t in parse_ticket_page(html, LETORA).ticket_rounds if '二次' not in t.name]
        self.service.db.save_parsed('letora', LETORA, 'old', 'test', old, [], [], [])
        self.service.db.save_parsed('letora', LETORA, 'new', 'test', parse_ticket_page(html, LETORA).ticket_rounds, [], [], [])
        due = await self.service.claim_new_ticket_announcements(NOW)
        self.assertEqual(len(due), 1)
        self.assertTrue(due[0]['subtitle'].startswith('先行抽選二次申込受付｜'))
        self.assertEqual(due[0]['url'], 'https://livepocket.jp/e/va-liv_letora_bd2026')

    async def test_livepocket_link_recovery_does_not_create_new_notices(self):
        html = fixture('valiv_letora_ticket.html')
        self.service.db.upsert_event({'id': 'letora', 'title': 'レトラ BIRTHDAY ONLINE LIVE 2026', 'url': LETORA, 'brands': ['VALIV']})
        old = parse_ticket_page(html.replace('https://livepocket.jp/e/va-liv_letora_bd2026', ''), LETORA).ticket_rounds
        self.service.db.save_parsed('letora', LETORA, 'old', 'test', old, [], [], [])
        self.service.db.save_parsed('letora', LETORA, 'new', 'test', parse_ticket_page(html, LETORA).ticket_rounds, [], [], [])
        self.assertEqual(len(self.service.db.detail('letora')['tickets']), 3)
        self.assertEqual(self.service.db.new_ticket_round_rows(), [])
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])
