"""Preflight and copy SQLite/Chroma/PDF data into an already migrated PostgreSQL DB.

Source files are read-only. Relational writes use one PostgreSQL transaction. PDF objects
are created conditionally (never overwritten); if SQL later rolls back, newly created PDFs
can remain as harmless orphans and are reported for operator review.
"""
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TABLES = ["users", "email_verification_tokens", "password_reset_tokens", "sessions",
          "auth_rate_limits", "documents", "conversations", "messages",
          "response_metrics", "app_settings"]
KEY_FIELDS = {"users": ("id",), "email_verification_tokens": ("id",),
              "password_reset_tokens": ("id",), "sessions": ("id",),
              "auth_rate_limits": ("scope", "subject_hash"), "documents": ("id",),
              "conversations": ("id",), "messages": ("id",),
              "response_metrics": ("message_id",), "app_settings": ("key",)}


def validate_counts(source_counts, destination_counts):
    errors = [f"{table}: source={count}, destination={destination_counts.get(table, 0)}"
              for table, count in source_counts.items()
              if destination_counts.get(table, 0) != count]
    if errors:
        raise RuntimeError("Destination row-count validation failed: " + "; ".join(errors))


def validate_ownership(source_documents, copied_documents):
    for document_id, source_owner in source_documents.items():
        if document_id not in copied_documents:
            raise RuntimeError(f"Destination is missing document {document_id}")
        if copied_documents[document_id] != source_owner:
            raise RuntimeError(f"Ownership mismatch for document {document_id}")


def validate_ids(table, source_ids, destination_ids):
    missing = source_ids - destination_ids
    extra = destination_ids - source_ids
    if missing or extra:
        raise RuntimeError(f"Destination {table} key mismatch: missing={len(missing)}, extra={len(extra)}")


def validate_chunk_counts(expected_by_document, actual_by_document):
    mismatches = {doc_id: (count, actual_by_document.get(doc_id, 0))
                  for doc_id, count in expected_by_document.items()
                  if count != actual_by_document.get(doc_id, 0)}
    extras = {doc_id: count for doc_id, count in actual_by_document.items()
              if doc_id not in expected_by_document and count}
    if mismatches or extras:
        raise RuntimeError(f"Document chunk count mismatch (expected, found): {mismatches}; unexpected docs: {extras}")


def validate_embedding(vector, dimensions):
    if vector is None or len(vector) != dimensions:
        actual = None if vector is None else len(vector)
        raise RuntimeError(f"Embedding dimension mismatch: found {actual}, expected {dimensions}")


def validate_pdf_bytes(content, expected_size, expected_hash):
    if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_hash:
        raise RuntimeError("Uploaded PDF size or SHA-256 does not match its SQLite document record")


def _backup(paths, backup_root):
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    target = backup_root / stamp
    target.mkdir(parents=True, exist_ok=False)
    for label, source in paths.items():
        if not source.exists():
            continue
        if source.is_dir():
            with zipfile.ZipFile(target / f"{label}.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                for file in source.rglob("*"):
                    if file.is_file():
                        archive.write(file, file.relative_to(source.parent))
        else:
            if label == "sqlite":
                src = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
                dst = sqlite3.connect(target / source.name)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
            else:
                shutil.copy2(source, target / source.name)
    if not any(target.iterdir()):
        raise RuntimeError("No source data found to back up")
    return target


def _read_source(sqlite_path):
    source = sqlite3.connect(f"file:{sqlite_path.resolve()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        source.execute("BEGIN")  # consistent read snapshot across all source tables
        existing = {r["name"] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows_by_table = {}
        for table in TABLES:
            if table not in existing:
                rows_by_table[table] = []
                continue
            if table == "messages":
                try:
                    rows = source.execute("SELECT rowid AS _source_rowid, * FROM messages ORDER BY conversation_id, created_at, rowid").fetchall()
                except sqlite3.OperationalError:  # WITHOUT ROWID legacy table
                    rows = source.execute("SELECT * FROM messages ORDER BY conversation_id, created_at, id").fetchall()
            else:
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
            rows_by_table[table] = [dict(row) for row in rows]
        return rows_by_table
    finally:
        source.close()


def _source_integrity(rows_by_table):
    users = {r["id"] for r in rows_by_table["users"]}
    documents = {r["id"]: r for r in rows_by_table["documents"]}
    conversations = {r["id"]: r for r in rows_by_table["conversations"]}
    messages = {r["id"]: r for r in rows_by_table["messages"]}

    for table in ("email_verification_tokens", "password_reset_tokens", "sessions"):
        for row in rows_by_table[table]:
            if row["user_id"] not in users:
                raise RuntimeError(f"Source {table} row {row['id']} references missing user {row['user_id']}")
    for row in rows_by_table["documents"]:
        if row.get("user_id") is not None and row["user_id"] not in users:
            raise RuntimeError(f"Document {row['id']} references missing owner {row['user_id']}")
    for row in rows_by_table["conversations"]:
        owner = row.get("user_id")
        if owner is not None and owner not in users:
            raise RuntimeError(f"Conversation {row['id']} references missing owner {owner}")
        doc_id = row.get("document_id")
        if doc_id and doc_id not in documents:
            raise RuntimeError(f"Conversation {row['id']} references missing document {doc_id}")
        if doc_id and owner is not None and documents[doc_id].get("user_id") != owner:
            raise RuntimeError(f"Conversation {row['id']} and document {doc_id} have different owners")
    for row in rows_by_table["messages"]:
        if row["conversation_id"] not in conversations:
            raise RuntimeError(f"Message {row['id']} references missing conversation {row['conversation_id']}")
    for row in rows_by_table["response_metrics"]:
        if row["message_id"] not in messages:
            raise RuntimeError(f"Response metrics reference missing message {row['message_id']}")

    grouped = {}
    for row in rows_by_table["messages"]:
        grouped.setdefault(row["conversation_id"], []).append(row)
    next_seq = {}
    for cid, messages_for_conversation in grouped.items():
        has_order = all("message_order" in row for row in messages_for_conversation)
        if has_order:
            values = [int(row["message_order"]) for row in messages_for_conversation]
            if any(value <= 0 for value in values) or len(values) != len(set(values)):
                raise RuntimeError(f"Conversation {cid} has invalid or duplicate message_order values")
            ordered = sorted(messages_for_conversation, key=lambda row: int(row["message_order"]))
        else:
            # rowid was selected in chronological order for ordinary legacy SQLite tables.
            ordered = sorted(messages_for_conversation,
                             key=lambda row: (row.get("created_at") or "", row.get("_source_rowid", 0), row["id"]))
        for number, row in enumerate(ordered, 1):
            row["message_order"] = int(row.get("message_order") or number)
        next_seq[cid] = max(int(messages_for_conversation[0].get("message_seq") or 0),
                            max(int(row["message_order"]) for row in ordered))
    for row in rows_by_table["conversations"]:
        row["message_seq"] = next_seq.get(row["id"], int(row.get("message_seq") or 0))
        row.setdefault("user_id", None)
    for row in rows_by_table["documents"]:
        row.setdefault("user_id", None)
    for row in rows_by_table["messages"]:
        row.pop("_source_rowid", None)
        row.setdefault("attachments", None)

    # Restore migration-preventing checks for all primary/composite keys before any write.
    for table in TABLES:
        fields = KEY_FIELDS[table]
        keys = [tuple(row.get(field) for field in fields) for row in rows_by_table[table]]
        if len(keys) != len(set(keys)):
            raise RuntimeError(f"Source table {table} contains duplicate migration keys")
    return documents


def _read_chroma(chroma_path, documents, dimensions):
    if not chroma_path.exists():
        return [], {}
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError("Install the legacy migration extra (chromadb) to inspect all Chroma collections") from exc

    records, per_document = [], {}
    seen_ids = set()
    with tempfile.TemporaryDirectory(prefix="docmind_chroma_migration_") as temp:
        # Chroma is opened only from a copy of the source backup.
        with zipfile.ZipFile(chroma_path) as archive:
            members = [PurePosixPath(name) for name in archive.namelist()]
            if not members:
                return [], {}
            if any(member.is_absolute() or any(part in {".", ".."} for part in member.parts)
                   for member in members):
                raise RuntimeError("Chroma source backup contains an unsafe archive path")
            roots = {member.parts[0] for member in members if member.parts}
            if len(roots) != 1:
                raise RuntimeError("Chroma source backup has an unexpected directory layout")
            archive.extractall(temp)
        client = chromadb.PersistentClient(path=str(Path(temp) / roots.pop()))
        for collection_info in client.list_collections():
            name = getattr(collection_info, "name", collection_info)
            collection = client.get_collection(name=name)
            total = collection.count()
            offset = count_read = 0
            while offset < total:
                batch = collection.get(include=["documents", "metadatas", "embeddings"], limit=500, offset=offset)
                size = len(batch.get("ids") or [])
                if not size:
                    break
                count_read += size
                for i, chunk_id in enumerate(batch["ids"]):
                    if chunk_id in seen_ids:
                        raise RuntimeError(f"Chroma chunk ID {chunk_id} appears in more than one collection")
                    seen_ids.add(chunk_id)
                    meta = batch["metadatas"][i] or {}
                    doc_id = meta.get("document_id")
                    if not doc_id or doc_id not in documents:
                        raise RuntimeError(f"Chroma collection {name!r} chunk {chunk_id} has no known document_id")
                    owner = documents[doc_id].get("user_id")
                    chroma_owner = meta.get("user_id")
                    if chroma_owner not in (None, "legacy", owner):
                        raise RuntimeError(f"Ownership mismatch for Chroma chunk {chunk_id}")
                    embedding = batch["embeddings"][i]
                    validate_embedding(embedding, dimensions)
                    content = batch["documents"][i]
                    if content is None:
                        raise RuntimeError(f"Chroma chunk {chunk_id} has no document content")
                    meta = dict(meta)
                    if "_legacy_chroma_collection" in meta and meta["_legacy_chroma_collection"] != name:
                        raise RuntimeError(f"Chroma chunk {chunk_id} uses reserved migration metadata")
                    meta["_legacy_chroma_collection"] = name
                    records.append({"id": chunk_id, "document_id": doc_id, "user_id": owner,
                                    "content": content, "metadata": meta,
                                    "embedding": [float(value) for value in embedding],
                                    "collection": name})
                    per_document[doc_id] = per_document.get(doc_id, 0) + 1
                offset += size
            if count_read != total:
                raise RuntimeError(f"Chroma collection {name!r} changed or could not be fully read: expected {total}, read {count_read}")
    expected = {row["id"]: int(row.get("chunks") or 0) for row in documents.values()
                if row.get("status") == "ready"}
    validate_chunk_counts(expected, per_document)
    return records, per_document


def _equal_value(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return left == right
    if isinstance(left, float) and isinstance(right, (float, int)):
        return abs(left - float(right)) <= 1e-9
    return left == right


def _key(row, fields):
    return tuple(row.get(field) for field in fields)


def _destination_preflight(conn, rows_by_table, chunks):
    """Reject unrelated destination rows and mismatched conflicts before inserting anything."""
    for table in TABLES:
        fields = KEY_FIELDS[table]
        source = {_key(row, fields): row for row in rows_by_table[table]}
        existing_rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
        existing = {_key(row, fields): row for row in existing_rows}
        extra = set(existing) - set(source)
        if extra:
            raise RuntimeError(f"Destination {table} contains {len(extra)} unrelated row(s); use a clean target")
        for key, dest in existing.items():
            src = source[key]
            for column, value in src.items():
                if column in dest and not _equal_value(dest[column], value):
                    raise RuntimeError(f"Conflicting destination row in {table} for key {key}; no rows were written")

    existing_chunks = {row["id"]: dict(row) for row in conn.execute(
        "SELECT id,document_id,user_id,content,metadata,embedding FROM document_chunks").fetchall()}
    expected_chunks = {item["id"]: item for item in chunks}
    extra_chunks = set(existing_chunks) - set(expected_chunks)
    if extra_chunks:
        raise RuntimeError(f"Destination document_chunks contains {len(extra_chunks)} unrelated row(s); use a clean target")
    for chunk_id, dest in existing_chunks.items():
        src = expected_chunks[chunk_id]
        for column in ("document_id", "user_id", "content", "metadata"):
            value = dest[column]
            if column == "metadata" and isinstance(value, str):
                value = json.loads(value)
            if not _equal_value(value, src[column]):
                raise RuntimeError(f"Conflicting destination chunk {chunk_id}; no rows were written")
        vector = dest["embedding"]
        vector = list(vector) if not isinstance(vector, str) else json.loads(vector)
        if len(vector) != len(src["embedding"]) or any(abs(float(a)-b) > 1e-6 for a, b in zip(vector, src["embedding"])):
            raise RuntimeError(f"Conflicting destination embedding for chunk {chunk_id}; no rows were written")
    return set(existing_chunks)


def _insert_rows(conn, rows_by_table):
    for table in TABLES:
        for row in rows_by_table[table]:
            columns = list(row)
            qmarks = ",".join("?" for _ in columns)
            conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({qmarks}) ON CONFLICT DO NOTHING",
                         tuple(row[column] for column in columns))


def _insert_chunks(conn, chunks):
    for chunk in chunks:
        vector = "[" + ",".join(str(float(value)) for value in chunk["embedding"]) + "]"
        conn.execute("""INSERT INTO document_chunks(id,document_id,user_id,content,metadata,embedding)
            VALUES(?,?,?,?,?::jsonb,?::vector) ON CONFLICT(id) DO NOTHING""",
            (chunk["id"], chunk["document_id"], chunk["user_id"], chunk["content"],
             json.dumps(chunk["metadata"], ensure_ascii=False), vector))


def _validate_destination(conn, rows_by_table, chunks):
    dest_counts = {}
    for table in TABLES:
        dest_rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
        dest_counts[table] = len(dest_rows)
        fields = KEY_FIELDS[table]
        source = {_key(row, fields): row for row in rows_by_table[table]}
        dest = {_key(row, fields): row for row in dest_rows}
        validate_ids(table, set(source), set(dest))
        for key, src in source.items():
            actual = dest[key]
            for column, value in src.items():
                if column not in actual or not _equal_value(actual[column], value):
                    raise RuntimeError(f"Post-migration data mismatch in {table} key {key}, column {column}")
    chunk_rows = [dict(row) for row in conn.execute(
        "SELECT id,document_id,user_id,content,metadata,embedding FROM document_chunks").fetchall()]
    validate_counts({"document_chunks": len(chunks)}, {"document_chunks": len(chunk_rows)})
    dest_chunks = {row["id"]: row for row in chunk_rows}
    validate_ids("document_chunks", {chunk["id"] for chunk in chunks}, set(dest_chunks))
    for chunk in chunks:
        actual = dest_chunks[chunk["id"]]
        metadata = actual["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if any(not _equal_value(actual[field], chunk[field]) for field in ("document_id", "user_id", "content")) or metadata != chunk["metadata"]:
            raise RuntimeError(f"Post-migration chunk metadata/content mismatch for {chunk['id']}")
        vector = list(actual["embedding"])
        if len(vector) != len(chunk["embedding"]) or any(abs(float(a)-b) > 1e-6 for a, b in zip(vector, chunk["embedding"])):
            raise RuntimeError(f"Post-migration embedding mismatch for {chunk['id']}")
    return dest_counts


def _pdf_plan(rows_by_table, uploads, storage):
    plans = []
    for row in rows_by_table["documents"]:
        owner = row.get("user_id")
        candidates = [uploads / (owner or "legacy") / row["stored_name"], uploads / row["stored_name"]]
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            raise RuntimeError(f"PDF missing for document {row['id']}; no destination writes were made")
        content = source.read_bytes()
        validate_pdf_bytes(content, row["size_bytes"], row["content_hash"])
        from app.storage import document_key
        key = document_key(owner, row["stored_name"])
        exists = storage.exists(key)
        if exists:
            validate_pdf_bytes(storage.get(key), row["size_bytes"], row["content_hash"])
        plans.append({"document_id": row["id"], "key": key, "source": source,
                      "size": row["size_bytes"], "sha256": row["content_hash"], "exists": exists})
    return plans


def _write_pdfs(storage, pdf_plans, created):
    for plan in pdf_plans:
        content = plan["source"].read_bytes()
        validate_pdf_bytes(content, plan["size"], plan["sha256"])
        if plan["exists"]:
            validate_pdf_bytes(storage.get(plan["key"]), plan["size"], plan["sha256"])
        else:
            did_create = storage.put_if_absent(plan["key"], content)
            if did_create:
                created.append(plan["key"])
            # A racing writer may have created it; verify bytes before considering it safe.
            validate_pdf_bytes(storage.get(plan["key"]), plan["size"], plan["sha256"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, default=ROOT / "data/app.db")
    parser.add_argument("--chroma", type=Path, default=ROOT / "data/chroma")
    parser.add_argument("--uploads", type=Path, default=ROOT / "data/uploads")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "data/migration-backups")
    parser.add_argument("--dry-run", action="store_true", help="validate source/destination without writing rows or PDFs")
    parser.add_argument("--source-quiesced", action="store_true",
                        help="confirm the DocMind app is stopped while taking SQLite/Chroma/PDF snapshots")
    args = parser.parse_args()
    from app.config import settings
    from app import db
    from app.storage import get_pdf_storage

    if not settings.database_url:
        raise SystemExit("Set DATABASE_URL to the destination PostgreSQL database")
    if int(settings.vector_dimensions) != 768:
        raise SystemExit("SQLite/Chroma migration requires VECTOR_DIMENSIONS=768")
    if not args.dry_run and not args.source_quiesced:
        raise SystemExit("Stop DocMind and pass --source-quiesced before taking the migration snapshot")
    if not args.sqlite.is_file():
        raise SystemExit(f"SQLite source not found: {args.sqlite}")

    db.init_db()  # verifies the operator-applied PostgreSQL schema; it does not migrate it
    backup = None if args.dry_run else _backup(
        {"sqlite": args.sqlite, "chroma": args.chroma, "uploads": args.uploads}, args.backup_dir)
    if backup:
        print(f"Read-only source backup created: {backup}")

    source_sqlite = backup / args.sqlite.name if backup else args.sqlite
    source_uploads = args.uploads
    if backup and (backup / "uploads.zip").is_file():
        snapshot_dir = backup / "uploads_snapshot"
        snapshot_dir.mkdir(exist_ok=False)
        with zipfile.ZipFile(backup / "uploads.zip") as archive:
            archive.extractall(snapshot_dir)
        source_uploads = snapshot_dir / args.uploads.name

    rows_by_table = _read_source(source_sqlite)
    documents = _source_integrity(rows_by_table)
    source_counts = {table: len(rows) for table, rows in rows_by_table.items()}

    if args.chroma.exists():
        if not backup:
            # In dry-run, inspect source Chroma read-only. No backup is created or source modified.
            records, per_document = _read_chroma_direct(args.chroma, documents, settings.vector_dimensions)
        else:
            records, per_document = _read_chroma(backup / "chroma.zip", documents, settings.vector_dimensions)
    else:
        records, per_document = [], {}
    expected_ready = {row["id"]: int(row.get("chunks") or 0)
                      for row in documents.values() if row.get("status") == "ready"}
    validate_chunk_counts(expected_ready, per_document)
    source_counts["document_chunks"] = len(records)
    target_storage = get_pdf_storage()
    pdf_plans = _pdf_plan(rows_by_table, source_uploads, target_storage)

    # One transaction locks target rows, checks every conflict, inserts all rows and
    # validates the exact result. No PostgreSQL row is committed on any failure.
    created_objects = []
    try:
        with db.connect() as conn:
            lock_tables = ", ".join(TABLES + ["document_chunks"])
            conn.execute(f"LOCK TABLE {lock_tables} IN SHARE ROW EXCLUSIVE MODE")
            _destination_preflight(conn, rows_by_table, records)
            if args.dry_run:
                print("Dry run passed: source IDs, ownership, message ordering, all Chroma collections, PDFs and destination conflicts validated.")
                return
            _insert_rows(conn, rows_by_table)
            _insert_chunks(conn, records)
            dest_counts = _validate_destination(conn, rows_by_table, records)
            validate_counts(source_counts, {**dest_counts, "document_chunks": len(records)})
            _write_pdfs(target_storage, pdf_plans, created_objects)
    except Exception:
        if created_objects:
            print(f"PostgreSQL rolled back. {len(created_objects)} newly created PDF object(s) may remain unreferenced; they were not overwritten or deleted.")
        raise

    print("Migration committed and validated: IDs, row counts, timestamps, ownership, messages, chunks, embeddings and PDF hashes.")
    if created_objects:
        print(f"Created {len(created_objects)} previously absent PDF object(s).")


def _read_chroma_direct(chroma_path, documents, dimensions):
    """Read source collections directly for dry-run; only fetch operations are used."""
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError("Install the legacy migration extra (chromadb) to inspect all Chroma collections") from exc
    records, per_document, seen_ids = [], {}, set()
    client = chromadb.PersistentClient(path=str(chroma_path))
    for info in client.list_collections():
        name = getattr(info, "name", info)
        collection = client.get_collection(name=name)
        offset, count_read, total = 0, 0, collection.count()
        while offset < total:
            batch = collection.get(include=["documents", "metadatas", "embeddings"], limit=500, offset=offset)
            size = len(batch.get("ids") or [])
            if not size:
                break
            count_read += size
            for i, chunk_id in enumerate(batch["ids"]):
                if chunk_id in seen_ids:
                    raise RuntimeError(f"Chroma chunk ID {chunk_id} appears in more than one collection")
                seen_ids.add(chunk_id)
                meta = batch["metadatas"][i] or {}
                doc_id = meta.get("document_id")
                if not doc_id or doc_id not in documents:
                    raise RuntimeError(f"Chroma collection {name!r} chunk {chunk_id} has no known document_id")
                owner = documents[doc_id].get("user_id")
                if meta.get("user_id") not in (None, "legacy", owner):
                    raise RuntimeError(f"Ownership mismatch for Chroma chunk {chunk_id}")
                vector = batch["embeddings"][i]
                validate_embedding(vector, dimensions)
                content = batch["documents"][i]
                if content is None:
                    raise RuntimeError(f"Chroma chunk {chunk_id} has no document content")
                meta = dict(meta)
                if "_legacy_chroma_collection" in meta and meta["_legacy_chroma_collection"] != name:
                    raise RuntimeError(f"Chroma chunk {chunk_id} uses reserved migration metadata")
                meta["_legacy_chroma_collection"] = name
                records.append({"id": chunk_id, "document_id": doc_id, "user_id": owner,
                                "content": content, "metadata": meta,
                                "embedding": [float(v) for v in vector], "collection": name})
                per_document[doc_id] = per_document.get(doc_id, 0) + 1
            offset += size
        if count_read != total:
            raise RuntimeError(f"Chroma collection {name!r} changed or could not be fully read: expected {total}, read {count_read}")
    expected = {row["id"]: int(row.get("chunks") or 0) for row in documents.values() if row.get("status") == "ready"}
    validate_chunk_counts(expected, per_document)
    return records, per_document


if __name__ == "__main__":
    main()
