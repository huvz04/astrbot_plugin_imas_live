"""Async orchestration layer: fetch, parse, store, query and conservative reminders."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib
import json
import logging
import re
from io import BytesIO
from datetime import datetime, timedelta, timezone
from dataclasses import asdict as vars_for_slots
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urldefrag
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from .cms import CmsArticle, OfficialCmsClient, SourceUnavailable
from .database import Database
from .parsing import parse_shiny_information, parse_day_cast, official_roster_image_urls, parse_ticket_page, parse_venue, parse_information, schedule_performances, clean
from .models import ParsedPage


BRAND_COMMANDS = {
    "imas": "IDOLMASTER", "765": "IDOLMASTER", "as": "IDOLMASTER",
    "cg": "CINDERELLAGIRLS", "ml": "MILLIONLIVE", "sidem": "SIDEM",
    "sm": "SIDEM", "sc": "SHINYCOLORS", "gk": "GAKUEN",
}
REMINDER_WINDOW_MINUTES = 6  # five-minute worker plus modest scheduler jitter
logger = logging.getLogger(__name__)


class ImasLiveService:
    def __init__(self, data_dir: Path, config: dict[str, Any] | None = None):
        self.config = config if config is not None else {}
        self.data_dir = data_dir
        self.db = Database(data_dir / "imas_live.sqlite3")
        self.db.recover_deliveries()
        # The old whitelist described ticket reminders only.  It is intentionally
        # never copied to the newly introduced LIVE reminder table.
        self.db.migrate_legacy_ticket_subscriptions(self.config.get("white_umos", []))
        self.client = OfficialCmsClient(float(self.config.get("request_timeout_seconds", 25)))
        self._sync_lock = asyncio.Lock()
        self.directory_ready = asyncio.Event()

    async def close(self) -> None:
        await self.client.close()

    def group_enabled(self, umo: str) -> bool:
        return self.db.group_enabled(umo, bool(self.config.get("enabled", True)))

    def set_group_enabled(self, umo: str, value: bool) -> None:
        self.db.set_group_enabled(umo, value)

    def subscription_enabled(self, kind: str, umo: str) -> bool:
        return self.db.subscription_enabled(kind, umo)

    def set_subscription(self, kind: str, umo: str, value: bool) -> None:
        self.db.set_subscription(kind, umo, value)

    async def sync(self, full_directory: bool = True) -> dict[str, Any]:
        """Fetch only verified CMS and direct public special pages. Never deletes old data."""
        if self._sync_lock.locked():
            return {"status": "already_running"}
        async with self._sync_lock:
            try:
                if full_directory:
                    articles = await self.client.live_articles(int(self.config.get("max_pages", 30)))
                else:
                    known = await asyncio.to_thread(self.db.fetchable_events, int(self.config.get("max_special_pages", 12)))
                    articles = [CmsArticle(row["id"], row["title"], row["official_url"], json.loads(row["brands_json"]), row["event_display"], row["venue"], row["source_updated"], {}) for row in known]
            except SourceUnavailable as exc:
                self.db.set_meta("last_error", str(exc)); return {"status": "failed", "error": str(exc)}
            directory_was_complete = self.db.meta("baseline_complete") is not None
            for article in articles:
                # Only a newly discovered entry in a later *complete* directory
                # can prove that the event itself is newly announced.  Partial
                # special-page coverage is deliberately not enough.
                title = article.title.lower()
                is_live = any(word in title for word in ('live', 'st@ge', 'stage', 'ライブ', 'musical'))
                excluded = any(word in title for word in ('museum', 'ホテル', '脱出', '物販', '上映', '発売記念', 'popup'))
                await asyncio.to_thread(self.db.upsert_event, self._event_record(article),
                                        full_directory and directory_was_complete and is_live and not excluded)
                if full_directory:
                    # These are date entries from the official event field, never range expansion.
                    dates = schedule_performances(article.event_display or '', article.url or f'https://idolmaster-official.jp/live_event#event-{article.cms_id}', article.venue, True) if is_live and not excluded else []
                    await asyncio.to_thread(self.db.save_directory_dates, article.cms_id, dates)
            if full_directory:
                self.db.set_meta('last_directory_sync', datetime.now(timezone.utc).isoformat(timespec='seconds'))
                self.directory_ready.set()
                known = await asyncio.to_thread(self.db.fetchable_events, max(1, int(self.config.get('max_special_pages', 12))))
                articles = [CmsArticle(row['id'], row['title'], row['official_url'], json.loads(row['brands_json']), row['event_display'], row['venue'], row['source_updated'], {}) for row in known]
            changed, failed = 0, 0
            for article in articles:
                if not self._fetchable_special_page(article.url):
                    continue
                try:
                    changed += int(await self._refresh_article(article))
                except Exception as exc:
                    # One malformed official page (including a database identity
                    # conflict) must not abort the remaining rotating sources.
                    # source_error persists the event and URL for WebUI/log review.
                    failed += 1
                    logger.exception("IM@S source refresh failed: event=%s source=%s", article.cms_id, article.url)
                    await asyncio.to_thread(self.db.source_error, article.cms_id, article.url, str(exc))
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.db.set_meta("last_successful_sync", stamp)
            with self.db._connect() as db:
                unverified = db.execute("SELECT COUNT(*) FROM sources WHERE quality='stale'").fetchone()[0]
            self.db.set_meta('last_error', f'{unverified} 个专题待核验' if unverified else '')
            first = self.db.meta("baseline_complete") is None
            self.db.set_meta("baseline_complete", "1")
            return {"status": "ok", "events": len(articles), "changed_pages": changed, "failed_pages": failed, "baseline": first}

    async def _refresh_article(self, article: CmsArticle) -> bool:
        """Refresh one already-known official event without broad directory work."""
        parsed = await self._collect_special(article)
        if not (parsed.ticket_rounds or parsed.performances):
            raise SourceUnavailable('专题未能解析，保留已知记录并暂停该来源提醒')
        digest = hashlib.sha256(json.dumps({
            'tickets': [row.record() for row in parsed.ticket_rounds],
            'performances': [vars_for_slots(row) for row in parsed.performances],
            'cast': [vars_for_slots(row) for row in parsed.cast],
        }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        changed = await asyncio.to_thread(self.db.save_parsed, article.cms_id, article.url, digest, "special-page-v2", parsed.ticket_rounds, parsed.performances, parsed.cast, parsed.review_notes)
        assets = await self._cache_roster_assets(article.cms_id, article.url, parsed.cast_asset_urls)
        if assets:
            await asyncio.to_thread(self.db.save_cast_assets, article.cms_id, article.url, assets)
        return changed

    async def refresh_open_ticket_sources(self, current: datetime | None = None) -> dict[str, int]:
        """Bounded on-demand recheck for cached *active* lotteries only.

        This makes a manual ticket query useful when the normal rotating
        background job has not revisited a currently-open page within the
        display freshness window.  It does not promote unverified rows.
        """
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        rows = await asyncio.to_thread(self.db.ticket_query_rows)
        ids: list[str] = []
        for row in rows:
            try:
                start, end = datetime.fromisoformat(row["application_start"]), datetime.fromisoformat(row["application_end"])
                active = start.tzinfo and end.tzinfo and start <= current.astimezone(end.tzinfo) < end
            except (TypeError, ValueError):
                active = False
            if active and not self._source_is_fresh(row, current):
                ids.append(row["event_id"])
        if not ids or self._sync_lock.locked():
            return {"refreshed": 0, "failed": 0}
        raw = await asyncio.to_thread(self.db.events_by_ids, ids[:max(1, int(self.config.get("ticket_query_refresh_max", 4)))])
        articles = [CmsArticle(row["id"], row["title"], row["official_url"], json.loads(row["brands_json"]), row["event_display"], row["venue"], row["source_updated"], {}) for row in raw]
        refreshed = failed = 0
        async with self._sync_lock:
            for article in articles:
                if not self._fetchable_special_page(article.url):
                    continue
                try:
                    await self._refresh_article(article)
                    refreshed += 1
                except Exception as exc:
                    failed += 1
                    logger.exception("IM@S on-demand source refresh failed: event=%s source=%s", article.cms_id, article.url)
                    await asyncio.to_thread(self.db.source_error, article.cms_id, str(article.url), str(exc))
        return {"refreshed": refreshed, "failed": failed}

    async def _collect_special(self, article: CmsArticle) -> ParsedPage:
        """Follow actual links under this event only, including HTML meta redirects."""
        root = 'https://idolmaster-official.jp/live_event/' + urlsplit(article.url).path.split('/')[2] + '/'
        queue, visited = [article.url], set()
        selected_stop = None
        result = ParsedPage()
        ticket_rows = {}
        while queue and len(visited) < 8:
            url = urldefrag(queue.pop(0))[0]
            if url in visited:
                continue
            visited.add(url)
            html = await self.client.fetch_html(url)
            soup = BeautifulSoup(html, 'html.parser')
            # The Shirube home links several cities. Bind the city before following ticket redirects.
            if 'gkmas_livetour_shirube' in root and selected_stop is None:
                for a in soup.select('a[href]'):
                    label = clean(a.get_text(' ') + ' '.join(i.get('alt', '') for i in a.select('img')))
                    destination = urljoin(url, a['href'])
                    if '/information/' in destination and destination.endswith('.php') and label and any(
                        word in label and word in article.title for word in ('福井', '福岡', '岩手', 'ファイナル')):
                        selected_stop = destination.rsplit('/', 1)[-1]
                        queue.insert(0, destination)
                        break
            parsed = parse_ticket_page(html, url)
            result.review_notes.extend(parsed.review_notes)
            for row in parsed.ticket_rounds:
                ticket_rows[row.stable_key] = row
            result.cast_asset_urls.extend(official_roster_image_urls(html, url))
            performances = parse_information(html, url)
            if performances and (not result.performances or '/information' in url):
                result.performances = performances
            if '283production_msp' in root:
                cast_data = parse_shiny_information(html, url)
                if cast_data.cast:
                    result.cast, result.performances = cast_data.cast, cast_data.performances
                    for row in result.performances:
                        row.venue = parse_venue(html) or article.venue
            elif 'cast' in url.lower() or '出演者' in clean(soup.get_text(' ')):
                cast_data = parse_day_cast(html, url)
                if cast_data.cast:
                    result.cast = cast_data.cast
                    # A dedicated CAST page gives more reliable day bindings
                    # than the overview page, so retain its paired sessions.
                    result.performances = cast_data.performances
            destinations = []
            for meta in soup.select('meta[http-equiv]'):
                if meta.get('http-equiv', '').lower() == 'refresh':
                    content = meta.get('content', '')
                    if 'url=' in content.lower():
                        destinations.append(urljoin(url, content.split('=', 1)[1].strip(' \"\'')))
            for a in soup.select('a[href]'):
                destination = urldefrag(urljoin(url, a['href']))[0]
                if any(part in destination[len(root):].lower() for part in ('ticket', 'information')):
                    destinations.append(destination)
            for destination in dict.fromkeys(destinations):
                if not destination.startswith(root) or destination in visited or destination in queue:
                    continue
                if selected_stop and destination.endswith('.php') and destination.rsplit('/', 1)[-1] != selected_stop:
                    continue
                if 'gkmas_livetour_shirube' in root and not selected_stop and destination != article.url:
                    continue
                queue.append(destination)
        result.ticket_rounds = list(ticket_rows.values())
        result.cast_asset_urls = list(dict.fromkeys(result.cast_asset_urls))
        return result

    async def _cache_roster_assets(self, event_id: str, source_url: str, urls: list[str]) -> list[tuple[str, str]]:
        """Cache a small validated official roster asset; never proxy it elsewhere."""
        saved: list[tuple[str, str]] = []
        target = self.data_dir / "cast-assets" / event_id
        for index, url in enumerate(urls[:4]):
            try:
                await self.client._wait_slot()
                response = await self.client.client.get(url)
                response.raise_for_status()
                content = response.content
                if not 64 < len(content) <= 8 * 1024 * 1024:
                    continue
                with Image.open(BytesIO(content)) as image:
                    image.verify()
                suffix = ".webp" if "webp" in response.headers.get("content-type", "").lower() else ".png"
                path = target / f"{index}-{hashlib.sha256(url.encode()).hexdigest()[:12]}{suffix}"
                path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(path.write_bytes, content)
                saved.append((url, str(path)))
            except (httpx.HTTPError, OSError, ValueError):
                continue
        return saved

    @staticmethod
    def _event_record(article: CmsArticle) -> dict[str, Any]:
        return {"id": article.cms_id, "title": article.title, "brands": article.brands, "url": article.url,
                "event_display": article.event_display, "venue": article.venue, "updated": article.updated}

    @staticmethod
    def _fetchable_special_page(url: str | None) -> bool:
        return bool(url and url.startswith("https://idolmaster-official.jp/live_event/"))

    async def events(self, query: str = "") -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.db.list_events, query)

    async def tickets(self, query: str = "") -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.db.tickets, query)

    async def detail(self, query: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.db.detail, query)

    async def stats(self, year: str = "", brand: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(self.db.stats, year, brand)

    async def review(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.db.review)

    async def resolve_review(self, item_id: int) -> bool:
        return await asyncio.to_thread(self.db.resolve_review, item_id)

    async def birthday_cast(self, query: str) -> dict[str, Any]:
        """Optional read-only adapter. Missing/unloaded birthday plugin is not an error."""
        if not self.config.get("birthday_integration", False):
            return {"status": "disabled"}
        try:
            module = importlib.import_module("data.plugins.astrbot_plugin_imas_birthday.main")
            api = getattr(module, "call_imasbd_api")
            profile = await api("profile", query=query, render_card=False)
        except (ImportError, AttributeError, RuntimeError, TypeError) as exc:
            return {"status": "unavailable", "reason": type(exc).__name__}
        cv = profile.get('profile', {}).get('cv') if isinstance(profile, dict) and profile.get('ok') else None
        if not cv:
            return {"status": "no_cv", "profile": profile}
        # Current CV is a lookup hint only; historical appearances remain source-authoritative.
        rows = await asyncio.to_thread(self._cast_lookup, str(cv))
        return {"status": "ok", "profile": profile, "matches": rows, "note": "匹配为候选；历史出演以活动官网当场资料为准。"}

    def _cast_lookup(self, query: str) -> list[dict[str, Any]]:
        # Use detail-level fields via a narrow local query without fuzzy auto-linking.
        with self.db._connect() as db:
            return [dict(x) for x in db.execute("SELECT c.*,e.title FROM cast_appearances c JOIN events e ON e.id=c.event_id WHERE c.person_name LIKE ? LIMIT 30", (f"%{query}%",))]

    def _brand_allowed(self, brands_json: str) -> bool:
        wanted = {str(item) for item in self.config.get("brands", []) if str(item)}
        if not wanted:
            return True
        try:
            actual = set(json.loads(brands_json))
        except (TypeError, json.JSONDecodeError):
            return False
        return bool(wanted & actual)

    @staticmethod
    def _display_deadline(value: str, zone: ZoneInfo) -> tuple[str, datetime]:
        deadline = datetime.fromisoformat(value)
        local = deadline.astimezone(zone)
        name = '北京时间' if zone.key == 'Asia/Shanghai' else zone.key
        jst = deadline.astimezone(ZoneInfo('Asia/Tokyo'))
        return f"截止：{local:%Y/%m/%d %H:%M} {name} / {jst:%m/%d %H:%M} JST", local

    @staticmethod
    def _month_window(current: datetime, month: int | None) -> tuple[datetime, datetime, str]:
        """Return a left-closed, right-open local query window and its image title."""
        if month is None:
            start = current.replace(hour=0, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=30), "IM@S LIVE! · Next 30 Days"
        year = current.year if month >= current.month else current.year + 1
        start = current.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1, month=1) if month == 12 else start.replace(month=month + 1)
        return start, end, f"IM@S LIVE! · {start.strftime('%B')} {year}"

    def _status(self, current: datetime, zone: ZoneInfo) -> str:
        last = self.db.meta("last_directory_sync") or self.db.meta('last_successful_sync')
        if not last:
            return "尚未同步"
        try:
            is_stale = current.astimezone(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=int(self.config.get("freshness_hours", 12)))
            local_stamp = datetime.fromisoformat(last).astimezone(zone)
            status = ("缓存陈旧｜" if is_stale else "目录更新 ") + local_stamp.strftime('%Y/%m/%d %H:%M')
        except ValueError:
            status = "核验时间格式异常"
        if self.db.meta('last_error'):
            status += '｜部分来源待核验'
        return status

    async def calendar_entries(self, current: datetime | None = None, month: int | None = None) -> tuple[list[dict[str, Any]], datetime, datetime, str, str]:
        """Return only performances in either the default 30-day or requested-month window."""
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        start, end, title = self._month_window(current, month)
        performances, _ = await asyncio.to_thread(self.db.calendar_rows)
        entries: list[dict[str, Any]] = []
        for row in performances:
            if not self._brand_allowed(row["brands_json"]):
                continue
            try:
                display_day = datetime.strptime(row["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            if start.date() <= display_day < end.date():
                venue = clean(row["venue"] or row["event_venue"] or "场馆待核验")
                session = row["session_label"] or "场次待核验"
                entries.append({"kind": "performance", "display_date": row["date"], "title": row["title"],
                                "subtitle": f"{session}｜{venue}", "brands": json.loads(row["brands_json"]), "url": row["source_url"],
                                "public_number": row.get("public_number")})
        entries.sort(key=lambda item: (item["display_date"], item["title"], item["subtitle"]))
        return entries, start, end, self._status(current, zone), title

    @staticmethod
    def _ticket_time(value: str, label: str, zone: ZoneInfo) -> str:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError("票务时间缺少时区")
        local = moment.astimezone(zone)
        jst = moment.astimezone(ZoneInfo("Asia/Tokyo"))
        zone_name = "北京时间" if zone.key == "Asia/Shanghai" else zone.key
        return f"{label}：{local:%Y/%m/%d %H:%M} {zone_name} / {jst:%Y/%m/%d %H:%M} JST"

    def _source_is_fresh(self, row: dict[str, Any], current: datetime) -> bool:
        try:
            fetched = datetime.fromisoformat(row["source_fetched_at"])
            return row["source_quality"] == "verified" and fetched.tzinfo is not None and current.astimezone(timezone.utc) - fetched.astimezone(timezone.utc) <= timedelta(hours=max(1, int(self.config.get("freshness_hours", 12))))
        except (TypeError, ValueError):
            return False

    async def ticket_entries(self, current: datetime | None = None) -> tuple[list[dict[str, Any]], datetime, datetime, str]:
        """Build cards solely for fresh, currently-open verified onsite lotteries."""
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        start, end, _ = self._month_window(current, None)
        rows = await asyncio.to_thread(self.db.ticket_query_rows)
        entries: list[dict[str, Any]] = []
        time_unverified = invalid_range = stale_source = 0
        for row in rows:
            if not self._brand_allowed(row["brands_json"]):
                continue
            try:
                deadline = datetime.fromisoformat(row["application_end"])
                application_start = datetime.fromisoformat(row["application_start"])
                if deadline.tzinfo is None or application_start.tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                time_unverified += 1
                continue
            now_at_ticket_zone = current.astimezone(deadline.tzinfo)
            if not (application_start <= now_at_ticket_zone < deadline):
                continue
            if deadline <= application_start:
                invalid_range += 1
                continue
            if not self._source_is_fresh(row, current):
                stale_source += 1
                continue
            remaining = deadline - now_at_ticket_zone
            ticket_status, status_label = ("urgent", "24小时内截止") if remaining <= timedelta(hours=24) else ("open", "正在抽选")
            details = [f"轮次：{row['name']}"]
            if application_start:
                details.append(self._ticket_time(row["application_start"], "开始", zone))
            details.append(self._ticket_time(row["application_end"], "截止", zone))
            if row.get("performance_date") or row.get("performance_venue"):
                details.append("演出：" + "｜".join(part for part in (
                    str(row["performance_date"]).replace("-", "/") if row.get("performance_date") else "",
                    clean(row["performance_venue"] or "") if row.get("performance_venue") else "",
                ) if part))
            entries.append({"kind": "ticket", "title": row["title"], "subtitle": "\n".join(details),
                            "brands": json.loads(row["brands_json"]), "url": row["url"] or row["source_url"],
                            "ticket_status": ticket_status, "status_label": status_label,
                            "public_number": row.get("public_number"), "sort_time": deadline})
        priority = {"urgent": 0, "open": 1}
        entries.sort(key=lambda item: (priority[item["ticket_status"]], item["sort_time"], item["title"], item["subtitle"]))
        status = self._status(current, zone)
        diagnostics = []
        if stale_source:
            diagnostics.append(f"{stale_source} 个当前开放轮次的专题缓存待复核")
        if time_unverified:
            diagnostics.append(f"{time_unverified} 个轮次起止时间待核验")
        if invalid_range:
            diagnostics.append(f"{invalid_range} 个轮次时间范围异常")
        if diagnostics:
            status += "｜" + "；".join(diagnostics)
        return entries, start, end, status

    @staticmethod
    def _performance_moment(row: dict[str, Any], zone: ZoneInfo) -> tuple[datetime | None, bool]:
        """Return an absolute JST moment only when the official page gave a clock."""
        try:
            day = datetime.strptime(str(row["date"]), "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError):
            return None, False
        clock = re.search(r"(?:开演\s*)?(\d{1,2}:\d{2})\s*JST", str(row.get("session_label") or ""), re.I)
        if not clock:
            return datetime.combine(day, datetime.min.time(), ZoneInfo("Asia/Tokyo")).astimezone(zone), False
        hour, minute = map(int, clock.group(1).split(":"))
        try:
            return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo("Asia/Tokyo")).astimezone(zone), True
        except ValueError:
            return None, False

    @staticmethod
    def normalize_brand_argument(value: str) -> str | None:
        return BRAND_COMMANDS.get(value.strip().casefold())

    async def next_entry(self, brand_argument: str = "", current: datetime | None = None) -> dict[str, Any] | None:
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        wanted = self.normalize_brand_argument(brand_argument) if brand_argument else None
        if brand_argument and not wanted:
            raise ValueError("未知企划缩写")
        performances, _ = await asyncio.to_thread(self.db.calendar_rows)
        candidates: list[tuple[datetime, bool, dict[str, Any]]] = []
        for row in performances:
            brands = json.loads(row["brands_json"])
            if wanted and wanted not in brands:
                continue
            moment, precise = self._performance_moment(row, zone)
            if not moment:
                continue
            # Date-only dates remain candidates until the date has passed; they
            # never fabricate a start time or become automatic reminders.
            if precise and moment <= current:
                continue
            if not precise and moment.date() < current.date():
                continue
            candidates.append((moment, precise, row))
        if not candidates:
            return None
        moment, precise, row = sorted(candidates, key=lambda item: (item[0], not item[1], item[2]["id"]))[0]
        detail = await asyncio.to_thread(self.db.detail, row["event_id"])
        cast = []
        if detail:
            cast = [item for item in detail["cast"] if item.get("performance_id") == row["id"]]
            # An event-level list is relevant only when no day-specific list exists.
            if not cast:
                cast = [item for item in detail["cast"] if not item.get("performance_id")]
        assets = await asyncio.to_thread(self.db.cast_asset_rows, row["event_id"])
        day_match = re.search(r"DAY\s*(\d+)", str(row.get("session_label") or ""), re.I)
        day_number = day_match.group(1) if day_match else None
        asset_paths = []
        for asset in assets:
            # Do not send Million's DAY2 roster for a DAY1 query.  Assets that
            # do not identify a day are only used when the performance itself
            # has no day label.
            image_day = re.search(r"bnr_day(\d+)\.webp", asset["image_url"], re.I)
            if day_number and image_day and image_day.group(1) != day_number:
                continue
            if day_number and not image_day:
                continue
            if Path(asset["cached_path"]).is_file():
                asset_paths.append(asset["cached_path"])
        venue = clean(row.get("venue") or row.get("event_venue") or "场馆待核验")
        time_text = f"{moment:%Y/%m/%d %H:%M} {'北京时间' if zone.key == 'Asia/Shanghai' else zone.key}" if precise else f"{moment:%Y/%m/%d}｜开演时间待公布/待核验"
        return {"kind": "performance", "display_date": row["date"], "title": row["title"],
                "subtitle": f"{row.get('session_label') or '场次待核验'}｜{time_text}｜{venue}",
                "brands": json.loads(row["brands_json"]), "url": row.get("source_url") or "",
                "public_number": row.get("public_number"), "cast": cast, "precise": precise,
                "official_url": detail["event"].get("official_url") if detail else row.get("source_url"),
                "cast_assets": asset_paths}

    async def ticket_detail(self, number: int, current: datetime | None = None) -> dict[str, Any] | None:
        """Return an event even when all lottery rounds have ended."""
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        detail = await asyncio.to_thread(self.db.detail_by_public_number, number)
        if not detail:
            return None
        event = detail["event"]
        tickets = [row for row in detail["tickets"] if row.get("ticket_scope") == "onsite"]
        result: list[dict[str, Any]] = []
        for row in tickets:
            status = "待核验"
            try:
                start = datetime.fromisoformat(row["application_start"])
                end = datetime.fromisoformat(row["application_end"])
                now_ticket = current.astimezone(end.tzinfo)
                status = "正在抽选" if start <= now_ticket < end and row["sale_method"] == "lottery" else ("抽选已结束" if now_ticket >= end else "尚未开始")
            except (TypeError, ValueError):
                end = None
            if row["sale_method"] == "first_come":
                status = "一般销售/先到先得"
            elif row["sale_method"] == "resale":
                status = "官方转售"
            details = [f"{status}｜{row['name']}"]
            if row.get("application_end"):
                try: details.append(self._ticket_time(row["application_end"], "截止", zone))
                except ValueError: details.append("截止时间待核验")
            result.append({"kind": "ticket", "title": event["title"], "subtitle": "\n".join(details),
                           "brands": json.loads(event["brands_json"]), "url": row.get("url") or row.get("source_url"),
                           "ticket_status": "open" if status == "正在抽选" else "unknown", "status_label": status,
                           "public_number": event["public_number"], "sort_time": row.get("application_end") or ""})
        if not result:
            result.append({"kind": "ticket", "title": event["title"], "subtitle": "尚未公布可核验的现场票务轮次。",
                           "brands": json.loads(event["brands_json"]), "url": event.get("official_url") or "",
                           "ticket_status": "unknown", "status_label": "尚未公布", "public_number": event["public_number"], "sort_time": ""})
        result.sort(key=lambda item: item["sort_time"], reverse=True)
        return {"event": event, "tickets": result, "performances": detail["performances"], "cast": detail["cast"]}

    async def claim_due_reminders(self, current: datetime | None = None) -> list[dict[str, Any]]:
        """Claim the 24h and 1h ticket alerts without backfilling missed nodes."""
        if not self.config.get('enabled', True) or not self.config.get("reminder_enabled", True):
            return []
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        last = self.db.meta("last_successful_sync")
        if not last:
            return []
        try:
            if current.astimezone(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=int(self.config.get("freshness_hours", 12))):
                return []
        except ValueError:
            return []
        _, rows = await asyncio.to_thread(self.db.calendar_rows)
        nodes = self.config.get("ticket_reminder_hours", [24, 1])
        try:
            nodes = sorted({max(1, int(value)) for value in nodes}, reverse=True)
        except (TypeError, ValueError):
            nodes = [24, 1]
        selected: list[dict[str, Any]] = []
        for row in rows:
            try:
                if row['source_quality'] != 'verified' or current.astimezone(timezone.utc) - datetime.fromisoformat(row['source_fetched_at']) > timedelta(hours=max(1, int(self.config.get('freshness_hours', 12)))):
                    continue
            except (TypeError, ValueError):
                continue
            if not self._brand_allowed(row["brands_json"]):
                continue
            try:
                deadline = datetime.fromisoformat(row["application_end"])
                started = datetime.fromisoformat(row["application_start"]) if row["application_start"] else None
            except ValueError:
                continue
            now_at_deadline_zone = current.astimezone(deadline.tzinfo)
            if not (started and started.tzinfo and started <= now_at_deadline_zone):
                continue
            deadline_text, local = self._display_deadline(row["application_end"], zone)
            with self.db._connect() as db:
                umos = [str(x["umo"]) for x in db.execute("SELECT umo FROM ticket_group_subscriptions WHERE enabled=1")]
            for node in nodes:
                due = deadline - timedelta(hours=node)
                # A fresh subscription must not cause an old 24h alert to be
                # dumped into the 1h window.  Failed sends remain retryable.
                if not (due <= now_at_deadline_zone < due + timedelta(minutes=REMINDER_WINDOW_MINUTES)):
                    continue
                for umo in umos:
                    created = self.db.subscription_created_at("ticket", umo)
                    if created and created.astimezone(deadline.tzinfo) > due:
                        continue
                    key = hashlib.sha256(f"ticket|{umo}|{row['id']}|{row['application_end']}|{node}h".encode()).hexdigest()
                    payload = f"{row['title']}｜{row['name']}｜{deadline_text}｜提前{node}小时"
                    claimed = await asyncio.to_thread(self.db.claim_delivery, key, umo, payload)
                    if claimed:
                        selected.append({"delivery_key": key, "umo": umo, "title": row["title"], "brands": json.loads(row["brands_json"]),
                                         "subtitle": f"{row['name']}｜{deadline_text}｜提前{node}小时", "url": row["url"] or row["source_url"],
                                         "remaining_minutes": max(0, int((deadline - now_at_deadline_zone).total_seconds() // 60))})
        return selected

    async def finish_reminders(self, rows: list[dict[str, Any]], success: bool) -> None:
        for row in rows:
            await asyncio.to_thread(self.db.finish_delivery, row["delivery_key"], success)

    async def claim_new_ticket_announcements(self, current: datetime | None = None) -> list[dict[str, Any]]:
        """Claim newly verified onsite lottery rounds for LIVE-subscribed groups.

        Candidate rows are written atomically with a successful special-page
        parse.  The source baseline rules live in the database, while this
        method only decides whether a still-valid candidate may be delivered.
        """
        if not self.config.get("enabled", True) or not self.config.get("ticket_new_announcement_enabled", True):
            return []
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        rows = await asyncio.to_thread(self.db.new_ticket_round_rows)
        with self.db._connect() as db:
            umos = [str(x["umo"]) for x in db.execute("SELECT umo FROM live_group_subscriptions WHERE enabled=1")]
        selected: list[dict[str, Any]] = []
        for row in rows:
            if not self._brand_allowed(row["brands_json"]) or not self._source_is_fresh(row, current):
                continue
            try:
                observed = datetime.fromisoformat(row["observed_at"])
            except (TypeError, ValueError):
                continue
            start = end = None
            try:
                start = datetime.fromisoformat(row["application_start"]) if row.get("application_start") else None
            except (TypeError, ValueError):
                pass
            try:
                end = datetime.fromisoformat(row["application_end"]) if row.get("application_end") else None
            except (TypeError, ValueError):
                pass
            if end and end.tzinfo and current.astimezone(end.tzinfo) >= end:
                continue
            if start and start.tzinfo:
                if current.astimezone(start.tzinfo) < start:
                    status, ticket_status = "新抽选已公布／尚未开始", "upcoming"
                else:
                    status, ticket_status = "新抽选现已开放", "open"
            else:
                status, ticket_status = "新抽选已公布／开始时间待核验", "unknown"
            times: list[str] = []
            if start and start.tzinfo:
                times.append(self._ticket_time(row["application_start"], "开始", zone))
            else:
                times.append("开始时间待核验")
            if end and end.tzinfo:
                times.append(self._ticket_time(row["application_end"], "截止", zone))
            else:
                times.append("截止时间待核验")
            subtitle = f"{row['name']}｜{status}\n" + "\n".join(times)
            url = row.get("url") or row.get("source_url") or ""
            for umo in umos:
                enabled_since = await asyncio.to_thread(self.db.subscription_updated_at, "live", umo)
                # A group enabled after discovery deliberately does not receive
                # accumulated announcements, including after disable/re-enable.
                if enabled_since and observed < enabled_since:
                    continue
                key = f"ticket_new|{umo}|{row['round_id']}"
                payload = f"#{row.get('public_number') or '?'} {row['title']}｜{row['name']}｜{status}"
                if await asyncio.to_thread(self.db.claim_delivery, key, umo, payload, "ticket_new"):
                    selected.append({"delivery_key": key, "notification_type": "ticket_new", "umo": umo,
                                     "title": row["title"], "brands": json.loads(row["brands_json"]),
                                     "public_number": row.get("public_number"), "subtitle": subtitle,
                                     "url": url, "ticket_status": ticket_status, "status_label": status,
                                     "kind": "ticket", "remaining_minutes": 0})
        return selected

    async def claim_due_live_reminders(self, current: datetime | None = None) -> list[dict[str, Any]]:
        """Claim only precise official start times, one hour before Beijing display time."""
        if not self.config.get("enabled", True) or not self.config.get("live_reminder_enabled", True):
            return []
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        performances, _ = await asyncio.to_thread(self.db.calendar_rows)
        with self.db._connect() as db:
            umos = [str(x["umo"]) for x in db.execute("SELECT umo FROM live_group_subscriptions WHERE enabled=1")]
        selected: list[dict[str, Any]] = []
        for row in performances:
            if not self._brand_allowed(row["brands_json"]) or not self._source_is_fresh(row, current):
                continue
            moment, precise = self._performance_moment(row, zone)
            if not precise or not moment:
                continue
            due = moment - timedelta(hours=max(1, int(self.config.get("live_reminder_before_hours", 1))))
            if not (due <= current < due + timedelta(minutes=REMINDER_WINDOW_MINUTES)):
                continue
            for umo in umos:
                created = self.db.subscription_created_at("live", umo)
                if created and created.astimezone(zone) > due:
                    continue
                key = hashlib.sha256(f"live|{umo}|{row['id']}|{moment.isoformat()}".encode()).hexdigest()
                text = f"#{row.get('public_number') or '?'} {row['title']}｜北京时间 {moment:%Y/%m/%d %H:%M} 开演"
                if await asyncio.to_thread(self.db.claim_delivery, key, umo, text):
                    selected.append({"delivery_key": key, "umo": umo, "title": row["title"],
                                     "brands": json.loads(row["brands_json"]), "public_number": row.get("public_number"),
                                     "subtitle": f"北京时间 {moment:%Y/%m/%d %H:%M} 开演｜{row.get('session_label') or ''}",
                                     "url": row.get("source_url") or "", "remaining_minutes": max(0, int((moment-current).total_seconds() // 60))})
        return selected

    async def import_records(self, rows: list[dict[str, Any]], override: bool = False) -> int:
        """Controlled programmatic import: only explicit CMS IDs may be overwritten."""
        count = 0
        for row in rows:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ("id", "title")):
                raise ValueError("导入记录必须有非空 id 与 title")
            if not override and await self.detail(row["id"]):
                continue
            await asyncio.to_thread(self.db.upsert_event, {"id": row["id"], "title": row["title"], "brands": row.get("brands", []), "url": row.get("url"), "event_display": None, "venue": None, "updated": None})
            count += 1
        return count

    async def import_file(self, filename: str, kind: str, override: bool = False) -> int:
        """Read a host-provided JSON/CSV only from this plugin's own imports directory."""
        name = Path(filename).name
        expected = ".json" if kind == "json" else ".csv"
        if name != filename or not name.endswith(expected):
            raise ValueError(f"只接受 imports 目录中的 {expected} 文件名")
        path = self.data_dir / "imports" / name
        if not path.is_file():
            raise ValueError("文件不存在；请由管理员先放入本插件 data/imports 目录")
        if kind == "json":
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")
            rows = json.loads(content)
        else:
            def read_csv() -> list[dict[str, str]]:
                with path.open(encoding="utf-8-sig", newline="") as stream:
                    return list(csv.DictReader(stream))
            rows = await asyncio.to_thread(read_csv)
        if not isinstance(rows, list):
            raise ValueError("导入文件根节点必须是记录数组")
        return await self.import_records(rows, override)
