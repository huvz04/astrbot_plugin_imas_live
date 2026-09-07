"""A single-command IM@S live calendar plus one-shot lottery-deadline images."""

from __future__ import annotations

import asyncio
import contextlib
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


class ImasLivePlugin(Star):
    """Keep group interaction deliberately small: only /imaslive returns PNG pages."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.service = ImasLiveService(self.data_dir, self.config)
        self.renderer = CalendarRenderer(self.data_dir / "rendered", str(self.config.get("font_path", "")))
        self._sync_task: asyncio.Task | None = None
        self._reminder_task: asyncio.Task | None = None
        self._last_directory = 0.0

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        self._sync_task = asyncio.create_task(self._sync_loop())
        self._reminder_task = asyncio.create_task(self._reminder_loop())

    async def _sync_loop(self) -> None:
        while True:
            try:
                interval = max(15, int(self.config.get("sync_interval_minutes", 60)))
                directory_seconds = max(1, int(self.config.get("directory_interval_hours", 6))) * 3600
                full = self.service.db.meta("baseline_complete") is None or time.monotonic() - self._last_directory >= directory_seconds
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
        """Independent 30s deadline check: it never waits for the slower HTTP sync loop."""
        while True:
            try:
                due = await self.service.claim_due_reminders()
                by_umo: dict[str, list[dict]] = defaultdict(list)
                for row in due:
                    by_umo[row["umo"]].append(row)
                for umo, rows in by_umo.items():
                    image = self.renderer.render_reminder(rows, datetime.now(ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))))
                    links = "\n".join(f"官方抽票地址：{url}" for url in dict.fromkeys(row["url"] for row in rows))
                    try:
                        message = MessageChain().message("抽选截止提醒\n" + links).file_image(str(image))
                        ok = bool(await self.context.send_message(umo, message))
                    except Exception:
                        ok = False
                        logger.exception("IM@S deadline image delivery failed")
                    await self.service.finish_reminders(rows, ok)
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("IM@S deadline cycle failed")
                await asyncio.sleep(30)

    @filter.command("imaslive")
    async def imaslive(self, event: AstrMessageEvent, action: str = ""):
        """显示今天起 30 个自然日的 PNG 列表日历。"""
        event.stop_event()
        command = action.strip().lower()
        if command in {"enable", "disable"}:
            group_id = str(event.get_group_id() or "").strip()
            umo = str(event.unified_msg_origin or "").strip()
            if not group_id or not umo:
                yield event.plain_result("请在群聊中使用 /imaslive enable 或 /imaslive disable。")
                return
            self.service.set_group_enabled(umo, command == "enable")
            state = "已开启" if command == "enable" else "已关闭"
            yield event.plain_result(f"本群 IMAS LIVE 已{state}。")
            return
        if command:
            yield event.plain_result("用法：/imaslive、/imaslive enable、/imaslive disable")
            return
        umo = str(event.unified_msg_origin or "").strip()
        if event.get_group_id() and not self.service.group_enabled(umo):
            yield event.plain_result("本群 IMAS LIVE 当前已关闭；请使用 /imaslive enable 开启。")
            return
        try:
            entries, start, end, status = await self.service.calendar_entries()
            now = datetime.now(ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai"))))
            images = self.renderer.render_calendar(entries, start.date(), end.date(), now, status)
            for image in images:
                yield event.image_result(str(image))
        except Exception:
            logger.exception("IM@S calendar image rendering failed")
            yield event.plain_result("日历图片生成失败；请检查插件字体配置和日志。")

    async def terminate(self):
        for task in (self._sync_task, self._reminder_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.service.close()
