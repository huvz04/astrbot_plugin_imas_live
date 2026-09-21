import tempfile
import unittest
from pathlib import Path

from imas_live.flight import FlightPlanner


def detail():
    return {
        "event": {"id": "IUOAFA", "title": "765 PRODUCTION × 961 PRODUCTION IDOL ULTIMATE ONCE AND FOR ALL", "public_number": 7, "venue": "京王アリーナ TOKYO"},
        "performances": [
            {"id": "IUOAFA:2027-07-24:18:00", "date": "2027-07-24", "session_label": "2027-07-24 18:00 JST", "venue": "京王アリーナ TOKYO"},
            {"id": "IUOAFA:2027-07-25:12:00", "date": "2027-07-25", "session_label": "2027-07-25 12:00 JST", "venue": "京王アリーナ TOKYO"},
            {"id": "IUOAFA:2027-07-25:18:00", "date": "2027-07-25", "session_label": "2027-07-25 18:00 JST", "venue": "京王アリーナ TOKYO"},
        ],
    }


class FlightPlannerTests(unittest.TestCase):
    def test_plan_uses_the_explicitly_selected_session_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FlightPlanner(Path(directory), {"flight_provider": "", "flight_target_price_cny": 0})
            task = planner.create_paused_plan(detail(), ["IUOAFA:2027-07-25:18:00"], "test:GroupMessage:42")

            self.assertEqual(task["session_ids"], ["IUOAFA:2027-07-25:18:00"])
            self.assertEqual(task["arrival_dates"], ["2027-07-23", "2027-07-24"])
            self.assertEqual(task["return_dates"], ["2027-07-26", "2027-07-27"])
            self.assertEqual(task["origin_airports"], ["PVG", "SHA"])
            self.assertEqual(task["destination_airports"], ["NRT", "HND"])
            self.assertTrue(task["direct_preferred"])
            self.assertFalse(task["enabled"])
            self.assertEqual(task["status"], "paused_needs_price_and_provider")
            self.assertEqual(planner.tasks_for("test:GroupMessage:42")[0]["id"], task["id"])

    def test_plan_rejects_unknown_sessions_and_unverified_venues(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FlightPlanner(Path(directory))
            with self.assertRaisesRegex(ValueError, "明确选择"):
                planner.create_paused_plan(detail(), ["not-a-session"], "test:GroupMessage:42")
            unsupported = detail()
            unsupported["performances"][0]["venue"] = "场馆待定"
            with self.assertRaisesRegex(ValueError, "场馆"):
                planner.create_paused_plan(unsupported, ["IUOAFA:2027-07-24:18:00"], "test:GroupMessage:42")
