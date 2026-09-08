"""Deterministic, local PNG rendering for the group calendar and deadline cards."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import re
import time
import uuid
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

BRAND_COLORS = {
    "IDOLMASTER": "#f05a7e", "CINDERELLAGIRLS": "#2f7fd3",
    "MILLIONLIVE": "#f2b84b", "SIDEM": "#1aa982", "SHINYCOLORS": "#5cc8f2",
    "GAKUEN": "#f08a33", "VALIV": "#5c7cfa", "876PRO": "#df6ea7",
}
BRAND_NAMES = {
    "IDOLMASTER": "765PRO", "CINDERELLAGIRLS": "Cinderella", "MILLIONLIVE": "Million Live!",
    "SIDEM": "SideM", "SHINYCOLORS": "Shiny Colors", "GAKUEN": "Gakuen",
    "VALIV": "vα-liv", "876PRO": "876 PRODUCTION",
}
BRAND_ALIASES = {
    "theidolmaster": "IDOLMASTER", "idolmaster": "IDOLMASTER", "765pro": "IDOLMASTER",
    "cinderellagirls": "CINDERELLAGIRLS", "millionlive": "MILLIONLIVE", "sidem": "SIDEM",
    "shinycolors": "SHINYCOLORS", "gakuen": "GAKUEN", "gakuenidolmaster": "GAKUEN",
    "valiv": "VALIV", "876": "876PRO", "876pro": "876PRO", "876production": "876PRO",
}
CONTRACT_COLOR = "#ec6fa7"
NEUTRAL = "#7b8794"


class CalendarRenderer:
    width = 1080
    margin = 48
    header_height = 194
    footer_height = 32

    def __init__(self, output_dir: Path, font_path: str = ""):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.font_path = self._font_path(font_path)

    @staticmethod
    def _font_path(configured: str) -> str:
        candidates = [configured, r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\YuGothR.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
        return ""

    def font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        path = self.font_path
        if bold and path.lower().endswith("msyh.ttc") and Path(r"C:\Windows\Fonts\msyhbd.ttc").is_file():
            path = r"C:\Windows\Fonts\msyhbd.ttc"
        try:
            
            if not path:
                raise RuntimeError('找不到中日文字体，请安装 fonts-noto-cjk 或配置 font_path。')
            return ImageFont.truetype(path, size)
        except OSError:
            raise RuntimeError('字体无法加载，请检查 font_path。')

    def _wrap(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
        lines: list[str] = []
        current = ""
        for char in text:
            if char == "\n":
                lines.append(current); current = ""; continue
            if current and draw.textlength(current + char, font=font) > max_width:
                lines.append(current); current = char
            else:
                current += char
        if current or not lines:
            lines.append(current)
        return lines

    @staticmethod
    def _brand_code(value: str) -> str:
        token = re.sub(r"[\s_-]", "", value.casefold().replace("α", "a"))
        return BRAND_ALIASES.get(token, value.strip().upper())

    @staticmethod
    def _centered_text_y(top: int, height: int, bbox: tuple[int, int, int, int]) -> int:
        """Place visible glyphs, not the font's ascent box, at the pill's vertical center."""
        return round(top + (height - (bbox[3] - bbox[1])) / 2 - bbox[1])

    @staticmethod
    def _brand_style(brands: Iterable[str]) -> tuple[str, str, bool]:
        values = list(dict.fromkeys(CalendarRenderer._brand_code(str(x)) for x in brands if x))
        if len(set(values) & set(BRAND_COLORS)) > 1:
            labels = " / ".join(BRAND_NAMES.get(x, x) for x in values)
            return CONTRACT_COLOR, f"CROSS · {labels}", True
        if values:
            code = values[0]
            return BRAND_COLORS.get(code, NEUTRAL), BRAND_NAMES.get(code, code), False
        return NEUTRAL, "UNVERIFIED", False

    def _card_layout(self, entry: dict[str, Any], reminder: bool = False) -> dict:
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        color, label, _ = self._brand_style(entry["brands"])
        width = self.width - 180
        labels = self._wrap(draw, label, self.font(20, True), width - 270)
        title = self._wrap(draw, entry["title"], self.font(28, True), width)
        deadline = entry.get("kind") == "deadline" or reminder
        subtitle = entry["subtitle"]
        if reminder:
            subtitle += f"\n剩余约 {entry['remaining_minutes']} 分钟"
        sub = self._wrap(draw, subtitle, self.font(22), width)
        return {"color": color, "labels": labels, "title": title, "sub": sub, "deadline": deadline,
                "height": 48 + 30 * len(labels) + 39 * len(title) + 31 * len(sub)}

    def _draw_card(self, draw, card: dict, y: int) -> None:
        left, right = self.margin, self.width - self.margin
        deadline = card['deadline']
        draw.rounded_rectangle((left, y, right, y + card["height"]), radius=18,
                               fill='#fff3f3' if deadline else 'white', outline='#e4b4be' if deadline else '#e3e8f1')
        draw.rounded_rectangle((left, y, left + 14, y + card["height"]), radius=7, fill=card["color"])
        badge = '抽票截止 · 非演出日' if deadline else 'LIVE 演出'
        font = self.font(21, True)
        badge_width = round(draw.textlength(badge, font=font)) + 28
        draw.rounded_rectangle((right - badge_width - 24, y + 18, right - 24, y + 50), radius=8,
                               fill='#b93863' if deadline else '#e9eff7')
        draw.text((right - badge_width - 10, self._centered_text_y(y + 18, 32, draw.textbbox((0, 0), badge, font=font))),
                  badge, font=font, fill='white' if deadline else '#263651')
        cursor = y + 20
        for label in card["labels"]:
            font = self.font(20, True)
            text_width = draw.textlength(label, font=font)
            draw.rounded_rectangle((left + 30, cursor, left + 62 + text_width, cursor + 28), radius=12, fill=card["color"])
            text_y = self._centered_text_y(cursor, 28, draw.textbbox((0, 0), label, font=font))
            draw.text((left + 46, text_y), label, font=font, fill="#172033")
            cursor += 30
        cursor += 8
        for line in card["title"]:
            draw.text((left + 32, cursor), line, font=self.font(28, True), fill="#1c2940")
            cursor += 39
        for line in card["sub"]:
            draw.text((left + 32, cursor), line, font=self.font(22), fill="#55647b")
            cursor += 31

    def _save(self, image: Image.Image, prefix: str) -> Path:
        # Unique names prevent two simultaneous group queries overwriting each other.
        path = self.output_dir / f"{prefix}-{uuid.uuid4().hex}.png"
        image.save(path, "PNG", optimize=True)
        # Only our own aged render files are eligible for cleanup.
        for old in self.output_dir.glob("*.png"):
            try:
                if old.name.startswith(("calendar-", "deadline-")) and time.time() - old.stat().st_mtime > 7 * 86400:
                    old.unlink()
            except OSError:
                pass
        return path

    def render_calendar(self, entries: list[dict[str, Any]], start: date, end: date, generated_at: datetime, status: str = "") -> list[Path]:
        # One measured canvas keeps the complete 30-day window in a single image.
        cards = [(entry['display_date'], self._card_layout(entry))
                 for entry in sorted(entries, key=lambda row: row['display_date'])]
        height = max(620, self.header_height + 36 + sum(
            card['height'] + 16 + (46 if i == 0 or cards[i-1][0] != day else 0)
            for i, (day, card) in enumerate(cards)) + 110)
        image = Image.new('RGB', (self.width, height), '#f5f7fb')
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, self.width, self.header_height), fill='#172033')
        draw.text((48, 28), 'IMAS LIVE！未来30天', font=self.font(42, True), fill='white')
        zone = '北京时间' if str(generated_at.tzinfo) == 'Asia/Shanghai' else str(generated_at.tzinfo)
        draw.text((48, 92), f'{zone} {start:%Y.%m.%d} — {end:%Y.%m.%d}', font=self.font(26), fill='#c8d3e6')
        performance_count = sum(not card['deadline'] for _, card in cards)
        deadline_count = len(cards) - performance_count
        draw.text((48, 145), f'完整日历 · LIVE 演出 {performance_count} 条 · 抽票截止 {deadline_count} 条',
                  font=self.font(22), fill='#c8d3e6')
        y = self.header_height + 28
        if not cards:
            message = '数据尚未同步完成，请稍后再发送 /imaslive。' if '尚未同步' in status else '这 30 天内暂无已收录的演出或抽票截止。'
            draw.text((48, 276), message, font=self.font(30, True), fill='#25324a')
        last_day = None
        for day, card in cards:
            value = date.fromisoformat(day)
            weekday = '一二三四五六日'[value.weekday()]
            marker = '今天' if value == start else ''
            if day != last_day:
                draw.text((48, y), f'{value:%m/%d} 周{weekday}  {marker}', font=self.font(30, True), fill='#263651')
                y += 46
            last_day = day
            self._draw_card(draw, card, y)
            y += card['height'] + 16
        for number, line in enumerate(self._wrap(draw, status or '尚未同步', self.font(19), self.width - 96)[:2]):
            draw.text((48, height - 86 + 27 * number), line, font=self.font(19), fill='#657187')
        draw.text((48, height - 30), '仅统计已收录信息 · 预告场次以官网为准', font=self.font(18), fill='#657187')
        return [self._save(image, 'calendar')]

    def render_reminder(self, rows: list[dict[str, Any]], generated_at: datetime) -> Path:
        cards = [self._card_layout(row, True) for row in rows]
        height = max(430, 166 + sum(card["height"] + 18 for card in cards) + 55)
        image = Image.new("RGB", (self.width, height), "#fff6f9")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, self.width, 112), fill="#b93863")
        draw.text((48, 28), "IMAS 现场抽票即将截止", font=self.font(42, True), fill="white")
        y = 138
        for card in cards:
            self._draw_card(draw, card, y)
            y += card["height"] + 18
        draw.text((48, height - 45), f"生成于 {generated_at:%Y/%m/%d %H:%M} · 请核对官方申请页面", font=self.font(20), fill="#634b58")
        return self._save(image, "deadline")
