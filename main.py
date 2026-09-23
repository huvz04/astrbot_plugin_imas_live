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
try:
    from astrbot.api.web import json_response
except ModuleNotFoundError:  # offline command-registration tests
    def json_response(value): return value

from .imas_live.render import CalendarRenderer
from .imas_live.service import ImasLiveService
from .imas_live.flight import FlightPlanner
from .imas_live.flight_render import FlightRenderer

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
        self.flight_planner = FlightPlanner(self.data_dir, self.config)
        self.renderer = CalendarRenderer(self.data_dir / "rendered", str(self.config.get("font_path", "")))
        self.flight_renderer = FlightRenderer(self.data_dir / "rendered", str(self.config.get("font_path", "")))
        self._sync_task: asyncio.Task | None = None
        self._reminder_task: asyncio.Task | None = None
        self._flight_task: asyncio.Task | None = None
        self._last_directory = 0.0
        if hasattr(context, "register_web_api"):
            context.register_web_api(f"/{PLUGIN_NAME}/flight/status", self.flight_page_status, ["GET"], "IM@S flight monitor")
            context.register_web_api(f"/{PLUGIN_NAME}/flight/activities", self.flight_page_activities, ["GET"], "IM@S flight activities")

    async def flight_page_status(self):
        return json_response(await asyncio.to_thread(self.flight_planner.snapshot))

    async def flight_page_activities(self):
        performances, _ = await asyncio.to_thread(self.service.db.calendar_rows)
        today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
        events: dict[str, dict] = {}
        for row in sorted(performances, key=lambda item: (item.get("date") or "", item["id"])):
            if not row.get("date") or row["date"] < today:
                continue
            item = events.setdefault(row["event_id"], {"number": row.get("public_number"),
                "title": row["title"], "sessions": []})
            venue = row.get("venue") or row.get("event_venue")
            item["sessions"].append({"id": row["id"], "date": row["date"],
                "label": row.get("session_label"), "venue": venue,
                "tokyo_route_supported": self.flight_planner._venue_supported(venue)})
        return json_response({"activities": list(events.values())[:100]})

    async def initialize(self):
        """Runs both on boot and WebUI install/reload; the global loaded event does not."""
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = asyncio.create_task(self._sync_loop())
        if self._reminder_task is None or self._reminder_task.done():
            self._reminder_task = asyncio.create_task(self._reminder_loop())
        if self._flight_task is None or self._flight_task.done():
            self._flight_task = asyncio.create_task(self._flight_loop())

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
                await self._run_reminder_cycle()
                await asyncio.sleep(REMINDER_CHECK_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("IM@S deadline cycle failed")
                await asyncio.sleep(REMINDER_CHECK_SECONDS)

    async def _flight_loop(self) -> None:
        """The optional fare worker never touches LIVE reminder subscriptions."""
        while True:
            try:
                await self._run_flight_cycle()
                await asyncio.sleep(REMINDER_CHECK_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("IM@S flight monitor cycle failed")
                await asyncio.sleep(REMINDER_CHECK_SECONDS)

    async def _run_flight_cycle(self) -> None:
        for task in await asyncio.to_thread(self.flight_planner.all_tasks):
            try:
                if task["event_id"]:
                    detail = await asyncio.to_thread(self.service.db.detail, task["event_id"])
                    task = await asyncio.to_thread(self.flight_planner.revalidate, task, detail)
                if await asyncio.to_thread(self.flight_planner.due, task):
                    await self.flight_planner.check(task["id"])
            except Exception:
                logger.exception("IM@S flight check failed: task=%s", task.get("id"))
        await self.flight_planner.deliver(self._send_flight_quotes)

    async def _send_flight_quotes(self, umo: str, task: dict, quotes: list[dict]) -> bool:
        """Render only the same at-most-three records that delivery will mark sent."""
        try:
            current = await asyncio.to_thread(self.flight_planner.task, task["id"])
            if task["event_id"]:
                detail = await asyncio.to_thread(self.service.db.detail, task["event_id"])
                current = await asyncio.to_thread(self.flight_planner.revalidate, current, detail)
            if not current.get("enabled") or current != task or current["umo"] != umo:
                return False
            image = await asyncio.to_thread(self.flight_renderer.render, task, quotes)
            current = await asyncio.to_thread(self.flight_planner.task, task["id"])
            if not current.get("enabled") or current != task or current["umo"] != umo:
                return False
            links = "\n".join(dict.fromkeys(item["link"] for item in quotes if item.get("link")))
            text = "IM@S 上海 ⇄ 东京机票达到心理价。\n" + (links or "来源未提供可安全打开的搜索链接。")
            message = MessageChain().file_image(str(image)).message(text)
            return bool(await self.context.send_message(umo, message))
        except Exception:
            logger.exception("IM@S flight reminder delivery failed: umo=%s", umo)
            return False

    async def _run_reminder_cycle(self) -> None:
        """Isolate claim/render/send failures by notification kind.

        A claimed row is always marked sent or failed before another kind is
        considered; a failed row is retryable in the service's recovery window.
        """
        try:
            await self.service.refresh_due_ticket_sources()
        except Exception:
            logger.exception("IM@S due-ticket refresh failed; using cached verified data")
        for kind, claimant in (
            ("ticket", self.service.claim_due_reminders),
            ("live", self.service.claim_due_live_reminders),
            ("ticket_new", self.service.claim_new_ticket_announcements),
        ):
            try:
                rows = await claimant()
            except Exception:
                logger.exception("IM@S %s reminder claim failed", kind)
                continue
            await self._deliver_reminder_rows(kind, rows)

    async def _deliver_reminder_rows(self, kind: str, rows: list[dict]) -> None:
        by_destination: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            by_destination[row["umo"]].append(row)
        for umo, destination_rows in by_destination.items():
            for offset in range(0, len(destination_rows), 3):
                batch = destination_rows[offset:offset + 3]
                ok = False
                try:
                    is_new = kind == "ticket_new"
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
                    if not ok:
                        logger.warning("IM@S %s reminder send returned false: umo=%s count=%s", kind, umo, len(batch))
                except Exception:
                    logger.exception("IM@S %s reminder delivery failed: umo=%s", kind, umo)
                finally:
                    try:
                        await self.service.finish_reminders(batch, ok)
                    except Exception:
                        # Inflight rows recover on restart; log the precise kind
                        # and destination instead of silently blocking all kinds.
                        logger.exception("IM@S %s reminder completion failed: umo=%s", kind, umo)

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

    @staticmethod
    def _data_updated_at(rows: list[dict]) -> datetime | None:
        """The footer reports only a verified source timestamp, never render time."""
        values = []
        for row in rows:
            try:
                value = datetime.fromisoformat(str(row.get("source_fetched_at") or ""))
                if value.tzinfo:
                    values.append(value)
            except ValueError:
                continue
        return max(values) if values else None

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
            images = await asyncio.to_thread(self.renderer.render_calendar, entries, start.date(), end.date(), now, status, title, self._data_updated_at(entries))
            for image in images:
                yield self._image_and_links(event, image, [])
        except Exception:
            logger.exception("IM@S calendar image rendering failed")
            yield event.plain_result("日历图片生成失败；请检查插件字体配置和日志。")

    @filter.command("imaslive next")
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
            image = await asyncio.to_thread(self.renderer.render_calendar, [entry], now.date(), now.date(), now, "官网资料以链接为准", "IM@S LIVE! · Next Performance", self._data_updated_at([entry]))
            links = [self._cast_text(entry["cast"]), f"官方活动页：{entry['official_url'] or entry['url']}"]
            yield self._image_and_links(event, image[0], links, entry.get("cast_assets"))
        except Exception:
            logger.exception("IM@S next live image rendering failed")
            yield event.plain_result("日历图片生成失败；请检查插件字体配置和日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("imaslive enable")
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
    @filter.command("imaslive disable")
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
            image = await asyncio.to_thread(self.renderer.render_ticket, entries, start.date(), end.date(), now, status, self._data_updated_at(entries))
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

    @filter.command("imasticket get")
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
            image = await asyncio.to_thread(self.renderer.render_ticket, detail["tickets"], now.date(), now.date(), now, "历史票务状态以官方页为准", self._data_updated_at(detail["tickets"]))
            urls = [f"官方活动页：{detail['event'].get('official_url')}"]
            urls.extend(f"官方票务页：{row['url']}" for row in detail["tickets"] if row.get("url"))
            urls.append(self._cast_text(detail["cast"]))
            yield self._image_and_links(event, image, list(dict.fromkeys(urls)))
        except Exception:
            logger.exception("IM@S historical ticket image rendering failed")
            yield event.plain_result("抽票图片生成失败；请检查插件字体配置和日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("imasticket enable")
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
    @filter.command("imasticket disable")
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

    @filter.command("imasflight")
    async def imasflight(self, event: AstrMessageEvent, activity_number: int = 0, extra_argument: str = ""):
        """查看一个活动可选择的已核验场次，并规划上海—东京机票日期。"""
        if not activity_number or extra_argument:
            yield event.plain_result("用法：/imasflight <活动编号>。随后由管理员执行 /imasflight plan <活动编号> <场次ID|all> 创建暂停计划。")
            return
        detail = await asyncio.to_thread(self.service.db.detail_by_public_number, activity_number)
        if not detail:
            yield event.plain_result("没有这个活动编号；请先通过 LIVE 或抽票图确认 #编号。")
            return
        sessions = detail.get("performances", [])
        if not sessions:
            yield event.plain_result("该活动尚无已核验场次，不能用抽选截止日期生成机票计划。")
            return
        lines = [f"#{activity_number} {detail['event']['title']}", "请选择实际参加的场次（不会默认全部参加）："]
        for row in sessions:
            lines.append(f"{row['id']}｜{row.get('date')}｜{row.get('session_label') or '开演时间待核验'}｜{row.get('venue') or detail['event'].get('venue') or '场馆待核验'}")
        lines.append("管理员可显式用 all 选择全部场次；新计划默认暂停，不会查询机票或消费额度。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("imasflight plan")
    async def imasflight_plan(self, event: AstrMessageEvent, activity_number: int, session_id: str, extra_argument: str = ""):
        """管理员：按明确选择的场次创建暂停的上海—东京往返机票计划。"""
        if extra_argument:
            yield event.plain_result("用法：/imasflight plan <活动编号> <场次ID|all>")
            return
        detail = await asyncio.to_thread(self.service.db.detail_by_public_number, activity_number)
        if not detail:
            yield event.plain_result("没有这个活动编号。")
            return
        selected = ([row["id"] for row in detail.get("performances", [])] if session_id.casefold() == "all"
                    else [part.strip() for part in session_id.split(",") if part.strip()])
        try:
            task = await asyncio.to_thread(self.flight_planner.create_paused_plan, detail, selected,
                                            str(getattr(event, "unified_msg_origin", "") or ""))
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(
            f"已创建暂停机票计划 {task['id']}：到达东京候选 {', '.join(task['arrival_dates'])}；"
            f"返程候选 {', '.join(task['return_dates'])}。共 {len(self.flight_planner._queries(task))} 个机场/日期组合。"
            f"管理员先 /imasflight price {task['id']} <人民币心理价>，配置数据源密钥和预算后，再 /imasflight enable {task['id']}。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("imasflight route")
    async def imasflight_route(self, event: AstrMessageEvent, arrival_date: str, return_date: str, extra_argument: str = ""):
        """管理员：按明确的东京到达日与返程日创建普通暂停监测。"""
        if extra_argument:
            yield event.plain_result("用法：/imasflight route <东京到达日YYYY-MM-DD> <东京返程日YYYY-MM-DD>")
            return
        try:
            task = await asyncio.to_thread(self.flight_planner.create_paused_route, arrival_date, return_date,
                                            str(getattr(event, "unified_msg_origin", "") or ""))
            yield event.plain_result(f"已创建暂停机票计划 {task['id']}：东京抵达 {arrival_date}，东京返程 {return_date}。请设置心理价并显式启用。")
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("imasflight price")
    async def imasflight_price(self, event: AstrMessageEvent, task_id: str, price: int, extra_argument: str = ""):
        """管理员：为当前会话计划设置往返含税报价的心理价。"""
        if extra_argument:
            yield event.plain_result("用法：/imasflight price <计划ID> <人民币心理价>")
            return
        try:
            task = await asyncio.to_thread(self.flight_planner.set_price, task_id, str(getattr(event, "unified_msg_origin", "") or ""), price)
            yield event.plain_result(f"机票计划 {task['id']} 心理价已设为 CNY {price}；计划保持暂停，需显式启用。")
        except (KeyError, ValueError) as exc:
            yield event.plain_result(str(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("imasflight baggage")
    async def imasflight_baggage(self, event: AstrMessageEvent, task_id: str, requirement: str, extra_argument: str = ""):
        """管理员：设置是否必须核验含托运行李。"""
        if extra_argument:
            yield event.plain_result("用法：/imasflight baggage <计划ID> <none|checked>")
            return
        try:
            task = await asyncio.to_thread(self.flight_planner.set_baggage, task_id, str(getattr(event, "unified_msg_origin", "") or ""), requirement)
            yield event.plain_result(f"机票计划 {task['id']} 的托运行李要求已设为 {requirement}；计划保持暂停。")
        except (KeyError, ValueError) as exc:
            yield event.plain_result(str(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("imasflight enable")
    async def imasflight_enable(self, event: AstrMessageEvent, task_id: str, extra_argument: str = ""):
        """管理员：满足配置条件后，显式启用当前会话的机票计划。"""
        if extra_argument:
            yield event.plain_result("用法：/imasflight enable <计划ID>")
            return
        try:
            current = await asyncio.to_thread(self.flight_planner.task, task_id)
            if current["event_id"]:
                detail = await asyncio.to_thread(self.service.db.detail, current["event_id"])
                current = await asyncio.to_thread(self.flight_planner.revalidate, current, detail)
            if current["status"].startswith("paused_event_"):
                raise ValueError("活动场次或来源已变化；旧计划已暂停，请重新创建计划。")
            task = await asyncio.to_thread(self.flight_planner.set_enabled, task_id, str(getattr(event, "unified_msg_origin", "") or ""), True)
            yield event.plain_result(f"已启用机票计划 {task['id']}；仅完整往返且达到 CNY {task['target_price']} 才会独立推送。")
        except (KeyError, ValueError) as exc:
            yield event.plain_result(str(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("imasflight disable")
    async def imasflight_disable(self, event: AstrMessageEvent, task_id: str, extra_argument: str = ""):
        """管理员：暂停并撤销该会话尚未发送的机票提醒。"""
        if extra_argument:
            yield event.plain_result("用法：/imasflight disable <计划ID>")
            return
        try:
            task = await asyncio.to_thread(self.flight_planner.set_enabled, task_id, str(getattr(event, "unified_msg_origin", "") or ""), False)
            yield event.plain_result(f"已暂停机票计划 {task['id']}；不会再查询或推送。")
        except (KeyError, ValueError) as exc:
            yield event.plain_result(str(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("imasflight check")
    async def imasflight_check(self, event: AstrMessageEvent, task_id: str, extra_argument: str = ""):
        """管理员：对已经启用的计划进行一次受预算约束的检查。"""
        if extra_argument:
            yield event.plain_result("用法：/imasflight check <计划ID>")
            return
        try:
            task = await asyncio.to_thread(self.flight_planner.task, task_id)
            if task["umo"] != str(getattr(event, "unified_msg_origin", "") or ""):
                raise ValueError("当前会话未创建该机票计划。")
            if task["event_id"]:
                detail = await asyncio.to_thread(self.service.db.detail, task["event_id"])
                task = await asyncio.to_thread(self.flight_planner.revalidate, task, detail)
            if not task.get("enabled"):
                raise ValueError("计划已暂停；请核对活动来源、场次与配置。")
            quotes = await self.flight_planner.check(task_id)
            yield event.plain_result(f"本次找到 {len(quotes)} 条达到心理价的完整往返候选；提醒将按独立去重规则投递。")
        except (KeyError, ValueError) as exc:
            yield event.plain_result(str(exc))

    @filter.command("imasflight list")
    async def imasflight_list(self, event: AstrMessageEvent, extra_argument: str = ""):
        """查看当前会话独立创建的机票计划。"""
        if extra_argument:
            yield event.plain_result("用法：/imasflight list")
            return
        tasks = await asyncio.to_thread(self.flight_planner.tasks_for, str(getattr(event, "unified_msg_origin", "") or ""))
        if not tasks:
            yield event.plain_result("当前会话没有机票计划。机票计划不继承 LIVE 或抽票订阅。")
            return
        yield event.plain_result("\n".join(
            f"{task['id']}｜{('#' + str(task['event_number']) + ' ') if task.get('event_number') else ''}{task['event_title']}｜"
            f"{task['status']}｜到达 {','.join(task['arrival_dates'])}／返程 {','.join(task['return_dates'])}" for task in tasks))

    async def terminate(self):
        for task in (self._sync_task, self._reminder_task, self._flight_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.service.close()
