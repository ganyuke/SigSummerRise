from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

REDACTED_SUMMARY = "[redacted]"
LLM_ISSUANCE_RETENTION_SECONDS = 30 * 24 * 3600


def _serialized(fn: F) -> F:
    @wraps(fn)
    def wrapper(self: Database, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    aci TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    consent_state TEXT NOT NULL DEFAULT 'unknown',
    opted_in_at INTEGER,
    last_consent_dm_at INTEGER,
    last_unopted_group_notice_at INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_aci TEXT,
    ts INTEGER NOT NULL,
    body TEXT,
    is_hole INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_aci);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    signal_timestamp INTEGER NOT NULL,
    window_json TEXT NOT NULL,
    summary_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_ts ON summaries(signal_timestamp);

CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_id INTEGER NOT NULL,
    sender_aci TEXT,
    body TEXT NOT NULL,
    ts INTEGER NOT NULL,
    FOREIGN KEY (summary_id) REFERENCES summaries(id)
);
CREATE INDEX IF NOT EXISTS idx_threads_ts ON threads(ts);

CREATE TABLE IF NOT EXISTS magic_tokens (
    token_hash TEXT PRIMARY KEY,
    user_aci TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_hash TEXT PRIMARY KEY,
    user_aci TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS link_issuance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_aci TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_link_issuance_user_ts ON link_issuance(user_aci, ts);

CREATE TABLE IF NOT EXISTS llm_issuance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_aci TEXT NOT NULL,
    ts INTEGER NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_issuance_user_ts ON llm_issuance(user_aci, ts);

CREATE TABLE IF NOT EXISTS runtime_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data_json TEXT NOT NULL DEFAULT '{}'
);
"""


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sqlcipher_module():
    try:
        from sqlcipher3 import dbapi2 as sqlcipher
    except ImportError as exc:
        raise RuntimeError(
            "sqlcipher3 is required. Install sqlcipher3-binary and the SQLCipher library."
        ) from exc
    return sqlcipher


def _pragma_key(conn: Any, key: str) -> None:
    escaped = key.replace("'", "''")
    conn.execute(f"PRAGMA key = '{escaped}'")
    conn.execute("PRAGMA cipher_compatibility = 4")


@dataclass
class User:
    aci: str
    display_name: str
    consent_state: str
    opted_in_at: int | None
    last_consent_dm_at: int | None
    last_unopted_group_notice_at: int | None = None
    exclude_from_summaries: bool = False
    exclude_from_questions: bool = False

    @property
    def opted_in(self) -> bool:
        return self.consent_state == "opted_in"


@dataclass
class StoredMessage:
    id: int
    sender_aci: str | None
    ts: int
    body: str | None
    is_hole: bool
    display_name: str | None = None


@dataclass
class Summary:
    id: int
    group_id: str
    signal_timestamp: int
    window_ids: list[int]
    summary_text: str
    kind: str = "summarize"


@dataclass
class ThreadEntry:
    id: int
    summary_id: int
    sender_aci: str | None
    body: str
    ts: int
    display_name: str | None = None


@dataclass
class DashboardRow:
    display_name: str
    consent_state: str
    body_count: int
    opted_in_at: int | None


@dataclass
class MemberUsageRow:
    aci: str
    display_name: str
    consent_state: str
    body_count: int
    opted_in_at: int | None
    llm_calls_24h: int
    llm_calls_7d: int
    cost_usd_7d: float
    rank_7d: int


@dataclass
class PendingMemberRow:
    display_name: str
    consent_state: str


@dataclass
class DashboardStats:
    opted_in: int
    not_opted_in: int
    body_messages: int
    holes: int
    redaction_pct_last_n: float
    messages_24h: int
    messages_7d: int
    summaries_7d: int
    llm_calls_24h: int
    llm_calls_7d: int
    cost_usd_7d: float


class Database:
    def __init__(self, path: str, key: str) -> None:
        if not key:
            raise ValueError("DB_KEY is required for SQLCipher")
        self.path = path
        self.key = key
        self._conn: Any | None = None
        self._lock = threading.RLock()

    def connect(self) -> Any:
        if self._conn is not None:
            return self._conn
        sqlcipher = _sqlcipher_module()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlcipher.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlcipher.Row
        _pragma_key(conn, self.key)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("SELECT count(*) FROM sqlite_master")
        except Exception as exc:
            raise RuntimeError("SQLCipher key is wrong or the database is not a SQLCipher file") from exc
        self._conn = conn
        return conn

    @_serialized
    def init(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)
        self._apply_migrations(conn)
        conn.commit()

    def _apply_migrations(self, conn: Any) -> None:
        conn.execute(
            """
            DELETE FROM messages WHERE id NOT IN (
                SELECT MIN(id) FROM messages
                GROUP BY COALESCE(sender_aci, ''), ts, is_hole
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_body_unique
            ON messages(sender_aci, ts)
            WHERE is_hole = 0 AND sender_aci IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_hole_ts
            ON messages(ts)
            WHERE is_hole = 1
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_threads_ts
            ON threads(ts)
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "last_unopted_group_notice_at" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN last_unopted_group_notice_at INTEGER"
            )
        if "exclude_from_summaries" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN exclude_from_summaries INTEGER NOT NULL DEFAULT 0"
            )
        if "exclude_from_questions" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN exclude_from_questions INTEGER NOT NULL DEFAULT 0"
            )
        summary_cols = {row[1] for row in conn.execute("PRAGMA table_info(summaries)").fetchall()}
        if "kind" not in summary_cols:
            conn.execute(
                "ALTER TABLE summaries ADD COLUMN kind TEXT NOT NULL DEFAULT 'summarize'"
            )
        llm_cols = {row[1] for row in conn.execute("PRAGMA table_info(llm_issuance)").fetchall()}
        if "prompt_tokens" not in llm_cols:
            conn.execute("ALTER TABLE llm_issuance ADD COLUMN prompt_tokens INTEGER")
        if "completion_tokens" not in llm_cols:
            conn.execute("ALTER TABLE llm_issuance ADD COLUMN completion_tokens INTEGER")
        if "cost_usd" not in llm_cols:
            conn.execute("ALTER TABLE llm_issuance ADD COLUMN cost_usd REAL")
        if "latency_ms" not in llm_cols:
            conn.execute("ALTER TABLE llm_issuance ADD COLUMN latency_ms INTEGER")
        if "model" not in llm_cols:
            conn.execute("ALTER TABLE llm_issuance ADD COLUMN model TEXT")
        if "provider" not in llm_cols:
            conn.execute("ALTER TABLE llm_issuance ADD COLUMN provider TEXT")
        if "outcome" not in llm_cols:
            conn.execute("ALTER TABLE llm_issuance ADD COLUMN outcome TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )

    @_serialized
    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @_serialized
    def upsert_user(self, aci: str, display_name: str | None = None) -> User:
        conn = self.connect()
        existing = self.get_user(aci)
        name = (display_name or "").strip()
        if existing is None:
            conn.execute(
                "INSERT INTO users (aci, display_name, consent_state) VALUES (?, ?, 'unknown')",
                (aci, name),
            )
            conn.commit()
            return self.get_user(aci)  # type: ignore[return-value]
        if name and name != existing.display_name:
            conn.execute("UPDATE users SET display_name = ? WHERE aci = ?", (name, aci))
            conn.commit()
        return self.get_user(aci)  # type: ignore[return-value]

    @_serialized
    def get_user(self, aci: str) -> User | None:
        row = self.connect().execute("SELECT * FROM users WHERE aci = ?", (aci,)).fetchone()
        if row is None:
            return None
        return User(
            aci=row["aci"],
            display_name=row["display_name"],
            consent_state=row["consent_state"],
            opted_in_at=row["opted_in_at"],
            last_consent_dm_at=row["last_consent_dm_at"],
            last_unopted_group_notice_at=row["last_unopted_group_notice_at"],
            exclude_from_summaries=bool(row["exclude_from_summaries"]),
            exclude_from_questions=bool(row["exclude_from_questions"]),
        )

    @_serialized
    def set_consent_dm_at(self, aci: str, now: int) -> None:
        self.connect().execute("UPDATE users SET last_consent_dm_at = ? WHERE aci = ?", (now, aci))
        self.connect().commit()

    @_serialized
    def set_unopted_group_notice_at(self, aci: str, now: int) -> None:
        self.connect().execute(
            "UPDATE users SET last_unopted_group_notice_at = ? WHERE aci = ?",
            (now, aci),
        )
        self.connect().commit()

    @_serialized
    def opt_in(self, aci: str, now: int) -> None:
        self.connect().execute(
            "UPDATE users SET consent_state = 'opted_in', opted_in_at = ? WHERE aci = ?",
            (now, aci),
        )
        self.connect().commit()

    @_serialized
    def decline(self, aci: str) -> None:
        self.connect().execute(
            "UPDATE users SET consent_state = 'declined' WHERE aci = ?",
            (aci,),
        )
        self.connect().commit()

    @_serialized
    def opt_out(self, aci: str) -> None:
        conn = self.connect()
        deleted_ids = {
            int(row["id"])
            for row in conn.execute("SELECT id FROM messages WHERE sender_aci = ?", (aci,)).fetchall()
        }
        if deleted_ids:
            for row in conn.execute("SELECT id, window_json FROM summaries").fetchall():
                window = json.loads(row["window_json"])
                if any(message_id in deleted_ids for message_id in window):
                    conn.execute(
                        "UPDATE summaries SET summary_text = ? WHERE id = ?",
                        (REDACTED_SUMMARY, row["id"]),
                    )
        conn.execute("DELETE FROM messages WHERE sender_aci = ?", (aci,))
        conn.execute("DELETE FROM threads WHERE sender_aci = ?", (aci,))
        conn.execute("DELETE FROM magic_tokens WHERE user_aci = ?", (aci,))
        conn.execute("DELETE FROM sessions WHERE user_aci = ?", (aci,))
        conn.execute(
            """
            UPDATE users
            SET consent_state = 'declined',
                opted_in_at = NULL,
                exclude_from_summaries = 0,
                exclude_from_questions = 0
            WHERE aci = ?
            """,
            (aci,),
        )
        conn.commit()

    @_serialized
    def set_privacy_flags(
        self,
        aci: str,
        *,
        exclude_from_summaries: bool,
        exclude_from_questions: bool,
    ) -> None:
        self.connect().execute(
            """
            UPDATE users
            SET exclude_from_summaries = ?, exclude_from_questions = ?
            WHERE aci = ? AND consent_state = 'opted_in'
            """,
            (int(exclude_from_summaries), int(exclude_from_questions), aci),
        )
        self.connect().commit()

    @_serialized
    def exclude_acis(self, kind: str, except_aci: str) -> frozenset[str]:
        if kind == "summarize":
            column = "exclude_from_summaries"
        elif kind == "ask":
            column = "exclude_from_questions"
        else:
            raise ValueError(f"unknown privacy kind: {kind}")
        rows = self.connect().execute(
            f"""
            SELECT aci FROM users
            WHERE consent_state = 'opted_in'
              AND {column} = 1
              AND aci != ?
            """,
            (except_aci,),
        ).fetchall()
        return frozenset(row["aci"] for row in rows)

    @_serialized
    def insert_body(self, sender_aci: str, ts: int, body: str) -> int:
        cur = self.connect().execute(
            "INSERT OR IGNORE INTO messages (sender_aci, ts, body, is_hole) VALUES (?, ?, ?, 0)",
            (sender_aci, ts, body),
        )
        self.connect().commit()
        if cur.rowcount == 0:
            return 0
        return int(cur.lastrowid or 0)

    @_serialized
    def insert_hole(self, ts: int) -> int:
        cur = self.connect().execute(
            "INSERT OR IGNORE INTO messages (sender_aci, ts, body, is_hole) VALUES (NULL, ?, NULL, 1)",
            (ts,),
        )
        self.connect().commit()
        if cur.rowcount == 0:
            return 0
        return int(cur.lastrowid or 0)

    @_serialized
    def last_n_kept(self, n: int) -> list[StoredMessage]:
        rows = self.connect().execute(
            """
            SELECT m.id, m.sender_aci, m.ts, m.body, m.is_hole, u.display_name
            FROM messages m
            LEFT JOIN users u ON u.aci = m.sender_aci
            ORDER BY m.ts DESC, m.id DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
        messages = [_message_from_row(row) for row in rows]
        messages.reverse()
        return messages

    @_serialized
    def get_messages_by_ids(self, ids: list[int]) -> dict[int, StoredMessage]:
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.connect().execute(
            f"""
            SELECT m.id, m.sender_aci, m.ts, m.body, m.is_hole, u.display_name
            FROM messages m
            LEFT JOIN users u ON u.aci = m.sender_aci
            WHERE m.id IN ({placeholders})
            """,
            ids,
        ).fetchall()
        return {row["id"]: _message_from_row(row) for row in rows}

    @_serialized
    def count_bodies(self, aci: str) -> int:
        row = self.connect().execute(
            "SELECT COUNT(*) AS n FROM messages WHERE sender_aci = ? AND is_hole = 0 AND body IS NOT NULL",
            (aci,),
        ).fetchone()
        return int(row["n"])

    @_serialized
    def dashboard_rows(self) -> list[DashboardRow]:
        rows = self.connect().execute(
            """
            SELECT u.display_name, u.consent_state, u.opted_in_at,
                   (SELECT COUNT(*) FROM messages m
                    WHERE m.sender_aci = u.aci AND m.is_hole = 0 AND m.body IS NOT NULL) AS body_count
            FROM users u
            WHERE u.consent_state = 'opted_in'
            ORDER BY u.display_name COLLATE NOCASE
            """
        ).fetchall()
        return [
            DashboardRow(
                display_name=row["display_name"] or "Unknown member",
                consent_state=row["consent_state"],
                body_count=int(row["body_count"]),
                opted_in_at=row["opted_in_at"],
            )
            for row in rows
        ]

    @_serialized
    def save_summary(
        self,
        group_id: str,
        signal_timestamp: int,
        window_ids: list[int],
        summary_text: str,
        *,
        kind: str = "summarize",
    ) -> int:
        cur = self.connect().execute(
            """
            INSERT INTO summaries (group_id, signal_timestamp, window_json, summary_text, kind)
            VALUES (?, ?, ?, ?, ?)
            """,
            (group_id, signal_timestamp, json.dumps(window_ids), summary_text, kind),
        )
        self.connect().commit()
        return int(cur.lastrowid)

    @_serialized
    def get_summary_by_timestamp(self, signal_timestamp: int) -> Summary | None:
        row = self.connect().execute(
            "SELECT * FROM summaries WHERE signal_timestamp = ?",
            (signal_timestamp,),
        ).fetchone()
        return _summary_from_row(row)

    @_serialized
    def get_summary_by_id(self, summary_id: int) -> Summary | None:
        row = self.connect().execute(
            "SELECT * FROM summaries WHERE id = ?",
            (summary_id,),
        ).fetchone()
        return _summary_from_row(row)

    @_serialized
    def get_summary_for_quote(self, quote_timestamp: int) -> Summary | None:
        found = self.get_summary_by_timestamp(quote_timestamp)
        if found is not None:
            return found
        row = self.connect().execute(
            "SELECT summary_id FROM threads WHERE ts = ? LIMIT 1",
            (quote_timestamp,),
        ).fetchone()
        if row is None:
            return None
        return self.get_summary_by_id(int(row["summary_id"]))

    @_serialized
    def add_thread(self, summary_id: int, sender_aci: str | None, body: str, ts: int) -> None:
        self.connect().execute(
            "INSERT INTO threads (summary_id, sender_aci, body, ts) VALUES (?, ?, ?, ?)",
            (summary_id, sender_aci, body, ts),
        )
        self.connect().commit()

    @_serialized
    def get_thread(self, summary_id: int) -> list[ThreadEntry]:
        rows = self.connect().execute(
            """
            SELECT t.id, t.summary_id, t.sender_aci, t.body, t.ts, u.display_name
            FROM threads t
            LEFT JOIN users u ON u.aci = t.sender_aci
            WHERE t.summary_id = ?
            ORDER BY t.ts ASC, t.id ASC
            """,
            (summary_id,),
        ).fetchall()
        return [
            ThreadEntry(
                id=row["id"],
                summary_id=row["summary_id"],
                sender_aci=row["sender_aci"],
                body=row["body"],
                ts=row["ts"],
                display_name=row["display_name"],
            )
            for row in rows
        ]

    @_serialized
    def create_magic_token(self, aci: str, raw_token: str, expires_at: int) -> None:
        self.connect().execute("DELETE FROM magic_tokens WHERE user_aci = ?", (aci,))
        self.connect().execute(
            "INSERT INTO magic_tokens (token_hash, user_aci, expires_at) VALUES (?, ?, ?)",
            (hash_secret(raw_token), aci, expires_at),
        )
        self.connect().commit()

    @_serialized
    def redeem_magic_token(self, raw_token: str, now: int) -> str | None:
        token_hash = hash_secret(raw_token)
        conn = self.connect()
        row = conn.execute(
            "DELETE FROM magic_tokens WHERE token_hash = ? RETURNING user_aci, expires_at",
            (token_hash,),
        ).fetchone()
        conn.commit()
        if row is None:
            return None
        if int(row["expires_at"]) < now:
            return None
        user = self.get_user(row["user_aci"])
        if user is None or not user.opted_in:
            return None
        return row["user_aci"]

    @_serialized
    def create_session(self, aci: str, raw_session: str, expires_at: int) -> None:
        self.connect().execute(
            "INSERT INTO sessions (session_hash, user_aci, expires_at) VALUES (?, ?, ?)",
            (hash_secret(raw_session), aci, expires_at),
        )
        self.connect().commit()

    @_serialized
    def get_session_aci(self, raw_session: str, now: int) -> str | None:
        row = self.connect().execute(
            "SELECT user_aci, expires_at FROM sessions WHERE session_hash = ?",
            (hash_secret(raw_session),),
        ).fetchone()
        if row is None:
            return None
        if int(row["expires_at"]) < now:
            self.connect().execute("DELETE FROM sessions WHERE session_hash = ?", (hash_secret(raw_session),))
            self.connect().commit()
            return None
        user = self.get_user(row["user_aci"])
        if user is None or not user.opted_in:
            return None
        return row["user_aci"]

    @_serialized
    def record_issuance(self, aci: str, now: int) -> None:
        conn = self.connect()
        conn.execute("INSERT INTO link_issuance (user_aci, ts) VALUES (?, ?)", (aci, now))
        cutoff = now - 3600
        conn.execute("DELETE FROM link_issuance WHERE ts < ?", (cutoff,))
        conn.commit()

    @_serialized
    def issuance_count(self, aci: str, now: int) -> int:
        row = self.connect().execute(
            "SELECT COUNT(*) AS n FROM link_issuance WHERE user_aci = ? AND ts >= ?",
            (aci, now - 3600),
        ).fetchone()
        return int(row["n"])

    @_serialized
    def record_llm_call(self, aci: str, now: int) -> int:
        conn = self.connect()
        cur = conn.execute("INSERT INTO llm_issuance (user_aci, ts) VALUES (?, ?)", (aci, now))
        cutoff = now - LLM_ISSUANCE_RETENTION_SECONDS
        conn.execute("DELETE FROM llm_issuance WHERE ts < ?", (cutoff,))
        conn.commit()
        return int(cur.lastrowid or 0)

    @_serialized
    def update_llm_usage(
        self,
        row_id: int,
        *,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cost_usd: float | None,
    ) -> None:
        self.finalize_llm_call(
            row_id,
            latency_ms=None,
            model=None,
            provider=None,
            outcome="ok",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
        )

    @_serialized
    def finalize_llm_call(
        self,
        row_id: int,
        *,
        latency_ms: int | None,
        model: str | None,
        provider: str | None,
        outcome: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        self.connect().execute(
            """
            UPDATE llm_issuance
            SET prompt_tokens = ?, completion_tokens = ?, cost_usd = ?,
                latency_ms = ?, model = ?, provider = ?, outcome = ?
            WHERE id = ?
            """,
            (
                prompt_tokens,
                completion_tokens,
                cost_usd,
                latency_ms,
                model,
                provider,
                outcome,
                row_id,
            ),
        )
        self.connect().commit()

    @_serialized
    def last_llm_provider(self) -> str | None:
        row = self.connect().execute(
            """
            SELECT provider FROM llm_issuance
            WHERE outcome = 'ok' AND provider IS NOT NULL AND provider != ''
            ORDER BY ts DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return str(row["provider"])

    @_serialized
    def llm_latency_stats(self, model: str, now: int, *, days: int = 7) -> tuple[int, int, int]:
        since = now - days * 86400
        rows = self.connect().execute(
            """
            SELECT latency_ms FROM llm_issuance
            WHERE outcome = 'ok' AND model = ? AND latency_ms IS NOT NULL AND ts >= ?
            ORDER BY latency_ms
            """,
            (model, since),
        ).fetchall()
        values = [int(row["latency_ms"]) for row in rows]
        if not values:
            return 0, 0, 0
        count = len(values)
        median = values[count // 2]
        p95_idx = min(int(count * 0.95), count - 1)
        return count, median, values[p95_idx]

    @_serialized
    def is_bot_message_ts(
        self,
        ts: int,
        *,
        bot_aci: str,
        quote_author_aci: str | None = None,
    ) -> bool:
        if quote_author_aci and bot_aci and quote_author_aci.strip().lower() == bot_aci.strip().lower():
            return True
        conn = self.connect()
        if conn.execute(
            "SELECT 1 FROM summaries WHERE signal_timestamp = ? LIMIT 1",
            (ts,),
        ).fetchone():
            return True
        row = conn.execute(
            "SELECT sender_aci FROM threads WHERE ts = ? LIMIT 1",
            (ts,),
        ).fetchone()
        if row is None:
            return False
        return row["sender_aci"] is None

    @_serialized
    def llm_count(self, aci: str, now: int) -> int:
        row = self.connect().execute(
            "SELECT COUNT(*) AS n FROM llm_issuance WHERE user_aci = ? AND ts >= ?",
            (aci, now - 3600),
        ).fetchone()
        return int(row["n"])

    @_serialized
    def get_runtime_config(self) -> dict[str, Any]:
        row = self.connect().execute("SELECT data_json FROM runtime_config WHERE id = 1").fetchone()
        if row is None:
            return {}
        try:
            data = json.loads(row["data_json"])
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @_serialized
    def set_runtime_config(self, data: dict[str, Any]) -> None:
        conn = self.connect()
        conn.execute(
            """
            INSERT INTO runtime_config (id, data_json) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET data_json = excluded.data_json
            """,
            (json.dumps(data),),
        )
        conn.commit()

    @_serialized
    def count_messages_since(self, now: int, seconds: int, *, holes_only: bool | None = None) -> int:
        cutoff = now - seconds
        if holes_only is True:
            clause = "is_hole = 1"
        elif holes_only is False:
            clause = "is_hole = 0 AND body IS NOT NULL"
        else:
            clause = "1=1"
        row = self.connect().execute(
            f"SELECT COUNT(*) AS n FROM messages WHERE ts >= ? AND {clause}",
            (cutoff,),
        ).fetchone()
        return int(row["n"])

    @_serialized
    def count_summaries_since(self, now: int, seconds: int) -> int:
        cutoff_ms = (now - seconds) * 1000
        row = self.connect().execute(
            "SELECT COUNT(*) AS n FROM summaries WHERE signal_timestamp >= ?",
            (cutoff_ms,),
        ).fetchone()
        return int(row["n"])

    @_serialized
    def redaction_pct_last_n(self, n: int) -> float:
        rows = self.last_n_kept(n)
        if not rows:
            return 0.0
        redacted = sum(1 for m in rows if m.is_hole or not m.body)
        return round(100.0 * redacted / len(rows), 1)

    @_serialized
    def consent_counts(self) -> tuple[int, int]:
        row = self.connect().execute(
            """
            SELECT
                SUM(CASE WHEN consent_state = 'opted_in' THEN 1 ELSE 0 END) AS opted_in,
                SUM(CASE WHEN consent_state != 'opted_in' THEN 1 ELSE 0 END) AS not_opted_in
            FROM users
            """
        ).fetchone()
        return int(row["opted_in"] or 0), int(row["not_opted_in"] or 0)

    @_serialized
    def pending_member_rows(self) -> list[PendingMemberRow]:
        rows = self.connect().execute(
            """
            SELECT display_name, consent_state FROM users
            WHERE consent_state != 'opted_in'
            ORDER BY display_name COLLATE NOCASE
            """
        ).fetchall()
        return [
            PendingMemberRow(
                display_name=row["display_name"] or "Unknown member",
                consent_state=row["consent_state"],
            )
            for row in rows
        ]

    @_serialized
    def member_usage_rows(self, now: int, *, sort: str = "name") -> list[MemberUsageRow]:
        since_24h = now - 86400
        since_7d = now - 7 * 86400
        rows = self.connect().execute(
            """
            SELECT u.aci, u.display_name, u.consent_state, u.opted_in_at,
                   (SELECT COUNT(*) FROM messages m
                    WHERE m.sender_aci = u.aci AND m.is_hole = 0 AND m.body IS NOT NULL) AS body_count,
                   (SELECT COUNT(*) FROM llm_issuance l
                    WHERE l.user_aci = u.aci AND l.ts >= ?) AS llm_24h,
                   (SELECT COUNT(*) FROM llm_issuance l
                    WHERE l.user_aci = u.aci AND l.ts >= ?) AS llm_7d,
                   (SELECT COALESCE(SUM(l.cost_usd), 0) FROM llm_issuance l
                    WHERE l.user_aci = u.aci AND l.ts >= ?) AS cost_7d
            FROM users u
            """,
            (since_24h, since_7d, since_7d),
        ).fetchall()
        opted_in_rows = [row for row in rows if row["consent_state"] == "opted_in"]
        ranked = sorted(
            opted_in_rows,
            key=lambda r: (-int(r["llm_7d"]), (r["display_name"] or "").lower()),
        )
        rank_by_aci = {row["aci"]: idx + 1 for idx, row in enumerate(ranked)}
        members = [
            MemberUsageRow(
                aci=row["aci"],
                display_name=row["display_name"] or "Unknown member",
                consent_state=row["consent_state"],
                body_count=int(row["body_count"]),
                opted_in_at=row["opted_in_at"],
                llm_calls_24h=int(row["llm_24h"]),
                llm_calls_7d=int(row["llm_7d"]),
                cost_usd_7d=float(row["cost_7d"] or 0),
                rank_7d=rank_by_aci.get(row["aci"], 0),
            )
            for row in rows
        ]

        def _sort_key(member: MemberUsageRow) -> tuple:
            opted_first = 0 if member.consent_state == "opted_in" else 1
            if sort == "llm7d":
                return (opted_first, -member.llm_calls_7d, member.display_name.lower())
            return (opted_first, member.display_name.lower())

        members.sort(key=_sort_key)
        return members

    @_serialized
    def llm_calls_since(self, now: int, seconds: int) -> int:
        row = self.connect().execute(
            "SELECT COUNT(*) AS n FROM llm_issuance WHERE ts >= ?",
            (now - seconds,),
        ).fetchone()
        return int(row["n"])

    @_serialized
    def llm_cost_since(self, now: int, seconds: int) -> float:
        row = self.connect().execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS n FROM llm_issuance WHERE ts >= ?",
            (now - seconds,),
        ).fetchone()
        return float(row["n"] or 0)

    @_serialized
    def total_message_counts(self) -> tuple[int, int]:
        row = self.connect().execute(
            """
            SELECT
                SUM(CASE WHEN is_hole = 0 AND body IS NOT NULL THEN 1 ELSE 0 END) AS bodies,
                SUM(CASE WHEN is_hole = 1 THEN 1 ELSE 0 END) AS holes
            FROM messages
            """
        ).fetchone()
        return int(row["bodies"] or 0), int(row["holes"] or 0)

    @_serialized
    def dashboard_stats(self, now: int, *, window_n: int = 200) -> DashboardStats:
        opted_in, not_opted_in = self.consent_counts()
        bodies, holes = self.total_message_counts()
        return DashboardStats(
            opted_in=opted_in,
            not_opted_in=not_opted_in,
            body_messages=bodies,
            holes=holes,
            redaction_pct_last_n=self.redaction_pct_last_n(window_n),
            messages_24h=self.count_messages_since(now, 86400),
            messages_7d=self.count_messages_since(now, 7 * 86400),
            summaries_7d=self.count_summaries_since(now, 7 * 86400),
            llm_calls_24h=self.llm_calls_since(now, 86400),
            llm_calls_7d=self.llm_calls_since(now, 7 * 86400),
            cost_usd_7d=self.llm_cost_since(now, 7 * 86400),
        )


def _summary_from_row(row: Any | None) -> Summary | None:
    if row is None:
        return None
    keys = row.keys()
    kind = row["kind"] if "kind" in keys else "summarize"
    return Summary(
        id=row["id"],
        group_id=row["group_id"],
        signal_timestamp=row["signal_timestamp"],
        window_ids=json.loads(row["window_json"]),
        summary_text=row["summary_text"],
        kind=kind or "summarize",
    )


def _message_from_row(row: Any) -> StoredMessage:
    return StoredMessage(
        id=row["id"],
        sender_aci=row["sender_aci"],
        ts=row["ts"],
        body=row["body"],
        is_hole=bool(row["is_hole"]),
        display_name=row["display_name"],
    )
