import tempfile
import unittest
from pathlib import Path

from imas_live.database import Database
from imas_live.models import Evidence, TicketRound


class DatabaseTests(unittest.TestCase):
    def test_deadline_change_keeps_round_identity_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "live.sqlite3")
            db.upsert_event({"id": "cms-1", "title": "测试活动", "brands": ["GAKUEN"], "url": "https://example.test", "event_display": None, "venue": None, "updated": None})
            def ticket(end: str):
                return TicketRound("round-stable", "会員先行", "onsite", "lottery", application_start="2026-09-01T12:00+09:00", application_end=end, evidence=Evidence("https://example.test/ticket", "期限", "test"))
            self.assertTrue(db.save_parsed("cms-1", "https://example.test/ticket", "hash-1", "test", [ticket("2026-09-10T23:59+09:00")], [], [], []))
            self.assertTrue(db.save_parsed("cms-1", "https://example.test/ticket", "hash-2", "test", [ticket("2026-09-11T23:59+09:00")], [], [], []))
            rows = db.tickets()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "round-stable")
            self.assertEqual(rows[0]["application_end"], "2026-09-11T23:59+09:00")
