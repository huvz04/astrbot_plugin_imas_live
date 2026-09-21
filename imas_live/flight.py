"""Paused, activity-bound Shanghai--Tokyo flight-monitor task planning.

This module intentionally stores no provider credentials and makes no network
requests.  It is the safe boundary between verified LIVE dates and an optional
flight quote provider: an administrator must explicitly create and later enable
a task after configuring a price rule and a provider.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any


SHANGHAI_AIRPORTS = ("PVG", "SHA")
TOKYO_AIRPORTS = ("NRT", "HND")
# Only maintained, explicit venue evidence may create this route automatically.
KANTO_VENUES = {"京王アリーナ TOKYO": "tokyo"}


class FlightPlanner:
    """Persist user-owned, paused plans independently of LIVE subscriptions."""

    def __init__(self, data_dir: Path, config: dict[str, Any] | None = None):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "imas_flight.sqlite3"
        self.config = config or {}
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS flight_tasks (
                id TEXT PRIMARY KEY, event_id TEXT NOT NULL, revision TEXT NOT NULL,
                session_ids_json TEXT NOT NULL, payload_json TEXT NOT NULL,
                umo TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")

    def _connect(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def _venue_supported(value: str | None) -> bool:
        return str(value or "").strip() in KANTO_VENUES

    def create_paused_plan(self, detail: dict[str, Any], session_ids: list[str], umo: str,
                           arrival_days: tuple[int, ...] = (2, 1), return_days: tuple[int, ...] = (1, 2)) -> dict[str, Any]:
        if not umo:
            raise ValueError("请在需要接收机票提醒的私聊或群聊中创建计划。")
        performances = {str(row["id"]): row for row in detail.get("performances", [])}
        selected = [performances[item] for item in dict.fromkeys(session_ids) if item in performances]
        if not selected or len(selected) != len(set(session_ids)):
            raise ValueError("请从活动详情中明确选择有效场次；不会默认参加全部场次。")
        if not all(self._venue_supported(row.get("venue") or detail["event"].get("venue")) for row in selected):
            raise ValueError("场馆尚未核实为可由东京机场服务的关东场馆，计划保持待核实。")
        try:
            days = sorted(date.fromisoformat(str(row["date"])) for row in selected)
        except (TypeError, ValueError):
            raise ValueError("所选场次缺少已核验的日本当地演出日期。") from None
        first, last = days[0], days[-1]
        arrivals = [(first - timedelta(days=value)).isoformat() for value in arrival_days]
        returns = [(last + timedelta(days=value)).isoformat() for value in return_days]
        revision = hashlib.sha256(json.dumps([(row["id"], row["date"], row.get("session_label"), row.get("venue")) for row in selected], ensure_ascii=False).encode()).hexdigest()[:20]
        task = {
            "id": uuid.uuid4().hex[:10], "event_id": detail["event"]["id"],
            "event_title": detail["event"]["title"], "event_number": detail["event"].get("public_number"),
            "session_ids": [row["id"] for row in selected], "sessions": [{"date": row["date"], "label": row.get("session_label"), "venue": row.get("venue") or detail["event"].get("venue")} for row in selected],
            "arrival_dates": arrivals, "return_dates": returns, "origin_airports": list(SHANGHAI_AIRPORTS),
            "destination_airports": list(TOKYO_AIRPORTS), "trip": "round_trip", "direct_preferred": True,
            "baggage": "unknown", "currency": "CNY", "target_price": self._target_price(),
            "provider": str(self.config.get("flight_provider", "") or "").strip(),
            "enabled": False, "status": "paused_needs_price_and_provider", "revision": revision,
        }
        with self._connect() as db:
            db.execute("INSERT INTO flight_tasks VALUES (?,?,?,?,?,?,0,CURRENT_TIMESTAMP)",
                       (task["id"], task["event_id"], revision, json.dumps(task["session_ids"]), json.dumps(task, ensure_ascii=False), umo))
        return task

    def _target_price(self) -> int | None:
        """A non-positive configured price means no price rule has been set."""
        try:
            value = int(self.config.get("flight_target_price_cny", 0))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def tasks_for(self, umo: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT payload_json,enabled FROM flight_tasks WHERE umo=? ORDER BY created_at DESC", (umo,)).fetchall()
        return [{**json.loads(row[0]), "enabled": bool(row[1])} for row in rows]
