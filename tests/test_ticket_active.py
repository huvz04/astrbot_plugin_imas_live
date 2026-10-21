"""Active-only queries and added resale rounds on an already known LIVE."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from imas_live.models import Evidence, Performance, TicketRound
from imas_live.parsing import parse_ticket_page
from imas_live.service import ImasLiveService

NOW = datetime(2026, 10, 21, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
ROOT = 'https://idolmaster-official.jp/live_event/gkmas_livetour_shirube/ticket/final.php'
RESALE = (Path(__file__).parent/'fixtures/gakuen_final_resale.html').read_text(encoding='utf-8')


class TicketActiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = patch('imas_live.database.now', return_value=NOW.astimezone(timezone.utc).isoformat())
        self.mock_now = self.clock.start()
        self.service = ImasLiveService(Path(self.temp.name))
        self.service.db.upsert_event({'id': 'final', 'title': '学園 LIVE TOUR -標- ファイナル公演',
            'url': ROOT, 'brands': ['GAKUEN']})
        self.service.set_subscription('ticket', 'test:GroupMessage:ticket', True)
        self.service.set_subscription('live', 'test:GroupMessage:live-only', True)
        with self.service.db._connect() as db:
            db.execute("UPDATE ticket_group_subscriptions SET updated_at='2026-10-01T00:00:00+00:00',created_at='2026-10-01T00:00:00+00:00'")

    async def asyncTearDown(self):
        await self.service.close()
        self.clock.stop()
        self.temp.cleanup()

    def ticket(self, key, start=NOW-timedelta(days=1), end=NOW+timedelta(days=4), method='lottery'):
        return TicketRound(key, key, 'onsite', method, start.isoformat() if start else None,
            end.isoformat() if end else None, url='https://ticket.test/'+key,
            evidence=Evidence(ROOT, 'official reception', 'test'))

    def save(self, rows, digest='new'):
        self.service.db.save_parsed('final', ROOT, digest, 'test', rows, [
            Performance('d1', '2026-11-07', '开演 17:00 JST', 'Kアリーナ横浜'),
            Performance('d2', '2026-11-08', '开演 17:00 JST', 'Kアリーナ横浜')], [], [])

    async def test_query_and_forward_only_include_open_verified_rounds_with_exact_boundaries(self):
        rows = [self.ticket('open'), self.ticket('starts-now', start=NOW),
            self.ticket('urgent', end=NOW+timedelta(hours=1)),
            self.ticket('resale', method='resale'),
            self.ticket('ends-now', end=NOW), self.ticket('past', end=NOW-timedelta(minutes=1)),
            self.ticket('future', start=NOW+timedelta(minutes=1)),
            self.ticket('unknown-start', start=None), self.ticket('unknown-end', end=None),
            self.ticket('invalid-period', start=NOW+timedelta(days=5)), self.ticket('stale')]
        self.save(rows)
        with self.service.db._connect() as db:
            db.execute("UPDATE ticket_rounds SET verification_quality='stale' WHERE id='final:stale'")
        entries, *_ = await self.service.ticket_entries(NOW)
        self.assertEqual({e['round_name'] for e in entries}, {'open', 'starts-now', 'urgent', 'resale'})
        texts = await self.service.query_detail_texts(entries, 'ticket')
        self.assertEqual(len(texts), 1)
        for hidden in ('past', 'ends-now', 'future', 'unknown-start', 'unknown-end', 'invalid-period', 'stale'):
            self.assertNotIn('https://ticket.test/'+hidden, texts[0])
        self.assertEqual(len(self.service.db.detail('final')['tickets']), len(rows))
        # Losing the source removes the remaining cards rather than claiming they are open.
        self.service.db.source_error('final', ROOT, 'offline')
        self.assertEqual((await self.service.ticket_entries(NOW))[0], [])

    async def test_historical_get_keeps_states_but_suppresses_expired_application_links(self):
        self.save([self.ticket('past', end=NOW), self.ticket('open'),
                   self.ticket('past-sale', end=NOW-timedelta(days=1), method='first_come')])
        number = self.service.db.detail('final')['event']['public_number']
        detail = await self.service.ticket_detail(number, NOW)
        self.assertEqual(len(detail['tickets']), 3)
        links = [r['url'] for r in detail['tickets'] if r.get('url')]
        self.assertEqual(links, ['https://ticket.test/open'])
        self.assertTrue(any(r['ticket_status'] == 'ended' for r in detail['tickets']))
        after_live = await self.service.ticket_detail(number, NOW.replace(month=11, day=9))
        self.assertTrue(all(r['url'] is None for r in after_live['tickets']))

    async def test_existing_live_added_normal_and_daily_resale_rounds_each_notify_once(self):
        premium = self.ticket('premium', end=NOW-timedelta(days=1))
        self.save([premium], 'baseline')
        rows = parse_ticket_page(RESALE, ROOT).ticket_rounds
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.sale_method for r in rows}, {'resale'})
        self.assertEqual([r.name for r in rows], ['公式リセール｜11月7日(土)公演', '公式リセール｜11月8日(日)公演'])
        self.assertEqual([r.application_start for r in rows], ['2026-10-20T20:00+09:00']*2)
        self.save([premium, self.ticket('general'), *rows])
        due = await self.service.claim_new_ticket_announcements(NOW)
        self.assertEqual(len(due), 3)
        self.assertEqual({r['umo'] for r in due}, {'test:GroupMessage:ticket'})
        self.assertEqual(sum('新增转售' in r['status_label'] for r in due), 2)
        self.assertEqual(len(self.service.db.list_events()), 1)
        await self.service.finish_reminders(due, True)
        # The unchanged page and a title correction cannot generate another round.
        self.save([premium, self.ticket('general'), *rows])
        rows[0].name += ' Update'
        self.save([premium, self.ticket('general'), *rows], 'correct-title')
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])
        await self.service.close()
        self.service = ImasLiveService(Path(self.temp.name))
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])

    async def test_resale_new_notice_and_deadline_nodes_have_independent_dedupe(self):
        self.save([self.ticket('old', end=NOW-timedelta(days=1))], 'baseline')
        new = self.ticket('resale', method='resale', end=NOW+timedelta(hours=24))
        self.save([new])
        announcement = await self.service.claim_new_ticket_announcements(NOW)
        deadline = await self.service.claim_due_reminders(NOW)
        self.assertEqual((len(announcement), len(deadline)), (1, 1))
        self.assertNotEqual(announcement[0]['delivery_key'], deadline[0]['delivery_key'])
        await self.service.finish_reminders(announcement+deadline, True)
        self.assertEqual(await self.service.claim_due_reminders(NOW), [])
        # Simulate a fresh official recheck before the separate one-hour node.
        later = NOW+timedelta(hours=23)
        self.mock_now.return_value = later.astimezone(timezone.utc).isoformat()
        self.save([new])
        self.assertEqual(len(await self.service.claim_due_reminders(later)), 1)

    async def seed_legacy_resale_cache(self, *, first_coverage=False, end=None, start=None):
        self.mock_now.return_value = (NOW-timedelta(days=3)).astimezone(timezone.utc).isoformat()
        if not first_coverage:
            self.save([self.ticket('premium', end=NOW-timedelta(days=20))], 'baseline')
        self.mock_now.return_value = NOW.astimezone(timezone.utc).isoformat()
        row = self.ticket('resale', start=start or NOW-timedelta(hours=12),
                          end=end or NOW+timedelta(days=3), method='resale')
        self.save([row])
        with self.service.db._connect() as db:
            # Reproduce 0.4.19: facts were saved, but the resale queue was excluded.
            db.execute('DELETE FROM ticket_new_rounds')
            db.execute("DELETE FROM meta WHERE key='resale_notice_recovery_v1'")
        await self.service.close()
        self.service = ImasLiveService(Path(self.temp.name))

    async def test_upgrade_recovers_recent_cached_resale_once_for_preexisting_ticket_groups(self):
        await self.seed_legacy_resale_cache()
        self.service.set_subscription('ticket', 'test:GroupMessage:late', True)
        due = await self.service.claim_new_ticket_announcements(NOW)
        self.assertEqual([(r['umo'], r['url']) for r in due], [('test:GroupMessage:ticket', 'https://ticket.test/resale')])
        await self.service.finish_reminders(due, True)
        marker = self.service.db.meta('resale_notice_recovery_v1')
        await self.service.close()
        self.service = ImasLiveService(Path(self.temp.name))
        self.assertEqual(self.service.db.meta('resale_notice_recovery_v1'), marker)
        self.assertEqual(await self.service.claim_new_ticket_announcements(NOW), [])

    async def test_upgrade_never_turns_first_coverage_into_historical_notice(self):
        await self.seed_legacy_resale_cache(first_coverage=True)
        self.assertEqual(self.service.db.new_ticket_round_rows(), [])

    async def test_upgrade_older_resale_is_not_replayed(self):
        await self.seed_legacy_resale_cache(start=NOW-timedelta(days=5))
        self.assertEqual(self.service.db.new_ticket_round_rows(), [])

    async def test_upgrade_expired_resale_is_not_replayed(self):
        await self.seed_legacy_resale_cache(end=NOW-timedelta(hours=1))
        self.assertEqual(self.service.db.new_ticket_round_rows(), [])
