"""Local, durable development calendar. It never pretends to be a cloud account."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .errors import AgentError
from .timeutils import parse_datetime, zone


class SQLiteBackend:
    kind = "local_development"
    capabilities = {"remote": False, "supports_reminders": False,
                    "supports_recurrence": True, "writes_enabled": True}

    def __init__(self, db_path):
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, calendar_id TEXT NOT NULL,
                    starts REAL NOT NULL, ends REAL NOT NULL, body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_window ON events(calendar_id, starts, ends);
                CREATE TABLE IF NOT EXISTS preferences (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)

    @contextmanager
    def connection(self, immediate=False):
        conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_calendars(self):
        return [{"id": "local", "name": "本地开发日历（未同步真实账号）", "writable": True}]

    def list_events(self, start, end, calendar_id, connection=None):
        if connection is None:
            with self.connection() as conn:
                return self.list_events(start, end, calendar_id, conn)
        a = parse_datetime(start, zone("UTC")).timestamp()
        b = parse_datetime(end, zone("UTC")).timestamp()
        rows = connection.execute(
            "SELECT body FROM events WHERE calendar_id=? AND starts<? AND ends>? ORDER BY starts, id LIMIT 201",
            (calendar_id, b, a),
        ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def get_event(self, event_id, connection=None):
        if connection is None:
            with self.connection() as conn:
                return self.get_event(event_id, conn)
        row = connection.execute("SELECT body FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise AgentError("not_found", "找不到日程", {"event_id": event_id})
        return json.loads(row["body"])

    def create_event(self, event, request_id, connection=None):
        if connection is None:
            with self.connection(immediate=True) as conn:
                return self.create_event(event, request_id, conn)
        saved = dict(event, id=str(uuid4()), version="1", recurring=False)
        connection.execute("INSERT INTO events VALUES (?,?,?,?,?)", (
            saved["id"], saved["calendar_id"],
            parse_datetime(saved["start"], zone("UTC")).timestamp(),
            parse_datetime(saved["end"], zone("UTC")).timestamp(), json.dumps(saved, ensure_ascii=False),
        ))
        return saved

    def update_event(self, event_id, event, expected_version, request_id, connection=None):
        if connection is None:
            with self.connection(immediate=True) as conn:
                return self.update_event(event_id, event, expected_version, request_id, conn)
        current = self.get_event(event_id, connection)
        if current["version"] != expected_version:
            raise AgentError("version_conflict", "日程已变化，请重新读取")
        saved = dict(event, id=event_id, version=str(int(expected_version) + 1), recurring=False)
        if current.get("external"): saved["external"] = current["external"]
        connection.execute("UPDATE events SET starts=?,ends=?,body=? WHERE id=?", (
            parse_datetime(saved["start"], zone("UTC")).timestamp(),
            parse_datetime(saved["end"], zone("UTC")).timestamp(),
            json.dumps(saved, ensure_ascii=False), event_id,
        ))
        return saved

    def delete_event(self, event_id, expected_version, request_id, connection=None):
        if connection is None:
            with self.connection(immediate=True) as conn:
                return self.delete_event(event_id, expected_version, request_id, conn)
        current = self.get_event(event_id, connection)
        if current["version"] != expected_version:
            raise AgentError("version_conflict", "日程已变化，请重新读取")
        connection.execute("DELETE FROM events WHERE id=?", (event_id,))
        return {"deleted": True, "event_id": event_id}
