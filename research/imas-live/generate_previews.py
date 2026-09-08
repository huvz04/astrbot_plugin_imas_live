"""Generate deterministic review PNGs for the IM@S LIVE plugin."""
from datetime import datetime, timedelta
from pathlib import Path
from shutil import copyfile
from zoneinfo import ZoneInfo
import sys

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "astrbot_plugin_imas_live"))
from imas_live.render import CalendarRenderer  # noqa: E402

zone = ZoneInfo("Asia/Shanghai")
now = datetime(2026, 9, 8, 10, 0, tzinfo=zone)
preview = ROOT / "astrbot_plugin_imas_live" / "previews"
renderer = CalendarRenderer(preview / "_generated")

live = [
    {"kind": "performance", "display_date": "2026-09-12", "title": "THE IDOLM@STER SideM 11th STAGE",
     "subtitle": "DAY1｜开演 16:30 北京时间｜会场待核验", "brands": ["SIDEM"], "public_number": 1},
    {"kind": "performance", "display_date": "2026-09-27", "title": "THE IDOLM@STER SHINY COLORS",
     "subtitle": "DAY2｜开演时间待公布/待核验｜会场待核验", "brands": ["SHINYCOLORS"], "public_number": 2},
]
copyfile(renderer.render_calendar(live, now.date(), now.date() + timedelta(days=30), now, "目录更新 2026/09/08 10:00", "IM@S LIVE! · Next 30 Days")[0], preview / "live-default.png")
copyfile(renderer.render_calendar(live, datetime(2026, 10, 1, tzinfo=zone).date(), datetime(2026, 11, 1, tzinfo=zone).date(), now, "目录更新 2026/09/08 10:00", "IM@S LIVE! · October 2026")[0], preview / "live-october.png")

tickets = [
    {"kind": "ticket", "title": "学园偶像大师 LIVE TOUR -标- 福冈公演", "subtitle": "轮次：一般会員2次先行\n截止：2026/09/08 22:59 北京时间 / 2026/09/08 23:59 JST",
     "brands": ["GAKUEN"], "ticket_status": "urgent", "status_label": "24小时内截止", "public_number": 3},
    {"kind": "ticket", "title": "THE IDOLM@STER MILLION LIVE! 14thLIVE", "subtitle": "轮次：会员先行\n截止：2026/10/20 22:59 北京时间 / 2026/10/20 23:59 JST",
     "brands": ["MILLIONLIVE"], "ticket_status": "open", "status_label": "正在抽选", "public_number": 4},
]
copyfile(renderer.render_ticket(tickets, now.date(), now.date() + timedelta(days=30), now, "目录更新 2026/09/08 10:00"), preview / "ticket-statuses.png")
copyfile(renderer.render_ticket([], now.date(), now.date() + timedelta(days=30), now, "目录更新 2026/09/08 10:00"), preview / "ticket-empty.png")
