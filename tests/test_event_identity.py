"""Conservative event identity, recoverable upgrade and notification aliases."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from imas_live.cms import CmsArticle
from imas_live.database import Database
from imas_live.models import CastAppearance, Evidence, Performance, TicketRound
from imas_live.parsing import stable
from imas_live.service import ImasLiveService

URL = 'https://idolmaster-official.jp/live_events/va-liv_letora_bd2026'
TITLE = 'レトラ BIRTHDAY ONLINE LIVE 2026'
VENUE = '配信会場 ASOBI STAGE ライブビューイング会場 ユナイテッド・シネマ アクアシティお台場 [3番スクリーン]'
NOW = datetime(2026, 10, 10, 12, tzinfo=ZoneInfo('Asia/Shanghai'))


class EventIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ImasLiveService(Path(self.temp.name))
        self.db = self.service.db

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def item(self, key, **extra):
        return dict(id=key, title=TITLE, url=URL, brands=['VALIV'],
                    event_display='2026年11月11日', venue=VENUE, **extra)

    def performance(self, key='day', day='2026-11-11', label='开演 19:00 JST', venue=VENUE):
        return Performance(key, day, label, venue, evidence=Evidence(URL, 'official', 'test'))

    def ticket(self, key='unlinked', url=None, seats=None):
        return TicketRound(key, '先行抽選申込受付', 'onsite', 'lottery',
            None, '2026-10-11T23:59+09:00', url=url, seats=seats, evidence=Evidence(URL, 'official', 'test'))

    def legacy(self, key, tickets=None, performances=None, number=None, **metadata):
        item = self.item(key)
        item.update(metadata)
        # Reproduce a pre-upgrade database, not the corrected ingestion path.
        with patch.object(self.db, '_same_event', return_value=False):
            self.db.upsert_event(item)
            self.db.save_parsed(key, item['url'], 'initial-'+key, 'test',
                tickets if tickets is not None else [self.ticket()],
                performances if performances is not None else [self.performance()], [], [])
        if number is not None:
            with self.db._connect() as conn:
                conn.execute('UPDATE event_numbers SET public_number=? WHERE event_id=?', (number, key))

    async def test_user_242_247_upgrade_backup_alias_numbers_and_idempotence(self):
        self.legacy('old-cms', number=242)
        self.legacy('23793', number=247, url=URL+'/?utm_source=official#top')
        self.db = self.service.db = Database(self.db.path)
        self.assertEqual(len(self.db.list_events(limit=20)), 1)
        for lookup in [242, 247]:
            detail = self.db.detail_by_public_number(lookup)
            self.assertEqual(detail['event']['id'], 'old-cms')
            self.assertEqual(detail['event']['public_number'], 242)
            self.assertEqual(len(detail['tickets']), 1)
            self.assertEqual(len(detail['performances']), 1)
        self.assertEqual(self.db.detail('23793')['event']['id'], 'old-cms')
        backup = Path(self.db.meta('event_identity_last_backup'))
        self.assertTrue(backup.is_file())
        with sqlite3.connect(backup) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)
        self.assertEqual(self.db.reconcile_event_identities(), 0)
        self.assertEqual(len(list(Path(self.temp.name).glob('*.before-event-merge-*.sqlite3'))), 1)
        self.db.upsert_event({**self.item('different'), 'url': URL+'-other'})
        self.assertEqual(self.db.detail('different')['event']['public_number'], 248)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM event_merge_audit').fetchone()[0], 1)

    async def test_cms_duplicate_reinsert_resolves_and_repeated_sync_cannot_rebirth(self):
        self.legacy('old-cms', number=242)
        self.assertFalse(self.db.upsert_event(self.item('23793'), discovered_after_baseline=True))
        self.assertEqual(self.db.resolve_event_id('23793'), 'old-cms')
        self.service.client.live_articles = AsyncMock(return_value=[CmsArticle('23793', TITLE, URL,
            ['VALIV'], '2026年11月11日', VENUE, None, {})])
        self.service.client.recent_news = AsyncMock(return_value=[])
        self.service._refresh_article = AsyncMock(return_value=False)
        for _ in range(2):
            await self.service.sync()
        self.assertEqual(len(self.db.list_events(limit=20)), 2)  # plus explicit controlled IUOAFA
        self.assertIn('old-cms', [c.args[0].cms_id for c in self.service._refresh_article.await_args_list])
        with self.db._connect() as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM ticket_new_event_discoveries WHERE event_id='old-cms'").fetchone())
        self.db.save_parsed('23793', URL, 'new', 'test', [self.ticket()], [self.performance()], [], [])
        self.assertEqual(len(self.db.detail('old-cms')['tickets']), 1)

    async def test_shared_url_different_title_city_date_or_session_not_merged(self):
        root = 'https://idolmaster-official.jp/live_event/gkmas_livetour_shirube/'
        examples = [dict(title='学園LIVE TOUR 東京', day='2026-11-07', venue='Kアリーナ横浜'),
                    dict(title='学園LIVE TOUR 大阪', day='2026-11-07', venue='大阪ホール'),
                    dict(title='学園LIVE TOUR 東京', day='2026-11-08', venue='Kアリーナ横浜'),
                    dict(title='学園LIVE TOUR 東京', day='2026-11-07', venue='Kアリーナ横浜', label='开演 13:00 JST')]
        for i, value in enumerate(examples):
            self.legacy(str(i), url=root, title=value['title'], venue=value['venue'], event_display=value['day'],
                performances=[self.performance(day=value['day'], venue=value['venue'], label=value.get('label', '开演 19:00 JST'))])
        self.assertEqual(self.db.reconcile_event_identities(), 0)
        self.assertEqual(len(self.db.list_events(limit=20)), 4)
        self.assertFalse(list(Path(self.temp.name).glob('*.before-event-merge-*.sqlite3')))

    async def test_missing_metadata_same_url_is_not_evidence_and_foreign_host_is_rejected(self):
        self.legacy('a', performances=[], venue=None, event_display=None)
        self.legacy('b', performances=[], venue=None, event_display=None)
        self.assertEqual(self.db.reconcile_event_identities(), 0)
        self.assertFalse(self.db._identity_url('https://idolmaster-official.jp.evil.test/live_event/same/'))
        self.assertFalse(self.db._identity_url('https://idolmaster-official.jp/news/01_18429'))

    async def test_multi_reception_seats_and_real_rounds_survive_merge(self):
        receipt = 'https://asobiticket2.asobistore.jp/receptions/'
        self.legacy('a', tickets=[self.ticket('sp-old', receipt+'sp', 'SP'), self.ticket()])
        self.legacy('b', tickets=[self.ticket('sp-new', receipt+'sp?utm_source=new', 'SP'),
            self.ticket('normal', receipt+'normal', 'S/A'), self.ticket('game', seats='ゲーム'), self.ticket()])
        self.db.reconcile_event_identities()
        detail = self.db.detail('a')
        self.assertEqual(len(detail['tickets']), 4)
        self.assertEqual({t['url'] for t in detail['tickets'] if t['url']}, {receipt+'sp', receipt+'normal'})
        canonical = 'a:'+stable(receipt+'sp')
        self.assertIn('b:'+stable(receipt+'sp'), self.db.ticket_identity_ids(canonical))
        self.assertIn('b:unlinked', self.db.ticket_identity_ids('a:unlinked'))

    async def test_merge_carries_pending_baselines_cast_assets_and_old_sent_dedupes(self):
        self.legacy('a')
        self.legacy('b')
        self.db.save_cast_assets('b', URL, [('https://official.test/cast.png', '/cached/cast.png')])
        with self.db._connect() as conn:
            conn.execute('INSERT INTO cast_appearances VALUES(?,?,?,?,?,?,?,?)',
                ('b:day:Actor:', 'b', 'day', 'Actor', None, 'announced', URL, 'official'))
            conn.execute('INSERT INTO ticket_new_rounds VALUES(?,?,?,?)', ('b:unlinked', 'b', URL, NOW.isoformat()))
            conn.execute('INSERT INTO ticket_new_event_discoveries VALUES(?,?,?)', ('b', NOW.isoformat(), 0))
            conn.execute('INSERT INTO review_items(event_id,source_url,note,observed_at) VALUES(?,?,?,?)',
                ('b', URL, '待核验', NOW.isoformat()))
        key = 'ticket_new|qq:GroupMessage:42|b:unlinked'
        self.db.claim_delivery(key, 'qq:GroupMessage:42', 'old payload', 'ticket_new')
        self.db.finish_delivery(key, True)
        self.db.reconcile_event_identities()
        self.assertEqual(self.db.cast_assets('b'), ['/cached/cast.png'])
        self.assertEqual(self.db.detail('a')['cast'][0]['person_name'], 'Actor')
        pending = self.db.new_ticket_round_rows()
        self.assertEqual([r['round_id'] for r in pending], ['a:unlinked'])
        self.assertEqual(pending[0]['observed_at'], NOW.isoformat())
        aliases = ['ticket_new|qq:GroupMessage:42|'+i for i in self.db.ticket_identity_ids('a:unlinked')]
        self.assertFalse(self.db.claim_delivery(aliases[0], 'qq:GroupMessage:42', 'new payload', 'ticket_new', aliases[1:]))
        with self.db._connect() as conn:
            self.assertEqual(conn.execute('SELECT initial_ticket_notice_recorded FROM ticket_new_event_discoveries').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT event_id FROM review_items').fetchone()[0], 'a')
            self.assertEqual(conn.execute('SELECT payload FROM delivery_log WHERE dedupe_key=?', (key,)).fetchone()[0], 'old payload')

    async def test_live_reminder_sent_under_old_performance_id_does_not_replay(self):
        moment = NOW + timedelta(hours=1)
        label = f'开演 {moment.astimezone(ZoneInfo("Asia/Tokyo")):%H:%M} JST'
        for event_id in ['a', 'b']:
            self.legacy(event_id, performances=[self.performance(day=NOW.date().isoformat(), label=label)])
        umo = 'qq:GroupMessage:42'
        self.service.set_subscription('live', umo, True)
        with self.db._connect() as conn:
            conn.execute("UPDATE live_group_subscriptions SET created_at='2000-01-01T00:00:00+00:00'")
        key = hashlib.sha256(f'live|{umo}|b:day|{moment.isoformat()}'.encode()).hexdigest()
        self.db.claim_delivery(key, umo, 'old LIVE')
        self.db.finish_delivery(key, True)
        self.db.reconcile_event_identities()
        self.assertIn('b:day', self.db.performance_identity_ids('a:day'))
        self.assertEqual(await self.service.claim_due_live_reminders(NOW), [])

    async def test_news_headline_promotion_needs_matching_full_roster_not_root_only(self):
        self.legacy('news:one', title='レトラ LIVE チケット先行受付開始！')
        self.legacy('23793')
        self.assertEqual(self.db.reconcile_event_identities(), 1)
        self.assertEqual(self.db.detail('23793')['event']['title'], TITLE)
        self.assertEqual(self.db.detail('23793')['event']['id'], 'news:one')

    async def test_ambiguous_shared_news_root_never_selects_last_city_for_ticket(self):
        root = 'https://idolmaster-official.jp/live_event/tour/'
        for city, day in [('Tokyo', '2026-11-07'), ('Osaka', '2026-12-07')]:
            self.legacy(city, url=root, title='LIVE '+city, venue=city, event_display=day,
                performances=[self.performance(day=day, venue=city)])
        self.service.client.recent_news = AsyncMock(return_value=[dict(_id=1, path='01_new', title='LIVE 先行', updated=1)])
        self.service.client.article_content = AsyncMock(return_value='<a href="'+root+'">公式サイト</a>')
        await self.service._discover_news_sources()
        self.assertEqual(len(self.db.list_events(limit=20)), 2)
        self.assertIsNone(self.db.meta('refresh_hint:Tokyo'))
        self.assertIsNone(self.db.meta('refresh_hint:Osaka'))

    async def test_empty_excerpt_and_different_unlinked_key_are_not_same_entrance(self):
        first, second = self.ticket('first'), self.ticket('second')
        first.evidence.excerpt = second.evidence.excerpt = ''
        self.legacy('a', tickets=[first])
        self.legacy('much-longer-id', tickets=[second])
        self.db.reconcile_event_identities()
        self.assertEqual(len(self.db.detail('a')['tickets']), 2)

    async def test_same_unlinked_key_with_empty_excerpt_and_different_length_event_ids(self):
        first, second = self.ticket('same'), self.ticket('same')
        first.evidence.excerpt = second.evidence.excerpt = ''
        self.legacy('a', tickets=[first])
        self.legacy('much-longer-id', tickets=[second])
        self.db.reconcile_event_identities()
        self.assertEqual(len(self.db.detail('a')['tickets']), 1)
        self.assertIn('much-longer-id:same', self.db.ticket_identity_ids('a:same'))

    async def test_unlinked_source_key_change_does_not_recreate_historical_notice(self):
        self.legacy('a')
        self.legacy('b')
        self.db.reconcile_event_identities()
        self.db.save_parsed('b', URL+'/', 'later', 'test', [self.ticket('changed-parser-key')],
                            [self.performance('changed-performance-key')], [], [])
        self.assertEqual(len(self.db.detail('a')['tickets']), 1)
        self.assertEqual(self.db.new_ticket_round_rows(), [])
        self.assertIn('a:changed-parser-key', self.db.ticket_identity_ids('a:unlinked'))
        self.assertIn('b:day', self.db.performance_identity_ids('a:changed-performance-key'))

    async def test_confirmed_merge_failure_rolls_back_with_recoverable_backup(self):
        self.legacy('a')
        self.legacy('b')
        original = self.db._merge_event
        def fail_after_changes(*args):
            original(*args)
            raise RuntimeError('simulated transaction failure')
        with patch.object(self.db, '_merge_event', side_effect=fail_after_changes):
            with self.assertRaisesRegex(RuntimeError, 'transaction failure'):
                self.db.reconcile_event_identities()
        self.assertEqual(len(self.db.list_events(limit=20)), 2)
        self.assertEqual(self.db.resolve_event_id('b'), 'b')
        self.assertEqual(len(list(Path(self.temp.name).glob('*.before-event-merge-*.sqlite3'))), 1)
        self.assertEqual(self.db.reconcile_event_identities(), 1)

    async def test_directory_clock_conflict_prevents_eager_cms_alias(self):
        self.legacy('a')
        self.assertTrue(self.db.upsert_event({**self.item('evening'), 'event_display': '2026年11月11日 開演13:00'}))
        self.assertEqual(self.db.resolve_event_id('evening'), 'evening')

    async def test_old_ticket_deadline_hash_dedupes_after_event_merge(self):
        start, end = NOW-timedelta(days=1), NOW+timedelta(hours=1)
        for event_id in ['a', 'b']:
            ticket = self.ticket()
            ticket.application_start, ticket.application_end = start.isoformat(), end.isoformat()
            self.legacy(event_id, tickets=[ticket])
        umo = 'qq:GroupMessage:42'
        self.service.set_subscription('ticket', umo, True)
        with self.db._connect() as conn:
            conn.execute("UPDATE ticket_group_subscriptions SET created_at='2000-01-01T00:00:00+00:00'")
        key = hashlib.sha256(f'ticket|{umo}|b:unlinked|{end.isoformat()}|1h'.encode()).hexdigest()
        self.db.claim_delivery(key, umo, 'old ticket')
        self.db.finish_delivery(key, True)
        self.db.reconcile_event_identities()
        self.assertEqual(await self.service.claim_due_reminders(NOW), [])

    async def test_late_identity_proof_does_not_announce_known_round_but_keeps_new_reception(self):
        receipt = 'https://asobiticket2.asobistore.jp/receptions/'
        self.legacy('a', tickets=[self.ticket('known', receipt+'known')])
        self.db.upsert_event({**self.item('changed-cms'), 'event_display': None, 'venue': None},
                             discovered_after_baseline=True)
        self.db.save_parsed('changed-cms', URL, 'late-identity-proof', 'test',
            [self.ticket('known', receipt+'known'), self.ticket('new', receipt+'genuinely-new')],
            [self.performance()], [], [])
        self.assertEqual(self.db.resolve_event_id('changed-cms'), 'a')
        self.assertEqual(len(self.db.detail('a')['tickets']), 2)
        self.assertEqual([r['url'] for r in self.db.new_ticket_round_rows()], [receipt+'genuinely-new'])
