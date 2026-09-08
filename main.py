"""IM@S performance calendars, ticket-query cards, and one-shot deadline reminders."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .imas_live.render import CalendarRenderer
from .imas_live.service import ImasLiveService

PLUGIN_NAME = "astrbot_plugin_imas_live"
REMINDER_CHECK_SECONDS = 5 * 60


def parse_live_month(argument: str) -> int | None:
    """Accept one integer month only; None means the rolling 30-day view."""
    parts = argument.split()
    if not parts:
        return None
    if len(parts) != 1 or not re.fullmatch(r"\d+", parts[0]):
        raise ValueError
    month = int(parts[0])
    if not 1 <= month <= 12:
        raise ValueError
    return month


class ImasLivePlugin(Star):
    """Keep group interaction deliberately small: only /imaslive returns PNG pages."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.context = context
        self.config = config if config is not None else {}
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.service = ImasLiveService(self.data_dir, self.config)
        self.renderer = CalendarRenderer(self.data_dir / "rendered", str(self.config.get("font_path", "")))
        self._sync_task: asyncio.Task | None = None
        self._reminder_task: asyncio.Task | None = None
        self._last_directory = 0.0

    async def initialize(self):
        """Runs both on boot and WebUI install/reload; the global loaded event does not."""
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = asyncio.create_task(self._sync_loop())
        if self._reminder_task is None or self._reminder_task.done():
            self._reminder_task = asyncio.create_task(self._reminder_loop())

    async def _sync_loop(self) -> None:
        while True:
            try:
                interval = max(15, int(self.config.get("sync_interval_minutes", 60)))
                directory_seconds = max(1, int(self.config.get("directory_interval_hours", 6))) * 3600
                full = self._last_directory == 0.0 or self.service.db.meta("baseline_complete") is None or time.monotonic() - self._last_directory >= directory_seconds
                if self.config.get("enabled", True):
                    result = await self.service.sync(full_directory=full)
                    if full and result.get("status") == "ok":
                        self._last_directory = time.monotonic()
                    logger.info(f"IM@S live sync: {result}")
                await asyncio.sleep(interval * 60)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("IM@S live sync cycle failed")
                await asyncio.sleep(60)

    async def _reminder_loop(self) -> None:
        """Check cached deadlines every five minutes, independently of website syncs."""
        while True:
            try:
                due = await self.service.claim_due_reminders()
                by_umo: dict[str, list[dict]] = defaultdict(list)
                for row in due:
                    by_umo[row["umo"]].append(row)
                for umo, rows in by_umo.items():
                    for offset in range(0, len(rows), 3):
                        batch = rows[offset:offset+3]
                        try:
                            image = await asyncio.to_thread(self.renderer.render_reminder, batch, datetime.now(ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))))
                            links = "\n".join(f"官方抽票地址：{url}" for url in dict.fromkeys(row["url"] for row in batch))
                            message = MessageChain().message("抽票截止提醒\n" + links).file_image(str(image))
                            ok = bool(await self.context.send_message(umo, message))
                        except Exception:
                            ok = False
                            logger.exception("IM@S deadline image delivery failed")
                        await self.service.finish_reminders(batch, ok)
                await asyncio.sleep(REMINDER_CHECK_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("IM@S deadline cycle failed")
                await asyncio.sleep(REMINDER_CHECK_SECONDS)

    async def _wait_for_first_directory(self, event: AstrMessageEvent) -> None:
        await self.initialize()
        if not self.service.db.meta('last_directory_sync') and self.config.get('enabled', True):
            yield event.plain_result('正在首次同步官网活动，稍候生成日历。')
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.service.directory_ready.wait(), timeout=40)

    @filter.command("imaslive")
    async def imaslive(self, event: AstrMessageEvent, month_argument: str = ""):
        """显示未来30天或指定完整自然月的演出长图。"""
        event.stop_event()
        try:
            try:
                month = parse_live_month(month_argument)
            except ValueError:
                yield event.plain_result('用法：/imaslive 或 /imaslive 1—12')
                return
            async for response in self._wait_for_first_directory(event):
                yield response
            entries, start, end, status, title = await self.service.calendar_entries(month=month)
            now = datetime.now(ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai"))))
            images = await asyncio.to_thread(self.renderer.render_calendar, entries, start.date(), end.date(), now, status, title)
            for image in images:
                yield event.image_result(str(image))
        except Exception:
            logger.exception("IM@S calendar image rendering failed")
            yield event.plain_result("日历图片生成失败；请检查插件字体配置和日志。")

    @filter.command("imasticket")
    async def imasticket(self, event: AstrMessageEvent):
        """显示当前开放与未来30天即将开放的现场抽选。"""
        event.stop_event()
        try:
            async for response in self._wait_for_first_directory(event):
                yield response
            entries, start, end, status = await self.service.ticket_entries()
            now = datetime.now(ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai"))))
            image = await asyncio.to_thread(self.renderer.render_ticket, entries, start.date(), end.date(), now, status)
            links: list[str] = []
            seen: set[str] = set()
            for entry in entries:
                url = str(entry.get('url') or '')
                if url and url not in seen:
                    seen.add(url)
                    links.append(f"{entry['title']}｜{entry['subtitle'].splitlines()[0].removeprefix('轮次：')}：{url}")
            if links:
                yield event.plain_result('官方申请链接：\n' + '\n'.join(links))
            yield event.image_result(str(image))
        except Exception:
            logger.exception("IM@S ticket image rendering failed")
            yield event.plain_result("抽票图片生成失败；请检查插件字体配置和日志。")

    async def terminate(self):
        for task in (self._sync_task, self._reminder_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.service.close()
