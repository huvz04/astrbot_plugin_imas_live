"""Async orchestration layer: fetch, parse, store, query and conservative reminders."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib
import json
from datetime import datetime, timedelta, timezone
from dataclasses import asdict as vars_for_slots
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urldefrag
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from .cms import CmsArticle, OfficialCmsClient, SourceUnavailable
from .database import Database
from .parsing import parse_shiny_information, parse_ticket_page, parse_venue, parse_information, schedule_performances, clean
from .models import ParsedPage


class ImasLiveService:
    def __init__(self, data_dir: Path, config: dict[str, Any] | None = None):
        self.config = config if config is not None else {}
        self.data_dir = data_dir
        self.db = Database(data_dir / "imas_live.sqlite3")
        self.db.recover_deliveries()
        self.client = OfficialCmsClient(float(self.config.get("request_timeout_seconds", 25)))
        self._sync_lock = asyncio.Lock()
        self.directory_ready = asyncio.Event()

    async def close(self) -> None:
        await self.client.close()

    def group_enabled(self, umo: str) -> bool:
        return self.db.group_enabled(umo, bool(self.config.get("enabled", True)))

    def set_group_enabled(self, umo: str, value: bool) -> None:
        self.db.set_group_enabled(umo, value)

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
            for article in articles:
                await asyncio.to_thread(self.db.upsert_event, self._event_record(article))
                if full_directory:
                    # These are date entries from the official event field, never range expansion.
                    title = article.title.lower()
                    is_live = any(word in title for word in ('live', 'st@ge', 'stage', 'ライブ', 'musical'))
                    excluded = any(word in title for word in ('museum', 'ホテル', '脱出', '物販', '上映', '発売記念', 'popup'))
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
                    parsed = await self._collect_special(article)
                    if not (parsed.ticket_rounds or parsed.performances):
                        raise SourceUnavailable('专题未能解析，保留已知记录并暂停该来源提醒')
                    digest = hashlib.sha256(json.dumps({
                        'tickets': [row.record() for row in parsed.ticket_rounds],
                        'performances': [vars_for_slots(row) for row in parsed.performances],
                        'cast': [vars_for_slots(row) for row in parsed.cast],
                    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    changed += int(await asyncio.to_thread(self.db.save_parsed, article.cms_id, article.url, digest, "special-page-v2", parsed.ticket_rounds, parsed.performances, parsed.cast, parsed.review_notes))
                except (httpx.HTTPError, SourceUnavailable, ValueError) as exc:
                    failed += 1; await asyncio.to_thread(self.db.source_error, article.cms_id, article.url, str(exc))
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.db.set_meta("last_successful_sync", stamp)
            with self.db._connect() as db:
                unverified = db.execute("SELECT COUNT(*) FROM sources WHERE quality='stale'").fetchone()[0]
            self.db.set_meta('last_error', f'{unverified} 个专题待核验' if unverified else '')
            first = self.db.meta("baseline_complete") is None
            self.db.set_meta("baseline_complete", "1")
            return {"status": "ok", "events": len(articles), "changed_pages": changed, "failed_pages": failed, "baseline": first}

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
            performances = parse_information(html, url)
            if performances and (not result.performances or '/information' in url):
                result.performances = performances
            if '283production_msp' in root:
                cast_data = parse_shiny_information(html, url)
                if cast_data.cast:
                    result.cast, result.performances = cast_data.cast, cast_data.performances
                    for row in result.performances:
                        row.venue = parse_venue(html) or article.venue
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
        return result

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

    async def calendar_entries(self, current: datetime | None = None) -> tuple[list[dict[str, Any]], datetime, datetime, str]:
        """Build the rolling 30-day presentation model independently of network sync."""
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=29)
        performances, deadlines = await asyncio.to_thread(self.db.calendar_rows)
        entries: list[dict[str, Any]] = []
        for row in performances:
            if not self._brand_allowed(row["brands_json"]):
                continue
            try:
                display_day = datetime.strptime(row["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            if start.date() <= display_day <= end.date():
                venue = clean(row["venue"] or row["event_venue"] or "场馆待核验")
                session = row["session_label"] or "场次待核验"
                entries.append({"kind": "performance", "display_date": row["date"], "title": row["title"],
                                "subtitle": f"{session}｜{venue}", "brands": json.loads(row["brands_json"]), "url": row["source_url"]})
        for row in deadlines:
            if not self._brand_allowed(row["brands_json"]):
                continue
            try:
                text, local = self._display_deadline(row["application_end"], zone)
            except ValueError:
                continue
            if start.date() <= local.date() <= end.date():
                entries.append({"kind": "deadline", "display_date": local.date().isoformat(), "title": row["title"],
                                "subtitle": f"{row['name']}｜{text}", "brands": json.loads(row["brands_json"]), "url": row["url"] or row["source_url"]})
        entries.sort(key=lambda item: (item["display_date"], item["kind"] != "deadline", item["title"]))
        last = self.db.meta("last_directory_sync") or self.db.meta('last_successful_sync')
        if not last:
            status = "尚未同步"
        else:
            try:
                is_stale = current.astimezone(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=int(self.config.get("freshness_hours", 12)))
                local_stamp = datetime.fromisoformat(last).astimezone(zone)
                status = ("缓存陈旧｜" if is_stale else "目录更新 ") + local_stamp.strftime('%Y/%m/%d %H:%M')
            except ValueError:
                status = "核验时间格式异常"
        error = self.db.meta('last_error')
        if error:
            status += '｜部分来源待核验'
        return entries, start, end, status

    async def claim_due_reminders(self, current: datetime | None = None) -> list[dict[str, Any]]:
        """Atomically claim valid one-shot lottery deadline reminders for configured UMO targets."""
        if not self.config.get('enabled', True) or not self.config.get("reminder_enabled", True):
            return []
        umos = [str(item) for item in self.config.get("white_umos", []) if str(item)]
        if not umos:
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
        before = max(1, int(self.config.get("reminder_before_minutes", 60)))
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
            due = deadline - timedelta(minutes=before)
            if not (started and started <= now_at_deadline_zone and due <= now_at_deadline_zone < deadline):
                continue
            deadline_text, local = self._display_deadline(row["application_end"], zone)
            for umo in umos:
                if not self.group_enabled(umo):
                    continue
                key = hashlib.sha256(f"{umo}|{row['id']}|deadline|{row['application_end']}".encode()).hexdigest()
                payload = f"{row['title']}｜{row['name']}｜{deadline_text}"
                claimed = await asyncio.to_thread(self.db.claim_delivery, key, umo, payload)
                if claimed:
                    selected.append({"delivery_key": key, "umo": umo, "title": row["title"], "brands": json.loads(row["brands_json"]),
                                     "subtitle": f"{row['name']}｜{deadline_text}", "url": row["url"] or row["source_url"],
                                     "remaining_minutes": max(0, int((deadline - now_at_deadline_zone).total_seconds() // 60))})
        return selected

    async def finish_reminders(self, rows: list[dict[str, Any]], success: bool) -> None:
        for row in rows:
            await asyncio.to_thread(self.db.finish_delivery, row["delivery_key"], success)

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
