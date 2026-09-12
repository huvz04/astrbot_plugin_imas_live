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
              payload TEXT NOT NULL, updated_at TEXT NOT NULL,
              notification_type TEXT NOT NULL DEFAULT 'legacy'
            );
            -- This mapping is separate from CMS IDs so a number does not change
            -- when an event is refreshed, filtered, or eventually removed.
            CREATE TABLE IF NOT EXISTS event_numbers (
              event_id TEXT PRIMARY KEY, public_number INTEGER NOT NULL UNIQUE,
              FOREIGN KEY(event_id) REFERENCES events(id)
            );
            CREATE TABLE IF NOT EXISTS live_group_subscriptions (
              umo TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ticket_group_subscriptions (
              umo TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cast_assets (
              event_id TEXT NOT NULL, image_url TEXT NOT NULL, cached_path TEXT NOT NULL,
              source_url TEXT NOT NULL, fetched_at TEXT NOT NULL,
              PRIMARY KEY(event_id,image_url), FOREIGN KEY(event_id) REFERENCES events(id)
            );
            -- A source must be successfully parsed once before its ticket
            -- rounds are eligible for change detection.  This avoids turning
            -- gradual coverage of old pages into announcements.
            CREATE TABLE IF NOT EXISTS ticket_source_baselines (
              event_id TEXT NOT NULL, source_url TEXT NOT NULL, baselined_at TEXT NOT NULL,
              PRIMARY KEY(event_id,source_url), FOREIGN KEY(event_id) REFERENCES events(id)
            );
            -- Full directory discovery is the only way a first-seen event can
            -- be treated as genuinely new rather than backlog coverage.
            CREATE TABLE IF NOT EXISTS ticket_new_event_discoveries (
              event_id TEXT PRIMARY KEY, discovered_at TEXT NOT NULL,
              initial_ticket_notice_recorded INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(event_id) REFERENCES events(id)
            );
            -- Pending, verified additions are intentionally independent of
            -- deadline reminders and are claimed per subscribed group later.
            CREATE TABLE IF NOT EXISTS ticket_new_rounds (
              round_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, source_url TEXT NOT NULL,
              observed_at TEXT NOT NULL, FOREIGN KEY(event_id) REFERENCES events(id)
            );
            """)
            if not any(row['name'] == 'notification_type' for row in db.execute('PRAGMA table_info(delivery_log)')):
                db.execute("ALTER TABLE delivery_log ADD COLUMN notification_type TEXT NOT NULL DEFAULT 'legacy'")
            # Early local builds keyed sources only by URL; tours share URLs.
            if not any(row['name'] == 'event_id' and row['pk'] for row in db.execute('PRAGMA table_info(sources)')):
                db.executescript('''ALTER TABLE sources RENAME TO sources_v1;
                    CREATE TABLE sources (
                      url TEXT NOT NULL, event_id TEXT NOT NULL, content_hash TEXT, fetched_at TEXT NOT NULL,
                      parser TEXT NOT NULL, quality TEXT NOT NULL, excerpt TEXT, error TEXT,
                      PRIMARY KEY(event_id,url), FOREIGN KEY(event_id) REFERENCES events(id));
                    INSERT INTO sources SELECT * FROM sources_v1 WHERE event_id IS NOT NULL;
                    DROP TABLE sources_v1;''')
            if not any(row['name'] == 'attempted_at' for row in db.execute('PRAGMA table_info(sources)')):
                db.execute("ALTER TABLE sources ADD COLUMN attempted_at TEXT")
                db.execute("UPDATE sources SET attempted_at=fetched_at")
            # Upgrade existing installations before their next directory sync.
            # First assignment prefers the nearest dated performance, then
            # stable creation/ID ordering; it is never used as a live ranking.
            missing = db.execute("""SELECT e.id FROM events e LEFT JOIN event_numbers n ON n.event_id=e.id
                LEFT JOIN performances p ON p.event_id=e.id
                WHERE n.event_id IS NULL GROUP BY e.id
                ORDER BY MIN(CASE WHEN p.date>=date('now') THEN p.date END) IS NULL,
                    MIN(CASE WHEN p.date>=date('now') THEN p.date END),e.created_at,e.id""").fetchall()
            for row in missing:
                db.execute("INSERT INTO event_numbers(event_id,public_number) VALUES(?, COALESCE((SELECT MAX(public_number)+1 FROM event_numbers),1))", (row["id"],))

    def upsert_event(self, item: dict[str, Any], discovered_after_baseline: bool = False) -> bool:
        stamp = now()
        with self._connect() as db:
            is_new = not bool(db.execute("SELECT 1 FROM events WHERE id=?", (item["id"],)).fetchone())
            db.execute("""INSERT INTO events(id,title,brands_json,official_url,event_display,venue,source_updated,created_at,updated_at)
                VALUES(:id,:title,:brands,:url,:display,:venue,:updated,:stamp,:stamp)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, brands_json=excluded.brands_json,
                official_url=excluded.official_url,event_display=excluded.event_display,venue=excluded.venue,
                source_updated=excluded.source_updated,updated_at=excluded.updated_at""", {
                "id": item["id"], "title": item["title"], "brands": json.dumps(item["brands"], ensure_ascii=False),
                "url": item.get("url"), "display": item.get("event_display"), "venue": item.get("venue"),
                "updated": item.get("updated"), "stamp": stamp,
            })
            # SQLite serializes writers, making MAX()+1 safe in this transaction.
            db.execute("""INSERT OR IGNORE INTO event_numbers(event_id,public_number)
                VALUES(?, COALESCE((SELECT MAX(public_number) + 1 FROM event_numbers), 1))""", (item["id"],))
            if is_new and discovered_after_baseline:
                db.execute("""INSERT OR IGNORE INTO ticket_new_event_discoveries(event_id,discovered_at)
                    VALUES(?,?)""", (item["id"], stamp))
        return is_new

    def save_parsed(self, event_id: str, source_url: str, content_hash: str, parser: str,
                    tickets: Iterable[TicketRound], performances: Iterable[Performance],
                    cast: Iterable[CastAppearance], review_notes: Iterable[str]) -> bool:
        """Store one page atomically. Return true only for a meaningful page change."""
        stamp = now()
        # Do this before opening the write transaction.  Exact duplicate PC/SP
        # markup is harmless, but two different rows claiming one stable ID is
        # ambiguous: do not delete the old verified cache to make room for it.
        tickets, performances, cast = list(tickets), list(performances), list(cast)
        performances = self._unique_performances(event_id, source_url, performances)
        with self._connect() as db:
            existing = db.execute("SELECT content_hash FROM sources WHERE event_id=? AND url=?", (event_id, source_url)).fetchone()
            source_baselined = bool(db.execute("SELECT 1 FROM ticket_source_baselines WHERE event_id=? AND source_url=?", (event_id, source_url)).fetchone())
            prior_ticket_ids = {row["id"] for row in db.execute("SELECT id FROM ticket_rounds WHERE event_id=?", (event_id,))}
            changed = not existing or existing["content_hash"] != content_hash
            db.execute("""INSERT INTO sources(url,event_id,content_hash,fetched_at,parser,quality,excerpt,error)
                VALUES(?,?,?,?,?,'verified','',NULL)
                ON CONFLICT(event_id,url) DO UPDATE SET content_hash=excluded.content_hash,
                fetched_at=excluded.fetched_at,parser=excluded.parser,quality='verified',error=NULL""",
                (source_url, event_id, content_hash, stamp, parser))
            db.execute('UPDATE sources SET attempted_at=? WHERE event_id=? AND url=?', (stamp, event_id, source_url))
            if not changed:
                if not source_baselined:
                    db.execute("INSERT OR IGNORE INTO ticket_source_baselines VALUES(?,?,?)", (event_id, source_url, stamp))
                return False
            db.execute("INSERT OR IGNORE INTO revisions(source_url,content_hash,observed_at,summary) VALUES(?,?,?,?)",
                       (source_url, content_hash, stamp, f"{parser} changed"))
            # Replacing records from this exact source keeps IDs stable across deadline changes.
            db.execute("DELETE FROM ticket_rounds WHERE event_id=?", (event_id,))
            if performances:
                db.execute("DELETE FROM performances WHERE event_id=?", (event_id,))
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
            current_tickets = {f'{event_id}:{row.stable_key}': row for row in tickets}
            initial = db.execute("""SELECT initial_ticket_notice_recorded FROM ticket_new_event_discoveries
                WHERE event_id=?""", (event_id,)).fetchone()
            if not source_baselined:
                db.execute("INSERT OR IGNORE INTO ticket_source_baselines VALUES(?,?,?)", (event_id, source_url, stamp))
                candidate_ids = set(current_tickets) if initial and not initial["initial_ticket_notice_recorded"] else set()
                if initial and not initial["initial_ticket_notice_recorded"]:
                    db.execute("""UPDATE ticket_new_event_discoveries SET initial_ticket_notice_recorded=1
                        WHERE event_id=?""", (event_id,))
            else:
                candidate_ids = set(current_tickets) - prior_ticket_ids
            for round_id in candidate_ids:
                row = current_tickets[round_id]
                if row.ticket_scope == "onsite" and row.sale_method == "lottery":
                    db.execute("""INSERT OR IGNORE INTO ticket_new_rounds(round_id,event_id,source_url,observed_at)
                        VALUES(?,?,?,?)""", (round_id, event_id, source_url, stamp))
        return True

    @staticmethod
    def _unique_performances(event_id: str, source_url: str,
                             rows: list[Performance]) -> list[Performance]:
        """Collapse repeated parsed sessions; reject unresolved identities.

        A deterministic key is required for reminder de-duplication.  Keeping
        the first of identical rows makes desktop/mobile copies safe, while a
        differing row with the same key is evidence of a parser ambiguity and
        must leave the previously verified event untouched.
        """
        unique: dict[str, Performance] = {}
        for row in rows:
            previous = unique.get(row.stable_key)
            if previous is None:
                unique[row.stable_key] = row
                continue
            if (previous.date, previous.session_label, previous.venue,
                    previous.status, previous.precision) == (
                        row.date, row.session_label, row.venue,
                        row.status, row.precision):
                continue
            raise ValueError(
                "场次稳定身份冲突，已保留旧缓存待核验："
                f"event={event_id} source={source_url} key={row.stable_key} "
                f"existing={previous.date}/{previous.session_label!r} "
                f"incoming={row.date}/{row.session_label!r}"
            )
        return list(unique.values())

    def source_error(self, event_id: str, url: str, error: str) -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO sources(url,event_id,fetched_at,parser,quality,error) VALUES(?,?,?,'fetch','stale',?)
              ON CONFLICT(event_id,url) DO UPDATE SET quality='stale',error=excluded.error""",
              (url, event_id, now(), error[:500]))
            db.execute('UPDATE sources SET attempted_at=? WHERE event_id=? AND url=?', (now(), event_id, url))

    def save_cast_assets(self, event_id: str, source_url: str, assets: Iterable[tuple[str, str]]) -> None:
        with self._connect() as db:
            for image_url, cached_path in assets:
                db.execute("""INSERT INTO cast_assets VALUES(?,?,?,?,?)
                    ON CONFLICT(event_id,image_url) DO UPDATE SET cached_path=excluded.cached_path,
                    source_url=excluded.source_url,fetched_at=excluded.fetched_at""",
                    (event_id, image_url, cached_path, source_url, now()))

    def cast_assets(self, event_id: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute("SELECT cached_path FROM cast_assets WHERE event_id=? ORDER BY image_url", (event_id,)).fetchall()
        return [row["cached_path"] for row in rows]

    def cast_asset_rows(self, event_id: str) -> list[dict[str, str]]:
        with self._connect() as db:
            rows = db.execute("SELECT image_url,cached_path FROM cast_assets WHERE event_id=? ORDER BY image_url", (event_id,)).fetchall()
        return [dict(row) for row in rows]

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
                ORDER BY CASE WHEN EXISTS(SELECT 1 FROM performances p WHERE p.event_id=e.id AND p.date>=date('now'))
                    OR NOT EXISTS(SELECT 1 FROM performances p WHERE p.event_id=e.id)
                    OR EXISTS(SELECT 1 FROM ticket_rounds t WHERE t.event_id=e.id AND t.application_end>=date('now'))
                    THEN 0 ELSE 1 END,
                    COALESCE(s.attempted_at,''), e.source_updated DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def events_by_ids(self, event_ids: Iterable[str]) -> list[dict[str, Any]]:
        values = list(dict.fromkeys(str(item) for item in event_ids if item))
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM events WHERE id IN ({placeholders})", values).fetchall()
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
            event = db.execute("""SELECT e.*,n.public_number FROM events e
                LEFT JOIN event_numbers n ON n.event_id=e.id
                WHERE e.id=? OR e.title LIKE ? ORDER BY e.id=? DESC LIMIT 1""", (query, f"%{query}%", query)).fetchone()
            if not event:
                return None
            event_id = event["id"]
            return {"event": dict(event), "performances": [dict(x) for x in db.execute("SELECT * FROM performances WHERE event_id=?", (event_id,))],
                    "tickets": [dict(x) for x in db.execute("SELECT * FROM ticket_rounds WHERE event_id=?", (event_id,))],
                    "cast": [dict(x) for x in db.execute("SELECT * FROM cast_appearances WHERE event_id=?", (event_id,))]}

    def detail_by_public_number(self, number: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT event_id FROM event_numbers WHERE public_number=?", (number,)).fetchone()
        return self.detail(row["event_id"]) if row else None

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

    def migrate_legacy_ticket_subscriptions(self, umos: Iterable[str]) -> None:
        """Import old whitelist once, without granting it the new LIVE channel."""
        with self._connect() as db:
            if db.execute("SELECT 1 FROM meta WHERE key='ticket_subscriptions_v1_migrated'").fetchone():
                return
            stamp = now()
            # A legacy whitelist predates this table, so it is not a "late
            # enable" for the purpose of the current alert window.
            legacy_created = "1970-01-01T00:00:00+00:00"
            for umo in dict.fromkeys(str(x) for x in umos if str(x)):
                old = db.execute("SELECT enabled FROM group_switches WHERE umo=?", (umo,)).fetchone()
                enabled = int(old["enabled"]) if old else 1
                db.execute("""INSERT OR IGNORE INTO ticket_group_subscriptions VALUES(?,?,?,?)""",
                           (umo, enabled, legacy_created, stamp))
            db.execute("INSERT INTO meta VALUES('ticket_subscriptions_v1_migrated','1')")

    def set_subscription(self, kind: str, umo: str, enabled: bool) -> None:
        table = self._subscription_table(kind)
        with self._connect() as db:
            stamp = now()
            db.execute(f"""INSERT INTO {table}(umo,enabled,created_at,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(umo) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at""",
                       (umo, int(enabled), stamp, stamp))

    def subscription_enabled(self, kind: str, umo: str) -> bool:
        table = self._subscription_table(kind)
        with self._connect() as db:
            row = db.execute(f"SELECT enabled FROM {table} WHERE umo=?", (umo,)).fetchone()
        return bool(row and row["enabled"])

    def subscription_created_at(self, kind: str, umo: str) -> datetime | None:
        table = self._subscription_table(kind)
        with self._connect() as db:
            row = db.execute(f"SELECT created_at FROM {table} WHERE umo=?", (umo,)).fetchone()
        try:
            return datetime.fromisoformat(row["created_at"]) if row else None
        except ValueError:
            return None

    def subscription_updated_at(self, kind: str, umo: str) -> datetime | None:
        table = self._subscription_table(kind)
        with self._connect() as db:
            row = db.execute(f"SELECT updated_at FROM {table} WHERE umo=? AND enabled=1", (umo,)).fetchone()
        try:
            return datetime.fromisoformat(row["updated_at"]) if row else None
        except ValueError:
            return None

    @staticmethod
    def _subscription_table(kind: str) -> str:
        if kind not in {"live", "ticket"}:
            raise ValueError("unknown subscription kind")
        return f"{kind}_group_subscriptions"

    def group_enabled(self, umo: str, default: bool = True) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT enabled FROM group_switches WHERE umo=?", (umo,)).fetchone()
        return bool(row["enabled"]) if row else default

    def claim_delivery(self, key: str, subscription_id: str, payload: str, notification_type: str = "legacy") -> bool:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT state FROM delivery_log WHERE dedupe_key=?", (key,)).fetchone()
            if row and row["state"] in ("sent", "inflight"):
                return False
            if row:
                db.execute("""UPDATE delivery_log SET state='inflight',payload=?,updated_at=?,notification_type=?
                    WHERE dedupe_key=?""", (payload, now(), notification_type, key))
            else:
                db.execute("""INSERT INTO delivery_log(dedupe_key,subscription_id,state,payload,updated_at,notification_type)
                    VALUES(?,?, 'inflight',?,?,?)""", (key, subscription_id, payload, now(), notification_type))
            return True

    def finish_delivery(self, key: str, success: bool) -> None:
        with self._connect() as db: db.execute("UPDATE delivery_log SET state=?,updated_at=? WHERE dedupe_key=?", ("sent" if success else "failed",now(),key))

    def calendar_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return only concrete performance dates and verified lottery deadlines."""
        with self._connect() as db:
            performances = [dict(row) for row in db.execute("""SELECT p.*,e.title,e.brands_json,e.event_display,e.venue AS event_venue,
                n.public_number,s.fetched_at AS source_fetched_at,s.quality AS source_quality FROM performances p JOIN events e ON e.id=p.event_id
                LEFT JOIN event_numbers n ON n.event_id=e.id
                LEFT JOIN sources s ON s.event_id=p.event_id AND s.url=CASE WHEN EXISTS(
                    SELECT 1 FROM sources root WHERE root.event_id=e.id AND root.url=e.official_url)
                    THEN e.official_url ELSE p.source_url END
                WHERE p.date IS NOT NULL AND p.status!='cancelled'""")]
            deadlines = [dict(row) for row in db.execute("""SELECT t.*,e.title,e.brands_json,e.event_display,e.venue,n.public_number,
                    s.fetched_at AS source_fetched_at,s.quality AS source_quality
                FROM ticket_rounds t JOIN events e ON e.id=t.event_id
                LEFT JOIN event_numbers n ON n.event_id=e.id
                LEFT JOIN sources s ON s.event_id=e.id AND s.url=CASE WHEN EXISTS(
                    SELECT 1 FROM sources root WHERE root.event_id=e.id AND root.url=e.official_url)
                    THEN e.official_url ELSE t.source_url END
                WHERE t.ticket_scope='onsite' AND t.sale_method='lottery' AND t.application_end IS NOT NULL
                  AND t.quality='verified'""")]
        return performances, deadlines

    def ticket_query_rows(self) -> list[dict[str, Any]]:
        """Return every verified onsite lottery round with source freshness evidence."""
        with self._connect() as db:
            rows = db.execute("""SELECT t.*,e.title,e.brands_json,e.venue AS event_venue,n.public_number,
                    s.fetched_at AS source_fetched_at,s.quality AS source_quality,
                    MIN(p.date) AS performance_date,
                    COALESCE(MIN(NULLIF(p.venue,'')), e.venue) AS performance_venue
                FROM ticket_rounds t
                JOIN events e ON e.id=t.event_id
                LEFT JOIN event_numbers n ON n.event_id=e.id
                LEFT JOIN performances p ON p.event_id=e.id AND p.date IS NOT NULL AND p.status!='cancelled'
                LEFT JOIN sources s ON s.event_id=e.id AND s.url=CASE WHEN EXISTS(
                    SELECT 1 FROM sources root WHERE root.event_id=e.id AND root.url=e.official_url)
                    THEN e.official_url ELSE t.source_url END
                WHERE t.ticket_scope='onsite' AND t.sale_method='lottery' AND t.quality='verified'
                GROUP BY t.id
                ORDER BY t.application_end IS NULL,t.application_end,t.application_start,t.id""").fetchall()
        return [dict(row) for row in rows]

    def new_ticket_round_rows(self) -> list[dict[str, Any]]:
        """Return queued verified additions that still exist in the latest page."""
        with self._connect() as db:
            rows = db.execute("""SELECT q.round_id,q.observed_at,q.source_url AS announced_source_url,
                    t.*,e.title,e.brands_json,n.public_number,s.fetched_at AS source_fetched_at,s.quality AS source_quality
                FROM ticket_new_rounds q
                JOIN ticket_rounds t ON t.id=q.round_id
                JOIN events e ON e.id=t.event_id
                LEFT JOIN event_numbers n ON n.event_id=e.id
                LEFT JOIN sources s ON s.event_id=q.event_id AND s.url=q.source_url
                WHERE t.ticket_scope='onsite' AND t.sale_method='lottery' AND t.quality='verified'
                ORDER BY q.observed_at,q.round_id""").fetchall()
        return [dict(row) for row in rows]
