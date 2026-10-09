"""Entrance identity, legacy cache upgrades, and official accordion ownership."""
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from imas_live.cms import CmsArticle
from imas_live.database import Database
from imas_live.models import Evidence, Performance, TicketRound
from imas_live.parsing import canonical_reception_url, parse_ticket_page, stable
from imas_live.service import ImasLiveService

ROOT = 'https://idolmaster-official.jp/live_event/REFRAC7IONS_Still_blue/'
TICKET = ROOT+'ticket/'
URL = 'https://asobiticket2.asobistore.jp/receptions/e50a8381-d718-4ecc-a9fa-0a9db0804812'
NOW = datetime(2026, 10, 9, 12, tzinfo=ZoneInfo('Asia/Shanghai'))


def fields(start='2026年8月27日 (木) 12:00', end='9月23日 (水・祝) 23:59', url=None):
    return '<dl class="twoColList"><dt>受付期間</dt><dd>'+start+'～'+end+'</dd>'+(
        '<dt>受付URL</dt><dd><a href="'+url+'">申込</a></dd>' if url else '')+'</dl>'


class TicketIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ImasLiveService(Path(self.temp.name))
        self.service.db.upsert_event({'id': 'blue', 'title': 'Still blue', 'brands': [], 'url': ROOT})

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def round(self, url=URL, key=None, name='一般先行'):
        return TicketRound(key or stable(url), name, 'onsite', 'lottery',
            (NOW-timedelta(days=3)).isoformat(), (NOW+timedelta(hours=1)).isoformat(), url=url,
            evidence=Evidence(TICKET, 'exact official field block', 'ticket-html'))

    def save(self, rounds, digest='v1'):
        self.service.db.save_parsed('blue', ROOT, digest, 'test', rounds,
            [Performance('live', '2026-12-19', '开演 17:00 JST', 'Hall', evidence=Evidence(ROOT, 'official', 'test'))], [], [])

    def old_subscriptions(self):
        umo = 'test:GroupMessage:42'
        self.service.set_subscription('ticket', umo, True)
        self.service.set_subscription('live', umo, True)
        with self.service.db._connect() as db:
            for table in ('ticket_group_subscriptions', 'live_group_subscriptions'):
                db.execute('UPDATE '+table+" SET created_at='2000-01-01T00:00:00+00:00',updated_at='2000-01-01T00:00:00+00:00'")
        return umo

    def test_same_accordion_owns_each_title_and_spaced_weekday_has_time(self):
        titles = ['アソビストア一般会員先行', 'ゲーム先行', '7th LIVE TOUR Blu-ray「早期予約者限定」先行', 'アソビストアプレミアム会員先行']
        html = '<dl class="accordion">'+''.join('<dt>'+name+'</dt><dd>'+fields(url=URL if i == 0 else None)+'</dd>'
                                              for i, name in enumerate(titles))+'</dl>'
        rows = parse_ticket_page(html, TICKET).ticket_rounds
        self.assertEqual([r.name for r in rows], titles)
        self.assertTrue(all(r.application_start == '2026-08-27T12:00+09:00' for r in rows))
        self.assertTrue(all(r.application_end == '2026-09-23T23:59+09:00' for r in rows))

    def test_local_seats_override_details_and_distinct_receptions_survive(self):
        html = '<details><summary>アソビストアプレミアム会員先行</summary>'+''.join(
            '<div class="box01"><h3>'+seat+' アソビストアプレミアム会員先行</h3>'+fields(url=URL+str(i))+'</div>'
            for i, seat in enumerate(['【SP席】', '【S席・A席】']))+'</details>'
        rows = parse_ticket_page(html, TICKET).ticket_rounds
        self.assertEqual(len(rows), 2)
        self.assertIn('【SP席】', rows[0].name)
        self.assertIn('【S席・A席】', rows[1].name)
        self.assertNotEqual(rows[0].stable_key, rows[1].stable_key)

    def test_only_real_reception_tracking_is_normalized(self):
        self.assertEqual(canonical_reception_url(URL+'/?utm_source=official#apply'), URL)
        self.assertIsNone(canonical_reception_url('https://example.test/tickets?round=one'))
        rows = parse_ticket_page('<section class="p-ticket__group"><h2>先行抽選</h2>'+fields(url=URL+'?utm_source=x')+'</section>', TICKET).ticket_rounds
        self.assertEqual(rows[0].stable_key, stable(URL))
        self.assertEqual(rows[0].url, URL)

    async def test_legacy_duplicate_migration_preserves_number_baseline_and_deliveries(self):
        self.save([self.round()])
        number = self.service.db.detail('blue')['event']['public_number']
        umo = self.old_subscriptions()
        old_id = 'blue:legacy-title-based-id'
        deadline = self.round().application_end
        old_deadline_key = hashlib.sha256(f'ticket|{umo}|{old_id}|{deadline}|1h'.encode()).hexdigest()
        old_new_key = 'ticket_new|'+umo+'|'+old_id
        with self.service.db._connect() as db:
            record = dict(db.execute('SELECT * FROM ticket_rounds').fetchone())
            record.update(id=old_id, url=URL+'/?utm_source=old#apply', application_start=None, application_end=None)
            db.execute('INSERT INTO ticket_rounds ('+','.join(record)+') VALUES('+','.join('?' for _ in record)+')', list(record.values()))
            db.execute('INSERT INTO ticket_new_rounds VALUES(?,?,?,?)', (old_id, 'blue', ROOT, NOW.isoformat()))
        for key in (old_deadline_key, old_new_key):
            self.service.db.claim_delivery(key, umo, 'previously sent')
            self.service.db.finish_delivery(key, True)
        self.service.db = Database(self.service.db.path)
        self.assertEqual(len(self.service.db.detail('blue')['tickets']), 1)
        self.assertEqual(self.service.db.detail('blue')['event']['public_number'], number)
        self.assertEqual(len((await self.service.ticket_entries(NOW))[0]), 1)
        self.assertEqual(await self.service.claim_due_reminders(NOW), [])
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])
        self.save([self.round()], 'v2')
        self.service.db = Database(self.service.db.path)
        self.assertEqual(len(self.service.db.ticket_query_rows()), 1)
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])
        self.assertIn(old_id, self.service.db.ticket_identity_ids('blue:'+stable(URL)))

    async def test_different_entrances_same_title_and_period_are_never_merged(self):
        self.save([self.round(), self.round(URL+'-other-seat')])
        self.service.db = Database(self.service.db.path)
        self.assertEqual(len((await self.service.ticket_entries(NOW))[0]), 2)
        self.old_subscriptions()
        self.assertEqual(len(await self.service.claim_due_reminders(NOW)), 2)

    async def test_corrected_game_title_replaces_exact_legacy_block_without_ghost_or_notice(self):
        general, old_game = self.round(), self.round(key=stable(TICKET, '一般先行', ''))
        old_game.url = None
        self.save([general, old_game])
        self.old_subscriptions()
        game = self.round(key=stable(TICKET, 'ゲーム先行', ''), name='ゲーム先行')
        game.url = None
        self.save([general, game], 'correct-titles')
        rows = self.service.db.ticket_query_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['name'] for r in rows}, {'一般先行', 'ゲーム先行'})
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])
        self.save([general, game], 'correct-titles-again')
        self.assertEqual(len(self.service.db.ticket_query_rows()), 2)

    async def test_shared_reception_city_bindings_are_unioned_not_last_city_only(self):
        pages = {ROOT: '<a href="information/a.php">情報</a><a href="information/b.php">情報</a>'
                 '<a href="ticket/a.php">票</a><a href="ticket/b.php">票</a>'}
        for city, day in [('a', '2026年12月19日'), ('b', '2027年1月23日')]:
            pages[ROOT+'information/'+city+'.php'] = '<dl><dt>公演日時</dt><dd>'+day+' 開演17:00</dd><dt>会場</dt><dd>'+city+'</dd></dl>'
            pages[ROOT+'ticket/'+city+'.php'] = '<section class="p-ticket__group"><h2>先行抽選</h2>'+fields(url=URL)+'</section>'
        self.service.client.event_html = AsyncMock(side_effect=lambda url: pages[url])
        parsed = await self.service._collect_special(CmsArticle('blue', 'tour', ROOT, [], None, None, None, {}))
        self.assertEqual(len(parsed.ticket_rounds), 1)
        self.assertEqual(set(parsed.ticket_rounds[0].performance_keys), {p.stable_key for p in parsed.performances})
