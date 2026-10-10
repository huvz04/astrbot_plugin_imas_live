"""SQLite persistence with revision evidence and notification de-duplication."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .models import CastAppearance, Performance, TicketRound
from .cms import event_root
from .parsing import canonical_reception_url, schedule_performances, stable

logger = logging.getLogger(__name__)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init()
        self.reconcile_event_identities()

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
            CREATE TABLE IF NOT EXISTS ticket_round_aliases (
              alias_id TEXT PRIMARY KEY, round_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_aliases (
              alias_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, public_number INTEGER UNIQUE,
              FOREIGN KEY(event_id) REFERENCES events(id)
            );
            CREATE TABLE IF NOT EXISTS performance_aliases (
              alias_id TEXT PRIMARY KEY, performance_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_merge_audit (
              alias_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, merged_at TEXT NOT NULL,
              backup_path TEXT NOT NULL, evidence_json TEXT NOT NULL
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
            ticket_columns = {row['name'] for row in db.execute('PRAGMA table_info(ticket_rounds)')}
            for name, definition in (
                ('verified_source_url', 'TEXT'), ('verified_at', 'TEXT'),
                ('verification_quality', "TEXT NOT NULL DEFAULT 'verified'"),
                ('performance_keys_json', "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if name not in ticket_columns:
                    db.execute(f'ALTER TABLE ticket_rounds ADD COLUMN {name} {definition}')
            if 'verified_source_url' not in ticket_columns:
                db.execute("""UPDATE ticket_rounds SET verified_source_url=COALESCE(
                    (SELECT e.official_url FROM events e JOIN sources s ON s.event_id=e.id AND s.url=e.official_url
                     WHERE e.id=ticket_rounds.event_id),source_url)""")
            self._normalize_ticket_identities(db)
            # Upgrade existing installations before their next directory sync.
            # First assignment prefers the nearest dated performance, then
            # stable creation/ID ordering; it is never used as a live ranking.
            missing = db.execute("""SELECT e.id FROM events e LEFT JOIN event_numbers n ON n.event_id=e.id
                LEFT JOIN performances p ON p.event_id=e.id
                WHERE n.event_id IS NULL GROUP BY e.id
                ORDER BY MIN(CASE WHEN p.date>=date('now') THEN p.date END) IS NULL,
                    MIN(CASE WHEN p.date>=date('now') THEN p.date END),e.created_at,e.id""").fetchall()
            for row in missing:
                db.execute('''INSERT INTO event_numbers(event_id,public_number) VALUES(?, COALESCE((SELECT MAX(public_number)+1 FROM
                    (SELECT public_number FROM event_numbers UNION ALL SELECT public_number FROM event_aliases)),1))''', (row['id'],))

    @staticmethod
    def _merge_ticket_identity(db, target_id: str, rows: list[dict[str, Any]]) -> None:
        """Keep one real entrance, retaining old notification identities as aliases."""
        winner = max(rows, key=lambda r: (
            bool(r.get('application_start') and r.get('application_end')),
            r.get('verification_quality') == 'verified', r.get('verified_at') or '', r['id'] == target_id))
        record = {**winner, 'id': target_id}
        record['url'] = canonical_reception_url(record['url']) or record['url']
        record['performance_keys_json'] = json.dumps(list(dict.fromkeys(
            key for row in rows for key in json.loads(row.get('performance_keys_json') or '[]'))))
        columns = list(record)
        db.execute(f"INSERT INTO ticket_rounds ({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
                   f"ON CONFLICT(id) DO UPDATE SET {','.join(c+'=excluded.'+c for c in columns if c != 'id')}",
                   [record[c] for c in columns])
        old_ids = [r['id'] for r in rows if r['id'] != target_id]
        for old_id in old_ids:
            db.execute('UPDATE ticket_round_aliases SET round_id=? WHERE round_id=?', (target_id, old_id))
            db.execute('INSERT OR REPLACE INTO ticket_round_aliases VALUES(?,?)', (old_id, target_id))
            # Carry the original observation time; migration never announces history.
            pending = db.execute('SELECT * FROM ticket_new_rounds WHERE round_id=?', (old_id,)).fetchone()
            if pending:
                db.execute('''INSERT INTO ticket_new_rounds VALUES(?,?,?,?) ON CONFLICT(round_id) DO UPDATE SET
                    observed_at=MIN(ticket_new_rounds.observed_at,excluded.observed_at)''',
                    (target_id, pending['event_id'], pending['source_url'], pending['observed_at']))
            db.execute('DELETE FROM ticket_new_rounds WHERE round_id=?', (old_id,))
            db.execute('DELETE FROM ticket_rounds WHERE id=?', (old_id,))

    def _normalize_ticket_identities(self, db, event_id: str | None = None) -> None:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        query = 'SELECT * FROM ticket_rounds' + (' WHERE event_id=?' if event_id else '')
        for raw in db.execute(query, (event_id,) if event_id else ()).fetchall():
            row = dict(raw)
            reception = canonical_reception_url(row['url'])
            if reception:
                groups.setdefault((row['event_id'], reception), []).append(row)
        for (event, reception), rows in groups.items():
            target_id = event + ':' + stable(reception)
            if len(rows) > 1 or rows[0]['id'] != target_id or rows[0]['url'] != reception:
                self._merge_ticket_identity(db, target_id, rows)

    def ticket_identity_ids(self, round_id: str) -> list[str]:
        with self._connect() as db:
            aliases = [r[0] for r in db.execute('SELECT alias_id FROM ticket_round_aliases WHERE round_id=?', (round_id,))]
        return [round_id, *aliases]

    @staticmethod
    def _identity_text(value: Any) -> str:
        return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()

    @staticmethod
    def _identity_url(value: str | None) -> str:
        parts = urlsplit(value or '')
        # Exact official page, not event_root(): a shared tour root or news
        # article is not an event identity. Only tracking/trailing slash differ.
        if parts.scheme.lower() != 'https' or parts.netloc.lower() != 'idolmaster-official.jp':
            return ''
        if not re.match(r'^/(live_events?|lp)/[^/]+/?$', parts.path):
            return ''
        return 'https://idolmaster-official.jp' + parts.path.rstrip('/')

    @staticmethod
    def _resolve_event_id(db, event_id: str) -> str:
        alias = db.execute('SELECT event_id FROM event_aliases WHERE alias_id=?', (event_id,)).fetchone()
        return alias['event_id'] if alias else event_id

    def resolve_event_id(self, event_id: str) -> str:
        with self._connect() as db:
            return self._resolve_event_id(db, event_id)

    def _event_scope(self, db, item: dict[str, Any]) -> tuple[set, set, set, bool, set]:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM performances WHERE event_id=? AND status!='cancelled' AND date IS NOT NULL", (item['id'],))]
        if not rows:
            rows = [dict(date=p.date, session_label=p.session_label, venue=p.venue, status=p.status)
                    for p in schedule_performances(item.get('event_display') or '',
                        item.get('official_url') or item.get('url') or '', item.get('venue'), True)]
        dates = {r['date'] for r in rows}
        venues = {self._identity_text(r.get('venue')) for r in rows if r.get('venue')}
        if not venues and item.get('venue'):
            venues.add(self._identity_text(item['venue']))
        sessions = {(r['date'], self._identity_text(r.get('session_label')), self._identity_text(r.get('venue')))
                    for r in rows if r.get('status') != 'directory'}
        clocks = {(r['date'], m.group(0)) for r in rows for m in re.finditer(r'\d{1,2}:\d{2}', r.get('session_label') or '')}
        return dates, venues, sessions, bool(rows) and all(r.get('status') != 'directory' for r in rows), clocks

    def _same_event(self, db, left: dict, right: dict) -> bool:
        page = self._identity_url(left.get('official_url') or left.get('url'))
        if not page or page != self._identity_url(right.get('official_url') or right.get('url')):
            return False
        ld, lv, ls, lp, lc = self._event_scope(db, left)
        rd, rv, rs, rp, rc = self._event_scope(db, right)
        # No missing-date/venue inference, subset matching or fuzzy city names.
        if not ld or ld != rd or not lv or lv != rv:
            return False
        if lc and rc and lc != rc:
            return False
        if lp and rp and ls != rs:
            return False
        titles_match = self._identity_text(left['title']) == self._identity_text(right['title'])
        # News discovery temporarily stores a headline, not the event's title.
        # Promotion requires BOTH full, identical parsed session rosters.
        provisional = left['id'].startswith('news:') != right['id'].startswith('news:')
        return titles_match or (provisional and lp and rp and bool(ls) and ls == rs)

    def performance_identity_ids(self, performance_id: str) -> list[str]:
        with self._connect() as db:
            aliases = [r[0] for r in db.execute(
                'SELECT alias_id FROM performance_aliases WHERE performance_id=?', (performance_id,))]
        return [performance_id, *aliases]

    @staticmethod
    def _alias_performance(db, old_id: str, target_id: str) -> None:
        if old_id != target_id:
            db.execute('DELETE FROM performance_aliases WHERE alias_id=?', (target_id,))
            db.execute('UPDATE performance_aliases SET performance_id=? WHERE performance_id=?', (target_id, old_id))
            db.execute('INSERT OR REPLACE INTO performance_aliases VALUES(?,?)', (old_id, target_id))

    def reconcile_event_identities(self) -> int:
        """Back up and merge only strongly confirmed duplicates, atomically.

        No schema-version shortcut: a later official ID change can introduce
        another duplicate. Aliases prevent known IDs from ever being reborn.
        """
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = [dict(r) for r in db.execute('''SELECT e.*,n.public_number FROM events e
                LEFT JOIN event_numbers n ON n.event_id=e.id ORDER BY n.public_number,e.created_at,e.id''')]
            groups: dict[str, list[dict]] = {}
            pairs = []
            for row in rows:
                key = self._identity_url(row['official_url'])
                if not key:
                    continue
                matches = [other for other in groups.get(key, []) if self._same_event(db, other, row)]
                if len(matches) == 1:
                    pairs.append((matches[0], row))
                else:
                    groups.setdefault(key, []).append(row)
            if not pairs:
                return 0
            backup_path = self.path.with_name(self.path.stem + '.before-event-merge-' + uuid.uuid4().hex + '.sqlite3')
            # A separate read connection sees the last committed database while
            # BEGIN IMMEDIATE prevents another writer changing the merge plan.
            with sqlite3.connect(self.path) as source, sqlite3.connect(backup_path) as backup:
                source.backup(backup)
            for winner, duplicate in pairs:
                self._merge_event(db, winner, duplicate)
                db.execute('INSERT INTO event_merge_audit VALUES(?,?,?,?,?)', (
                    duplicate['id'], winner['id'], now(), str(backup_path),
                    json.dumps({'retained': winner, 'duplicate': duplicate}, ensure_ascii=False)))
            db.execute("INSERT INTO meta VALUES('event_identity_last_backup',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (str(backup_path),))
        logger.warning('IM@S confirmed event duplicates merged: count=%s backup=%s', len(pairs), backup_path)
        return len(pairs)

    def _merge_event(self, db, winner: dict, duplicate: dict) -> None:
        target, old = winner['id'], duplicate['id']
        db.execute('UPDATE event_aliases SET event_id=? WHERE event_id=?', (target, old))
        db.execute('INSERT INTO event_aliases VALUES(?,?,?)', (old, target, duplicate['public_number']))
        # Keep the early public number, but prefer the CMS event title over a
        # provisional news headline after full parsed evidence has confirmed it.
        if target.startswith('news:') and not old.startswith('news:'):
            db.execute('UPDATE events SET title=?,brands_json=? WHERE id=?', (duplicate['title'], duplicate['brands_json'], target))
        performance_map = {}
        for raw in db.execute('SELECT * FROM performances WHERE event_id=?', (old,)).fetchall():
            row = dict(raw)
            candidates = [dict(r) for r in db.execute('SELECT * FROM performances WHERE event_id=?', (target,))]
            match = next((r for r in candidates if all(self._identity_text(r[k]) == self._identity_text(row[k])
                         for k in ('date', 'session_label', 'venue'))), None)
            new_id = match['id'] if match else target + ':' + row['id'][len(old)+1:]
            if not match:
                if db.execute('SELECT 1 FROM performances WHERE id=?', (new_id,)).fetchone():
                    new_id = target + ':merged:' + stable(row['id'])
                db.execute('UPDATE performances SET id=?,event_id=? WHERE id=?', (new_id, target, row['id']))
            else:
                if match['status'] == 'directory' and row['status'] != 'directory':
                    db.execute('''UPDATE performances SET status=?,precision=?,source_url=?,excerpt=? WHERE id=?''',
                        (row['status'], row['precision'], row['source_url'], row['excerpt'], new_id))
                db.execute('DELETE FROM performances WHERE id=?', (row['id'],))
            performance_map[row['id']] = new_id
            self._alias_performance(db, row['id'], new_id)
        db.execute('UPDATE ticket_new_rounds SET event_id=? WHERE event_id=?', (target, old))
        for raw in db.execute('SELECT * FROM ticket_rounds WHERE event_id=?', (old,)).fetchall():
            row = dict(raw)
            row['event_id'] = target
            keys = json.loads(row.get('performance_keys_json') or '[]')
            row['performance_keys_json'] = json.dumps([performance_map.get(k, performance_map.get(old+':'+k, k))
                                                       for k in keys])
            reception = canonical_reception_url(row['url'])
            existing = [dict(r) for r in db.execute('SELECT * FROM ticket_rounds WHERE event_id=?', (target,))]
            match = next((r for r in existing if self._same_ticket_entry(r, row, old)), None)
            new_id = (target + ':' + stable(reception)) if reception else (
                match['id'] if match else target + ':' + row['id'][len(old)+1:])
            if not match and db.execute('SELECT 1 FROM ticket_rounds WHERE id=?', (new_id,)).fetchone():
                new_id = target + ':merged:' + stable(row['id'])
            self._merge_ticket_identity(db, new_id, [row] + ([match] if match else []))
        for raw in db.execute('SELECT * FROM cast_appearances WHERE event_id=?', (old,)).fetchall():
            row = dict(raw)
            performance = row['performance_id']
            performance = performance_map.get(performance, performance_map.get(old+':'+str(performance), performance))
            if performance and performance.startswith(target+':'):
                performance = performance[len(target)+1:]
            existing_cast = db.execute('''SELECT 1 FROM cast_appearances WHERE event_id=? AND
                (performance_id IS ? OR performance_id=?) AND person_name=? AND role_name IS ?''',
                (target, performance, target+':'+str(performance) if performance else None, row['person_name'], row['role_name'])).fetchone()
            if existing_cast:
                continue
            db.execute('INSERT OR IGNORE INTO cast_appearances VALUES(?,?,?,?,?,?,?,?)', (
                f"{target}:{performance or 'unknown'}:{row['person_name']}:{row['role_name'] or ''}", target,
                performance, row['person_name'], row['role_name'], row['status'], row['source_url'], row['excerpt']))
        db.execute('DELETE FROM cast_appearances WHERE event_id=?', (old,))
        for raw in db.execute('SELECT * FROM sources WHERE event_id=?', (old,)).fetchall():
            row = dict(raw)
            other = db.execute('SELECT * FROM sources WHERE event_id=? AND url=?', (target, row['url'])).fetchone()
            if other and (other['attempted_at'] or other['fetched_at']) >= (row['attempted_at'] or row['fetched_at']):
                continue
            row['event_id'] = target
            columns = list(row)
            db.execute(f"INSERT OR REPLACE INTO sources ({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                       [row[c] for c in columns])
        db.execute('DELETE FROM sources WHERE event_id=?', (old,))
        db.execute('''INSERT INTO ticket_source_baselines SELECT ?,source_url,baselined_at FROM ticket_source_baselines WHERE event_id=?
            ON CONFLICT(event_id,source_url) DO UPDATE SET baselined_at=MIN(baselined_at,excluded.baselined_at)''', (target, old))
        db.execute('DELETE FROM ticket_source_baselines WHERE event_id=?', (old,))
        db.execute('''INSERT INTO ticket_new_event_discoveries SELECT ?,discovered_at,initial_ticket_notice_recorded
            FROM ticket_new_event_discoveries WHERE event_id=? ON CONFLICT(event_id) DO UPDATE SET
            discovered_at=MIN(discovered_at,excluded.discovered_at),
            initial_ticket_notice_recorded=MAX(initial_ticket_notice_recorded,excluded.initial_ticket_notice_recorded)''', (target, old))
        db.execute('''UPDATE ticket_new_event_discoveries SET initial_ticket_notice_recorded=1 WHERE event_id=?
            AND EXISTS(SELECT 1 FROM ticket_source_baselines WHERE event_id=?)''', (target, target))
        db.execute('DELETE FROM ticket_new_event_discoveries WHERE event_id=?', (old,))
        db.execute('''INSERT OR IGNORE INTO cast_assets SELECT ?,image_url,cached_path,source_url,fetched_at
            FROM cast_assets WHERE event_id=?''', (target, old))
        db.execute('DELETE FROM cast_assets WHERE event_id=?', (old,))
        db.execute('''INSERT OR IGNORE INTO review_items(event_id,source_url,note,state,observed_at)
            SELECT ?,source_url,note,state,observed_at FROM review_items WHERE event_id=?''', (target, old))
        db.execute('DELETE FROM review_items WHERE event_id=?', (old,))
        db.execute('''INSERT INTO meta SELECT ?,value FROM meta WHERE key=? ON CONFLICT(key) DO UPDATE SET
            value=MAX(value,excluded.value)''', ('refresh_hint:'+target, 'refresh_hint:'+old))
        db.execute('DELETE FROM meta WHERE key=?', ('refresh_hint:'+old,))
        db.execute('DELETE FROM event_numbers WHERE event_id=?', (old,))
        db.execute('DELETE FROM events WHERE id=?', (old,))

    def _same_unlinked_evidence(self, left: dict, right: dict, right_event_id: str | None = None) -> bool:
        def page(value):
            parts = urlsplit(value or '')
            return (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'))
        same_key = left['id'][len(left['event_id'])+1:] == right['id'][len(right_event_id or right['event_id'])+1:]
        excerpt = self._identity_text(left.get('excerpt'))
        same_excerpt = bool(excerpt) and excerpt == self._identity_text(right.get('excerpt'))
        return bool(left.get('source_url') and right.get('source_url')) and page(left['source_url']) == page(right['source_url']) and (same_key or same_excerpt)

    def _same_ticket_entry(self, left: dict, right: dict, right_event_id: str | None = None) -> bool:
        receipt = canonical_reception_url(left.get('url'))
        if receipt:
            return receipt == canonical_reception_url(right.get('url'))
        fields = ('name', 'ticket_scope', 'sale_method', 'application_start', 'application_end',
                  'result_at', 'payment_start', 'payment_end', 'seats', 'eligibility')
        return not left.get('url') and not right.get('url') and all(left[k] == right[k] for k in fields) and \
            self._same_unlinked_evidence(left, right, right_event_id)

    def upsert_event(self, item: dict[str, Any], discovered_after_baseline: bool = False) -> bool:
        stamp = now()
        item = dict(item)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            requested = str(item['id'])
            item['id'] = self._resolve_event_id(db, requested)
            if item['id'] == requested and not db.execute('SELECT 1 FROM events WHERE id=?', (requested,)).fetchone():
                matches = [dict(r) for r in db.execute('SELECT * FROM events') if self._same_event(db, dict(r), item)]
                if len(matches) == 1:
                    item['id'] = matches[0]['id']
                    db.execute('INSERT INTO event_aliases VALUES(?,?,NULL)', (requested, item['id']))
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
                VALUES(?, COALESCE((SELECT MAX(public_number) + 1 FROM
                  (SELECT public_number FROM event_numbers UNION ALL SELECT public_number FROM event_aliases)), 1))""", (item["id"],))
            if is_new and discovered_after_baseline:
                db.execute("""INSERT OR IGNORE INTO ticket_new_event_discoveries(event_id,discovered_at)
                    VALUES(?,?)""", (item["id"], stamp))
        return is_new

    def save_parsed(self, event_id: str, source_url: str, content_hash: str, parser: str,
                    tickets: Iterable[TicketRound], performances: Iterable[Performance],
                    cast: Iterable[CastAppearance], review_notes: Iterable[str], ticket_only: bool = False,
                    announce_initial: bool = False) -> bool:
        """Store one page atomically. Return true only for a meaningful page change."""
        stamp = now()
        # Do this before opening the write transaction.  Exact duplicate PC/SP
        # markup is harmless, but two different rows claiming one stable ID is
        # ambiguous: do not delete the old verified cache to make room for it.
        tickets, performances, cast = list(tickets), list(performances), list(cast)
        performances = self._unique_performances(event_id, source_url, performances)
        with self._connect() as db:
            event_id = self._resolve_event_id(db, event_id)
            self._normalize_ticket_identities(db, event_id)
            for row in tickets:
                reception = canonical_reception_url(row.url)
                if reception:
                    row.url, row.stable_key = reception, stable(reception)
                elif not row.url:
                    # Trailing-slash/CMS ID changes can change source-derived
                    # keys even though an unlinked application is unchanged.
                    # Require exact business facts AND matching page evidence.
                    incoming = {**row.record(), 'id': event_id+':'+row.stable_key, 'event_id': event_id,
                        'source_url': row.evidence.url if row.evidence else source_url,
                        'excerpt': row.evidence.excerpt if row.evidence else ''}
                    matches = [dict(r) for r in db.execute('SELECT * FROM ticket_rounds WHERE event_id=? AND url IS NULL', (event_id,))
                               if self._same_ticket_entry(dict(r), incoming)]
                    if len(matches) == 1:
                        retained_id = matches[0]['id']
                        if incoming['id'] != retained_id:
                            db.execute('INSERT OR REPLACE INTO ticket_round_aliases VALUES(?,?)', (incoming['id'], retained_id))
                            row.stable_key = retained_id[len(event_id)+1:]
            # Earlier accordion parsing stole a sibling's title for the game-only
            # entrance. Match its exact source/block evidence, not just a period.
            linked_names = {row.name for row in tickets if row.url}
            for row in tickets:
                if row.url or not row.evidence or not row.evidence.excerpt:
                    continue
                legacy = [dict(r) for r in db.execute('''SELECT * FROM ticket_rounds WHERE event_id=? AND url IS NULL
                    AND source_url=? AND excerpt=? AND application_start IS ? AND application_end IS ?''',
                    (event_id, row.evidence.url, row.evidence.excerpt, row.application_start, row.application_end))
                    if r['name'] in linked_names and r['name'] != row.name]
                if len(legacy) == 1:
                    target_id = event_id + ':' + row.stable_key
                    current = db.execute('SELECT * FROM ticket_rounds WHERE id=?', (target_id,)).fetchone()
                    self._merge_ticket_identity(db, target_id, legacy + ([dict(current)] if current else []))
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
            current_ids = [f'{event_id}:{row.stable_key}' for row in tickets]
            # Preserve historical rounds, but a disappeared round is no longer
            # reverified by a successful fetch of the newer page.
            placeholders = ','.join('?' for _ in current_ids) or "''"
            db.execute(f"""UPDATE ticket_rounds SET verification_quality='stale'
                WHERE event_id=? AND (verified_source_url=? OR (verified_source_url IS NULL AND source_url=?))
                AND id NOT IN ({placeholders})""", (event_id, source_url, source_url, *current_ids))
            for round_id in current_ids:
                db.execute("""UPDATE ticket_rounds SET verified_source_url=?,verified_at=?,verification_quality='verified'
                    WHERE id=?""", (source_url, stamp, round_id))
            if not changed:
                if not source_baselined:
                    db.execute("INSERT OR IGNORE INTO ticket_source_baselines VALUES(?,?,?)", (event_id, source_url, stamp))
                return False
            db.execute("INSERT OR IGNORE INTO revisions(source_url,content_hash,observed_at,summary) VALUES(?,?,?,?)",
                       (source_url, content_hash, stamp, f"{parser} changed"))
            # Upsert current rounds without removing history; stable IDs survive deadline edits.
            if performances:
                replacements = {}
                for previous in db.execute('SELECT * FROM performances WHERE event_id=?', (event_id,)).fetchall():
                    replacement = next((p for p in performances if
                        (self._identity_text(p.date), self._identity_text(p.session_label), self._identity_text(p.venue)) ==
                        tuple(self._identity_text(previous[k]) for k in ('date', 'session_label', 'venue'))), None)
                    if replacement:
                        new_id = event_id+':'+replacement.stable_key
                        replacements[previous['id']] = new_id
                        replacements[previous['id'][len(event_id)+1:]] = new_id
                        self._alias_performance(db, previous['id'], new_id)
                for ticket in db.execute('SELECT id,performance_keys_json FROM ticket_rounds WHERE event_id=?', (event_id,)).fetchall():
                    keys = json.loads(ticket['performance_keys_json'] or '[]')
                    updated = [replacements.get(key, key) for key in keys]
                    if updated != keys:
                        db.execute('UPDATE ticket_rounds SET performance_keys_json=? WHERE id=?', (json.dumps(updated), ticket['id']))
                db.execute("DELETE FROM performances WHERE event_id=?", (event_id,))
            if not ticket_only:
                db.execute("DELETE FROM cast_appearances WHERE event_id=?", (event_id,))
            for row in tickets:
                evidence = row.evidence
                db.execute("""INSERT INTO ticket_rounds
                    (id,event_id,name,ticket_scope,sale_method,application_start,application_end,result_at,
                     payment_start,payment_end,url,seats,eligibility,source_url,excerpt,quality,
                     verified_source_url,verified_at,verification_quality,performance_keys_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,ticket_scope=excluded.ticket_scope,sale_method=excluded.sale_method,
                    application_start=excluded.application_start,application_end=excluded.application_end,
                    result_at=excluded.result_at,payment_start=excluded.payment_start,payment_end=excluded.payment_end,
                    url=excluded.url,seats=excluded.seats,eligibility=excluded.eligibility,source_url=excluded.source_url,
                    excerpt=excluded.excerpt,quality=excluded.quality,verified_source_url=excluded.verified_source_url,
                    verified_at=excluded.verified_at,verification_quality='verified',performance_keys_json=excluded.performance_keys_json""", (
                    f'{event_id}:{row.stable_key}',event_id,row.name,row.ticket_scope,row.sale_method,row.application_start,row.application_end,
                    row.result_at,row.payment_start,row.payment_end,row.url,row.seats,row.eligibility,
                    evidence.url if evidence else source_url,evidence.excerpt if evidence else "",evidence.quality if evidence else "verified",
                    source_url,stamp,'verified',json.dumps(row.performance_keys)))
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
                candidate_ids = (set(current_tickets) - prior_ticket_ids) if announce_initial else (
                    set(current_tickets) if initial and not initial["initial_ticket_notice_recorded"] else set())
                if initial and not initial["initial_ticket_notice_recorded"]:
                    db.execute("""UPDATE ticket_new_event_discoveries SET initial_ticket_notice_recorded=1
                        WHERE event_id=?""", (event_id,))
            else:
                candidate_ids = set(current_tickets) - prior_ticket_ids
            if candidate_ids:
                # A CMS ID first seen without dates may only become provably
                # duplicate after this parse. Do not queue its already-known
                # entrances as new announcements before post-commit merging.
                this_event = dict(db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone())
                duplicates = [dict(r) for r in db.execute('SELECT * FROM events WHERE id!=?', (event_id,))
                              if self._same_event(db, this_event, dict(r))]
                known_entrances = [dict(t) for other in duplicates for t in db.execute(
                    'SELECT * FROM ticket_rounds WHERE event_id=?', (other['id'],))]
                candidate_ids = {key for key in candidate_ids if not any(self._same_ticket_entry(old,
                    dict(db.execute('SELECT * FROM ticket_rounds WHERE id=?', (key,)).fetchone())) for old in known_entrances)}
            for round_id in candidate_ids:
                row = current_tickets[round_id]
                if row.ticket_scope == "onsite" and row.sale_method == "lottery":
                    db.execute("""INSERT OR IGNORE INTO ticket_new_rounds(round_id,event_id,source_url,observed_at)
                        VALUES(?,?,?,?)""", (round_id, event_id, source_url, stamp))
        self.reconcile_event_identities()
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
            event_id = self._resolve_event_id(db, event_id)
            db.execute("""INSERT INTO sources(url,event_id,fetched_at,parser,quality,error) VALUES(?,?,?,'fetch','stale',?)
              ON CONFLICT(event_id,url) DO UPDATE SET quality='stale',error=excluded.error""",
              (url, event_id, now(), error[:500]))
            db.execute('UPDATE sources SET attempted_at=? WHERE event_id=? AND url=?', (now(), event_id, url))

    def save_cast_assets(self, event_id: str, source_url: str, assets: Iterable[tuple[str, str]]) -> None:
        with self._connect() as db:
            event_id = self._resolve_event_id(db, event_id)
            for image_url, cached_path in assets:
                db.execute("""INSERT INTO cast_assets VALUES(?,?,?,?,?)
                    ON CONFLICT(event_id,image_url) DO UPDATE SET cached_path=excluded.cached_path,
                    source_url=excluded.source_url,fetched_at=excluded.fetched_at""",
                    (event_id, image_url, cached_path, source_url, now()))

    def cast_assets(self, event_id: str) -> list[str]:
        with self._connect() as db:
            event_id = self._resolve_event_id(db, event_id)
            rows = db.execute("SELECT cached_path FROM cast_assets WHERE event_id=? ORDER BY image_url", (event_id,)).fetchall()
        return [row["cached_path"] for row in rows]

    def cast_asset_rows(self, event_id: str) -> list[dict[str, str]]:
        with self._connect() as db:
            event_id = self._resolve_event_id(db, event_id)
            rows = db.execute("SELECT image_url,cached_path FROM cast_assets WHERE event_id=? ORDER BY image_url", (event_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_events(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""SELECT e.*, COUNT(DISTINCT p.id) performance_count FROM events e
              LEFT JOIN performances p ON p.event_id=e.id WHERE e.title LIKE ? OR e.brands_json LIKE ?
              GROUP BY e.id ORDER BY e.event_display IS NULL,e.event_display LIMIT ?""", (f"%{query}%", f"%{query}%", limit)).fetchall()
        return [dict(row) for row in rows]

    def source_candidates(self, current: datetime | None = None) -> list[dict[str, Any]]:
        current = current or datetime.now(timezone.utc)
        with self._connect() as db:
            rows = db.execute("""SELECT e.*,s.attempted_at,s.fetched_at,s.content_hash,s.quality AS source_quality,
                m.value AS refresh_hint,
                (EXISTS(SELECT 1 FROM performances p WHERE p.event_id=e.id AND p.date>=?)
                 OR EXISTS(SELECT 1 FROM ticket_rounds t WHERE t.event_id=e.id AND t.application_end>=?)) AS active
                FROM events e LEFT JOIN sources s ON s.event_id=e.id AND s.url=e.official_url
                LEFT JOIN meta m ON m.key='refresh_hint:' || e.id""", (current.date().isoformat(), current.date().isoformat())).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            if not event_root(row['official_url']):
                continue
            try:
                updated = datetime.fromtimestamp(float(row['source_updated']), timezone.utc)
            except (ValueError, TypeError, OSError, OverflowError):
                updated = datetime.min.replace(tzinfo=timezone.utc)
            years = [int(y) for y in re.findall(r'20\d{2}', row['event_display'] or '')]
            recent_unknown = not years and updated >= current - timedelta(days=60)
            row['active'] = bool(row['active'] or recent_unknown)
            row['hint_pending'] = bool(row['refresh_hint'] and row['refresh_hint'] > (row['attempted_at'] or ''))
            result.append(row)
        result.sort(key=lambda r: (not r['hint_pending'], r['attempted_at'] or '', -(float(r['source_updated']) if str(r['source_updated']).isdigit() else 0), r['id']))
        return result

    def fetchable_events(self, limit: int) -> list[dict[str, Any]]:
        rows = self.source_candidates()
        active = [row for row in rows if row['active'] or row['hint_pending']]
        historical = [row for row in rows if not row['active'] and not row['hint_pending']]
        # Undated old failures must not displace ongoing events. Reserve one
        # history slot so the lower-priority queue still makes progress.
        budget = max(1, limit)
        slots = budget - int(bool(historical and active and budget > 1))
        selected = active[:slots]
        return selected + historical[:budget - len(selected)]

    def sync_diagnostics(self, freshness_hours: int = 12, current: datetime | None = None) -> dict[str, Any]:
        current = current or datetime.now(timezone.utc)
        rows = self.source_candidates(current)
        cutoff = (current.astimezone(timezone.utc) - timedelta(hours=freshness_hours)).isoformat(timespec='seconds')
        pending = [r for r in rows if r['source_quality'] != 'verified' or not r['fetched_at'] or r['fetched_at'] < cutoff]
        successful = [r['fetched_at'] for r in rows if r['content_hash'] and r['fetched_at']]
        return {'candidate_total': len(rows), 'active_candidates': sum(r['active'] for r in rows),
                'pending_verification': len(pending), 'active_pending': sum(r['active'] for r in pending),
                'never_checked': sum(not r['attempted_at'] for r in rows),
                'last_source_success': max(successful, default=None),
                'last_directory_sync': self.meta('last_directory_sync'),
                'last_sync_attempt': self.meta('last_sync_attempt'),
                'last_news_discovery': self.meta('last_news_discovery'),
                'last_news_error': self.meta('last_news_error'), 'last_error': self.meta('last_error')}

    def events_by_ids(self, event_ids: Iterable[str]) -> list[dict[str, Any]]:
        values = list(dict.fromkeys(self.resolve_event_id(str(item)) for item in event_ids if item))
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM events WHERE id IN ({placeholders})", values).fetchall()
        return [dict(row) for row in rows]

    def save_directory_dates(self, event_id: str, rows: list[Performance]) -> None:
        with self._connect() as db:
            event_id = self._resolve_event_id(db, event_id)
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
            query = self._resolve_event_id(db, query)
            event = db.execute("""SELECT e.*,n.public_number,
                (SELECT MAX(s.fetched_at) FROM sources s WHERE s.event_id=e.id AND s.quality='verified') AS source_fetched_at,
                (SELECT s.quality FROM sources s WHERE s.event_id=e.id AND s.url=e.official_url LIMIT 1) AS source_quality FROM events e
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
            row = db.execute('''SELECT event_id FROM event_numbers WHERE public_number=?
                UNION ALL SELECT event_id FROM event_aliases WHERE public_number=? LIMIT 1''', (number, number)).fetchone()
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
                ON CONFLICT(umo) DO UPDATE SET
                enabled=excluded.enabled,
                created_at=CASE WHEN {table}.enabled=0 AND excluded.enabled=1
                    THEN excluded.created_at ELSE {table}.created_at END,
                updated_at=CASE WHEN {table}.enabled!=excluded.enabled
                    THEN excluded.updated_at ELSE {table}.updated_at END""",
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

    def claim_delivery(self, key: str, subscription_id: str, payload: str, notification_type: str = "legacy",
                       equivalent_keys: Iterable[str] = ()) -> bool:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for alias in equivalent_keys:
                if db.execute("SELECT 1 FROM delivery_log WHERE dedupe_key=? AND state IN ('sent','inflight')", (alias,)).fetchone():
                    return False
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
        deadlines = [row for row in self.ticket_query_rows() if row['sale_method'] == 'lottery' and row['application_end']]
        return performances, deadlines

    def ticket_performances(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM performances WHERE status!='cancelled'")]

    def ticket_query_rows(self) -> list[dict[str, Any]]:
        """All onsite lottery/resale rounds, with independently verified evidence."""
        with self._connect() as db:
            rows = db.execute("""SELECT t.*,e.title,e.brands_json,e.event_display,e.venue AS event_venue,n.public_number,
                    CASE WHEN t.verified_at IS NULL THEN s.fetched_at ELSE MIN(t.verified_at,s.fetched_at) END AS source_fetched_at,
                    CASE WHEN t.verification_quality!='verified' THEN 'stale' ELSE s.quality END AS source_quality,
                    MIN(p.date) AS performance_date,
                    COALESCE(MIN(NULLIF(p.venue,'')), e.venue) AS performance_venue
                FROM ticket_rounds t
                JOIN events e ON e.id=t.event_id
                LEFT JOIN event_numbers n ON n.event_id=e.id
                LEFT JOIN performances p ON p.event_id=e.id AND p.date IS NOT NULL AND p.status!='cancelled'
                LEFT JOIN sources s ON s.event_id=e.id AND s.url=COALESCE(t.verified_source_url,CASE WHEN EXISTS(
                    SELECT 1 FROM sources root WHERE root.event_id=e.id AND root.url=e.official_url)
                    THEN e.official_url ELSE t.source_url END)
                WHERE t.ticket_scope='onsite' AND t.sale_method IN ('lottery','resale') AND t.quality='verified'
                GROUP BY t.id
                ORDER BY t.application_end IS NULL,t.application_end,t.application_start,t.id""").fetchall()
        return [dict(row) for row in rows]

    def new_ticket_round_rows(self) -> list[dict[str, Any]]:
        """Return queued verified additions that still exist in the latest page."""
        with self._connect() as db:
            rows = db.execute("""SELECT q.round_id,q.observed_at,q.source_url AS announced_source_url,
                    t.*,e.title,e.brands_json,n.public_number,
                    CASE WHEN t.verified_at IS NULL THEN s.fetched_at ELSE MIN(t.verified_at,s.fetched_at) END AS source_fetched_at,
                    CASE WHEN t.verification_quality!='verified' THEN 'stale' ELSE s.quality END AS source_quality
                FROM ticket_new_rounds q
                JOIN ticket_rounds t ON t.id=q.round_id
                JOIN events e ON e.id=t.event_id
                LEFT JOIN event_numbers n ON n.event_id=e.id
                LEFT JOIN sources s ON s.event_id=q.event_id AND s.url=COALESCE(t.verified_source_url,q.source_url)
                WHERE t.ticket_scope='onsite' AND t.sale_method='lottery' AND t.quality='verified'
                ORDER BY q.observed_at,q.round_id""").fetchall()
        return [dict(row) for row in rows]
