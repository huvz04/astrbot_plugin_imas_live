import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import AsyncMock, patch

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
    def test_standalone_route_has_explicit_dates_and_starts_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FlightPlanner(Path(directory))
            task = planner.create_paused_route("2027-07-23", "2027-07-26", "test:GroupMessage:1")
            self.assertEqual(task["arrival_dates"], ["2027-07-23"])
            self.assertEqual(task["return_dates"], ["2027-07-26"])
            self.assertEqual(task["event_id"], "")
            self.assertFalse(task["enabled"])
            self.assertEqual(len(planner._queries(task)), 2)

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

    def test_v046_paused_plan_is_migrated_without_losing_the_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "imas_flight.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("""CREATE TABLE flight_tasks (id TEXT PRIMARY KEY,event_id TEXT,revision TEXT,
                    session_ids_json TEXT,payload_json TEXT,umo TEXT,enabled INTEGER,created_at TEXT)""")
                db.execute("INSERT INTO flight_tasks VALUES (?,?,?,?,?,?,0,'2026-09-21')", (
                    "old-plan", "IUOAFA", "revision", '["session"]',
                    json.dumps({"id": "old-plan", "event_id": "IUOAFA", "enabled": False}), "test:GroupMessage:42"))
            planner = FlightPlanner(Path(directory))
            self.assertEqual(planner.task("old-plan")["umo"], "test:GroupMessage:42")
            self.assertFalse(planner.task("old-plan")["enabled"])


def complete_quote(task, *, price=1000, return_stops=0, baggage="unknown"):
    return {"provider": "serpapi", "market": "us", "origin": "PVG", "destination": "NRT",
            "return_origin": "HND", "return_destination": "SHA", "departure": "2027-07-23",
            "departure_time": "2027-07-23 08:00", "arrival_date": task["arrival_dates"][0],
            "return_date": task["return_dates"][0], "return_departure_date": task["return_dates"][0],
            "outbound_flights": ["JL 001"], "return_flights": ["JL 002"],
            "outbound_stops": 0, "return_stops": return_stops, "price": price,
            "currency": "CNY", "adults": 1, "cabin": "economy", "baggage": baggage,
            "link": "https://www.google.com/travel/flights", "fetched_at": time.time()}


class FlightMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_serpapi_outbound_token_is_expanded_before_a_quote_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FlightPlanner(Path(directory), {"flight_serpapi_key": "test-only", "flight_serpapi_monthly_budget": 2})
            task = planner.create_paused_plan(detail(), ["IUOAFA:2027-07-25:18:00"], "test:GroupMessage:1")
            outbound = {"departure_token": "fake-token", "flights": [{
                "departure_airport": {"id": "PVG", "time": "2027-07-23 08:00"},
                "arrival_airport": {"id": "NRT", "time": "2027-07-23 12:00"}, "flight_number": "JL 001"}]}
            incoming = {"flights": [{"departure_airport": {"id": "HND", "time": "2027-07-26 10:00"},
                "arrival_airport": {"id": "SHA", "time": "2027-07-26 12:00"}, "flight_number": "JL 002"}],
                "price": 1000, "type": "Round trip"}
            get = AsyncMock(side_effect=[
                {"best_flights": [outbound], "search_metadata": {"status": "Success"}},
                {"best_flights": [incoming], "search_metadata": {"status": "Success"}},
            ])
            with patch.object(planner, "_serpapi_get", get):
                quotes = await planner._fetch_serpapi(task, "PVG,SHA", "NRT,HND", "2027-07-23", "2027-07-26")
            self.assertEqual(len(quotes), 1)
            self.assertEqual(quotes[0]["return_flights"], ["JL 002"])
            self.assertEqual(get.await_count, 2)
            self.assertEqual(get.await_args_list[1].args[1]["departure_token"], "fake-token")

    async def test_disabled_zero_budget_and_cross_group_cache_with_independent_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"flight_providers": ["serpapi"], "flight_serpapi_key": "test-only",
                      "flight_serpapi_monthly_budget": 10, "flight_max_queries_per_cycle": 1}
            planner = FlightPlanner(Path(directory), config)
            first = planner.create_paused_plan(detail(), ["IUOAFA:2027-07-25:18:00"], "test:GroupMessage:1")
            second = planner.create_paused_plan(detail(), ["IUOAFA:2027-07-25:18:00"], "test:GroupMessage:2")
            calls = []

            async def fetch(task, origin, destination, outbound, returning):
                calls.append((origin, destination, outbound, returning))
                return [complete_quote(task)]

            self.assertEqual(await planner.check(first["id"], fetch), [])
            self.assertEqual(calls, [])
            planner.set_price(first["id"], first["umo"], 1200)
            planner.set_price(second["id"], second["umo"], 1200)
            planner.set_enabled(first["id"], first["umo"], True)
            planner.set_enabled(second["id"], second["umo"], True)
            await planner.check(first["id"], fetch)
            await planner.check(second["id"], fetch)
            self.assertEqual(len(calls), 1)
            sent = []

            async def sender(umo, task, quotes):
                sent.append((umo, len(quotes)))
                return True

            await planner.deliver(sender)
            self.assertEqual({umo for umo, _ in sent}, {first["umo"], second["umo"]})
            self.assertEqual(len(sent), 2)
            await planner.deliver(sender)
            self.assertEqual(len(sent), 2)

    async def test_round_trip_constraints_revision_pause_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FlightPlanner(Path(directory), {"flight_providers": ["serpapi"],
                "flight_serpapi_key": "test-only", "flight_serpapi_monthly_budget": 2})
            task = planner.create_paused_plan(detail(), ["IUOAFA:2027-07-25:18:00"], "test:GroupMessage:1")
            planner.set_price(task["id"], task["umo"], 1200)
            planner.set_enabled(task["id"], task["umo"], True)

            async def bad(task, *_):
                return [complete_quote(task, return_stops=1),
                        {**complete_quote(task), "return_flights": []}]

            self.assertEqual(await planner.check(task["id"], bad), [])
            async def good(task, *_): return [complete_quote(task)]
            # Advance the date/airport cursor so this is a new query, not the cached bad result.
            await planner.check(task["id"], good)
            attempts = []
            async def sender(umo, task, quotes):
                attempts.append(umo)
                return len(attempts) > 1
            await planner.deliver(sender)
            await planner.deliver(sender)
            self.assertEqual(len(attempts), 2)
            changed = detail()
            changed["performances"][2]["date"] = "2027-07-26"
            updated = planner.revalidate(planner.task(task["id"]), changed)
            self.assertFalse(updated["enabled"])
            self.assertEqual(updated["status"], "paused_event_changed")

    async def test_disabling_or_source_change_revokes_pending_before_send(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FlightPlanner(Path(directory), {"flight_providers": ["serpapi"],
                "flight_serpapi_key": "test-only", "flight_serpapi_monthly_budget": 10})
            task = planner.create_paused_plan(detail(), ["IUOAFA:2027-07-25:18:00"], "test:GroupMessage:1")
            planner.set_price(task["id"], task["umo"], 1200)
            planner.set_enabled(task["id"], task["umo"], True)

            async def fetch(task, *_): return [complete_quote(task)]
            await planner.check(task["id"], fetch)
            planner.set_enabled(task["id"], task["umo"], False)
            sender = AsyncMock(return_value=True)
            await planner.deliver(sender)
            sender.assert_not_awaited()

            planner.set_enabled(task["id"], task["umo"], True)
            await planner.check(task["id"], fetch)
            stale = detail(); stale["event"]["source_quality"] = "stale"
            planner.revalidate(planner.task(task["id"]), stale)
            await planner.deliver(sender)
            sender.assert_not_awaited()

    async def test_only_displayed_batch_is_marked_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FlightPlanner(Path(directory), {"flight_providers": ["serpapi"],
                "flight_serpapi_key": "test-only", "flight_serpapi_monthly_budget": 10})
            task = planner.create_paused_plan(detail(), ["IUOAFA:2027-07-25:18:00"], "test:GroupMessage:1")
            planner.set_price(task["id"], task["umo"], 1200)
            planner.set_enabled(task["id"], task["umo"], True)
            counter = 0

            async def fetch(task, *_):
                nonlocal counter
                counter += 1
                return [{**complete_quote(task), "outbound_flights": [f"JL {counter}{n}"]} for n in range(4)]

            await planner.check(task["id"], fetch)
            await planner.check(task["id"], fetch)
            batches = []
            async def sender(_umo, _task, quotes):
                batches.append(len(quotes))
                return True
            await planner.deliver(sender)
            with planner._connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM flight_sent").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM flight_pending").fetchone()[0], 3)
            await planner.deliver(sender)
            self.assertEqual(batches, [3, 3])

    async def test_lower_price_dedupe_uses_same_itinerary_and_only_sent_rows_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FlightPlanner(Path(directory), {"flight_providers": ["serpapi"],
                "flight_serpapi_key": "test-only", "flight_serpapi_monthly_budget": 10})
            task = planner.create_paused_plan(detail(), ["IUOAFA:2027-07-25:18:00"], "test:GroupMessage:1")
            planner.set_price(task["id"], task["umo"], 1200)
            planner.set_enabled(task["id"], task["umo"], True)
            sent = []

            async def sender(umo, task, quotes):
                sent.extend(quote["price"] for quote in quotes)
                return True

            for price in (1000, 980, 940):
                async def fetch(task, *_): return [complete_quote(task, price=price)]
                await planner.check(task["id"], fetch)
                await planner.deliver(sender)
            self.assertEqual(sent, [1000, 940])

    def test_explicit_budget_is_persistent_and_zero_blocks_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FlightPlanner(Path(directory))
            with self.assertRaisesRegex(ValueError, "预算"):
                planner._spend()
            planner.config["flight_serpapi_monthly_budget"] = 1
            planner._spend()
            reopened = FlightPlanner(Path(directory), {"flight_serpapi_monthly_budget": 1})
            with self.assertRaisesRegex(ValueError, "预算"):
                reopened._spend()

    def test_serpapi_requires_return_expansion_and_checked_baggage_is_not_assumed(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = FlightPlanner(Path(directory))
            task = planner.create_paused_plan(detail(), ["IUOAFA:2027-07-25:18:00"], "test:GroupMessage:1")
            outbound = {"flights": [{"departure_airport": {"id": "PVG", "time": "2027-07-23 08:00"},
                                     "arrival_airport": {"id": "NRT", "time": "2027-07-23 12:00"}, "flight_number": "JL 001"}]}
            return_data = {"best_flights": [{"flights": [{"departure_airport": {"id": "HND", "time": "2027-07-26 10:00"},
                                     "arrival_airport": {"id": "SHA", "time": "2027-07-26 12:00"}, "flight_number": "JL 002"}],
                                     "price": 1000, "type": "Round trip"}]}
            quotes = planner._parse_serpapi(return_data, task, "PVG,SHA", "NRT,HND", "2027-07-23", "2027-07-26", outbound)
            self.assertEqual(len(quotes), 1)
            task["target_price"] = 1200
            self.assertTrue(planner._eligible_quote(task, quotes[0]))
            task["baggage_requirement"] = "checked"
            self.assertFalse(planner._eligible_quote(task, quotes[0]))
