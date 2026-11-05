"""Database access for local SQLite and pooled PostgreSQL deployments."""
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,            -- processing | ready | failed
    error TEXT,
    pages INTEGER DEFAULT 0,
    chunks INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    document_id TEXT,                -- optional: chat limited to one document
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    message_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    sources TEXT,                    -- JSON list
    follow_ups TEXT,                 -- JSON list
    grounded INTEGER,
    attachments TEXT,                -- JSON list of {id, filename} attached to a user message
    created_at TEXT NOT NULL,
    message_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS response_metrics (   -- one row per answered question
    message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    retrieval_ms REAL,
    generation_ms REAL,
    total_ms REAL,
    llm_calls INTEGER,
    passages INTEGER,
    tokens INTEGER,
    tokens_per_second REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, full_name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL, is_verified INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1, role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_token_hash TEXT NOT NULL UNIQUE, csrf_token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS auth_rate_limits (
    scope TEXT NOT NULL, subject_hash TEXT NOT NULL, window_started TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope, subject_hash)
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


_initialized: set[str] = set()
_pg_pool = None
_pg_pool_url: str | None = None
_pg_pool_lock = threading.Lock()
PG_SCHEMA_VERSION = 2
PG_APP_SCHEMA = "docmind"
PG_SEARCH_PATH = "docmind,extensions"


def _prepare(conn: sqlite3.Connection) -> None:
    """Create any missing tables and add columns that older databases don't have yet.
    Only ever adds things, so existing data is never touched."""
    conn.executescript(SCHEMA)
    # Add ownership columns without deleting or silently assigning legacy single-user data.
    for table in ("documents", "conversations"):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "user_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at)")
    convo_columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)")}
    if "message_seq" not in convo_columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN message_seq INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    conn.execute("""CREATE TABLE IF NOT EXISTS document_chunks (
        id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        user_id TEXT, content TEXT NOT NULL, metadata TEXT NOT NULL, embedding TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_owner_document ON document_chunks(user_id, document_id)")
    message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "attachments" not in message_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")
    if "message_order" not in message_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN message_order INTEGER NOT NULL DEFAULT 0")
        # SQLite rowid is used only once to preserve chronology while upgrading this local DB.
        rows = conn.execute("SELECT rowid, id, conversation_id FROM messages ORDER BY conversation_id, created_at, rowid").fetchall()
        counters: dict[str, int] = {}
        for row in rows:
            order = counters.get(row["conversation_id"], 0) + 1
            counters[row["conversation_id"]] = order
            conn.execute("UPDATE messages SET message_order=? WHERE id=?", (order, row["id"]))
        for cid, order in counters.items():
            conn.execute("UPDATE conversations SET message_seq=? WHERE id=?", (order, cid))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv_order ON messages(conversation_id, message_order)")


def _postgres_pool():
    """Return one bounded psycopg pool per configured URL."""
    global _pg_pool, _pg_pool_url
    url = settings.database_url
    if not url:
        raise RuntimeError("DATABASE_URL is empty")
    with _pg_pool_lock:
        if _pg_pool is None or _pg_pool_url != url:
            if _pg_pool is not None:
                _pg_pool.close()
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
            pool_max = int(settings.database_pool_max)
            pool_timeout = float(settings.database_pool_timeout)
            if not 1 <= pool_max <= 20 or pool_timeout <= 0:
                raise RuntimeError("DATABASE_POOL_MAX must be 1..20 and DATABASE_POOL_TIMEOUT must be positive")
            kwargs = {"row_factory": dict_row, "prepare_threshold": None,
                      "options": f"-c search_path={PG_SEARCH_PATH}"}
            sslmode = settings.database_sslmode
            if sslmode:
                kwargs["sslmode"] = sslmode
            rootcert = settings.database_sslrootcert
            if rootcert:
                kwargs["sslrootcert"] = rootcert
            _pg_pool = ConnectionPool(
                conninfo=url, min_size=1, max_size=pool_max,
                timeout=pool_timeout, kwargs=kwargs, open=True,
            )
            _pg_pool_url = url
    return _pg_pool


def close() -> None:
    """Close the process-wide PostgreSQL pool during application shutdown."""
    global _pg_pool, _pg_pool_url
    with _pg_pool_lock:
        if _pg_pool is not None:
            _pg_pool.close()
            _pg_pool = None
            _pg_pool_url = None


class PostgreSQLConnection:
    """Small DB-API adapter for the app's SQLite-style qmark SQL."""
    def __init__(self, raw):
        self.raw = raw

    def execute(self, query, params=None):
        if isinstance(params, dict):
            keys = re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", query)
            query = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", "%s", query)
            params = tuple(params[k] for k in keys)
        else:
            query = query.replace("?", "%s")
        query = re.sub(r"%s\s+IS\s+NULL", r"CAST(%s AS TEXT) IS NULL", query, flags=re.I)
        return self.raw.execute(query, params)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    if settings.database_url:
        pool = _postgres_pool()
        with pool.connection() as raw:
            yield PostgreSQLConnection(raw)
        return
    if settings.app_env.lower() == "production":
        raise RuntimeError("DATABASE_URL must point to PostgreSQL with pgvector in production")
    conn = sqlite3.connect(settings.database_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        if settings.database_path not in _initialized:   # set up the tables the first time this file is used
            _prepare(conn)
            _initialized.add(settings.database_path)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        if settings.database_url:
            try:
                row = conn.execute("SELECT MAX(version) AS version FROM docmind.schema_migrations").fetchone()
            except Exception as exc:
                raise RuntimeError("PostgreSQL schema is not initialized. Run `python -m scripts.migrate_postgres` explicitly.") from exc
            version = row["version"] if row else None
            if version != PG_SCHEMA_VERSION:
                raise RuntimeError(f"PostgreSQL DocMind schema version {version!r}; expected {PG_SCHEMA_VERSION}. Run `python -m scripts.migrate_postgres` explicitly.")
            active_schema = conn.execute("SELECT current_schema() AS schema").fetchone()
            if not active_schema or active_schema["schema"] != PG_APP_SCHEMA:
                raise RuntimeError("PostgreSQL connection search_path must resolve DocMind to the isolated docmind schema")
            required_tables = conn.execute("""SELECT name, to_regclass('docmind.' || name)::text AS relation
                FROM (VALUES ('users'),('documents'),('conversations'),('messages'),('document_chunks'),
                             ('sessions'),('email_verification_tokens'),('password_reset_tokens'),
                             ('auth_rate_limits'),('app_settings'),('response_metrics')) AS required(name)""").fetchall()
            missing_tables = [row["name"] for row in required_tables if not row["relation"]]
            if missing_tables:
                raise RuntimeError("Missing DocMind PostgreSQL tables: " + ", ".join(missing_tables))
            vector_extension = conn.execute("""SELECT n.nspname AS schema FROM pg_extension e
                JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'""").fetchone()
            if not vector_extension or vector_extension["schema"] != "extensions":
                raise RuntimeError("pgvector must be installed in the extensions schema")
            vector_col = conn.execute("SELECT format_type(a.atttypid,a.atttypmod) AS type FROM pg_attribute a WHERE a.attrelid='docmind.document_chunks'::regclass AND a.attname='embedding' AND NOT a.attisdropped").fetchone()
            if not vector_col or vector_col["type"] != "vector(768)":
                raise RuntimeError("PostgreSQL docmind.document_chunks.embedding must be vector(768)")
        else:
            _prepare(conn)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
