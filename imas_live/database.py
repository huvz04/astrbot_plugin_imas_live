"""SQLite persistence with revision evidence and notification de-duplication."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

from .models import CastAppearance, Performance, TicketRound


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, brands_json TEXT NOT NULL,
              official_url TEXT, event_display TEXT, venue TEXT, source_updated TEXT,
              quality TEXT NOT NULL DEFAULT 'candidate', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
              url TEXT PRIMARY KEY, event_id TEXT, content_hash TEXT, fetched_at TEXT NOT NULL,
              parser TEXT NOT NULL, quality TEXT NOT NULL, excerpt TEXT, error TEXT,
              FOREIGN KEY(event_id) REFERENCES events(id)
            );
            CREATE TABLE IF NOT EXISTS revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, source_url TEXT NOT NULL, content_hash TEXT NOT NULL,
              observed_at TEXT NOT NULL, summary TEXT NOT NULL, UNIQUE(source_url, content_hash)
            );
            CREATE TABLE IF NOT EXISTS performances (
              id TEXT PRIMARY KEY, event_id TEXT NOT NULL, date TEXT, session_label TEXT, venue TEXT,
              status TEXT NOT NULL, precision TEXT NOT NULL, source_url TEXT NOT NULL, excerpt TEXT,
              FOREIGN KEY(event_id) REFERENCES events(id)
            );
            CREATE TABLE IF NOT EXISTS ticket_rounds (
              id TEXT PRIMARY KEY, event_id TEXT NOT NULL, name TEXT NOT NULL, ticket_scope TEXT NOT NULL,
              sale_method TEXT NOT NULL, application_start TEXT, application_end TEXT, result_at TEXT,
              payment_start TEXT, payment_end TEXT, url TEXT, seats TEXT, eligibility TEXT,
              source_url TEXT NOT NULL, excerpt TEXT, quality TEXT NOT NULL DEFAULT 'verified',
              FOREIGN KEY(event_id) REFERENCES events(id)
            );
            CREATE TABLE IF NOT EXISTS cast_appearances (
              id TEXT PRIMARY KEY, event_id TEXT NOT NULL, performance_id TEXT, person_name TEXT NOT NULL,
              role_name TEXT, status TEXT NOT NULL, source_url TEXT NOT NULL, excerpt TEXT,
              FOREIGN KEY(event_id) REFERENCES events(id)
            );
            CREATE TABLE IF NOT EXISTS review_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, source_url TEXT NOT NULL,
              note TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open', observed_at TEXT NOT NULL,
              UNIQUE(event_id, source_url, note, state)
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
              id TEXT PRIMARY KEY, umo TEXT NOT NULL, brand TEXT, enabled INTEGER NOT NULL DEFAULT 1,
              remind_results INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_switches (
              umo TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_log (
              dedupe_key TEXT PRIMARY KEY, subscription_id TEXT NOT NULL, state TEXT NOT NULL,
              payload TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """)
            # Early local builds keyed sources only by URL; tours share URLs.
            if not any(row['name'] == 'event_id' and row['pk'] for row in db.execute('PRAGMA table_info(sources)')):
                db.executescript('''ALTER TABLE sources RENAME TO sources_v1;
                    CREATE TABLE sources (
                      url TEXT NOT NULL, event_id TEXT NOT NULL, content_hash TEXT, fetched_at TEXT NOT NULL,
                      parser TEXT NOT NULL, quality TEXT NOT NULL, excerpt TEXT, error TEXT,
                      PRIMARY KEY(event_id,url), FOREIGN KEY(event_id) REFERENCES events(id));
                    INSERT INTO sources SELECT * FROM sources_v1 WHERE event_id IS NOT NULL;
                    DROP TABLE sources_v1;''')

    def upsert_event(self, item: dict[str, Any]) -> None:
        stamp = now()
        with self._connect() as db:
            db.execute("""INSERT INTO events(id,title,brands_json,official_url,event_display,venue,source_updated,created_at,updated_at)
                VALUES(:id,:title,:brands,:url,:display,:venue,:updated,:stamp,:stamp)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, brands_json=excluded.brands_json,
                official_url=excluded.official_url,event_display=excluded.event_display,venue=excluded.venue,
                source_updated=excluded.source_updated,updated_at=excluded.updated_at""", {
                "id": item["id"], "title": item["title"], "brands": json.dumps(item["brands"], ensure_ascii=False),
                "url": item.get("url"), "display": item.get("event_display"), "venue": item.get("venue"),
                "updated": item.get("updated"), "stamp": stamp,
            })

    def save_parsed(self, event_id: str, source_url: str, content_hash: str, parser: str,
                    tickets: Iterable[TicketRound], performances: Iterable[Performance],
                    cast: Iterable[CastAppearance], review_notes: Iterable[str]) -> bool:
        """Store one page atomically. Return true only for a meaningful page change."""
        stamp = now()
        with self._connect() as db:
            existing = db.execute("SELECT content_hash FROM sources WHERE event_id=? AND url=?", (event_id, source_url)).fetchone()
            changed = not existing or existing["content_hash"] != content_hash
            db.execute("""INSERT INTO sources(url,event_id,content_hash,fetched_at,parser,quality,excerpt,error)
                VALUES(?,?,?,?,?,'verified','',NULL)
                ON CONFLICT(event_id,url) DO UPDATE SET content_hash=excluded.content_hash,
                fetched_at=excluded.fetched_at,parser=excluded.parser,quality='verified',error=NULL""",
                (source_url, event_id, content_hash, stamp, parser))
            if not changed:
                return False
            db.execute("INSERT OR IGNORE INTO revisions(source_url,content_hash,observed_at,summary) VALUES(?,?,?,?)",
                       (source_url, content_hash, stamp, f"{parser} changed"))
            # Replacing records from this exact source keeps IDs stable across deadline changes.
            tickets, performances, cast = list(tickets), list(performances), list(cast)
            db.execute("DELETE FROM ticket_rounds WHERE event_id=?", (event_id,))
            db.execute("DELETE FROM performances WHERE event_id=? AND (status!='directory' OR ?)", (event_id, bool(performances)))
            db.execute("DELETE FROM cast_appearances WHERE event_id=?", (event_id,))
            for row in tickets:
                evidence = row.evidence
                db.execute("""INSERT INTO ticket_rounds VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    f'{event_id}:{row.stable_key}',event_id,row.name,row.ticket_scope,row.sale_method,row.application_start,row.application_end,
                    row.result_at,row.payment_start,row.payment_end,row.url,row.seats,row.eligibility,
                    evidence.url if evidence else source_url,evidence.excerpt if evidence else "",evidence.quality if evidence else "verified"))
            for row in performances:
                evidence = row.evidence
                db.execute("INSERT INTO performances VALUES(?,?,?,?,?,?,?,?,?)", (
                    f'{event_id}:{row.stable_key}',event_id,row.date,row.session_label,row.venue,row.status,row.precision,
                    evidence.url if evidence else source_url,evidence.excerpt if evidence else ""))
            for row in cast:
                evidence = row.evidence
                db.execute("INSERT OR IGNORE INTO cast_appearances VALUES(?,?,?,?,?,?,?,?)", (
                    f"{event_id}:{row.performance_key or 'unknown'}:{row.name}:{row.role or ''}", event_id,row.performance_key,row.name,row.role,row.status,
                    evidence.url if evidence else source_url,evidence.excerpt if evidence else ""))
            for note in review_notes:
                db.execute("INSERT OR IGNORE INTO review_items(event_id,source_url,note,observed_at) VALUES(?,?,?,?)",
                           (event_id, source_url, note, stamp))
        return True

    def source_error(self, event_id: str, url: str, error: str) -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO sources(url,event_id,fetched_at,parser,quality,error) VALUES(?,?,?,'fetch','stale',?)
              ON CONFLICT(event_id,url) DO UPDATE SET quality='stale',error=excluded.error""",
              (url, event_id, now(), error[:500]))

    def list_events(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""SELECT e.*, COUNT(DISTINCT p.id) performance_count FROM events e
              LEFT JOIN performances p ON p.event_id=e.id WHERE e.title LIKE ? OR e.brands_json LIKE ?
              GROUP BY e.id ORDER BY e.event_display IS NULL,e.event_display LIMIT ?""", (f"%{query}%", f"%{query}%", limit)).fetchall()
        return [dict(row) for row in rows]

    def fetchable_events(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""SELECT e.* FROM events e LEFT JOIN sources s ON s.event_id=e.id AND s.url=e.official_url
                WHERE e.official_url LIKE 'https://idolmaster-official.jp/live_event/%'
                ORDER BY COALESCE(s.fetched_at,''), e.source_updated DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def save_directory_dates(self, event_id: str, rows: list[Performance]) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM performances WHERE event_id=? AND status='directory'", (event_id,))
            if db.execute("SELECT 1 FROM performances WHERE event_id=? AND status!='directory' LIMIT 1", (event_id,)).fetchone():
                return
            for row in rows:
                db.execute('INSERT OR IGNORE INTO performances VALUES(?,?,?,?,?,?,?,?,?)', (
                    f'{event_id}:directory:{row.stable_key}', event_id, row.date, row.session_label, row.venue,
                    'directory', row.precision, row.evidence.url, row.evidence.excerpt))

    def recover_deliveries(self) -> None:
        with self._connect() as db:
            db.execute("UPDATE delivery_log SET state='failed' WHERE state='inflight'")

    def tickets(self, query: str = "", limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""SELECT t.*, e.title, e.brands_json FROM ticket_rounds t JOIN events e ON e.id=t.event_id
              WHERE (e.title LIKE ? OR e.brands_json LIKE ?) AND t.ticket_scope='onsite'
              ORDER BY t.application_end IS NULL,t.application_end LIMIT ?""", (f"%{query}%", f"%{query}%", limit)).fetchall()
        return [dict(row) for row in rows]

    def detail(self, query: str) -> dict[str, Any] | None:
        with self._connect() as db:
            event = db.execute("SELECT * FROM events WHERE id=? OR title LIKE ? ORDER BY id=? DESC LIMIT 1", (query, f"%{query}%", query)).fetchone()
            if not event:
                return None
            event_id = event["id"]
            return {"event": dict(event), "performances": [dict(x) for x in db.execute("SELECT * FROM performances WHERE event_id=?", (event_id,))],
                    "tickets": [dict(x) for x in db.execute("SELECT * FROM ticket_rounds WHERE event_id=?", (event_id,))],
                    "cast": [dict(x) for x in db.execute("SELECT * FROM cast_appearances WHERE event_id=?", (event_id,))]}

    def stats(self, year: str = "", brand: str = "") -> dict[str, Any]:
        clauses, values = ["1=1"], []
        if year:
            clauses.append("p.date LIKE ?"); values.append(f"{year}%")
        if brand:
            clauses.append("e.brands_json LIKE ?"); values.append(f"%{brand}%")
        where = " AND ".join(clauses)
        with self._connect() as db:
            total = db.execute(f"SELECT COUNT(DISTINCT e.id) events,COUNT(DISTINCT p.id) performances FROM events e LEFT JOIN performances p ON p.event_id=e.id WHERE {where}", values).fetchone()
            uncertain = db.execute("SELECT COUNT(*) n FROM events WHERE id NOT IN (SELECT DISTINCT event_id FROM performances)").fetchone()["n"]
            coverage = db.execute("SELECT MIN(date) first,MAX(date) last FROM performances").fetchone()
        return {"events": total["events"], "performances": total["performances"], "unsplit_candidates": uncertain,
                "coverage_start": coverage["first"], "coverage_end": coverage["last"]}

    def review(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(x) for x in db.execute("SELECT r.*,e.title FROM review_items r LEFT JOIN events e ON e.id=r.event_id WHERE r.state='open' ORDER BY r.observed_at DESC LIMIT ?", (limit,))]

    def resolve_review(self, item_id: int) -> bool:
        with self._connect() as db:
            cursor = db.execute("UPDATE review_items SET state='resolved' WHERE id=? AND state='open'", (item_id,))
            return cursor.rowcount == 1

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as db: db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,value))

    def meta(self, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_group_enabled(self, umo: str, enabled: bool) -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO group_switches(umo,enabled,updated_at) VALUES(?,?,?)
                ON CONFLICT(umo) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at""",
                       (umo, int(enabled), now()))

    def group_enabled(self, umo: str, default: bool = True) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT enabled FROM group_switches WHERE umo=?", (umo,)).fetchone()
        return bool(row["enabled"]) if row else default

    def claim_delivery(self, key: str, subscription_id: str, payload: str) -> bool:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT state FROM delivery_log WHERE dedupe_key=?", (key,)).fetchone()
            if row and row["state"] in ("sent", "inflight"):
                return False
            if row:
                db.execute("UPDATE delivery_log SET state='inflight',payload=?,updated_at=? WHERE dedupe_key=?", (payload, now(), key))
            else:
                db.execute("INSERT INTO delivery_log VALUES(?,?, 'inflight',?,?)", (key,subscription_id,payload,now()))
            return True

    def finish_delivery(self, key: str, success: bool) -> None:
        with self._connect() as db: db.execute("UPDATE delivery_log SET state=?,updated_at=? WHERE dedupe_key=?", ("sent" if success else "failed",now(),key))

    def calendar_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return only concrete performance dates and verified lottery deadlines."""
        with self._connect() as db:
            performances = [dict(row) for row in db.execute("""SELECT p.*,e.title,e.brands_json,e.event_display,e.venue AS event_venue
                FROM performances p JOIN events e ON e.id=p.event_id WHERE p.date IS NOT NULL AND p.status!='cancelled'""")]
            deadlines = [dict(row) for row in db.execute("""SELECT t.*,e.title,e.brands_json,e.event_display,e.venue,
                    s.fetched_at AS source_fetched_at,s.quality AS source_quality
                FROM ticket_rounds t JOIN events e ON e.id=t.event_id
                LEFT JOIN sources s ON s.event_id=e.id AND s.url=CASE WHEN EXISTS(
                    SELECT 1 FROM sources root WHERE root.event_id=e.id AND root.url=e.official_url)
                    THEN e.official_url ELSE t.source_url END
                WHERE t.ticket_scope='onsite' AND t.sale_method='lottery' AND t.application_end IS NOT NULL
                  AND t.quality='verified'""")]
        return performances, deadlines
