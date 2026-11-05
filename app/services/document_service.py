"""Document library: list, look up, preview, delete and search documents."""
import logging
from pathlib import Path

from app import db
from app.config import settings
from app.errors import NotFound
from app.storage import document_key, get_pdf_storage

logger = logging.getLogger(__name__)

PUBLIC_FIELDS = "id, filename, size_bytes, status, error, pages, chunks, created_at"


def delete_document_vectors(document_id: str, user_id: str | None = None) -> int:
    from app.rag.retriever import delete_document_vectors as delete_vectors
    return delete_vectors(document_id, user_id) if user_id else delete_vectors(document_id)


def retrieve(*args, **kwargs):
    from app.rag.retriever import retrieve as do_retrieve
    return do_retrieve(*args, **kwargs)


def list_documents(user_id: str | None = None) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(f"SELECT {PUBLIC_FIELDS} FROM documents WHERE user_id = ? ORDER BY created_at DESC", (user_id,)).fetchall() if user_id else conn.execute(f"SELECT {PUBLIC_FIELDS} FROM documents ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_document(document_id: str, user_id: str | None = None) -> dict:
    with db.connect() as conn:
        row = conn.execute(f"SELECT {PUBLIC_FIELDS} FROM documents WHERE id = ? AND (? IS NULL OR user_id = ?)", (document_id, user_id, user_id)).fetchone()
    if row is None:
        raise NotFound("Document not found")
    return dict(row)


def ready_document_ids(user_id: str | None = None) -> list[str]:
    with db.connect() as conn:
        rows = conn.execute("SELECT id FROM documents WHERE status = 'ready' AND (? IS NULL OR user_id = ?)", (user_id, user_id)).fetchall()
    return [r["id"] for r in rows]


def get_file_path(document_id: str, user_id: str | None = None) -> Path:
    """Find the stored PDF on disk. The file name comes from the database, and we also check
    that the final path really is inside the upload folder."""
    with db.connect() as conn:
        row = conn.execute("SELECT stored_name FROM documents WHERE id = ? AND (? IS NULL OR user_id = ?)", (document_id, user_id, user_id)).fetchone()
    if row is None:
        raise NotFound("Document not found")
    if settings.upload_storage.lower() != "local":
        raise NotFound("PDF is stored remotely")
    storage = get_pdf_storage()
    try:
        path = storage.path(document_key(user_id, row["stored_name"]))
    except (ValueError, OSError) as exc:
        # A corrupt or tampered database key is an unavailable document, while the
        # storage backend still rejects traversal before touching the filesystem.
        raise NotFound("The PDF file is no longer available on disk") from exc
    if not path.is_file() and user_id is None:
        path = (Path(settings.upload_dir).resolve() / row["stored_name"]).resolve()
    base = Path(settings.upload_dir).resolve()
    if base not in path.parents or not path.is_file():
        raise NotFound("The PDF file is no longer available on disk")
    return path


def get_file_content(document_id: str, user_id: str | None = None) -> bytes:
    with db.connect() as conn:
        row = conn.execute("SELECT stored_name, user_id FROM documents WHERE id=? AND (? IS NULL OR user_id=?)", (document_id, user_id, user_id)).fetchone()
    if row is None:
        raise NotFound("Document not found")
    try:
        return get_pdf_storage().get(document_key(row["user_id"], row["stored_name"]))
    except FileNotFoundError as exc:
        raise NotFound("The PDF file is no longer available") from exc


def delete_document(document_id: str, user_id: str | None = None) -> None:
    """Delete the vectors first, then the file, then the database row. If deleting the vectors
    fails we stop there, so the document stays in the list and can be retried."""
    with db.connect() as conn:
        row = conn.execute("SELECT stored_name FROM documents WHERE id = ? AND (? IS NULL OR user_id = ?)", (document_id, user_id, user_id)).fetchone()
    if row is None:
        raise NotFound("Document not found")
    delete_document_vectors(document_id, user_id)
    get_pdf_storage().delete(document_key(user_id, row["stored_name"]))
    with db.connect() as conn:
        conn.execute("DELETE FROM documents WHERE id = ? AND (? IS NULL OR user_id = ?)", (document_id, user_id, user_id))
        # chats that were limited to this document go back to searching everything
        conn.execute("UPDATE conversations SET document_id = NULL WHERE document_id = ? AND (? IS NULL OR user_id = ?)", (document_id, user_id, user_id))
    logger.info("Deleted document %s", document_id)


def search_documents(query: str, limit: int = 8, user_id: str | None = None) -> list[dict]:
    """Search the text of every ready document."""
    ids = ready_document_ids(user_id)
    if not ids:
        return []
    results = retrieve(query, k=limit, document_ids=ids, min_score=0.0, user_id=user_id)
    return [{
        "document_id": doc.metadata.get("document_id"),
        "filename": doc.metadata.get("filename", "Unknown"),
        "page": doc.metadata.get("page_number"),
        "score": round(float(score), 3),
        "text": doc.page_content.strip(),
    } for doc, score in results]
