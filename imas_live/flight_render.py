"""Small local PNG card for verified round-trip fare alerts."""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from .render import CalendarRenderer


class FlightRenderer:
    def __init__(self, directory: Path, font_path: str = ""):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.base = CalendarRenderer(directory, font_path)

    def render(self, task: dict[str, Any], quotes: list[dict[str, Any]]) -> Path:
        font = {size: self.base.font(size, size >= 30) for size in (18, 22, 28, 36)}
        height = 230 + 178 * max(1, len(quotes))
        image = Image.new("RGB", (1080, height), "#f2f5f0")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((32, 24, 1048, 190), radius=24, fill="#193d32")
        heading = f"#{task['event_number']} · 上海 ⇄ 东京机票" if task.get("event_number") else "上海 ⇄ 东京机票"
        draw.text((64, 48), heading, font=font[22], fill="#b8d3bd")
        title = str(task.get("event_title", "IM@S LIVE"))
        if draw.textlength(title, font=font[28]) > 946:
            while title and draw.textlength(title + "…", font=font[28]) > 946:
                title = title[:-1]
            title += "…"
        draw.text((64, 82), title, font=font[28], fill="white")
        draw.text((64, 132), "仅展示已返回去回两段的候选；价格、税费和行李以结果页为准", font=font[18], fill="#dce9df")
        y = 214
        if not quotes:
            draw.text((64, y + 24), "暂无达到心理价的完整往返报价", font=font[28], fill="#193d32")
        for quote in quotes[:3]:
            draw.rounded_rectangle((32, y, 1048, y + 154), radius=18, fill="white")
            draw.text((64, y + 18), f"CNY {float(quote['price']):,.0f}", font=font[36], fill="#193d32")
            draw.text((64, y + 70), f"去程 {quote['origin']} → {quote['destination']} · {quote['departure']} 出发 / {quote['arrival_date']} 抵达", font=font[22], fill="#38493f")
            draw.text((64, y + 106), f"返程 {quote['return_origin']} → {quote['return_destination']} · {quote['return_departure_date']} 离开 · {' / '.join(quote['outbound_flights'] + quote['return_flights'])}", font=font[18], fill="#687469")
            y += 172
        stamps = [float(quote["source_at"]) for quote in quotes if quote.get("source_at") is not None]
        update = (f"update time {datetime.fromtimestamp(min(stamps), ZoneInfo('Asia/Shanghai')):%Y.%m.%d %H:%M}"
                  if len(stamps) == len(quotes) and stamps else "update time --")
        draw.text((40, height - 34), update, font=font[18], fill="#687469")
        path = self.directory / f"flight-{uuid.uuid4().hex}.png"
        image.save(path)
        return path
