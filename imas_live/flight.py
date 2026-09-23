"""Optional Shanghai--Tokyo fare monitoring bound to verified LIVE sessions.

No request is made until an administrator both configures a provider/key and
explicitly enables a paused task. Quotes stay separate from LIVE data and are
rechecked against the selected-performance revision before notification.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

SHANGHAI_AIRPORTS = ("PVG", "SHA")
TOKYO_AIRPORTS = ("NRT", "HND")
# Explicit maintained facts only. Unknown venues never receive this route.
KANTO_VENUES = {"京王アリーナTOKYO", "K-Arena Yokohama", "Kアリーナ横浜", "ぴあアリーナMM",
                "さいたまスーパーアリーナ", "幕張メッセ", "東京ドーム", "有明アリーナ",
                "国立代々木競技場第一体育館"}


class FlightError(ValueError):
    """Safe configuration/provider error intended for a chat response."""


def _normal(value: object) -> str:
    return "".join(str(value or "").split()).casefold()


def _revision(rows: list[dict[str, Any]]) -> str:
    body = [(str(row.get("id")), str(row.get("date")), str(row.get("session_label")),
             _normal(row.get("venue")), str(row.get("status"))) for row in rows]
    return hashlib.sha256(json.dumps(body, ensure_ascii=False).encode()).hexdigest()[:20]


def _safe_link(value: object) -> str:
    parsed = urlparse(str(value or ""))
    return str(value) if parsed.scheme == "https" and parsed.hostname in {"google.com", "www.google.com"} and not parsed.username else ""


class FlightPlanner:
    """SQLite plans, shared quote cache, request budget, and separate delivery."""

    def __init__(self, data_dir: Path, config: dict[str, Any] | None = None):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path, self.config, self.lock = data_dir / "imas_flight.sqlite3", config or {}, asyncio.Lock()
        with self._connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(flight_tasks)")}
            if "event_id" in columns:
                # v0.4.6 plans were always paused; preserve their IDs and UMO.
                old = db.execute("SELECT id,payload_json,umo FROM flight_tasks").fetchall()
                db.execute("ALTER TABLE flight_tasks RENAME TO flight_tasks_v046")
                db.execute("CREATE TABLE flight_tasks (id TEXT PRIMARY KEY,payload_json TEXT NOT NULL)")
                for row in old:
                    task = json.loads(row["payload_json"])
                    task["umo"], task["enabled"] = row["umo"], False
                    task["status"] = "paused_migrated"
                    task["providers"] = []
                    task["baggage_requirement"] = "none"
                    task["updated_at"] = time.time()
                    db.execute("INSERT INTO flight_tasks VALUES (?,?)", (row["id"], json.dumps(task, ensure_ascii=False)))
                db.execute("DROP TABLE flight_tasks_v046")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS flight_tasks (id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS flight_cache (key TEXT PRIMARY KEY, checked REAL NOT NULL, error TEXT NOT NULL, quotes_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS flight_pending (task_id TEXT, umo TEXT, revision TEXT, quote_key TEXT, quote_json TEXT, created REAL, PRIMARY KEY(task_id,umo,quote_key));
                CREATE TABLE IF NOT EXISTS flight_sent (task_id TEXT, umo TEXT, revision TEXT, quote_key TEXT, price REAL, PRIMARY KEY(task_id,umo,revision,quote_key));
                CREATE TABLE IF NOT EXISTS flight_usage (month TEXT PRIMARY KEY, requests INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS flight_progress (task_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0, checked REAL NOT NULL DEFAULT 0);
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15); db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _venue_supported(value: object) -> bool:
        return _normal(value) in {_normal(item) for item in KANTO_VENUES}

    def _providers(self) -> list[str]:
        raw = self.config.get("flight_providers", []) or []
        raw = [raw] if isinstance(raw, str) else raw
        return [p for p in dict.fromkeys(str(x).strip().casefold() for x in raw) if p in {"serpapi", "aviasales"}]

    def _target_price(self) -> int | None:
        try: price = int(self.config.get("flight_target_price_cny", 0))
        except (TypeError, ValueError): return None
        return price if price > 0 else None

    def create_paused_plan(self, detail: dict[str, Any], session_ids: list[str], umo: str,
                           arrival_days: tuple[int, ...] = (2, 1), return_days: tuple[int, ...] = (1, 2)) -> dict[str, Any]:
        if not umo: raise FlightError("请在接收机票提醒的群聊或私聊中创建计划。")
        if detail["event"].get("source_quality", "verified") != "verified":
            raise FlightError("活动来源尚未完成核验，请等待官网同步。")
        rows = {str(row.get("id")): row for row in detail.get("performances", [])}
        selected = [{**rows[key], "venue": rows[key].get("venue") or detail["event"].get("venue")} for key in dict.fromkeys(session_ids) if key in rows]
        if not selected or len(selected) != len(set(session_ids)): raise FlightError("请明确选择有效场次；不会默认参加全部场次。")
        if any(row.get("status") == "cancelled" for row in selected): raise FlightError("所选场次已取消，不能创建机票计划。")
        if not all(self._venue_supported(row.get("venue") or detail["event"].get("venue")) for row in selected): raise FlightError("场馆未被核验为东京机场可服务的关东场馆，计划保持待核实。")
        try: days = sorted(date.fromisoformat(str(row["date"])) for row in selected)
        except (KeyError, TypeError, ValueError): raise FlightError("所选场次缺少已核验的日本当地演出日期。") from None
        first, last = days[0], days[-1]
        if last < datetime.now(ZoneInfo("Asia/Tokyo")).date(): raise FlightError("所选演出已结束，不能创建机票计划。")
        task = {
            "id": uuid.uuid4().hex[:10], "umo": umo, "event_id": str(detail["event"]["id"]), "event_title": str(detail["event"]["title"]), "event_number": detail["event"].get("public_number"),
            "session_ids": [str(row["id"]) for row in selected], "sessions": [{"id": str(row["id"]), "date": str(row["date"]), "label": row.get("session_label"), "venue": row.get("venue") or detail["event"].get("venue")} for row in selected],
            "arrival_dates": [(first - timedelta(days=value)).isoformat() for value in arrival_days], "return_dates": [(last + timedelta(days=value)).isoformat() for value in return_days],
            "origin_airports": list(SHANGHAI_AIRPORTS), "destination_airports": list(TOKYO_AIRPORTS), "currency": "CNY", "adults": 1, "cabin": "economy", "trip": "round_trip", "direct_preferred": True, "baggage_requirement": "none",
            "target_price": self._target_price(), "providers": self._providers(), "enabled": False, "status": "paused_needs_price_and_provider", "revision": _revision(selected), "updated_at": time.time(),
        }
        self._save(task, clear_pending=False)
        return task

    def create_paused_route(self, arrival_date: str, return_date: str, umo: str) -> dict[str, Any]:
        """Ordinary Shanghai--Tokyo monitoring with two explicitly chosen dates."""
        if not umo: raise FlightError("请在接收机票提醒的群聊或私聊中创建计划。")
        try: arrival, returning = date.fromisoformat(arrival_date), date.fromisoformat(return_date)
        except ValueError: raise FlightError("日期格式应为 YYYY-MM-DD。") from None
        if arrival < datetime.now(ZoneInfo("Asia/Tokyo")).date() or returning <= arrival:
            raise FlightError("抵达日必须是今天以后；返程日必须晚于抵达日。")
        task = {
            "id": uuid.uuid4().hex[:10], "umo": umo, "event_id": "", "event_title": "上海—东京自选日期",
            "event_number": None, "session_ids": [], "sessions": [], "arrival_dates": [arrival.isoformat()],
            "return_dates": [returning.isoformat()], "origin_airports": list(SHANGHAI_AIRPORTS),
            "destination_airports": list(TOKYO_AIRPORTS), "currency": "CNY", "adults": 1, "cabin": "economy",
            "trip": "round_trip", "direct_preferred": True, "baggage_requirement": "none",
            "target_price": self._target_price(), "providers": self._providers(), "enabled": False,
            "status": "paused_needs_price_and_provider",
            "revision": hashlib.sha256(f"route|{arrival}|{returning}".encode()).hexdigest()[:20],
            "updated_at": time.time(),
        }
        self._save(task, clear_pending=False)
        return task

    def _save(self, task: dict[str, Any], clear_pending: bool = True) -> None:
        task["updated_at"] = time.time()
        with self._connect() as db:
            db.execute("INSERT INTO flight_tasks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json", (task["id"], json.dumps(task, ensure_ascii=False)))
            if clear_pending: db.execute("DELETE FROM flight_pending WHERE task_id=?", (task["id"],))

    def set_price(self, task_id: str, umo: str, price: int) -> dict[str, Any]:
        task = self.task(task_id)
        if task["umo"] != umo: raise FlightError("当前会话未创建该机票计划。")
        if not 1 <= price <= 1000000: raise FlightError("心理价需为 1 至 1000000 人民币。")
        task["target_price"] = price
        task["enabled"], task["status"] = False, "paused"
        self._save(task)
        return task

    def set_baggage(self, task_id: str, umo: str, requirement: str) -> dict[str, Any]:
        task = self.task(task_id)
        if task["umo"] != umo: raise FlightError("当前会话未创建该机票计划。")
        if requirement not in {"none", "checked"}: raise FlightError("行李要求只能是 none 或 checked。")
        task["baggage_requirement"] = requirement
        task["enabled"], task["status"] = False, "paused"
        self._save(task)
        return task

    def task(self, task_id: str) -> dict[str, Any]:
        with self._connect() as db: row = db.execute("SELECT payload_json FROM flight_tasks WHERE id=?", (task_id,)).fetchone()
        if not row: raise KeyError("机票计划不存在。")
        return json.loads(row["payload_json"])

    def tasks_for(self, umo: str) -> list[dict[str, Any]]:
        with self._connect() as db: rows = db.execute("SELECT payload_json FROM flight_tasks WHERE json_extract(payload_json,'$.umo')=? ORDER BY json_extract(payload_json,'$.updated_at') DESC", (umo,)).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def all_tasks(self) -> list[dict[str, Any]]:
        # Least recently checked tasks go first when a shared monthly budget is scarce.
        with self._connect() as db:
            rows = db.execute("""SELECT t.payload_json FROM flight_tasks t
                LEFT JOIN flight_progress p ON p.task_id=t.id
                ORDER BY COALESCE(p.checked,0),t.id""").fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def set_enabled(self, task_id: str, umo: str, enabled: bool) -> dict[str, Any]:
        task = self.task(task_id)
        if task["umo"] != umo: raise FlightError("当前会话未创建该机票计划。")
        if enabled:
            task["providers"] = self._providers()
            if not task.get("target_price"): raise FlightError("请先设置大于 0 的 flight_target_price_cny。")
            if not task.get("providers"): raise FlightError("请先设置 flight_providers 并填写相应密钥。")
            if not str(self.config.get("flight_serpapi_key", "")).strip(): raise FlightError("尚未配置 flight_serpapi_key。")
            if int(self.config.get("flight_serpapi_monthly_budget", 0)) <= 0: raise FlightError("请先设置正数的 flight_serpapi_monthly_budget。")
            # Aviasales is reserved for a later adapter; it cannot activate monitoring by itself.
            if "serpapi" not in task["providers"]: raise FlightError("Aviasales 适配器尚未接入；完整往返提醒需要 SerpApi。")
        task["enabled"], task["status"] = bool(enabled), "active" if enabled else "paused"; self._save(task)
        return task

    def _spend(self) -> None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self._connect() as db:
            row = db.execute("SELECT requests FROM flight_usage WHERE month=?", (month,)).fetchone(); used = int(row[0]) if row else 0
            limit = max(0, int(self.config.get("flight_serpapi_monthly_budget", 0)))
            if not limit or used >= limit: raise FlightError("SerpApi 月度请求预算为 0 或已用尽。")
            db.execute("INSERT INTO flight_usage VALUES (?,?) ON CONFLICT(month) DO UPDATE SET requests=excluded.requests", (month, used + 1))

    @staticmethod
    def _quote_key(quote: dict[str, Any]) -> str:
        fields = ("provider", "market", "origin", "destination", "return_origin", "return_destination", "departure", "arrival_date", "return_date", "return_departure_date", "outbound_flights", "return_flights", "currency", "adults", "cabin", "baggage")
        return hashlib.sha256(json.dumps([quote.get(k) for k in fields], sort_keys=True).encode()).hexdigest()[:24]

    async def _serpapi_get(self, session: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Count every attempted paid request; never expose the key in errors."""
        import aiohttp
        self._spend()
        try:
            async with session.get("https://serpapi.com/search.json", params=params, allow_redirects=False) as response:
                if response.status != 200: raise FlightError(f"SerpApi HTTP {response.status}。")
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise FlightError("SerpApi 网络异常或返回结构错误。") from exc
        if not isinstance(data, dict) or data.get("error") or (data.get("search_metadata") or {}).get("status") != "Success":
            raise FlightError("SerpApi 查询失败；请检查密钥或额度。")
        return data

    async def _fetch_serpapi(self, task: dict[str, Any], origin: str, destination: str, outbound: str, returning: str) -> list[dict[str, Any]]:
        try: import aiohttp
        except ModuleNotFoundError as exc: raise FlightError("缺少 aiohttp；请安装插件依赖后再启用机票数据源。") from exc
        key = str(self.config.get("flight_serpapi_key", "")).strip()
        if not key: raise FlightError("尚未配置 flight_serpapi_key。")
        params = {"engine":"google_flights", "api_key":key, "departure_id":origin, "arrival_id":destination,
                  "outbound_date":outbound, "return_date":returning, "type":"1", "adults":1,
                  "travel_class":1, "currency":"CNY", "stops":"1" if task["direct_preferred"] else "0",
                  "hl":"zh-CN", "gl":str(self.config.get("flight_serpapi_market", "us")), "deep_search":"true"}
        results = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=50)) as session:
            first = await self._serpapi_get(session, params)
            candidates = (first.get("best_flights") or []) + (first.get("other_flights") or [])
            candidates = [item for item in candidates if isinstance(item, dict) and item.get("departure_token")
                          and self._valid_leg(item.get("flights"), origin, destination, outbound,
                                              task["arrival_dates"], task["direct_preferred"])]
            # Return selection is an additional paid request. A low bounded cap
            # avoids multiplying one initial search into uncontrolled spending.
            for outbound_item in candidates[:max(1, min(4, int(self.config.get("flight_max_return_expansions", 2))))]:
                token = outbound_item.get("departure_token") if isinstance(outbound_item, dict) else None
                second = await self._serpapi_get(session, {**params, "departure_token": token})
                results.extend(self._parse_serpapi(second, task, origin, destination, outbound, returning, outbound_item,
                                                   _safe_link((first.get("search_metadata") or {}).get("google_flights_url"))))
        return results

    @staticmethod
    def _valid_leg(segments: object, origin: str, destination: str, departure: str,
                   arrivals: list[str], direct: bool) -> bool:
        if not isinstance(segments, list) or not segments or (direct and len(segments) != 1): return False
        try:
            first, last = segments[0], segments[-1]
            start = str(first["departure_airport"]["time"]); finish = str(last["arrival_airport"]["time"])
            datetime.strptime(start, "%Y-%m-%d %H:%M"); datetime.strptime(finish, "%Y-%m-%d %H:%M")
            return (first["departure_airport"]["id"] in origin.split(",") and last["arrival_airport"]["id"] in destination.split(",")
                    and start[:10] == departure and finish[:10] in arrivals)
        except (KeyError, TypeError, ValueError): return False

    def _parse_serpapi(self, data: dict[str, Any], task: dict[str, Any], origin: str, destination: str,
                       outbound: str, returning: str, outbound_item: dict[str, Any], search_link: str = "") -> list[dict[str, Any]]:
        """Only the expanded return response may form an alertable round trip."""
        result, now = [], time.time()
        try:
            source_at = datetime.strptime((data.get("search_metadata") or {})["created_at"],
                                          "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp()
        except (KeyError, TypeError, ValueError):
            source_at = None
        out = outbound_item.get("flights")
        if not self._valid_leg(out, origin, destination, outbound, task["arrival_dates"], task["direct_preferred"]): return result
        for item in (data.get("best_flights") or []) + (data.get("other_flights") or []):
            if not isinstance(item, dict): continue
            back = item.get("flights")
            if not isinstance(back, list) or not back or (task["direct_preferred"] and len(back) != 1): continue
            try:
                ro = str(back[0]["departure_airport"]["id"]); rd = str(back[-1]["arrival_airport"]["id"])
                return_time = str(back[0]["departure_airport"]["time"])
                datetime.strptime(return_time, "%Y-%m-%d %H:%M")
                price = float(item["price"])
            except (KeyError, TypeError, ValueError): continue
            if ro not in task["destination_airports"] or rd not in task["origin_airports"] or return_time[:10] != returning: continue
            if item.get("type") not in ("Round trip", "round_trip"): continue
            if not math.isfinite(price) or price <= 0: continue
            result.append({"provider":"serpapi", "origin":str(out[0]["departure_airport"]["id"]),
                "destination":str(out[-1]["arrival_airport"]["id"]),
                "return_origin":ro, "return_destination":rd, "departure":outbound,
                "departure_time":str(out[0]["departure_airport"]["time"]),
                "arrival_date":str(out[-1]["arrival_airport"]["time"])[:10],
                "return_date":returning, "return_departure_date":returning,
                "return_departure_time":return_time,
                "outbound_flights":[str(x.get("flight_number") or x.get("airline") or "") for x in out],
                "return_flights":[str(x.get("flight_number") or x.get("airline") or "") for x in back],
                "outbound_stops":len(out)-1, "return_stops":len(back)-1, "price":price,
                "currency":"CNY", "adults":1, "cabin":"economy", "baggage":"unknown",
                "market":str(self.config.get("flight_serpapi_market", "us")),
                "link":search_link or _safe_link((data.get("search_metadata") or {}).get("google_flights_url")),
                "fetched_at":now, "source_at":source_at,
                "note":"Google Flights / SerpApi；往返两段已返回，可能使用缓存。税费与托运行李请在结果页复核。"})
        return result

    def _queries(self, task: dict[str, Any]) -> list[tuple[str, str, str, str]]:
        origin, destination = ",".join(task["origin_airports"]), ",".join(task["destination_airports"])
        return list(dict.fromkeys((origin, destination, outbound, returning)
                                  for arrival in task["arrival_dates"] for returning in task["return_dates"]
                                  for outbound in ((date.fromisoformat(arrival) - timedelta(days=1)).isoformat(), arrival)))

    def _cache_key(self, task: dict[str, Any], query: tuple[str, str, str, str]) -> str:
        origin, destination, outbound, returning = query
        payload = ("serpapi", origin, destination, outbound, returning, task["currency"],
                   task["arrival_dates"], task["direct_preferred"], str(self.config.get("flight_serpapi_market", "us")))
        return hashlib.sha256(json.dumps(payload).encode()).hexdigest()

    def snapshot(self) -> dict[str, Any]:
        """Authenticated WebUI view; credentials never enter the response."""
        tasks = self.all_tasks()
        with self._connect() as db:
            for task in tasks:
                states, quotes = [], []
                for query in self._queries(task):
                    row = db.execute("SELECT checked,error,quotes_json FROM flight_cache WHERE key=?", (self._cache_key(task, query),)).fetchone()
                    if not row: continue
                    states.append({"query": query, "checked": row["checked"], "error": row["error"]})
                    for quote in json.loads(row["quotes_json"]):
                        quote["stale"] = (bool(row["error"]) or time.time() - row["checked"] > 6 * 3600
                                          or time.time() - float(quote.get("fetched_at") or 0) > 6 * 3600
                                          or (quote.get("source_at") is not None
                                              and time.time() - float(quote["source_at"]) > 6 * 3600))
                        quote["meets_price"] = bool(task.get("target_price") and quote.get("price", float("inf")) <= task["target_price"])
                        quotes.append(quote)
                task["states"] = states
                task["quotes"] = sorted(quotes, key=lambda q: q.get("price", float("inf")))[:30]
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            usage = db.execute("SELECT requests FROM flight_usage WHERE month=?", (month,)).fetchone()
            pending = db.execute("SELECT COUNT(*) FROM flight_pending").fetchone()[0]
        return {"tasks": tasks, "serpapi_requests": usage[0] if usage else 0,
                "serpapi_budget": max(0, int(self.config.get("flight_serpapi_monthly_budget", 0))),
                "pending": pending, "provider_configured": bool(self.config.get("flight_serpapi_key"))}

    @staticmethod
    def _eligible_quote(task: dict[str, Any], quote: dict[str, Any]) -> bool:
        try:
            price = float(quote["price"])
            fetched = float(quote["fetched_at"])
            source_at = quote.get("source_at")
            if source_at is not None and not 0 <= time.time() - float(source_at) <= 6 * 3600:
                return False
            departure_local = datetime.strptime(str(quote["departure_time"]), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            if departure_local <= datetime.now(ZoneInfo("Asia/Shanghai")):
                return False
            return (quote.get("provider") == "serpapi" and math.isfinite(price) and 0 < price <= float(task["target_price"])
                    and 0 <= time.time() - fetched <= 6 * 3600
                    and quote.get("origin") in task["origin_airports"] and quote.get("destination") in task["destination_airports"]
                    and quote.get("return_origin") in task["destination_airports"] and quote.get("return_destination") in task["origin_airports"]
                    and quote.get("arrival_date") in task["arrival_dates"] and quote.get("return_departure_date") in task["return_dates"]
                    and quote.get("return_date") == quote.get("return_departure_date")
                    and quote.get("currency") == task["currency"] and quote.get("adults") == 1 and quote.get("cabin") == "economy"
                    and all(quote.get("outbound_flights") or []) and all(quote.get("return_flights") or [])
                    and bool(quote.get("outbound_flights")) and bool(quote.get("return_flights"))
                    and (not task["direct_preferred"] or (quote.get("outbound_stops") == 0 and quote.get("return_stops") == 0))
                    and (task.get("baggage_requirement") != "checked" or quote.get("baggage") == "checked_included"))
        except (KeyError, TypeError, ValueError): return False

    def due(self, task: dict[str, Any]) -> bool:
        if not task.get("enabled"): return False
        with self._connect() as db: row = db.execute("SELECT checked FROM flight_progress WHERE task_id=?", (task["id"],)).fetchone()
        return not row or time.time() - row["checked"] >= max(1, int(self.config.get("flight_interval_hours", 12))) * 3600

    async def check(self, task_id: str, fetcher: Callable[[dict[str, Any], str, str, str, str], Awaitable[list[dict[str, Any]]]] | None = None) -> list[dict[str, Any]]:
        async with self.lock:
            task = self.task(task_id)
            if not task.get("enabled") or "serpapi" not in task["providers"]: return []
            fetch = fetcher or (lambda t, o, d, out, ret: self._fetch_serpapi(t, o, d, out, ret)); quotes = []
            queries = self._queries(task)
            with self._connect() as db: progress = db.execute("SELECT cursor FROM flight_progress WHERE task_id=?", (task_id,)).fetchone()
            cursor = int(progress["cursor"]) if progress else 0
            limit = max(1, min(8, int(self.config.get("flight_max_queries_per_cycle", 1))))
            picked = [queries[(cursor + index) % len(queries)] for index in range(min(limit, len(queries)))]
            for origin, destination, outbound, returning in picked:
                cache_key = self._cache_key(task, (origin, destination, outbound, returning))
                with self._connect() as db: cached = db.execute("SELECT checked,error,quotes_json FROM flight_cache WHERE key=?", (cache_key,)).fetchone()
                if cached and not cached["error"] and time.time() - cached["checked"] < 3600:
                    quotes.extend(json.loads(cached["quotes_json"])); continue
                try:
                    found = await fetch(task, origin, destination, outbound, returning)
                    with self._connect() as db: db.execute("INSERT INTO flight_cache VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET checked=excluded.checked,error=excluded.error,quotes_json=excluded.quotes_json", (cache_key, time.time(), "", json.dumps(found, ensure_ascii=False)))
                    quotes.extend(found)
                except FlightError as exc:
                    with self._connect() as db:
                        db.execute("INSERT INTO flight_cache VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET checked=excluded.checked,error=excluded.error", (cache_key, time.time(), str(exc), "[]"))
                        db.execute("DELETE FROM flight_pending WHERE task_id=?", (task_id,))
                    if "预算" in str(exc): break
                except Exception:
                    # A malformed provider response must not expose the API key
                    # or leave old notifications queued as if the check succeeded.
                    with self._connect() as db:
                        db.execute("INSERT INTO flight_cache VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET checked=excluded.checked,error=excluded.error", (cache_key, time.time(), "供应商返回结构异常。", "[]"))
                        db.execute("DELETE FROM flight_pending WHERE task_id=?", (task_id,))
            if self.task(task_id) != task or not task.get("enabled"): return []
            with self._connect() as db:
                db.execute("INSERT INTO flight_progress VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET cursor=excluded.cursor,checked=excluded.checked", (task_id, (cursor + len(picked)) % len(queries), time.time()))
            valid = {self._quote_key(q): q for q in quotes if self._eligible_quote(task, q)}
            with self._connect() as db:
                for key, quote in sorted(valid.items(), key=lambda item: item[1]["price"])[:3]:
                    sent = db.execute("SELECT price FROM flight_sent WHERE task_id=? AND umo=? AND revision=? AND quote_key=?", (task_id, task["umo"], task["revision"], key)).fetchone()
                    if not sent or quote["price"] <= sent["price"] * .95: db.execute("INSERT OR REPLACE INTO flight_pending VALUES (?,?,?,?,?,?)", (task_id, task["umo"], task["revision"], key, json.dumps(quote, ensure_ascii=False), time.time()))
            return list(valid.values())

    def revalidate(self, task: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
        if not detail:
            if task.get("enabled"):
                task["enabled"], task["status"] = False, "paused_event_missing"; self._save(task)
            return task
        if detail["event"].get("source_quality", "verified") != "verified":
            if task.get("enabled"):
                task["enabled"], task["status"] = False, "paused_event_source_stale"; self._save(task)
            return task
        rows = {str(row.get("id")): row for row in detail.get("performances", [])}
        selected = [{**rows[key], "venue": rows[key].get("venue") or detail["event"].get("venue")} for key in task["session_ids"] if key in rows]
        if (len(selected) != len(task["session_ids"]) or any(row.get("status") == "cancelled" for row in selected)
                or not all(self._venue_supported(row.get("venue") or detail["event"].get("venue")) for row in selected)):
            if task.get("enabled"):
                task["enabled"], task["status"] = False, "paused_event_changed_or_unverified"; self._save(task)
            return task
        if _revision(selected) != task["revision"]:
            if task.get("enabled") or task.get("status") != "paused_event_changed":
                task["enabled"], task["status"] = False, "paused_event_changed"
                self._save(task)
        elif task.get("status") == "paused_event_source_stale":
            task["status"] = "paused"
            self._save(task)
        return task

    async def deliver(self, sender: Callable[[str, dict[str, Any], list[dict[str, Any]]], Awaitable[bool]]) -> None:
        with self._connect() as db: groups = db.execute("SELECT DISTINCT task_id,umo,revision FROM flight_pending").fetchall()
        for group in groups:
            try: task = self.task(group["task_id"])
            except KeyError:
                with self._connect() as db: db.execute("DELETE FROM flight_pending WHERE task_id=?", (group["task_id"],))
                continue
            with self._connect() as db: rows = db.execute("SELECT quote_key,quote_json FROM flight_pending WHERE task_id=? AND umo=? AND revision=? ORDER BY created LIMIT 3", tuple(group)).fetchall()
            if not task.get("enabled") or task["umo"] != group["umo"] or task["revision"] != group["revision"]:
                with self._connect() as db: db.execute("DELETE FROM flight_pending WHERE task_id=? AND umo=?", (group["task_id"], group["umo"])); continue
            eligible = [(row, json.loads(row["quote_json"])) for row in rows]
            eligible = [(row, quote) for row, quote in eligible if self._eligible_quote(task, quote)]
            if len(eligible) != len(rows):
                with self._connect() as db:
                    for row in rows:
                        if row["quote_key"] not in {item[0]["quote_key"] for item in eligible}:
                            db.execute("DELETE FROM flight_pending WHERE task_id=? AND umo=? AND quote_key=?", (task["id"], task["umo"], row["quote_key"]))
            if not eligible: continue
            try: ok = await sender(group["umo"], task, [quote for _, quote in eligible])
            except Exception: ok = False
            if ok and self.task(task["id"]) == task:
                with self._connect() as db:
                    for row, quote in eligible:
                        db.execute("INSERT OR REPLACE INTO flight_sent VALUES (?,?,?,?,?)", (task["id"], task["umo"], task["revision"], row["quote_key"], quote["price"]))
                        db.execute("DELETE FROM flight_pending WHERE task_id=? AND umo=? AND quote_key=?", (task["id"], task["umo"], row["quote_key"]))
