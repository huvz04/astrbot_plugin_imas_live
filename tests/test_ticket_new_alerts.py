"""Regression coverage for conservative new-lottery group announcements."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from imas_live.models import Evidence, TicketRound
from imas_live.service import ImasLiveService


class TicketNewAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.zone = ZoneInfo("Asia/Shanghai")
        self.service = ImasLiveService(Path(self.temp.name), {"display_timezone": "Asia/Shanghai"})
        self.source = "https://example.test/live"
        self.now = datetime.now(self.zone)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def ticket(self, key: str, *, start: datetime | None = None, end: datetime | None = None) -> TicketRound:
        start = start or self.now - timedelta(hours=1)
        end = end or self.now + timedelta(days=2)
        evidence = Evidence(self.source, "official ticket page", "test")
        return TicketRound(key, f"会员{key}次先行", "onsite", "lottery",
                           start.astimezone(ZoneInfo("Asia/Tokyo")).isoformat(timespec="minutes"),
                           end.astimezone(ZoneInfo("Asia/Tokyo")).isoformat(timespec="minutes"),
                           url=self.source, evidence=evidence)

    def add_event(self, event_id: str, *, discovered: bool = False):
        self.service.db.upsert_event({"id": event_id, "title": f"{event_id} LIVE", "brands": ["GAKUEN"],
                                      "url": self.source, "event_display": None, "venue": None, "updated": None},
                                     discovered)

    def make_ticket_subscription_old(self, umo: str):
        self.service.set_subscription("ticket", umo, True)
        with self.service.db._connect() as db:
            db.execute("UPDATE ticket_group_subscriptions SET updated_at='2000-01-01T00:00:00+00:00' WHERE umo=?", (umo,))

    async def test_existing_event_new_round_is_ticket_routed_and_deduplicated(self):
        live_umo, ticket_umo = "test:GroupMessage:live", "test:GroupMessage:ticket"
        self.service.set_subscription("live", live_umo, True)
        self.make_ticket_subscription_old(ticket_umo)
        self.add_event("existing")
        # First verified page is the source baseline, never an announcement.
        self.service.db.save_parsed("existing", self.source, "v1", "test", [self.ticket("one")], [], [], [])
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])

        self.service.db.save_parsed("existing", self.source, "v2", "test", [self.ticket("one"), self.ticket("two")], [], [], [])
        due = await self.service.claim_new_ticket_announcements(self.now)
        self.assertEqual([(row["umo"], row["notification_type"], row["subtitle"].split("｜")[0]) for row in due],
                         [(ticket_umo, "ticket_new", "会员two次先行")])
        self.assertIn("现已开放", due[0]["status_label"])
        with self.service.db._connect() as db:
            self.assertEqual(db.execute("SELECT notification_type FROM delivery_log WHERE dedupe_key=?", (due[0]["delivery_key"],)).fetchone()[0], "ticket_new")
        await self.service.finish_reminders(due, True)
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])

        # A deadline/title/link correction keeps the stable round ID and is not new.
        self.service.db.save_parsed("existing", self.source, "v3", "test", [
            self.ticket("one"), self.ticket("two", end=self.now + timedelta(days=4))], [], [], [])
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])

    async def test_new_directory_event_gets_one_initial_verified_ticket_announcement(self):
        umo = "test:GroupMessage:new"
        self.make_ticket_subscription_old(umo)
        # This represents an event first observed in a later completed directory.
        self.add_event("directory-new", discovered=True)
        self.service.db.save_parsed("directory-new", self.source, "v1", "test", [self.ticket("first")], [], [], [])
        due = await self.service.claim_new_ticket_announcements(self.now)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["umo"], umo)

        # A first parse of another source/event without directory discovery is silent.
        self.add_event("coverage-only")
        self.service.db.save_parsed("coverage-only", self.source, "v1", "test", [self.ticket("old")], [], [], [])
        await self.service.finish_reminders(due, True)
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])

    async def test_upgrade_and_first_source_coverage_are_silent(self):
        umo = "test:GroupMessage:upgrade"
        self.make_ticket_subscription_old(umo)
        self.add_event("upgrade-cache")
        self.service.db.save_parsed("upgrade-cache", self.source, "v1", "test", [self.ticket("one")], [], [], [])
        # Simulate an installation created before ticket_source_baselines existed.
        with self.service.db._connect() as db:
            db.execute("DELETE FROM ticket_source_baselines WHERE event_id='upgrade-cache'")
        self.service.db.save_parsed("upgrade-cache", self.source, "v2", "test", [self.ticket("one"), self.ticket("two")], [], [], [])
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])

        # Only a later successful verification of the already-covered source compares deltas.
        self.service.db.save_parsed("upgrade-cache", self.source, "v3", "test", [
            self.ticket("one"), self.ticket("two"), self.ticket("three")], [], [], [])
        due = await self.service.claim_new_ticket_announcements(self.now)
        self.assertEqual([row["subtitle"].split("｜")[0] for row in due], ["会员three次先行"])

    async def test_late_enable_does_not_replay_and_failed_delivery_retries_after_restart(self):
        umo = "test:GroupMessage:retry"
        self.add_event("retry")
        self.service.db.save_parsed("retry", self.source, "v1", "test", [self.ticket("one")], [], [], [])
        self.service.db.save_parsed("retry", self.source, "v2", "test", [self.ticket("one"), self.ticket("two")], [], [], [])

        # A group enabled after discovery cannot receive the accumulated row.
        self.service.set_subscription("ticket", umo, True)
        with self.service.db._connect() as db:
            db.execute("UPDATE ticket_new_rounds SET observed_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])

        # A later genuine round is claimed again; failed delivery survives restart.
        self.service.db.save_parsed("retry", self.source, "v3", "test", [self.ticket("one"), self.ticket("two"), self.ticket("three")], [], [], [])
        due = await self.service.claim_new_ticket_announcements(self.now)
        self.assertEqual(len(due), 1)
        await self.service.finish_reminders(due, False)
        await self.service.close()
        self.service = ImasLiveService(Path(self.temp.name), {"display_timezone": "Asia/Shanghai"})
        retried = await self.service.claim_new_ticket_announcements(self.now)
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["notification_type"], "ticket_new")

    async def test_future_and_ended_rounds_are_distinguished(self):
        umo = "test:GroupMessage:state"
        self.make_ticket_subscription_old(umo)
        self.add_event("state")
        self.service.db.save_parsed("state", self.source, "v1", "test", [self.ticket("one")], [], [], [])
        self.service.db.save_parsed("state", self.source, "v2", "test", [
            self.ticket("one"),
            self.ticket("future", start=self.now + timedelta(days=1)),
            self.ticket("ended", end=self.now - timedelta(minutes=1)),
        ], [], [], [])
        due = await self.service.claim_new_ticket_announcements(self.now)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["ticket_status"], "upcoming")
        self.assertIn("尚未开始", due[0]["subtitle"])

    async def test_two_groups_delivery_results_and_empty_config_persistence_are_independent(self):
        first, second = 'test:GroupMessage:first', 'test:GroupMessage:second'
        for umo in (first, second):
            self.make_ticket_subscription_old(umo)
        self.add_event('two-groups')
        self.service.db.save_parsed('two-groups', self.source, 'v1', 'test', [self.ticket('one')], [], [], [])
        self.service.db.save_parsed('two-groups', self.source, 'v2', 'test', [self.ticket('one'), self.ticket('two')], [], [], [])
        due = await self.service.claim_new_ticket_announcements(self.now)
        self.assertEqual({r['umo'] for r in due}, {first, second})
        await self.service.finish_reminders([r for r in due if r['umo'] == first], True)
        await self.service.finish_reminders([r for r in due if r['umo'] == second], False)
        await self.service.close()
        self.service = ImasLiveService(Path(self.temp.name), {})
        for umo in (first, second):
            self.assertTrue(self.service.subscription_enabled('ticket', umo))
            before = self.service.db.notification_status(umo)
            text = await self.service.notification_status(umo)
            self.assertIn('新增票务通知：开启', text)
            self.assertIn('已发送' if umo == first else '发送失败', text)
            self.assertEqual(before, self.service.db.notification_status(umo))
            self.assertNotIn(second if umo == first else first, text)
        retried = await self.service.claim_new_ticket_announcements(self.now)
        self.assertEqual([r['umo'] for r in retried], [second])

    async def test_status_explains_late_enable_and_old_whitelist_is_ticket_only(self):
        self.add_event('late')
        self.service.db.save_parsed('late', self.source, 'v1', 'test', [self.ticket('one')], [], [], [])
        self.service.db.save_parsed('late', self.source, 'v2', 'test', [self.ticket('one'), self.ticket('two')], [], [], [])
        umo = 'test:GroupMessage:late'
        self.service.set_subscription('ticket', umo, True)
        with self.service.db._connect() as db:
            db.execute("UPDATE ticket_new_rounds SET observed_at='2000-01-01T00:00:00+00:00'")
        self.assertIn('开启前发现', await self.service.notification_status(umo))
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])
        with tempfile.TemporaryDirectory() as tmp:
            legacy = ImasLiveService(Path(tmp), {'white_umos': [umo]})
            try:
                self.assertFalse(legacy.subscription_enabled('live', umo))
                self.assertTrue(legacy.subscription_enabled('ticket', umo))
                self.assertIn('新增票务通知：开启', await legacy.notification_status(umo))
            finally:
                await legacy.close()

    async def test_ticket_enable_times_control_announcements_independently_of_live(self):
        umo = 'test:GroupMessage:independent'
        self.make_ticket_subscription_old(umo)
        self.service.set_subscription('live', umo, True)
        self.add_event('switches')
        self.service.db.save_parsed('switches', self.source, 'v1', 'test', [self.ticket('one')], [], [], [])
        self.service.db.save_parsed('switches', self.source, 'v2', 'test', [self.ticket('one'), self.ticket('two')], [], [], [])
        self.service.set_subscription('live', umo, False)
        first = await self.service.claim_new_ticket_announcements(self.now)
        self.assertEqual([row['umo'] for row in first], [umo])
        await self.service.finish_reminders(first, True)
        self.service.set_subscription('ticket', umo, False)
        self.service.db.save_parsed('switches', self.source, 'v3', 'test', [self.ticket('one'), self.ticket('two'), self.ticket('three')], [], [], [])
        self.service.set_subscription('live', umo, True)
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])
        with self.service.db._connect() as db:
            db.execute("UPDATE ticket_new_rounds SET observed_at='2000-01-01T00:00:00+00:00' WHERE round_id='switches:three'")
        self.service.set_subscription('ticket', umo, True)
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])
        self.service.db.save_parsed('switches', self.source, 'v4', 'test', [self.ticket('one'), self.ticket('two'), self.ticket('three'), self.ticket('four')], [], [], [])
        self.assertEqual(len(await self.service.claim_new_ticket_announcements(self.now)), 1)

    async def test_route_upgrade_baselines_existing_queue_without_changing_flags_or_delivery_history(self):
        umo, live_only = 'test:GroupMessage:ticket', 'test:GroupMessage:live'
        self.make_ticket_subscription_old(umo)
        self.service.set_subscription('live', live_only, True)
        self.add_event('upgrade-route')
        self.service.db.save_parsed('upgrade-route', self.source, 'v1', 'test', [self.ticket('one')], [], [], [])
        self.service.db.save_parsed('upgrade-route', self.source, 'v2', 'test', [self.ticket('one'), self.ticket('two')], [], [], [])
        key = 'ticket_new|'+live_only+'|upgrade-route:two'
        self.service.db.claim_delivery(key, live_only, 'already sent under LIVE', 'ticket_new')
        self.service.db.finish_delivery(key, True)
        before = self.service.db.notification_status(umo)['subscriptions']
        with self.service.db._connect() as db:
            db.execute("DELETE FROM meta WHERE key='ticket_new_ticket_channel_v1'")
        await self.service.close()
        self.service = ImasLiveService(Path(self.temp.name), {})
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])
        self.assertEqual(self.service.db.notification_status(umo)['subscriptions'], before)
        self.assertTrue(self.service.subscription_enabled('live', live_only))
        self.assertFalse(self.service.subscription_enabled('ticket', live_only))
        with self.service.db._connect() as db:
            self.assertEqual(db.execute('SELECT state FROM delivery_log WHERE dedupe_key=?', (key,)).fetchone()[0], 'sent')
        self.service.db.save_parsed('upgrade-route', self.source, 'v3', 'test', [self.ticket('one'), self.ticket('two'), self.ticket('three')], [], [], [])
        due = await self.service.claim_new_ticket_announcements(self.now)
        self.assertEqual([row['umo'] for row in due], [umo])
        await self.service.finish_reminders(due, True)
        await self.service.close()
        self.service = ImasLiveService(Path(self.temp.name), {})
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])
