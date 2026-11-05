"""Checks, stores, reads, chunks and indexes uploaded PDFs."""
import hashlib
import logging
import re
import unicodedata
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from app import db
from app.config import settings
from app.errors import Conflict, NotFound, PayloadTooLarge, ValidationFailed
from app.rag.retriever import delete_document_vectors
from app.rag.vector_store import get_vector_store
from app.services import settings_service
from app.storage import document_key, get_pdf_storage

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf"}
PDF_MAGIC = b"%PDF-"


# ---------------------------------------------------------------- validation
def sanitize_filename(filename: str | None) -> str:
    """Keep only the base name and safe characters. The name is just for display.
    Files are saved on disk under a generated id, so user input never becomes a path."""
    name = Path((filename or "document.pdf").replace("\\", "/")).name
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[^\w.\- ()]", "_", name).strip(" .")
    if not name:
        name = "document.pdf"
    return name[:120]


def validate_pdf(filename: str | None, content: bytes) -> None:
    if not filename or Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValidationFailed("Only PDF files are supported")
    if not content:
        raise ValidationFailed("The uploaded file is empty")
    if len(content) > settings_service.get_max_upload_bytes():
        raise PayloadTooLarge(f"File exceeds the {settings_service.get_int('max_upload_size_mb')} MB upload limit")
    if not content.startswith(PDF_MAGIC):
        raise ValidationFailed("The uploaded file is not a valid PDF")


# ---------------------------------------------------------------- text handling
def clean_text(text: str) -> str:
    """Clean up raw PDF text: drop null bytes, join hyphenated line breaks, collapse extra blanks."""
    text = text.replace("\x00", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # "informa-\ntion" -> "information"
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_pdf_pages(path: Path | bytes) -> list[Document]:
    """Read a PDF with pypdf into one LangChain Document per page (the 'page' metadata starts at 0)."""
    try:
        from io import BytesIO
        reader = PdfReader(BytesIO(path) if isinstance(path, bytes) else str(path))
        if reader.is_encrypted:
            raise ValidationFailed("The PDF is password-protected and cannot be read")
        return [Document(page_content=page.extract_text() or "", metadata={"page": index})
                for index, page in enumerate(reader.pages)]
    except ValidationFailed:
        raise
    except Exception as exc:
        raise ValidationFailed("The PDF could not be parsed (it may be corrupted or encrypted)") from exc


def extract_and_chunk(path: Path | bytes, document_id: str, filename: str, user_id: str | None = None):
    pages = read_pdf_pages(path)

    for page in pages:
        page.page_content = clean_text(page.page_content or "")
    page_count = len(pages)
    pages = [p for p in pages if p.page_content]
    if not pages:
        raise ValidationFailed("The PDF contains no extractable text (it may be a scanned image)")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings_service.get_int("chunk_size"),
        chunk_overlap=settings_service.get_int("chunk_overlap"),
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks = splitter.split_documents(pages)
    for index, chunk in enumerate(chunks):
        page = chunk.metadata.get("page")
        chunk.metadata.update({
            "filename": filename,
            "page_number": (page + 1) if isinstance(page, int) else None,
            "chunk_id": f"{document_id}:{index}",
            "chunk_index": index,
            "source": filename,
            "document_id": document_id,
            "user_id": user_id or "legacy",
            "total_pages": page_count,
        })
    return chunks


# ---------------------------------------------------------------- workflow
def register_upload(filename: str | None, content: bytes, user_id: str | None = None) -> tuple[dict, bool]:
    """Validate the file, check for duplicates and save it. Returns (document_row, is_duplicate).

    Indexing happens later in process_document(), so the upload request returns quickly
    and the browser can poll the status."""
    validate_pdf(filename, content)
    digest = hashlib.sha256(content).hexdigest()
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT * FROM documents WHERE content_hash = ? AND status != 'failed' AND (? IS NULL OR user_id = ?)", (digest, user_id, user_id)).fetchone()
    if existing:
        return dict(existing), True

    document_id = db.new_id()
    stored_name = f"{document_id}.pdf"
    get_pdf_storage().put(document_key(user_id, stored_name), content)
    row = {
        "id": document_id, "filename": sanitize_filename(filename), "stored_name": stored_name,
        "size_bytes": len(content), "content_hash": digest, "status": "processing",
        "error": None, "pages": 0, "chunks": 0, "created_at": db.now_iso(), "user_id": user_id,
    }
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO documents(id, filename, stored_name, size_bytes, content_hash, status, error, "
            "pages, chunks, created_at, user_id) VALUES(:id, :filename, :stored_name, :size_bytes, :content_hash, "
            ":status, :error, :pages, :chunks, :created_at, :user_id)", row)
    return row, False


def process_document(document_id: str, user_id: str | None = None) -> None:
    """Read, chunk, embed and store a registered document. Doesn't raise: if something goes
    wrong, the error is saved on the document so the UI can show it."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ? AND (? IS NULL OR user_id = ?)", (document_id, user_id, user_id)).fetchone()
    if row is None:
        return
    try:
        content = get_pdf_storage().get(document_key(row["user_id"], row["stored_name"]))
        chunks = extract_and_chunk_bytes(content, document_id, row["filename"], row["user_id"])
        ids = [c.metadata["chunk_id"] for c in chunks]
        get_vector_store().add_documents(chunks, ids=ids)
        pages = len({c.metadata["page_number"] for c in chunks if c.metadata.get("page_number")})
        with db.connect() as conn:
            still_exists = conn.execute("UPDATE documents SET status='ready', error=NULL, pages=?, chunks=? "
                                        "WHERE id=?", (pages, len(chunks), document_id)).rowcount
        if not still_exists:
            # the document was deleted while it was being indexed, so remove the vectors we just added
            delete_document_vectors(document_id, row["user_id"]) if row["user_id"] else delete_document_vectors(document_id)
            logger.info("Document %s was deleted during indexing; its vectors were removed", document_id)
            return
        logger.info("Indexed document id=%s file=%s chunks=%d", document_id, row["filename"], len(chunks))
    except Exception as exc:  # noqa: BLE001 - saved on the document and logged, not hidden
        logger.exception("Processing failed for %s", document_id)
        message = str(exc) if isinstance(exc, ValidationFailed) else _friendly_processing_error(exc)
        with db.connect() as conn:
            conn.execute("UPDATE documents SET status='failed', error=? WHERE id=?", (message, document_id))


def _friendly_processing_error(exc: Exception) -> str:
    text = str(exc).lower()
    if settings.embedding_provider.lower() == "ollama":
        if "not found" in text and "model" in text:
            return f"Embedding model '{settings.embedding_model}' is not installed. Run: ollama pull {settings.embedding_model}"
        if any(w in text for w in ("connect", "refused", "unreachable")):
            return f"Cannot reach Ollama at {settings.ollama_base_url}. Start it with 'ollama serve' and re-upload."
    elif any(w in text for w in ("connect", "refused", "unreachable", "timeout")):
        return "Cannot reach the configured embedding API. Check EMBEDDING_BASE_URL and provider availability."
    return "Document processing failed. See server logs for details."


def extract_and_chunk_bytes(content: bytes, document_id: str, filename: str, user_id: str | None = None):
    """Chunk PDF bytes from either local persistent storage or object storage."""
    return extract_and_chunk(content, document_id, filename, user_id)


def ingest_pdf(filename: str, content: bytes, user_id: str | None = None) -> dict:
    """Register and process a PDF in one go. Mostly handy in tests."""
    row, duplicate = register_upload(filename, content, user_id)
    if duplicate:
        raise Conflict(f"'{row['filename']}' has already been uploaded")
    process_document(row["id"], user_id)
    with db.connect() as conn:
        final = conn.execute("SELECT * FROM documents WHERE id = ?", (row["id"],)).fetchone()
    if final["status"] == "failed":
        raise ValidationFailed(final["error"] or "Document processing failed")
    return {"document_id": final["id"], "filename": final["filename"],
            "chunks": final["chunks"], "pages": final["pages"]}


def reprocess_document(document_id: str, user_id: str | None = None) -> None:
    """Retry a failed document: clear any leftover vectors, then index it again."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ? AND (? IS NULL OR user_id = ?)", (document_id, user_id, user_id)).fetchone()
        if row is None:
            raise NotFound("Document not found")
        conn.execute("UPDATE documents SET status='processing', error=NULL WHERE id=?", (document_id,))
    try:
        delete_document_vectors(document_id, user_id) if user_id else delete_document_vectors(document_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not clear old vectors for %s before re-processing", document_id)
    process_document(document_id, user_id)
