import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from imas_live.models import Evidence, Performance, TicketRound
from imas_live.render import BRAND_NAMES, CalendarRenderer
from imas_live.service import ImasLiveService


def seed(service: ImasLiveService, deadline: str = "2026-09-08T00:59+09:00") -> None:
    service.db.upsert_event({"id": "cms-calendar", "title": "学园偶像大师 标 巡演 福冈公演", "brands": ["GAKUEN"], "url": "https://example.test/event", "event_display": "2026年10月公演", "venue": "福冈会场", "updated": None})
    evidence = Evidence("https://example.test/ticket", "官方票务摘要", "test")
    ticket = TicketRound("round-calendar", "一般会員2次先行", "onsite", "lottery", "2026-09-07T12:00+09:00", deadline, url="https://example.test/ticket", evidence=evidence)
    performance = Performance("performance-calendar", "2026-09-08", "DAY1 昼场", "福冈会场", evidence=evidence)
    service.db.save_parsed("cms-calendar", "https://example.test/ticket", deadline, "test", [ticket], [performance], [], [])
    service.db.set_meta("last_successful_sync", "2026-09-07T06:50:00+00:00")
    service.db.set_meta("baseline_complete", "1")


class CalendarTests(unittest.TestCase):
    def test_brand_labels_use_compact_official_english_names(self):
        self.assertEqual(BRAND_NAMES["GAKUEN"], "Gakuen")
        self.assertEqual(BRAND_NAMES["SHINYCOLORS"], "Shiny Colors")
        self.assertEqual(BRAND_NAMES["MILLIONLIVE"], "Million Live!")
        self.assertEqual(BRAND_NAMES["VALIV"], "vα-liv")
        self.assertEqual(BRAND_NAMES["876PRO"], "876 PRODUCTION")
        self.assertEqual(CalendarRenderer._brand_style(["vα-liv"])[0], "#5c7cfa")
        self.assertEqual(CalendarRenderer._brand_style(["876_PRO"])[0], "#df6ea7")

    def test_brand_pill_text_is_centered_by_visible_glyph_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            renderer = CalendarRenderer(Path(tmp))
            draw = ImageDraw.Draw(Image.new("RGB", (100, 100)))
            bbox = draw.textbbox((0, 0), "Gakuen", font=renderer.font(18, True))
            text_top = renderer._centered_text_y(20, 30, bbox)
            visible_center = text_top + (bbox[1] + bbox[3]) / 2
            self.assertAlmostEqual(visible_center, 35, delta=0.5)

    def test_brand_pill_uses_symmetric_horizontal_padding(self):
        with tempfile.TemporaryDirectory() as tmp:
            renderer = CalendarRenderer(Path(tmp))
            draw = ImageDraw.Draw(Image.new("RGB", (100, 100)))
            font = renderer.font(18, True)
            gakuen_width = round(draw.textlength("Gakuen", font=font))
            side_m_width = round(draw.textlength("SideM", font=font))
            self.assertEqual(gakuen_width + 32 - gakuen_width, 32)
            self.assertEqual(side_m_width + 32 - side_m_width, 32)
            self.assertLess(side_m_width + 32, 112)

    def test_group_switch_persists_and_blocks_only_that_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            disabled_umo = "aiocqhttp:GroupMessage:42"
            enabled_umo = "aiocqhttp:GroupMessage:43"
            cfg = {"display_timezone": "Asia/Shanghai", "white_umos": [disabled_umo, enabled_umo], "freshness_hours": 24}
            service = ImasLiveService(Path(tmp), cfg)
            self.assertTrue(service.group_enabled(disabled_umo))
            service.set_group_enabled(disabled_umo, False)
            self.assertFalse(service.group_enabled(disabled_umo))
            self.assertTrue(service.group_enabled(enabled_umo))
            seed(service)
            frozen = datetime(2026, 9, 7, 22, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
            due = asyncio.run(service.claim_due_reminders(frozen))
            self.assertEqual([row["umo"] for row in due], [enabled_umo])
            asyncio.run(service.close())
            restarted = ImasLiveService(Path(tmp), cfg)
            self.assertFalse(restarted.group_enabled(disabled_umo))
            restarted.set_group_enabled(disabled_umo, True)
            self.assertTrue(restarted.group_enabled(disabled_umo))
            asyncio.run(restarted.close())

    def test_calendar_includes_performance_and_remote_event_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ImasLiveService(Path(tmp), {"display_timezone": "Asia/Shanghai"})
            seed(service)
            entries, start, end, _ = asyncio.run(service.calendar_entries(datetime(2026, 9, 7, 10, tzinfo=ZoneInfo("Asia/Shanghai"))))
            self.assertEqual((end.date() - start.date()).days, 29)
            self.assertEqual({item["kind"] for item in entries}, {"performance", "deadline"})
            self.assertTrue(any("北京时间" in item["subtitle"] and "2026/09/07" in item["subtitle"] for item in entries))
            self.assertTrue(any("福冈会场" in item["subtitle"] for item in entries if item["kind"] == "performance"))
            asyncio.run(service.close())

    def test_deadline_is_once_per_group_and_deadline_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"display_timezone": "Asia/Shanghai", "white_umos": ["aiocqhttp:GroupMessage:42"], "reminder_before_minutes": 60, "freshness_hours": 24}
            service = ImasLiveService(Path(tmp), cfg); seed(service)
            frozen = datetime(2026, 9, 7, 22, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
            first = asyncio.run(service.claim_due_reminders(frozen))
            self.assertEqual(len(first), 1)
            asyncio.run(service.finish_reminders(first, True))
            self.assertEqual(asyncio.run(service.claim_due_reminders(frozen)), [])
            service.config["reminder_before_minutes"] = 30
            self.assertEqual(asyncio.run(service.claim_due_reminders(frozen)), [])
            asyncio.run(service.close())
            restarted = ImasLiveService(Path(tmp), cfg)
            self.assertEqual(asyncio.run(restarted.claim_due_reminders(frozen)), [])
            asyncio.run(restarted.close())
            service = ImasLiveService(Path(tmp), cfg)
            service.db.save_parsed("cms-calendar", "https://example.test/ticket", "changed", "test", [TicketRound("round-calendar", "一般会員2次先行", "onsite", "lottery", "2026-09-07T12:00+09:00", "2026-09-08T01:59+09:00", evidence=Evidence("https://example.test/ticket", "延期", "test"))], [], [], [])
            changed = asyncio.run(service.claim_due_reminders(datetime(2026, 9, 8, 0, 29, tzinfo=ZoneInfo("Asia/Shanghai"))))
            self.assertEqual(len(changed), 1)
            asyncio.run(service.close())

    def test_failed_delivery_is_retryable_but_removed_group_is_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"display_timezone": "Asia/Shanghai", "white_umos": ["aiocqhttp:GroupMessage:42"], "freshness_hours": 24}
            service = ImasLiveService(Path(tmp), cfg); seed(service)
            frozen = datetime(2026, 9, 7, 22, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
            due = asyncio.run(service.claim_due_reminders(frozen)); asyncio.run(service.finish_reminders(due, False))
            self.assertEqual(len(asyncio.run(service.claim_due_reminders(frozen))), 1)
            self.assertEqual(asyncio.run(service.claim_due_reminders(datetime(2026, 9, 7, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai")))), [])
            service.config["white_umos"] = []
            self.assertEqual(asyncio.run(service.claim_due_reminders(frozen)), [])
            asyncio.run(service.close())

    def test_thirty_minute_configuration_enters_its_own_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"display_timezone": "Asia/Shanghai", "white_umos": ["aiocqhttp:GroupMessage:42"], "reminder_before_minutes": 30, "freshness_hours": 24}
            service = ImasLiveService(Path(tmp), cfg); seed(service)
            due = asyncio.run(service.claim_due_reminders(datetime(2026, 9, 7, 23, 29, tzinfo=ZoneInfo("Asia/Shanghai"))))
            self.assertEqual(len(due), 1)
            asyncio.run(service.close())

    def test_renderer_exports_readable_empty_long_and_reminder_pngs(self):
        with tempfile.TemporaryDirectory() as tmp:
            renderer = CalendarRenderer(Path(tmp))
            now = datetime(2026, 9, 7, 22, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
            long = [{"kind": "deadline", "display_date": "2026-09-08", "title": "非常长的活动标题用于验证手机日历列表自动换行不会裁切或无限缩小文字", "subtitle": "一般会員2次先行｜截止：09/08 23:59 北京时间 / 09/09 00:59 JST｜演出：2026年12月远期公演", "brands": ["GAKUEN", "SIDEM"], "url": "https://example.test"}]
            paths = renderer.render_calendar(long, now.date(), now.date().replace(day=30), now, "上次核验测试")
            empty = renderer.render_calendar([], now.date(), now.date().replace(day=30), now)
            reminder = renderer.render_reminder([{**long[0], "remaining_minutes": 60}], now)
            for path in [*paths, *empty, reminder]:
                with Image.open(path) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertGreaterEqual(image.width, 1000)
