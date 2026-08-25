"""SQLite persistence and durable long-poll events for dwell."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import date as calendar_date
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Iterable


def _validated_date(value: Any, *, allow_empty: bool = False) -> str:
    raw = str(value or "")
    if allow_empty and not raw:
        return ""
    if len(raw) != 10:
        raise ValueError("date must be a real YYYY-MM-DD date")
    try:
        calendar_date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("date must be a real YYYY-MM-DD date") from exc
    return raw


def _validated_time(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if len(raw) != 5:
        raise ValueError("time must be HH:MM")
    try:
        clock_time.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("time must be HH:MM") from exc
    return raw


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    last REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    extra TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat_seq ON messages(chat_id, seq);
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    message_seq INTEGER NOT NULL REFERENCES messages(seq) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK(type IN ('image','file','audio','video')),
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    width INTEGER,
    height INTEGER,
    duration REAL,
    data BLOB NOT NULL DEFAULT X'',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS attachments_message_seq
    ON attachments(chat_id, message_seq, id);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    created_at REAL NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS domain_events_idempotency
    ON domain_events(idempotency_key) WHERE idempotency_key<>'';
CREATE INDEX IF NOT EXISTS domain_events_type_id ON domain_events(type,id);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    who TEXT NOT NULL CHECK(who IN ('gu','her')),
    text TEXT NOT NULL,
    boxed INTEGER NOT NULL DEFAULT 0,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    side TEXT NOT NULL CHECK(side IN ('mine','hers')),
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    at_time TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL DEFAULT '',
    fixed INTEGER NOT NULL DEFAULT 0,
    by_who TEXT NOT NULL DEFAULT 'her',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    text TEXT NOT NULL,
    time_text TEXT NOT NULL DEFAULT '',
    yearly INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL DEFAULT 'normal'
);
CREATE TABLE IF NOT EXISTS calendar_days (
    date TEXT PRIMARY KEY,
    mood TEXT NOT NULL DEFAULT '',
    flow TEXT NOT NULL DEFAULT '',
    pain INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    private TEXT NOT NULL DEFAULT '',
    menstrual INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS her_diary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    at REAL NOT NULL,
    author_type TEXT NOT NULL DEFAULT 'user',
    author_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS whispers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    who TEXT NOT NULL CHECK(who IN ('gu','her')),
    text TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gong_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    think TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS nook_progress (
    slug TEXT PRIMARY KEY,
    chapter INTEGER NOT NULL DEFAULT 0,
    page INTEGER NOT NULL DEFAULT 0,
    mode INTEGER NOT NULL DEFAULT 2,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS nook_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    chapter INTEGER NOT NULL,
    parent_id INTEGER REFERENCES nook_annotations(id) ON DELETE CASCADE,
    anchor TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    who TEXT NOT NULL DEFAULT 'user',
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS nook_annotations_lookup
    ON nook_annotations(slug, chapter, parent_id, id);
CREATE TABLE IF NOT EXISTS nook_book_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    author TEXT NOT NULL CHECK(author IN ('user','resident')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS nook_book_notes_order
    ON nook_book_notes(slug, pinned DESC, updated DESC, id DESC);
CREATE TABLE IF NOT EXISTS nook_book_note_requests (
    client_message_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    note_id INTEGER NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS nook_reading_deliveries (
    delivery_key TEXT PRIMARY KEY,
    event_key TEXT NOT NULL,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    message_seq INTEGER NOT NULL REFERENCES messages(seq) ON DELETE CASCADE,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS nook_reading_deliveries_event
    ON nook_reading_deliveries(event_key, created);
CREATE TABLE IF NOT EXISTS nook_book_settings (
    slug TEXT PRIMARY KEY,
    prompt TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_replies (
    user_seq INTEGER PRIMARY KEY REFERENCES messages(seq) ON DELETE CASCADE,
    assistant_seq INTEGER NOT NULL REFERENCES messages(seq) ON DELETE CASCADE,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_wait_log (
    request_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL DEFAULT '',
    after_seq INTEGER NOT NULL,
    returned_cursor INTEGER NOT NULL DEFAULT 0,
    timed_out INTEGER NOT NULL DEFAULT 0,
    disconnect_reason TEXT NOT NULL DEFAULT '',
    continuous INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS mcp_wait_log_started
    ON mcp_wait_log(started_at);
CREATE TABLE IF NOT EXISTS daily_reports (
    date TEXT PRIMARY KEY,
    body TEXT NOT NULL DEFAULT '',
    resident_comment TEXT NOT NULL DEFAULT '',
    commented_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS message_search_index (
    message_seq INTEGER PRIMARY KEY REFERENCES messages(seq) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS message_search_index_chat
    ON message_search_index(chat_id, message_seq);
CREATE TABLE IF NOT EXISTS message_embeddings (
    message_seq INTEGER PRIMARY KEY REFERENCES messages(seq) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    model TEXT NOT NULL,
    dims INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    embedding BLOB NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS message_embeddings_lookup
    ON message_embeddings(chat_id, model, kind, message_seq);
CREATE TABLE IF NOT EXISTS message_embedding_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    start_seq INTEGER NOT NULL,
    end_seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    dims INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    embedding BLOB NOT NULL,
    updated REAL NOT NULL,
    UNIQUE(chat_id, start_seq, end_seq, model)
);
CREATE INDEX IF NOT EXISTS message_embedding_segments_lookup
    ON message_embedding_segments(chat_id, model, start_seq, end_seq);
CREATE TABLE IF NOT EXISTS proactive_messages (
    client_message_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    assistant_seq INTEGER NOT NULL,
    created REAL NOT NULL
);
-- FTS5 mirror over messages(text). External-content FTS requires the virtual
-- table's column name to match the underlying content table's column name.
-- We therefore name the FTS column "text" (same as messages.text).
-- Database.index_message keeps the mirror in sync after every insert.
CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    text,
    content='messages',
    content_rowid='seq',
    tokenize='unicode61'
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._condition = threading.Condition()
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Lightweight migrations for DBs created before later schema additions.
            # SQLite lacks ADD COLUMN IF NOT EXISTS, so introspect and ALTER as needed.
            self._migrate_columns(conn)
            if conn.execute("SELECT 1 FROM chats LIMIT 1").fetchone() is None:
                now = time.time()
                chat_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO chats(id,name,created,last) VALUES(?,?,?,?)",
                    (chat_id, "Claude", now, now),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES('current_chat',?)",
                    (chat_id,),
                )
            elif conn.execute("SELECT 1 FROM settings WHERE key='current_chat'").fetchone() is None:
                chat_id = conn.execute(
                    "SELECT id FROM chats WHERE archived=0 ORDER BY last DESC LIMIT 1"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO settings(key,value) VALUES('current_chat',?)", (chat_id,)
                )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(str(row[1]) == column for row in rows)

    def _migrate_columns(self, conn: sqlite3.Connection) -> None:
        if not self._column_exists(conn, "todos", "due_date"):
            conn.execute(
                "ALTER TABLE todos ADD COLUMN due_date TEXT NOT NULL DEFAULT ''"
            )
        if not self._column_exists(conn, "calendar_days", "menstrual"):
            conn.execute(
                "ALTER TABLE calendar_days ADD COLUMN menstrual INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            "UPDATE calendar_days SET menstrual=1,flow='' WHERE flow='menstrual'"
        )
        if not self._column_exists(conn, "her_diary", "author_type"):
            conn.execute(
                "ALTER TABLE her_diary ADD COLUMN author_type TEXT NOT NULL DEFAULT 'user'"
            )
        if not self._column_exists(conn, "her_diary", "author_id"):
            conn.execute(
                "ALTER TABLE her_diary ADD COLUMN author_id TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "UPDATE her_diary SET author_id="
            "CASE author_type WHEN 'resident' THEN 'resident' ELSE 'owner' END "
            "WHERE author_id=''"
        )
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS her_diary_author ON her_diary(author_type, at)")
        except sqlite3.OperationalError:
            pass
        self._ensure_message_fts(conn)
        # Older builds stored the current reading page only in a mutable setting.
        # Recover stable delivery keys from existing nook messages so the first
        # reopen after an upgrade does not resend a page already delivered.
        for row in conn.execute(
            "SELECT seq,chat_id,extra,at FROM messages WHERE kind='nook' ORDER BY seq"
        ).fetchall():
            try:
                extra = json.loads(row[2] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(extra, dict):
                continue
            reading = extra.get("reading")
            if extra.get("source") != "nook-page" or not isinstance(reading, dict):
                continue
            slug = str(reading.get("slug") or "")
            if not slug:
                continue
            event_key = (
                f"reading:{slug}:{int(reading.get('chapter') or 0)}:"
                f"{int(reading.get('page') or 0)}"
            )
            conn.execute(
                "INSERT OR IGNORE INTO nook_reading_deliveries"
                "(delivery_key,event_key,chat_id,message_seq,created) VALUES(?,?,?,?,?)",
                (event_key, event_key, row[1], int(row[0]), float(row[3] or 0)),
            )

    @staticmethod
    def _ensure_message_fts(conn: sqlite3.Connection) -> None:
        """Use trigram FTS where available and keep the external index in sync."""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='message_fts'"
        ).fetchone()
        sql = str(row[0] or "") if row else ""
        rebuilt = False
        if "tokenize='trigram'" not in sql:
            for trigger in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.execute("DROP TABLE IF EXISTS message_fts")
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE message_fts USING fts5("
                    "text,content='messages',content_rowid='seq',tokenize='trigram')"
                )
            except sqlite3.OperationalError:
                conn.execute(
                    "CREATE VIRTUAL TABLE message_fts USING fts5("
                    "text,content='messages',content_rowid='seq',tokenize='unicode61')"
                )
            rebuilt = True
        trigger_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='messages_fts_ai'"
        ).fetchone()
        conn.executescript(
            "CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN "
            "INSERT INTO message_fts(rowid,text) VALUES(new.seq,new.text); END;"
            "CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN "
            "INSERT INTO message_fts(message_fts,rowid,text) VALUES('delete',old.seq,old.text); END;"
            "CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF text ON messages BEGIN "
            "INSERT INTO message_fts(message_fts,rowid,text) VALUES('delete',old.seq,old.text); "
            "INSERT INTO message_fts(rowid,text) VALUES(new.seq,new.text); END;"
        )
        if rebuilt or not trigger_exists:
            try:
                conn.execute("INSERT INTO message_fts(message_fts) VALUES('rebuild')")
            except sqlite3.OperationalError:
                pass

    def query(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(args)).fetchall()]

    def one(self, sql: str, args: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(sql, tuple(args)).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self.connect() as conn:
            cur = conn.execute(sql, tuple(args))
            return int(cur.lastrowid or cur.rowcount or 0)

    def setting(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def delete_setting(self, key: str) -> None:
        self.execute("DELETE FROM settings WHERE key=?", (key,))

    def current_chat(self) -> dict[str, Any]:
        chat_id = self.setting("current_chat")
        row = self.one("SELECT * FROM chats WHERE id=?", (chat_id,))
        if row:
            return row
        row = self.one("SELECT * FROM chats ORDER BY last DESC LIMIT 1")
        if not row:
            raise RuntimeError("chat database is not initialized")
        self.set_setting("current_chat", row["id"])
        return row

    def create_chat(self, name: str = "New chat") -> dict[str, Any]:
        now = time.time()
        chat_id = uuid.uuid4().hex
        self.execute(
            "INSERT INTO chats(id,name,created,last) VALUES(?,?,?,?)",
            (chat_id, name[:80] or "New chat", now, now),
        )
        self.set_setting("current_chat", chat_id)
        return self.one("SELECT * FROM chats WHERE id=?", (chat_id,)) or {}

    def append_message(
        self,
        kind: str,
        text: str,
        extra: str = "",
        chat_id: str | None = None,
    ) -> int:
        target = chat_id or self.current_chat()["id"]
        now = time.time()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages(chat_id,kind,text,extra,at) VALUES(?,?,?,?,?)",
                (target, kind, text, extra, now),
            )
            conn.execute("UPDATE chats SET last=? WHERE id=?", (now, target))
            seq = int(cur.lastrowid)
            conn.execute(
                "INSERT OR REPLACE INTO message_search_index(message_seq,chat_id,body) VALUES(?,?,?)",
                (seq, target, str(text or "")),
            )
        with self._condition:
            self._condition.notify_all()
        return seq

    def append_message_with_attachments(
        self,
        kind: str,
        text: str,
        attachments: Iterable[dict[str, Any]],
        extra: str = "",
        chat_id: str | None = None,
    ) -> int:
        """Append one message and its durable attachment payloads atomically."""
        target = chat_id or self.current_chat()["id"]
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO messages(chat_id,kind,text,extra,at) VALUES(?,?,?,?,?)",
                (target, kind, text, extra, now),
            )
            seq = int(cur.lastrowid)
            conn.execute(
                "INSERT OR REPLACE INTO message_search_index(message_seq,chat_id,body) VALUES(?,?,?)",
                (seq, target, str(text or "")),
            )
            for item in attachments:
                payload = item.get("data") or b""
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                conn.execute(
                    "INSERT INTO attachments("
                    "id,chat_id,message_seq,type,name,mime,url,size,width,height,duration,data,created"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(item["id"]),
                        target,
                        seq,
                        str(item["type"]),
                        str(item["name"]),
                        str(item["mime"]),
                        str(item.get("url") or ""),
                        int(item.get("size") or len(payload)),
                        item.get("width"),
                        item.get("height"),
                        item.get("duration"),
                        sqlite3.Binary(bytes(payload)),
                        now,
                    ),
                )
            conn.execute("UPDATE chats SET last=? WHERE id=?", (now, target))
        with self._condition:
            self._condition.notify_all()
        return seq

    def append_reading_event(
        self,
        chat_id: str,
        event_key: str,
        text: str,
        extra: str,
        force: bool = False,
    ) -> tuple[int, bool]:
        """Atomically deliver one stable reading page event to one resident chat."""
        event_key = str(event_key or "")[:500]
        if not event_key:
            raise ValueError("reading event_key is required")
        delivery_key = event_key if not force else f"{event_key}:force:{uuid.uuid4().hex}"
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not force:
                existing = conn.execute(
                    "SELECT message_seq FROM nook_reading_deliveries "
                    "WHERE delivery_key=? AND chat_id=?",
                    (event_key, chat_id),
                ).fetchone()
                if existing:
                    return int(existing[0]), True
            cur = conn.execute(
                "INSERT INTO messages(chat_id,kind,text,extra,at) VALUES(?,?,?,?,?)",
                (chat_id, "nook", text, extra, now),
            )
            seq = int(cur.lastrowid)
            conn.execute(
                "INSERT OR REPLACE INTO message_search_index(message_seq,chat_id,body) "
                "VALUES(?,?,?)",
                (seq, chat_id, str(text or "")),
            )
            conn.execute(
                "INSERT INTO nook_reading_deliveries"
                "(delivery_key,event_key,chat_id,message_seq,created) VALUES(?,?,?,?,?)",
                (delivery_key, event_key, chat_id, seq, now),
            )
            conn.execute("UPDATE chats SET last=? WHERE id=?", (now, chat_id))
        with self._condition:
            self._condition.notify_all()
        return seq, False

    def messages(
        self,
        chat_id: str,
        limit: int = 400,
        before: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        limit = max(1, min(limit, 500))
        args: list[Any] = [chat_id]
        where = "chat_id=?"
        if before is not None:
            where += " AND seq<?"
            args.append(before)
        args.append(limit + 1)
        rows = self.query(
            f"SELECT seq,kind,text,extra,at FROM messages WHERE {where} "
            "ORDER BY seq DESC LIMIT ?",
            args,
        )
        more = len(rows) > limit
        rows = list(reversed(rows[:limit]))
        return rows, more

    def latest_message(self, chat_id: str | None = None) -> int:
        target = chat_id or self.current_chat()["id"]
        row = self.one(
            "SELECT COALESCE(MAX(seq),0) AS n FROM messages WHERE chat_id=?",
            (target,),
        )
        return int(row["n"] if row else 0)

    def validate_message_quote(
        self,
        chat_id: str,
        value: dict[str, Any],
        max_text: int = 2000,
    ) -> dict[str, Any]:
        """Validate a whole-message or selected-range quote against stored text."""
        message_seq = int(value.get("message_seq") or 0)
        row = self.one(
            "SELECT text FROM messages WHERE chat_id=? AND seq=? AND kind IN ('me','gu')",
            (chat_id, message_seq),
        )
        if not row:
            raise ValueError("quote message is not in the current chat")
        source = str(row.get("text") or "")
        quoted = str(value.get("text") or "")[:max_text]
        start = value.get("start_offset")
        end = value.get("end_offset")
        if start is None and end is None:
            if not quoted or quoted not in source:
                raise ValueError("quote text does not match the source message")
            return {"message_seq": message_seq, "text": quoted}
        if not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("quote offsets must be provided together")
        if start < 0 or end <= start or end > len(source):
            raise ValueError("quote offsets are outside the source message")
        if source[start:end] != quoted:
            raise ValueError("quote text does not match quote offsets")
        return {
            "message_seq": message_seq,
            "start_offset": start,
            "end_offset": end,
            "text": quoted,
        }

    def messages_after(
        self,
        chat_id: str,
        after: int,
        limit: int = 100,
        kinds: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        args: list[Any] = [chat_id, max(0, int(after))]
        where = "chat_id=? AND seq>?"
        if kinds:
            where += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            args.extend(kinds)
        args.append(limit)
        return self.query(
            f"SELECT seq,kind,text,extra,at FROM messages WHERE {where} "
            "ORDER BY seq LIMIT ?",
            args,
        )

    def message_context(
        self,
        chat_id: str,
        target_seq: int,
        before: int = 5,
        after: int = 5,
        kinds: tuple[str, ...] = ("me", "gu", "nook"),
    ) -> list[dict[str, Any]]:
        """Return N visible rows on either side, independent of numeric seq gaps."""
        placeholders = ",".join("?" for _ in kinds)
        target = self.one(
            f"SELECT seq,kind,text,extra,at FROM messages WHERE chat_id=? AND seq=? "
            f"AND kind IN ({placeholders})",
            (chat_id, int(target_seq), *kinds),
        )
        if not target:
            return []
        earlier = self.query(
            f"SELECT seq,kind,text,extra,at FROM messages WHERE chat_id=? AND seq<? "
            f"AND kind IN ({placeholders}) ORDER BY seq DESC LIMIT ?",
            (chat_id, int(target_seq), *kinds, max(0, int(before))),
        )
        later = self.query(
            f"SELECT seq,kind,text,extra,at FROM messages WHERE chat_id=? AND seq>? "
            f"AND kind IN ({placeholders}) ORDER BY seq LIMIT ?",
            (chat_id, int(target_seq), *kinds, max(0, int(after))),
        )
        return [*reversed(earlier), target, *later]

    def wait_messages(
        self,
        chat_id: str,
        after: int,
        timeout: float = 25.0,
        kinds: tuple[str, ...] = ("me",),
    ) -> tuple[int, list[dict[str, Any]]]:
        deadline = time.monotonic() + max(0.0, min(float(timeout), 30.0))
        while True:
            rows = self.messages_after(chat_id, after, 100, kinds)
            if rows:
                return self.latest_message(chat_id), rows
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.latest_message(chat_id), []
            with self._condition:
                self._condition.wait(timeout=min(remaining, 2.0))

    def notify_waiters(self) -> None:
        """Wake local long-poll callers, including a cancelled MCP wait."""
        with self._condition:
            self._condition.notify_all()

    def begin_mcp_wait(
        self,
        request_id: str,
        chat_id: str,
        started_at: str,
        after_seq: int,
        continuous: bool,
    ) -> None:
        self.execute(
            "INSERT INTO mcp_wait_log "
            "(request_id,chat_id,started_at,after_seq,continuous) "
            "VALUES(?,?,?,?,?)",
            (
                request_id,
                chat_id,
                started_at,
                max(0, int(after_seq)),
                1 if continuous else 0,
            ),
        )

    def finish_mcp_wait(
        self,
        request_id: str,
        ended_at: str,
        returned_cursor: int,
        timed_out: bool,
        disconnect_reason: str,
    ) -> None:
        self.execute(
            "UPDATE mcp_wait_log SET ended_at=?, returned_cursor=?, "
            "timed_out=?, disconnect_reason=? WHERE request_id=?",
            (
                ended_at,
                max(0, int(returned_cursor)),
                1 if timed_out else 0,
                str(disconnect_reason or "")[:120],
                request_id,
            ),
        )

    def append_mcp_reply(
        self,
        chat_id: str,
        user_seq: int,
        text: str,
        thinking: str = "",
        thinking_extra: str = "",
    ) -> tuple[int, int | None, bool]:
        """Append at most one resident reply for a user message."""
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT assistant_seq FROM mcp_replies WHERE user_seq=?",
                (user_seq,),
            ).fetchone()
            if existing:
                return int(existing[0]), None, True
            user = conn.execute(
                "SELECT 1 FROM messages WHERE seq=? AND chat_id=? AND kind='me'",
                (user_seq, chat_id),
            ).fetchone()
            if not user:
                raise ValueError("reply_to_seq is not a user message in the current chat")
            thinking_seq: int | None = None
            if thinking:
                cur = conn.execute(
                    "INSERT INTO messages(chat_id,kind,text,extra,at) VALUES(?,?,?,?,?)",
                    (chat_id, "think", thinking, thinking_extra, now),
                )
                thinking_seq = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO messages(chat_id,kind,text,extra,at) VALUES(?,?,?,?,?)",
                (chat_id, "gu", text, "", now),
            )
            assistant_seq = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO mcp_replies(user_seq,assistant_seq,created) VALUES(?,?,?)",
                (user_seq, assistant_seq, now),
            )
            conn.execute("UPDATE chats SET last=? WHERE id=?", (now, chat_id))
        with self._condition:
            self._condition.notify_all()
        return assistant_seq, thinking_seq, False

    def append_resident_message(
        self,
        chat_id: str,
        text: str,
        thinking: str = "",
        thinking_extra: str = "",
        reply_to_seq: int = 0,
        client_message_id: str = "",
        message_extra: str = "",
    ) -> tuple[int, int | None, bool]:
        """Atomically save a proactive message or a reply with retry idempotency."""
        now = time.time()
        reply_to_seq = max(0, int(reply_to_seq))
        client_message_id = str(client_message_id or "")[:200]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if client_message_id:
                existing = conn.execute(
                    "SELECT chat_id,assistant_seq FROM proactive_messages "
                    "WHERE client_message_id=?",
                    (client_message_id,),
                ).fetchone()
                if existing:
                    if str(existing[0]) != chat_id:
                        raise ValueError("client_message_id belongs to another chat")
                    return int(existing[1]), None, True

            if reply_to_seq:
                user = conn.execute(
                    "SELECT 1 FROM messages WHERE seq=? AND chat_id=? AND kind='me'",
                    (reply_to_seq, chat_id),
                ).fetchone()
                if not user:
                    raise ValueError("reply_to_seq is not a user message in the current chat")
                existing_reply = conn.execute(
                    "SELECT assistant_seq FROM mcp_replies WHERE user_seq=?",
                    (reply_to_seq,),
                ).fetchone()
                if existing_reply:
                    assistant_seq = int(existing_reply[0])
                    if client_message_id:
                        conn.execute(
                            "INSERT INTO proactive_messages"
                            "(client_message_id,chat_id,assistant_seq,created) VALUES(?,?,?,?)",
                            (client_message_id, chat_id, assistant_seq, now),
                        )
                    return assistant_seq, None, True

            thinking_seq: int | None = None
            if thinking:
                cur = conn.execute(
                    "INSERT INTO messages(chat_id,kind,text,extra,at) VALUES(?,?,?,?,?)",
                    (chat_id, "think", thinking, thinking_extra, now),
                )
                thinking_seq = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO messages(chat_id,kind,text,extra,at) VALUES(?,?,?,?,?)",
                (chat_id, "gu", text, message_extra, now),
            )
            assistant_seq = int(cur.lastrowid)
            conn.execute(
                "INSERT OR REPLACE INTO message_search_index(message_seq,chat_id,body) "
                "VALUES(?,?,?)",
                (assistant_seq, chat_id, text),
            )
            if reply_to_seq:
                conn.execute(
                    "INSERT INTO mcp_replies(user_seq,assistant_seq,created) VALUES(?,?,?)",
                    (reply_to_seq, assistant_seq, now),
                )
            if client_message_id:
                conn.execute(
                    "INSERT INTO proactive_messages"
                    "(client_message_id,chat_id,assistant_seq,created) VALUES(?,?,?,?)",
                    (client_message_id, chat_id, assistant_seq, now),
                )
            conn.execute("UPDATE chats SET last=? WHERE id=?", (now, chat_id))
        with self._condition:
            self._condition.notify_all()
        return assistant_seq, thinking_seq, False

    def replace_mcp_reply(
        self,
        chat_id: str,
        user_seq: int,
        text: str,
        thinking: str,
        thinking_extra: str = "",
    ) -> tuple[int, int]:
        """Replace one resident answer and its thinking without moving later rounds."""
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            mapping = conn.execute(
                "SELECT assistant_seq FROM mcp_replies WHERE user_seq=?",
                (user_seq,),
            ).fetchone()
            if not mapping:
                raise ValueError("reply_to_seq has no resident reply to regenerate")
            assistant_seq = int(mapping[0])
            answer = conn.execute(
                "SELECT 1 FROM messages WHERE seq=? AND chat_id=? AND kind='gu'",
                (assistant_seq, chat_id),
            ).fetchone()
            if not answer:
                raise ValueError("resident reply is missing from the current chat")
            thinking_rows = conn.execute(
                "SELECT seq FROM messages WHERE chat_id=? AND kind='think' "
                "AND seq>? AND seq<? ORDER BY seq",
                (chat_id, user_seq, assistant_seq),
            ).fetchall()
            if not thinking_rows:
                raise ValueError("resident reply has no thinking row to replace")
            thinking_seq = int(thinking_rows[0][0])
            conn.execute(
                "UPDATE messages SET text=?,extra=?,at=? WHERE seq=? AND chat_id=?",
                (thinking, thinking_extra, now, thinking_seq, chat_id),
            )
            if len(thinking_rows) > 1:
                conn.executemany(
                    "DELETE FROM messages WHERE seq=? AND chat_id=?",
                    [(int(row[0]), chat_id) for row in thinking_rows[1:]],
                )
            conn.execute(
                "UPDATE messages SET text=?,at=? WHERE seq=? AND chat_id=?",
                (text, now, assistant_seq, chat_id),
            )
            conn.execute(
                "UPDATE mcp_replies SET created=? WHERE user_seq=?",
                (now, user_seq),
            )
            conn.execute(
                "DELETE FROM messages WHERE chat_id=? AND kind='mcp_regenerate' AND text=?",
                (chat_id, str(user_seq)),
            )
            conn.execute("UPDATE chats SET last=? WHERE id=?", (now, chat_id))
        with self._condition:
            self._condition.notify_all()
        return assistant_seq, thinking_seq

    def book_notes(self, slug: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT id,slug,author,title,summary,body,pinned,created,updated "
            "FROM nook_book_notes WHERE slug=? "
            "ORDER BY pinned DESC, updated DESC, id DESC",
            (slug,),
        )

    def search_book_notes(self, slug: str, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        needle = query.strip().casefold()
        found = []
        for note in self.book_notes(slug):
            searchable = "\n".join(
                str(note.get(key) or "") for key in ("title", "summary", "body")
            ).casefold()
            if needle and needle not in searchable:
                continue
            found.append({
                "id": note["id"],
                "title": note["title"],
                "summary": note["summary"],
                "pinned": note["pinned"],
                "updated": note["updated"],
            })
            if len(found) >= max(1, min(int(limit), 100)):
                break
        return found

    def book_note(self, slug: str, note_id: int) -> dict[str, Any] | None:
        return self.one(
            "SELECT id,slug,author,title,summary,body,pinned,created,updated "
            "FROM nook_book_notes WHERE slug=? AND id=?",
            (slug, note_id),
        )

    def add_book_note(
        self,
        slug: str,
        author: str,
        title: str,
        summary: str,
        body: str,
        pinned: bool = False,
    ) -> int:
        now = time.time()
        return self.execute(
            "INSERT INTO nook_book_notes(slug,author,title,summary,body,pinned,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (slug, author, title, summary, body, 1 if pinned else 0, now, now),
        )

    def save_book_note_idempotent(
        self,
        slug: str,
        author: str,
        title: str,
        summary: str,
        body: str,
        pinned: bool,
        client_message_id: str,
        note_id: int = 0,
    ) -> tuple[int, bool]:
        """Create/update one book note exactly once for a resident request key."""
        client_message_id = str(client_message_id or "")[:200]
        if not client_message_id:
            raise ValueError("client_message_id is required")
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT slug,note_id FROM nook_book_note_requests WHERE client_message_id=?",
                (client_message_id,),
            ).fetchone()
            if previous:
                if str(previous[0]) != slug:
                    raise ValueError("client_message_id belongs to another book")
                return int(previous[1]), True
            if note_id:
                current = conn.execute(
                    "SELECT author FROM nook_book_notes WHERE slug=? AND id=?",
                    (slug, int(note_id)),
                ).fetchone()
                if not current:
                    raise ValueError("note not found")
                if str(current[0]) != author:
                    raise ValueError("resident cannot overwrite the user's book note")
                conn.execute(
                    "UPDATE nook_book_notes SET title=?,summary=?,body=?,pinned=?,updated=? "
                    "WHERE id=? AND slug=?",
                    (
                        title,
                        summary,
                        body,
                        1 if pinned else 0,
                        now,
                        int(note_id),
                        slug,
                    ),
                )
                saved_id = int(note_id)
            else:
                cur = conn.execute(
                    "INSERT INTO nook_book_notes"
                    "(slug,author,title,summary,body,pinned,created,updated) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (slug, author, title, summary, body, 1 if pinned else 0, now, now),
                )
                saved_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO nook_book_note_requests"
                "(client_message_id,slug,note_id,created) VALUES(?,?,?,?)",
                (client_message_id, slug, saved_id, now),
            )
        return saved_id, False

    def update_book_note(
        self,
        slug: str,
        note_id: int,
        title: str,
        summary: str,
        body: str,
    ) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE nook_book_notes SET title=?,summary=?,body=?,updated=? "
                "WHERE id=? AND slug=?",
                (title, summary, body, time.time(), note_id, slug),
            )
            return cur.rowcount > 0

    def pin_book_note(self, slug: str, note_id: int, pinned: bool) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE nook_book_notes SET pinned=?,updated=? WHERE id=? AND slug=?",
                (1 if pinned else 0, time.time(), note_id, slug),
            )
            return cur.rowcount > 0

    def delete_book_note(self, slug: str, note_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM nook_book_notes WHERE id=? AND slug=?",
                (note_id, slug),
            )
            return cur.rowcount > 0

    def book_prompt(self, slug: str) -> str:
        row = self.one("SELECT prompt FROM nook_book_settings WHERE slug=?", (slug,))
        return str(row["prompt"]) if row else ""

    def set_book_prompt(self, slug: str, prompt: str) -> None:
        self.execute(
            "INSERT INTO nook_book_settings(slug,prompt,updated) VALUES(?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET prompt=excluded.prompt,updated=excluded.updated",
            (slug, prompt, time.time()),
        )

    def delete_book_data(self, slug: str) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM nook_annotations WHERE slug=?", (slug,))
            conn.execute("DELETE FROM nook_progress WHERE slug=?", (slug,))
            conn.execute("DELETE FROM nook_book_notes WHERE slug=?", (slug,))
            conn.execute("DELETE FROM nook_book_settings WHERE slug=?", (slug,))

    def latest_event(self) -> int:
        row = self.one("SELECT COALESCE(MAX(seq),0) AS n FROM events")
        return int(row["n"] if row else 0)

    def append_event(self, payload: dict[str, Any]) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO events(payload,at) VALUES(?,?)",
                (json.dumps(payload, ensure_ascii=False), time.time()),
            )
            seq = int(cur.lastrowid)
            conn.execute(
                "DELETE FROM events WHERE seq < (SELECT COALESCE(MAX(seq),0)-5000 FROM events)"
            )
        with self._condition:
            self._condition.notify_all()
        return seq

    def poll_events(self, since: int, timeout: float = 25.0) -> tuple[int, list[dict[str, Any]]]:
        deadline = time.monotonic() + max(0.0, min(timeout, 30.0))
        while True:
            rows = self.query(
                "SELECT seq,payload FROM events WHERE seq>? ORDER BY seq LIMIT 200",
                (since,),
            )
            if rows:
                events: list[dict[str, Any]] = []
                next_id = since
                for row in rows:
                    next_id = max(next_id, int(row["seq"]))
                    try:
                        events.append(json.loads(row["payload"]))
                    except ValueError:
                        continue
                return next_id, events
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return max(since, self.latest_event()), []
            with self._condition:
                self._condition.wait(timeout=min(remaining, 2.0))

    def append_domain_event(
        self,
        event_type: str,
        actor_type: str,
        actor_id: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> int:
        """Append one durable business event, separate from ephemeral UI frames."""
        event_type = str(event_type or "")[:120]
        if not event_type:
            raise ValueError("event type is required")
        actor_type = str(actor_type or "system")[:40]
        actor_id = str(actor_id or "")[:120]
        key = str(idempotency_key or "")[:300]
        now = time.time()
        encoded = json.dumps(payload or {}, ensure_ascii=False)
        with self.connect() as conn:
            if key:
                conn.execute(
                    "INSERT OR IGNORE INTO domain_events"
                    "(type,created_at,actor_type,actor_id,payload,idempotency_key) "
                    "VALUES(?,?,?,?,?,?)",
                    (event_type, now, actor_type, actor_id, encoded, key),
                )
                row = conn.execute(
                    "SELECT id FROM domain_events WHERE idempotency_key=?", (key,)
                ).fetchone()
                return int(row[0])
            cur = conn.execute(
                "INSERT INTO domain_events"
                "(type,created_at,actor_type,actor_id,payload,idempotency_key) "
                "VALUES(?,?,?,?,?,'')",
                (event_type, now, actor_type, actor_id, encoded),
            )
            return int(cur.lastrowid)

    def domain_events_after(
        self,
        after_event_id: int = 0,
        types: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["id>?"]
        args: list[Any] = [max(0, int(after_event_id))]
        if types:
            clauses.append("type IN (" + ",".join("?" for _ in types) + ")")
            args.extend(types)
        args.append(max(1, min(int(limit), 200)))
        rows = self.query(
            "SELECT id,type,created_at,actor_type,actor_id,payload FROM domain_events "
            "WHERE " + " AND ".join(clauses) + " ORDER BY id LIMIT ?",
            args,
        )
        for row in rows:
            try:
                row["payload"] = json.loads(row.get("payload") or "{}")
            except (TypeError, ValueError):
                row["payload"] = {}
        return rows

    # ---------------- diary (with author_type) ----------------

    def add_diary_entry(
        self,
        text: str,
        author_type: str = "user",
        author_id: str = "",
    ) -> int:
        if author_type not in ("user", "resident"):
            author_type = "user"
        stable_author_id = str(author_id or ("resident" if author_type == "resident" else "owner"))[:80]
        return self.execute(
            "INSERT INTO her_diary(text,at,author_type,author_id) VALUES(?,?,?,?)",
            (text, time.time(), author_type, stable_author_id),
        )

    def update_diary_entry(
        self,
        entry_id: int,
        text: str,
        required_author_type: str | None = None,
    ) -> bool:
        where = "id=?"
        args: list[Any] = [text, time.time(), int(entry_id)]
        if required_author_type in ("user", "resident"):
            where += " AND author_type=?"
            args.append(required_author_type)
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE her_diary SET text=?, at=? WHERE {where}",
                args,
            )
            return cur.rowcount > 0

    def delete_diary_entry(
        self,
        entry_id: int,
        required_author_type: str | None = None,
    ) -> bool:
        where = "id=?"
        args: list[Any] = [int(entry_id)]
        if required_author_type in ("user", "resident"):
            where += " AND author_type=?"
            args.append(required_author_type)
        with self.connect() as conn:
            cur = conn.execute(f"DELETE FROM her_diary WHERE {where}", args)
            return cur.rowcount > 0

    def diary_entry(self, entry_id: int) -> dict[str, Any] | None:
        return self.one(
            "SELECT id,text,at,author_type,author_id FROM her_diary WHERE id=?",
            (int(entry_id),),
        )

    def diary_entries(
        self,
        author_type: str | None = None,
        date_from: float | None = None,
        date_to: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if author_type in ("user", "resident"):
            clauses.append("author_type=?")
            args.append(author_type)
        if date_from is not None:
            clauses.append("at>=?")
            args.append(float(date_from))
        if date_to is not None:
            clauses.append("at<=?")
            args.append(float(date_to))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(max(1, min(int(limit), 500)))
        return self.query(
            f"SELECT id,text,at,author_type,author_id FROM her_diary{where} "
            "ORDER BY at DESC, id DESC LIMIT ?",
            args,
        )

    # ---------------- todos ----------------

    def todos_list(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {"mine": [], "hers": []}
        for row in self.query(
            "SELECT id,side,text,done,at_time,due_date,fixed,by_who,created FROM todos ORDER BY id"
        ):
            side = row["side"] if row["side"] in ("mine", "hers") else "hers"
            result[side].append(
                {
                    "id": int(row["id"]),
                    "text": str(row["text"]),
                    "done": bool(row["done"]),
                    "at_time": str(row["at_time"] or ""),
                    "due_date": str(row["due_date"] or ""),
                    "fixed": bool(row["fixed"]),
                    "by_who": str(row["by_who"] or ""),
                    "created": float(row["created"] or 0),
                }
            )
        return result

    def todos_add(
        self,
        side: str,
        text: str,
        at_time: str = "",
        due_date: str = "",
        fixed: bool = False,
        by_who: str = "her",
    ) -> int:
        if side not in ("mine", "hers"):
            side = "hers"
        clean_time = _validated_time(at_time)
        clean_due_date = _validated_date(due_date, allow_empty=True)
        return self.execute(
            "INSERT INTO todos(side,text,at_time,due_date,fixed,by_who,created) VALUES(?,?,?,?,?,?,?)",
            (side, text[:500], clean_time, clean_due_date, 1 if fixed else 0, str(by_who or "her")[:20], time.time()),
        )

    def todos_toggle(self, side: str, todo_id: int) -> bool:
        if side not in ("mine", "hers"):
            side = "hers"
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE todos SET done=1-done WHERE id=? AND side=?",
                (int(todo_id), side),
            )
            return cur.rowcount > 0

    def todos_set_done(self, side: str, todo_id: int, done: bool) -> bool:
        if side not in ("mine", "hers"):
            side = "hers"
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE todos SET done=? WHERE id=? AND side=?",
                (1 if done else 0, int(todo_id), side),
            )
            return cur.rowcount > 0

    def todos_delete(self, side: str, todo_id: int) -> bool:
        if side not in ("mine", "hers"):
            side = "hers"
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM todos WHERE id=? AND side=?",
                (int(todo_id), side),
            )
            return cur.rowcount > 0

    def todos_update(self, side: str, todo_id: int, text: str | None = None,
                     at_time: str | None = None, due_date: str | None = None,
                     fixed: bool | None = None) -> bool:
        if side not in ("mine", "hers"):
            side = "hers"
        sets: list[str] = []
        args: list[Any] = []
        if text is not None:
            sets.append("text=?")
            args.append(str(text)[:500])
        if at_time is not None:
            sets.append("at_time=?")
            args.append(_validated_time(at_time))
        if due_date is not None:
            sets.append("due_date=?")
            args.append(_validated_date(due_date, allow_empty=True))
        if fixed is not None:
            sets.append("fixed=?")
            args.append(1 if fixed else 0)
        if not sets:
            return False
        args.extend([int(todo_id), side])
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE todos SET {', '.join(sets)} WHERE id=? AND side=?",
                args,
            )
            return cur.rowcount > 0

    # ---------------- calendar ----------------

    def calendar_events(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT id,date,text,time_text,yearly,type FROM calendar_events "
            "ORDER BY date,time_text,id"
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            events.append(
                {
                    "id": int(row["id"]),
                    "date": str(row["date"]),
                    "text": str(row["text"]),
                    "time": str(row["time_text"] or ""),
                    "yearly": bool(row["yearly"]),
                    "type": str(row["type"] or "normal"),
                }
            )
        return events

    def calendar_day_states(self) -> dict[str, dict[str, Any]]:
        days: dict[str, dict[str, Any]] = {}
        for row in self.query("SELECT * FROM calendar_days"):
            date = row["date"]
            days[date] = {
                "mood": str(row["mood"] or ""),
                "flow": str(row["flow"] or ""),
                "pain": int(row["pain"] or 0),
                "note": str(row["note"] or ""),
                "private": str(row["private"] or ""),
                "menstrual": bool(row["menstrual"]),
            }
        return days

    def calendar_add_event(
        self,
        date: str,
        text: str,
        time_text: str = "",
        yearly: bool = False,
        special: bool = False,
    ) -> int:
        clean_date = _validated_date(date)
        clean_text = str(text or "").strip()[:500]
        if not clean_text:
            raise ValueError("text is required")
        return self.execute(
            "INSERT INTO calendar_events(date,text,time_text,yearly,type) VALUES(?,?,?,?,?)",
            (clean_date, clean_text, _validated_time(time_text), 1 if yearly else 0,
             "special" if special else "normal"),
        )

    def calendar_delete_event(self, event_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM calendar_events WHERE id=?", (int(event_id),))
            return cur.rowcount > 0

    def calendar_update_event(
        self,
        event_id: int,
        date: str | None = None,
        text: str | None = None,
        time_text: str | None = None,
        yearly: bool | None = None,
        special: bool | None = None,
    ) -> bool:
        current = self.one("SELECT * FROM calendar_events WHERE id=?", (int(event_id),))
        if not current:
            return False
        clean_date = _validated_date(date if date is not None else current["date"])
        clean_text = str(text if text is not None else current["text"]).strip()[:500]
        if not clean_text:
            raise ValueError("text is required")
        clean_time = _validated_time(
            time_text if time_text is not None else current["time_text"]
        )
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE calendar_events SET date=?,text=?,time_text=?,yearly=?,type=? WHERE id=?",
                (
                    clean_date,
                    clean_text,
                    clean_time,
                    1 if (yearly if yearly is not None else bool(current["yearly"])) else 0,
                    "special" if (special if special is not None else current["type"] == "special") else "normal",
                    int(event_id),
                ),
            )
            return cur.rowcount > 0

    def calendar_upsert_day(
        self,
        date: str,
        mood: str | None = None,
        flow: str | None = None,
        pain: int | None = None,
        note: str | None = None,
        private: str | None = None,
        menstrual: bool | None = None,
    ) -> None:
        date = _validated_date(date)
        old = self.one("SELECT * FROM calendar_days WHERE date=?", (date,)) or {}
        next_mood = str(mood if mood is not None else old.get("mood", ""))[:40]
        next_flow = str(flow if flow is not None else old.get("flow", ""))[:100]
        next_pain = int(pain if pain is not None else old.get("pain", 0) or 0)
        next_note = str(note if note is not None else old.get("note", ""))[:2000]
        next_private = str(private if private is not None else old.get("private", ""))[:2000]
        next_menstrual = bool(
            menstrual if menstrual is not None else old.get("menstrual", 0)
        )
        self.execute(
            "INSERT INTO calendar_days(date,mood,flow,pain,note,private,menstrual) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET mood=excluded.mood,flow=excluded.flow,"
            "pain=excluded.pain,note=excluded.note,private=excluded.private,"
            "menstrual=excluded.menstrual",
            (date, next_mood, next_flow, next_pain, next_note, next_private, 1 if next_menstrual else 0),
        )

    def calendar_day_menstrual(self, date: str) -> bool:
        row = self.one("SELECT menstrual FROM calendar_days WHERE date=?", (date,))
        return bool(row and row.get("menstrual"))

    # ---------------- daily reports ----------------

    def daily_report_dates(self) -> list[str]:
        return [row["date"] for row in self.query("SELECT date FROM daily_reports ORDER BY date DESC")]

    def daily_report(self, date: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT date,body,resident_comment,commented_at,updated_at FROM daily_reports WHERE date=?",
            (date,),
        )

    def upsert_daily_report(self, date: str, body: str) -> dict[str, Any]:
        now = time.time()
        self.execute(
            "INSERT INTO daily_reports(date,body,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at",
            (date, body, now),
        )
        return self.daily_report(date) or {}

    def update_daily_report_comment(self, date: str, comment: str) -> dict[str, Any]:
        now = time.time()
        self.execute(
            "INSERT INTO daily_reports(date,resident_comment,commented_at,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET resident_comment=excluded.resident_comment,"
            "commented_at=excluded.commented_at,updated_at=excluded.updated_at",
            (date, comment, now, now),
        )
        return self.daily_report(date) or {}

    # ---------------- proactive messages (idempotency) ----------------

    def remember_proactive(self, client_message_id: str, chat_id: str, assistant_seq: int) -> bool:
        """Return True if this client_message_id is new (and we stored the mapping)."""
        if not client_message_id:
            return True
        try:
            self.execute(
                "INSERT INTO proactive_messages(client_message_id,chat_id,assistant_seq,created) VALUES(?,?,?,?)",
                (str(client_message_id)[:200], chat_id, int(assistant_seq), time.time()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def proactive_seq(self, client_message_id: str) -> int:
        if not client_message_id:
            return 0
        row = self.one(
            "SELECT assistant_seq FROM proactive_messages WHERE client_message_id=?",
            (str(client_message_id),),
        )
        return int(row["assistant_seq"]) if row else 0

    def proactive_message(self, client_message_id: str) -> dict[str, Any] | None:
        """Return the message saved for an idempotency key, including its chat."""
        if not client_message_id:
            return None
        return self.one(
            "SELECT p.chat_id,p.assistant_seq,m.text,m.extra,m.at "
            "FROM proactive_messages p JOIN messages m ON m.seq=p.assistant_seq "
            "WHERE p.client_message_id=?",
            (str(client_message_id)[:200],),
        )

    # ---------------- message search index ----------------

    def index_message(self, chat_id: str, message_seq: int, text: str) -> None:
        try:
            self.execute(
                "INSERT OR REPLACE INTO message_search_index(message_seq,chat_id,body) VALUES(?,?,?)",
                (int(message_seq), chat_id, str(text or "")),
            )
        except sqlite3.IntegrityError:
            pass
        # Keep FTS5 mirror in sync if it exists. External-content FTS needs
        # an explicit INSERT into the mirror table; we use 'content=x' so the
        # mirror doesn't duplicate storage.
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO message_fts(rowid, text) VALUES (?, ?) "
                    "ON CONFLICT(rowid) DO UPDATE SET text=excluded.text",
                    (int(message_seq), str(text or "")),
                )
        except sqlite3.OperationalError:
            # FTS table not initialized yet — fine, LIKE fallback still works.
            pass

    def search_messages(
        self,
        needle: str,
        chat_id: str | None = None,
        kinds: tuple[str, ...] = ("me", "gu", "nook"),
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Keyword search over messages.

        Strategy: LIKE is the primary path. FTS5/trigram misses Chinese queries
        shorter than 3 characters and is awkward to mix with the external-content
        design, so we keep LIKE as the workhorse for keyword search and reserve
        FTS/sqlite-vec for the future semantic-search layer.
        """
        if not needle:
            return []
        return self._search_messages_like(needle, chat_id, kinds, limit)

    def _search_messages_fts(
        self,
        needle: str,
        chat_id: str | None,
        kinds: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        # Currently unused: FTS5 trigram tokenizer misses Chinese queries shorter
        # than 3 characters. Kept here so we can wire it back in once sqlite-vec
        # embedding search lands and we want a fused ranking path.
        clauses: list[str] = []
        args: list[Any] = []
        if chat_id:
            clauses.append("m.chat_id=?")
            args.append(chat_id)
        if kinds:
            clauses.append("m.kind IN (" + ",".join("?" for _ in kinds) + ")")
            args.extend(kinds)
        join_where = (" AND " + " AND ".join(clauses)) if clauses else ""
        sanitized = needle.replace('"', '""')
        args.append('"' + sanitized + '"')
        args.append(limit)
        return self.query(
            "SELECT m.seq,m.chat_id,m.kind,m.text,m.at "
            "FROM message_fts f JOIN messages m ON m.rowid = f.rowid "
            "WHERE f.text MATCH ?" + join_where + " "
            "ORDER BY bm25(message_fts) LIMIT ?",
            args,
        )

    def _search_messages_like(
        self,
        needle: str,
        chat_id: str | None,
        kinds: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        like = "%" + needle.replace("%", "\\%").replace("_", "\\_") + "%"
        clauses = ["m.text LIKE ? ESCAPE '\\'"]
        args: list[Any] = [like]
        if chat_id:
            clauses.append("m.chat_id=?")
            args.append(chat_id)
        if kinds:
            clauses.append("m.kind IN (" + ",".join("?" for _ in kinds) + ")")
            args.extend(kinds)
        args.append(limit)
        return self.query(
            "SELECT m.seq,m.chat_id,m.kind,m.text,m.at FROM messages m "
            "WHERE " + " AND ".join(clauses) + " ORDER BY m.seq DESC LIMIT ?",
            args,
        )

    def search_message_bigrams(
        self,
        bigrams: list[str],
        chat_id: str | None = None,
        kinds: tuple[str, ...] = ("me", "gu"),
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """OR-style LIKE search across multiple 2-character needles. Used by the
        passive related-history recall: extracts bigrams from a new user message
        and returns any older message that contains at least one of them.
        Results are deduplicated by seq and ordered by seq DESC."""
        bigrams = [b for b in bigrams if b and len(b) == 2][:10]
        if not bigrams:
            return []
        clauses: list[str] = []
        args: list[Any] = []
        for gram in bigrams:
            escaped = gram.replace("%", "\\%").replace("_", "\\_")
            clauses.append("m.text LIKE ? ESCAPE '\\'")
            args.append("%" + escaped + "%")
        if chat_id:
            clauses.append("m.chat_id=?")
            args.append(chat_id)
        if kinds:
            clauses.append("m.kind IN (" + ",".join("?" for _ in kinds) + ")")
            args.extend(kinds)
        args.append(max(1, min(int(limit), 200)))
        return self.query(
            "SELECT DISTINCT m.seq,m.chat_id,m.kind,m.text,m.at FROM messages m "
            "WHERE (" + " OR ".join(f"({c})" if c.startswith("m.text") else c for c in clauses[:len(bigrams)]) + ") "
            + (" AND " + " AND ".join(clauses[len(bigrams):]) if len(clauses) > len(bigrams) else "")
            + " ORDER BY m.seq DESC LIMIT ?",
            args,
        )
