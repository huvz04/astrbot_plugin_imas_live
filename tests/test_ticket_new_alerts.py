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

    def make_live_subscription_old(self, umo: str):
        self.service.set_subscription("live", umo, True)
        with self.service.db._connect() as db:
            db.execute("UPDATE live_group_subscriptions SET updated_at='2000-01-01T00:00:00+00:00' WHERE umo=?", (umo,))

    async def test_existing_event_new_round_is_live_routed_and_deduplicated(self):
        live_umo, ticket_umo = "test:GroupMessage:live", "test:GroupMessage:ticket"
        self.make_live_subscription_old(live_umo)
        self.service.set_subscription("ticket", ticket_umo, True)
        self.add_event("existing")
        # First verified page is the source baseline, never an announcement.
        self.service.db.save_parsed("existing", self.source, "v1", "test", [self.ticket("one")], [], [], [])
        self.assertEqual(await self.service.claim_new_ticket_announcements(self.now), [])

        self.service.db.save_parsed("existing", self.source, "v2", "test", [self.ticket("one"), self.ticket("two")], [], [], [])
        due = await self.service.claim_new_ticket_announcements(self.now)
        self.assertEqual([(row["umo"], row["notification_type"], row["subtitle"].split("｜")[0]) for row in due],
                         [(live_umo, "ticket_new", "会员two次先行")])
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
        self.make_live_subscription_old(umo)
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
        self.make_live_subscription_old(umo)
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
        self.service.set_subscription("live", umo, True)
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
        self.make_live_subscription_old(umo)
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
