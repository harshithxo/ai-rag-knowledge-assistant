"""SQLite vector fallback and mocked PostgreSQL/query guard tests.

These tests do not contact PostgreSQL. Live PostgreSQL tests are separately gated below.
"""
import json
import os
import sys
import types
from contextlib import contextmanager

import pytest
from langchain_core.documents import Document

from app import db
from app.config import settings
from app.rag import vector_store


class Embeddings:
    def __init__(self, dimension):
        self.dimension = dimension

    def embed_documents(self, texts):
        return [[1.0] + [0.0] * (self.dimension - 1) for _ in texts]

    def embed_query(self, text):
        return [1.0] + [0.0] * (self.dimension - 1)


def test_sqlite_vector_search_is_scoped_and_document_delete(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(vector_store, "get_embeddings", lambda: Embeddings(settings.vector_dimensions))
    doc_id = "stage1-vector-doc"
    with db.connect() as conn:
        conn.execute("INSERT INTO users(id,full_name,email,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     ("owner-a", "Owner A", "stage1@example.test", "hash", db.now_iso(), db.now_iso()))
        conn.execute("INSERT INTO documents(id,filename,stored_name,size_bytes,content_hash,status,created_at,user_id) VALUES(?,?,?,?,?,?,?,?)",
                     (doc_id, "a.pdf", "a.pdf", 10, "hash-stage1", "ready", db.now_iso(), "owner-a"))
    store = vector_store.PgVectorStore()
    store.add_documents([Document(page_content="private alpha", metadata={"document_id": doc_id, "user_id": "owner-a", "chunk_index": 0})], ["stage1-chunk"])
    assert len(store.search("alpha", 5, "owner-a")) == 1
    assert store.search("alpha", 5, "owner-b") == []
    assert store.delete_document(doc_id, "owner-b") == 0
    assert len(store.search("alpha", 5, "owner-a")) == 1
    assert store.delete_document(doc_id, "owner-a") == 1
    assert store.search("alpha", 5, "owner-a") == []


def test_pg_search_requires_owner_and_query_contains_owner_predicate(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://test")
    monkeypatch.setattr(vector_store, "get_embeddings", lambda: Embeddings(settings.vector_dimensions))
    seen = {}

    class Cursor:
        def fetchall(self):
            return []

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params):
            seen["query"], seen["params"] = query, params
            return Cursor()

    monkeypatch.setattr(db, "connect", lambda: Connection())
    store = vector_store.PgVectorStore()
    with pytest.raises(ValueError):
        store.search("x", 3, None)
    store.search("x", 3, "owner-a", ["doc-a"])
    assert "WHERE user_id=?" in seen["query"]
    assert seen["params"][1] == "owner-a"
    assert seen["params"][2:4] == (["doc-a"], ["doc-a"])


def test_pg_delete_is_scoped_to_owner(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://test")
    seen = {}

    class Cursor:
        rowcount = 2

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params):
            seen["query"], seen["params"] = query, params
            return Cursor()

    monkeypatch.setattr(db, "connect", lambda: Connection())
    assert vector_store.PgVectorStore().delete_document("doc-a", "owner-a") == 2
    assert "WHERE user_id=? AND document_id=?" in seen["query"]
    assert seen["params"] == ("owner-a", "doc-a")


def test_pg_chunk_insert_checks_owner_and_uses_jsonb_vector_types(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://test")
    monkeypatch.setattr(vector_store, "get_embeddings", lambda: Embeddings(settings.vector_dimensions))
    statements = []
    class Cursor:
        def fetchone(self): return {"ok": 1}
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params):
            statements.append((query, params))
            return Cursor()
    monkeypatch.setattr(db, "connect", lambda: Connection())
    doc = Document(page_content="owned", metadata={"document_id": "doc-a", "user_id": "owner-a"})
    vector_store.PgVectorStore().add_documents([doc], ["chunk-a"])
    assert "documents WHERE id=? AND user_id=?" in statements[0][0]
    insert_query, params = statements[1]
    assert "?::jsonb" in insert_query and "?::vector" in insert_query
    assert params[2] == "owner-a" and params[3] == "owned"
    assert len(params[-1].strip("[]").split(",")) == 768


def test_postgres_adapter_types_null_parameters_explicitly():
    seen = {}
    class Raw:
        def execute(self, query, params):
            seen["query"], seen["params"] = query, params
            return "cursor"
    result = db.PostgreSQLConnection(Raw()).execute(
        "SELECT 1 WHERE (? IS NULL OR user_id=?)", (None, None))
    assert result == "cursor"
    assert "CAST(%s AS TEXT) IS NULL" in seen["query"]
    assert seen["params"] == (None, None)


def test_postgres_connection_pool_is_reused_and_returns_connection(monkeypatch):
    made, returned, closed = [], [], []
    class FakeRaw:
        def execute(self, query, params=None):
            return None
    class FakePool:
        def __init__(self, **kwargs):
            made.append(kwargs)
        @contextmanager
        def connection(self):
            try:
                yield FakeRaw()
            finally:
                returned.append(True)
        def close(self): closed.append(True)
    psycopg_pkg = types.ModuleType("psycopg")
    psycopg_pkg.__path__ = []
    rows_mod = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = object()
    pool_mod = types.ModuleType("psycopg_pool")
    pool_mod.ConnectionPool = FakePool
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_pkg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_mod)
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_mod)
    monkeypatch.setattr(settings, "database_url", "postgresql://pool-test")
    monkeypatch.setattr(db, "_pg_pool", None)
    monkeypatch.setattr(db, "_pg_pool_url", None)
    with db.connect() as conn:
        conn.execute("SELECT 1")
    with db.connect():
        pass
    assert len(made) == 1
    assert made[0]["min_size"] == 1 and made[0]["max_size"] == 5
    assert made[0]["kwargs"]["prepare_threshold"] is None
    assert made[0]["kwargs"]["sslmode"] == "verify-full"
    assert made[0]["kwargs"]["options"] == "-c search_path=docmind,extensions"
    assert len(returned) == 2
    db.close()
    assert closed == [True]


def test_postgres_migration_files_pin_vector_dimension_and_explicit_order():
    from scripts.migrate_postgres import migrations_dir
    files = sorted(migrations_dir().glob("*.sql"))
    assert [p.name for p in files] == ["001_initial.sql", "002_message_order.sql"]
    initial = files[0].read_text(encoding="utf-8-sig")
    order = files[1].read_text(encoding="utf-8-sig")
    assert "embedding vector(768) NOT NULL" in initial
    assert "CREATE SCHEMA IF NOT EXISTS docmind" in initial
    assert "SET search_path TO docmind, extensions" in initial
    assert "ADD COLUMN IF NOT EXISTS message_order" in order


def test_postgres_startup_requires_migrations_without_applying_them(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://test")
    seen = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params=None):
            seen.append(query)
            class Result:
                def fetchone(self): return {"version": 1}
            return Result()
    monkeypatch.setattr(db, "connect", lambda: Connection())
    with pytest.raises(RuntimeError, match="Run `python -m scripts.migrate_postgres` explicitly"):
        db.init_db()
    assert seen == ["SELECT MAX(version) AS version FROM docmind.schema_migrations"]


def test_live_pgvector_integration_requires_explicit_database():
    url = os.getenv("DOCMIND_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set DOCMIND_TEST_POSTGRES_URL to run actual PostgreSQL+pgvector integration tests")
    pytest.importorskip("psycopg")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "database_url", url)
    db._initialized.discard(url)
    user_id, doc_id = "pgtest-user-isolation", "pgtest-document-isolation"
    try:
        db.init_db()
        monkeypatch.setattr(vector_store, "get_embeddings", lambda: Embeddings(settings.vector_dimensions))
        with db.connect() as conn:
            conn.execute("INSERT INTO users(id,full_name,email,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                         (user_id, "PG Integration", "pg-vector-test@example.invalid", "test", db.now_iso(), db.now_iso()))
            conn.execute("INSERT INTO documents(id,filename,stored_name,size_bytes,content_hash,status,created_at,user_id) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                         (doc_id, "pg.pdf", "pg.pdf", 1, "pg-vector-test-hash", "ready", db.now_iso(), user_id))
        store = vector_store.PgVectorStore()
        store.add_documents([Document(page_content="integration private content", metadata={"document_id": doc_id, "user_id": user_id, "chunk_index": 0})], ["pgtest-chunk-isolation"])
        assert len(store.search("content", 5, user_id)) == 1
        assert store.search("content", 5, "different-user") == []
        assert store.delete_document(doc_id, "different-user") == 0
        assert len(store.search("content", 5, user_id)) == 1
        assert store.delete_document(doc_id, user_id) == 1
    finally:
        with db.connect() as conn:
            conn.execute("DELETE FROM document_chunks WHERE id=?", ("pgtest-chunk-isolation",))
            conn.execute("DELETE FROM documents WHERE id=? AND user_id=?", (doc_id, user_id))
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        monkeypatch.undo()
