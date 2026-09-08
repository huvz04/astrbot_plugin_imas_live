import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from imas_live.models import Evidence, Performance, TicketRound
from imas_live.render import CalendarRenderer
from imas_live.service import ImasLiveService
from PIL import Image


ZONE = ZoneInfo("Asia/Shanghai")
CURRENT = datetime(2026, 9, 8, 0, 0, tzinfo=ZONE)


def add_event(service, event_id, performances=(), tickets=(), source_quality="verified"):
    source = f"https://example.test/{event_id}"
    service.db.upsert_event({"id": event_id, "title": f"{event_id} LIVE", "brands": ["GAKUEN"],
                             "url": source, "event_display": None, "venue": "测试会场", "updated": None})
    evidence = Evidence(source, "official", "test")
    service.db.save_parsed(event_id, source, event_id, "test", tickets, performances, [], [])
    with service.db._connect() as db:
        db.execute("UPDATE sources SET fetched_at=?,quality=? WHERE event_id=?", ("2026-09-07T18:00:00+00:00", source_quality, event_id))


class QueryTests(unittest.TestCase):
    def test_month_window_rules_and_leap_year(self):
        current = datetime(2026, 9, 8, 13, tzinfo=ZONE)
        for month, start, end, title in (
            (10, "2026-10-01", "2026-11-01", "2026年10月 LIVE"),
            (1, "2027-01-01", "2027-02-01", "2027年1月 LIVE"),
            (9, "2026-09-01", "2026-10-01", "2026年9月 LIVE"),
        ):
            actual_start, actual_end, actual_title = ImasLiveService._month_window(current, month)
            self.assertEqual(str(actual_start.date()), start)
            self.assertEqual(str(actual_end.date()), end)
            self.assertEqual(actual_title, title)
        december = datetime(2026, 12, 3, tzinfo=ZONE)
        self.assertEqual(str(ImasLiveService._month_window(december, 1)[0].date()), "2027-01-01")
        january = datetime(2026, 1, 3, tzinfo=ZONE)
        self.assertEqual(str(ImasLiveService._month_window(january, 12)[0].date()), "2026-12-01")
        leap = datetime(2028, 1, 3, tzinfo=ZONE)
        self.assertEqual(str(ImasLiveService._month_window(leap, 2)[1].date()), "2028-03-01")
        rolling_start, rolling_end, _ = ImasLiveService._month_window(current, None)
        self.assertEqual((rolling_end - rolling_start).days, 30)

    def test_live_month_boundaries_exclude_tickets(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImasLiveService(Path(directory))
            evidence = Evidence("https://example.test/live", "official", "test")
            performances = [
                Performance("past", "2026-09-01", "DAY0", "会场", evidence=evidence),
                Performance("today", "2026-09-08", "DAY1", "会场", evidence=evidence),
                Performance("last", "2026-10-07", "DAY2", "会场", evidence=evidence),
                Performance("outside", "2026-10-08", "DAY3", "会场", evidence=evidence),
            ]
            ticket = TicketRound("ticket", "抽选", "onsite", "lottery", "2026-09-01T12:00+09:00", "2026-09-30T23:59+09:00", evidence=evidence)
            add_event(service, "range", performances, [ticket])
            rows, start, end, _, _ = asyncio.run(service.calendar_entries(CURRENT))
            self.assertEqual([row["display_date"] for row in rows], ["2026-09-08", "2026-10-07"])
            month_rows, month_start, month_end, _, title = asyncio.run(service.calendar_entries(CURRENT, 9))
            self.assertEqual([row["display_date"] for row in month_rows], ["2026-09-01", "2026-09-08"])
            self.assertEqual((str(month_start.date()), str(month_end.date()), title), ("2026-09-01", "2026-10-01", "2026年9月 LIVE"))
            asyncio.run(service.close())

    def test_ticket_action_window_statuses_and_source_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ImasLiveService(Path(directory), {"freshness_hours": 24})
            evidence = Evidence("https://example.test/ticket", "official", "test")
            tickets = [
                TicketRound("open-long", "开放超过窗口", "onsite", "lottery", "2026-09-01T00:00+09:00", "2026-11-01T23:59+09:00", evidence=evidence),
                TicketRound("urgent", "恰好24小时", "onsite", "lottery", "2026-09-01T00:00+09:00", "2026-09-09T01:00+09:00", evidence=evidence),
                TicketRound("upcoming", "窗口内即将开始", "onsite", "lottery", "2026-09-20T12:00+09:00", "2026-09-22T23:59+09:00", evidence=evidence),
                TicketRound("not-open", "尚未开始不能标开放", "onsite", "lottery", "2026-09-08T12:00+09:00", "2026-09-08T23:00+09:00", evidence=evidence),
                TicketRound("missing-start", "开始待核验", "onsite", "lottery", None, "2026-09-12T23:59+09:00", evidence=evidence),
                TicketRound("outside", "窗口外才开始", "onsite", "lottery", "2026-10-08T00:00+08:00", "2026-10-12T00:00+08:00", evidence=evidence),
                TicketRound("expired", "已经截止", "onsite", "lottery", "2026-09-01T00:00+09:00", "2026-09-08T00:00+09:00", evidence=evidence),
            ]
            add_event(service, "tickets", (), tickets)
            stale = TicketRound("stale", "陈旧缓存", "onsite", "lottery", "2026-09-01T00:00+09:00", "2026-09-20T23:59+09:00", evidence=evidence)
            add_event(service, "stale", (), [stale], source_quality="stale")
            entries, _, _, _ = asyncio.run(service.ticket_entries(CURRENT))
            by_round = {entry["subtitle"].splitlines()[0]: entry for entry in entries}
            self.assertEqual(by_round["轮次：恰好24小时"]["ticket_status"], "urgent")
            self.assertEqual(by_round["轮次：开放超过窗口"]["ticket_status"], "open")
            self.assertEqual(by_round["轮次：窗口内即将开始"]["ticket_status"], "upcoming")
            self.assertEqual(by_round["轮次：尚未开始不能标开放"]["ticket_status"], "upcoming")
            self.assertEqual(by_round["轮次：开始待核验"]["ticket_status"], "unknown")
            self.assertEqual(by_round["轮次：陈旧缓存"]["ticket_status"], "stale")
            self.assertNotIn("轮次：窗口外才开始", by_round)
            self.assertNotIn("轮次：已经截止", by_round)
            self.assertIn("北京时间", by_round["轮次：恰好24小时"]["subtitle"])
            self.assertIn("JST", by_round["轮次：恰好24小时"]["subtitle"])
            asyncio.run(service.close())

    def test_ticket_renderer_keeps_all_statuses_in_one_long_png(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = CalendarRenderer(Path(directory))
            entries = [
                {"kind": "ticket", "title": "urgent", "subtitle": "轮次：urgent\n截止：2026/09/09", "brands": ["GAKUEN"], "ticket_status": "urgent", "status_label": "24小时内截止"},
                {"kind": "ticket", "title": "open", "subtitle": "轮次：open\n截止：2026/10/01", "brands": ["SIDEM"], "ticket_status": "open", "status_label": "正在抽选"},
                {"kind": "ticket", "title": "upcoming", "subtitle": "轮次：upcoming\n开始：2026/09/20", "brands": ["SHINYCOLORS"], "ticket_status": "upcoming", "status_label": "即将开始"},
                {"kind": "ticket", "title": "unknown", "subtitle": "轮次：unknown\n截止：2026/09/12", "brands": ["876_PRO"], "ticket_status": "unknown", "status_label": "开放状态待核验"},
            ]
            path = renderer.render_ticket(entries, CURRENT.date(), (CURRENT + timedelta(days=30)).date(), CURRENT, "目录更新")
            with Image.open(path) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, 1080)
                self.assertGreater(image.height, 900)
                self.assertEqual(image.getpixel((250, 240)), (255, 243, 243))
                self.assertEqual(image.getpixel((250, 445)), (239, 250, 242))
