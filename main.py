"""IM@S performance calendars, ticket-query cards, and conservative group alerts."""

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
try:  # AstrBot >=4.18 exposes these rich passive-message components.
    from astrbot.api.message_components import Image, Plain
except ModuleNotFoundError:  # lightweight import fallback for offline unit tests
    class Image:  # pragma: no cover - production always uses AstrBot's component
        @staticmethod
        def fromFileSystem(path: str): return ("image", path)
    class Plain:
        def __init__(self, text: str): self.text = text
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


def _allow_nonempty_command_group(group):
    """Keep a native command group from consuming its public query command.

    AstrBot command groups normally render their help tree for the bare group
    name.  These two groups deliberately retain a same-named public query
    command (`/imaslive` and `/imasticket`), so only the exact bare group name
    is declined here.  Subcommands remain native group children and therefore
    keep their WebUI enable/rename and permission metadata.
    """
    class NonEmptyGroup(filter.CustomFilter):
        def filter(self, event: AstrMessageEvent, _config) -> bool:
            message = event.get_message_str().strip()
            return message not in group.parent_group.get_complete_command_names()
    return NonEmptyGroup


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
        """Deliver retryable cached alerts every five minutes, independently of syncs."""
        while True:
            try:
                ticket_due = await self.service.claim_due_reminders()
                live_due = await self.service.claim_due_live_reminders()
                ticket_new = await self.service.claim_new_ticket_announcements()
                by_destination: dict[tuple[str, str], list[dict]] = defaultdict(list)
                for row in ticket_due + live_due:
                    by_destination[("reminder", row["umo"])].append(row)
                for row in ticket_new:
                    by_destination[("ticket_new", row["umo"])].append(row)
                for (notification_type, umo), rows in by_destination.items():
                    for offset in range(0, len(rows), 3):
                        batch = rows[offset:offset+3]
                        try:
                            is_new = notification_type == "ticket_new"
                            image = await asyncio.to_thread(
                                self.renderer.render_reminder, batch,
                                datetime.now(ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))),
                                "IMAS 新增现场抽选" if is_new else "IMAS 现场提醒",
                                "请核对官方申请页面" if is_new else "请核对官方页面",
                                not is_new,
                            )
                            links = "\n".join(f"官方链接：{url}" for url in dict.fromkeys(row["url"] for row in batch if row["url"]))
                            message = MessageChain().message(("IM@S 新增现场抽选\n" if is_new else "IM@S 提醒\n") + links).file_image(str(image))
                            ok = bool(await self.context.send_message(umo, message))
                        except Exception:
                            ok = False
                            logger.exception("IM@S alert image delivery failed")
                        await self.service.finish_reminders(batch, ok)
                await asyncio.sleep(REMINDER_CHECK_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("IM@S deadline cycle failed")
                await asyncio.sleep(REMINDER_CHECK_SECONDS)

    @staticmethod
    def _group_umo(event: AstrMessageEvent) -> str | None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return umo if "GroupMessage" in umo else None

    @staticmethod
    def _cast_text(cast: list[dict]) -> str:
        if not cast:
            return "出演资料：官网尚未公布或待核验。"
        values = []
        for item in cast:
            role = f"（{item['role_name']}）" if item.get("role_name") else ""
            values.append(f"{item['person_name']}{role}")
        return "出演资料：" + "、".join(values)

    def _image_and_links(self, event: AstrMessageEvent, image: Path, links: list[str], extra_images: list[str] | None = None):
        """One passive response chain keeps the PNG before every clickable URL."""
        if not image.is_file():
            logger.error("IM@S rendered image missing before send: %s", image)
            raise FileNotFoundError(image)
        chain = [Image.fromFileSystem(str(image))]
        for asset in extra_images or []:
            if Path(asset).is_file():
                chain.append(Image.fromFileSystem(asset))
        if links:
            chain.append(Plain("\n" + "\n".join(links)))
        return event.chain_result(chain)

    async def _wait_for_first_directory(self, event: AstrMessageEvent) -> None:
        await self.initialize()
        if not self.service.db.meta('last_directory_sync') and self.config.get('enabled', True):
            yield event.plain_result('正在首次同步官网活动，稍候生成日历。')
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.service.directory_ready.wait(), timeout=40)

    @filter.command_group("imaslive")
    def imaslive_group(self):
        """LIVE 查询、下一场与群订阅管理。"""
        pass

    # The group and query share their public root intentionally; see helper.
    imaslive_group.custom_filter(_allow_nonempty_command_group(imaslive_group), False)(imaslive_group)

    @filter.command("imaslive")
    async def imaslive(self, event: AstrMessageEvent, month_argument: str = "", extra_argument: str = ""):
        """显示未来30天或指定完整自然月的演出长图。"""
        # Native children own these verbs.  This public query handler never
        # writes subscriptions, including if a child is disabled or renamed.
        if month_argument.casefold() in {"next", "enable", "disable"}:
            return
        event.stop_event()
        try:
            try:
                month = parse_live_month(" ".join(part for part in (month_argument, extra_argument) if part))
            except ValueError:
                yield event.plain_result('用法：/imaslive、/imaslive 1—12、/imaslive next [企划]、/imaslive enable|disable')
                return
            async for response in self._wait_for_first_directory(event):
                yield response
            entries, start, end, status, title = await self.service.calendar_entries(month=month)
            now = datetime.now(ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai"))))
            images = await asyncio.to_thread(self.renderer.render_calendar, entries, start.date(), end.date(), now, status, title)
            for image in images:
                yield self._image_and_links(event, image, [])
        except Exception:
            logger.exception("IM@S calendar image rendering failed")
            yield event.plain_result("日历图片生成失败；请检查插件字体配置和日志。")

    @imaslive_group.command("next")
    async def imaslive_next(self, event: AstrMessageEvent, brand: str = "", extra_argument: str = ""):
        """显示下一场已收录 LIVE；可选 imas/cg/ml/sidem/sc/gk。"""
        event.stop_event()
        if extra_argument:
            yield event.plain_result("用法：/imaslive next [imas/cg/ml/sidem/sc/gk]")
            return
        try:
            async for response in self._wait_for_first_directory(event):
                yield response
            try:
                entry = await self.service.next_entry(brand)
            except ValueError:
                yield event.plain_result("未知企划。可用：imas、cg、ml、sidem、sc、gk。")
                return
            if not entry:
                yield event.plain_result("没有找到尚未开始的已收录公演。")
                return
            now = datetime.now(ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai"))))
            image = await asyncio.to_thread(self.renderer.render_calendar, [entry], now.date(), now.date(), now, "官网资料以链接为准", "IM@S LIVE! · Next Performance")
            links = [self._cast_text(entry["cast"]), f"官方活动页：{entry['official_url'] or entry['url']}"]
            yield self._image_and_links(event, image[0], links, entry.get("cast_assets"))
        except Exception:
            logger.exception("IM@S next live image rendering failed")
            yield event.plain_result("日历图片生成失败；请检查插件字体配置和日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @imaslive_group.command("enable")
    async def imaslive_enable(self, event: AstrMessageEvent, extra_argument: str = ""):
        """管理员：开启本群 LIVE 开演提醒与新增现场抽选公告。"""
        event.stop_event()
        if extra_argument:
            yield event.plain_result("用法：/imaslive enable")
            return
        umo = self._group_umo(event)
        if not umo:
            yield event.plain_result("LIVE 订阅只能在群内设置。")
            return
        self.service.set_subscription("live", umo, True)
        yield event.plain_result("已开启本群 LIVE 开演提醒与新增现场抽选公告。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @imaslive_group.command("disable")
    async def imaslive_disable(self, event: AstrMessageEvent, extra_argument: str = ""):
        """管理员：关闭本群 LIVE 开演提醒与新增现场抽选公告。"""
        event.stop_event()
        if extra_argument:
            yield event.plain_result("用法：/imaslive disable")
            return
        umo = self._group_umo(event)
        if not umo:
            yield event.plain_result("LIVE 订阅只能在群内设置。")
            return
        self.service.set_subscription("live", umo, False)
        yield event.plain_result("已关闭本群 LIVE 开演提醒与新增现场抽选公告。")

    @filter.command_group("imasticket")
    def imasticket_group(self):
        """抽票查询、历史详情与群截止提醒订阅管理。"""
        pass

    imasticket_group.custom_filter(_allow_nonempty_command_group(imasticket_group), False)(imasticket_group)

    @filter.command("imasticket")
    async def imasticket(self, event: AstrMessageEvent, action: str = "", extra_argument: str = ""):
        """显示当前正在开放、已核验的现场抽选。"""
        if action.casefold() in {"get", "enable", "disable"}:
            return
        event.stop_event()
        if action or extra_argument:
            yield event.plain_result("用法：/imasticket、/imasticket get <活动编号>、/imasticket enable|disable")
            return
        try:
            async for response in self._wait_for_first_directory(event):
                yield response
            refresh = await self.service.refresh_open_ticket_sources()
            entries, start, end, status = await self.service.ticket_entries()
            if refresh["failed"]:
                status += f"｜{refresh['failed']} 个当前开放专题复核失败，未当作开放显示"
            now = datetime.now(ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai"))))
            image = await asyncio.to_thread(self.renderer.render_ticket, entries, start.date(), end.date(), now, status)
            links: list[str] = []
            seen: set[str] = set()
            for entry in entries:
                url = str(entry.get('url') or '')
                if url and url not in seen:
                    seen.add(url)
                    links.append(f"{entry['title']}｜{entry['subtitle'].splitlines()[0].removeprefix('轮次：')}：{url}")
            yield self._image_and_links(event, image, links)
        except Exception:
            logger.exception("IM@S ticket image rendering failed")
            yield event.plain_result("抽票图片生成失败；请检查插件字体配置和日志。")

    @imasticket_group.command("get")
    async def imasticket_get(self, event: AstrMessageEvent, ticket_number: int, extra_argument: str = ""):
        """按活动编号查看已收录的历史现场票务。"""
        event.stop_event()
        if ticket_number < 1 or extra_argument:
            yield event.plain_result("用法：/imasticket get <活动编号>，例如 /imasticket get 1。")
            return
        try:
            async for response in self._wait_for_first_directory(event):
                yield response
            detail = await self.service.ticket_detail(ticket_number)
            if not detail:
                yield event.plain_result("没有这个活动编号；请使用 LIVE 或抽票图中的 #编号。")
                return
            now = datetime.now(ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai"))))
            image = await asyncio.to_thread(self.renderer.render_ticket, detail["tickets"], now.date(), now.date(), now, "历史票务状态以官方页为准")
            urls = [f"官方活动页：{detail['event'].get('official_url')}"]
            urls.extend(f"官方票务页：{row['url']}" for row in detail["tickets"] if row.get("url"))
            urls.append(self._cast_text(detail["cast"]))
            yield self._image_and_links(event, image, list(dict.fromkeys(urls)))
        except Exception:
            logger.exception("IM@S historical ticket image rendering failed")
            yield event.plain_result("抽票图片生成失败；请检查插件字体配置和日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @imasticket_group.command("enable")
    async def imasticket_enable(self, event: AstrMessageEvent, extra_argument: str = ""):
        """管理员：开启本群抽票截止提醒。"""
        event.stop_event()
        if extra_argument:
            yield event.plain_result("用法：/imasticket enable")
            return
        umo = self._group_umo(event)
        if not umo:
            yield event.plain_result("抽票截止提醒只能在群内设置。")
            return
        self.service.set_subscription("ticket", umo, True)
        yield event.plain_result("已开启本群抽票截止提醒（截止前 24 小时和 1 小时各一次）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @imasticket_group.command("disable")
    async def imasticket_disable(self, event: AstrMessageEvent, extra_argument: str = ""):
        """管理员：关闭本群抽票截止提醒。"""
        event.stop_event()
        if extra_argument:
            yield event.plain_result("用法：/imasticket disable")
            return
        umo = self._group_umo(event)
        if not umo:
            yield event.plain_result("抽票截止提醒只能在群内设置。")
            return
        self.service.set_subscription("ticket", umo, False)
        yield event.plain_result("已关闭本群抽票截止提醒。")

    async def terminate(self):
        for task in (self._sync_task, self._reminder_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.service.close()
