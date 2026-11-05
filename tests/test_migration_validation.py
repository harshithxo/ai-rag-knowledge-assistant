import pytest
import sys
import types

from scripts.migrate_sqlite_chroma import (TABLES, _read_chroma_direct, _source_integrity,
                                           validate_chunk_counts, validate_counts, validate_embedding,
                                           validate_ids, validate_ownership, validate_pdf_bytes)
import hashlib


def test_migration_count_validation_requires_exact_destination():
    validate_counts({"users": 2, "documents": 3}, {"users": 2, "documents": 3})
    with pytest.raises(RuntimeError, match="row-count"):
        validate_counts({"documents": 3}, {"documents": 4})


def test_migration_count_validation_rejects_missing_destination_rows():
    with pytest.raises(RuntimeError, match="documents"):
        validate_counts({"documents": 3}, {"documents": 2})


def test_migration_preserves_null_ownership_and_document_ids():
    validate_ownership({"legacy": None, "private": "user-1"}, {"legacy": None, "private": "user-1"})
    with pytest.raises(RuntimeError, match="Ownership mismatch"):
        validate_ownership({"legacy": None}, {"legacy": "user-1"})


def test_migration_rejects_wrong_embedding_dimension():
    validate_embedding([0.1, 0.2], 2)
    with pytest.raises(RuntimeError, match="dimension mismatch"):
        validate_embedding([0.1], 2)


def test_migration_checks_pdf_size_and_checksum():
    content = b"%PDF- test"
    validate_pdf_bytes(content, len(content), hashlib.sha256(content).hexdigest())
    with pytest.raises(RuntimeError, match="SHA-256"):
        validate_pdf_bytes(content, len(content), "wrong")


def test_migration_checks_per_document_chunk_counts():
    validate_chunk_counts({"doc": 2}, {"doc": 2})
    with pytest.raises(RuntimeError, match="chunk count mismatch"):
        validate_chunk_counts({"doc": 2}, {"doc": 1})


def test_migration_checks_source_ids():
    with pytest.raises(RuntimeError, match="extra"):
        validate_ids("users", {("u1",)}, {("u1",), ("u2",)})
    with pytest.raises(RuntimeError, match="missing"):
        validate_ids("users", {("u1",)}, set())
    with pytest.raises(RuntimeError, match="extra"):
        validate_ids("users", {("u1",)}, {("u1",), ("u2",)})


def test_old_sqlite_messages_receive_stable_order_and_sequence():
    rows = {table: [] for table in TABLES}
    rows["users"] = [{"id": "u1"}]
    rows["documents"] = [{"id": "d1", "user_id": "u1", "chunks": 1, "status": "ready"}]
    rows["conversations"] = [{"id": "c1", "user_id": "u1", "document_id": "d1"}]
    rows["messages"] = [
        {"id": "m1", "conversation_id": "c1", "created_at": "2024-01-01", "_source_rowid": 7},
        {"id": "m2", "conversation_id": "c1", "created_at": "2024-01-01", "_source_rowid": 8},
    ]
    _source_integrity(rows)
    assert [r["message_order"] for r in rows["messages"]] == [1, 2]
    assert rows["conversations"][0]["message_seq"] == 2


def test_source_preflight_rejects_cross_user_conversation_document():
    rows = {table: [] for table in TABLES}
    rows["users"] = [{"id": "u1"}, {"id": "u2"}]
    rows["documents"] = [{"id": "d1", "user_id": "u1"}]
    rows["conversations"] = [{"id": "c1", "user_id": "u2", "document_id": "d1"}]
    with pytest.raises(RuntimeError, match="different owners"):
        _source_integrity(rows)


def test_local_migration_storage_never_overwrites_an_existing_pdf(tmp_path, monkeypatch):
    from app.config import settings
    from app.storage import LocalPDFStorage
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    storage = LocalPDFStorage()
    assert storage.put_if_absent("u1/a.pdf", b"first") is True
    assert storage.put_if_absent("u1/a.pdf", b"second") is False
    assert storage.get("u1/a.pdf") == b"first"


def test_s3_migration_write_uses_conditional_put(monkeypatch):
    from botocore.exceptions import ClientError
    from app.config import settings
    from app.storage import S3PDFStorage
    calls = []
    class Client:
        def put_object(self, **kwargs):
            calls.append(kwargs)
            raise ClientError({"Error": {"Code": "PreconditionFailed"},
                               "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
    store = S3PDFStorage.__new__(S3PDFStorage)
    store.client = Client()
    monkeypatch.setattr(settings, "s3_bucket", "bucket-test")
    assert store.put_if_absent("u1/a.pdf", b"bytes") is False
    assert calls[0]["IfNoneMatch"] == "*"


def test_chroma_preflight_reads_every_collection_and_preserves_collection_name(monkeypatch, tmp_path):
    class Collection:
        def __init__(self, record): self.record = record
        def count(self): return len(self.record)
        def get(self, include, limit, offset):
            batch = self.record[offset:offset + limit]
            return {"ids": [r["id"] for r in batch], "documents": [r["content"] for r in batch],
                    "metadatas": [r["metadata"] for r in batch], "embeddings": [r["embedding"] for r in batch]}
    records = {
        "collection-a": [{"id": "a", "content": "A", "metadata": {"document_id": "d", "user_id": "u"}, "embedding": [1.0] * 768}],
        "collection-b": [{"id": "b", "content": "B", "metadata": {"document_id": "d", "user_id": "u"}, "embedding": [0.5] * 768}],
    }
    class Client:
        def list_collections(self): return [types.SimpleNamespace(name=name) for name in records]
        def get_collection(self, name): return Collection(records[name])
    module = types.ModuleType("chromadb")
    module.PersistentClient = lambda path: Client()
    monkeypatch.setitem(sys.modules, "chromadb", module)
    docs = {"d": {"id": "d", "user_id": "u", "chunks": 2, "status": "ready"}}
    chunks, counts = _read_chroma_direct(tmp_path, docs, 768)
    assert {c["id"] for c in chunks} == {"a", "b"}
    assert {c["metadata"]["_legacy_chroma_collection"] for c in chunks} == set(records)
    assert counts == {"d": 2}
