import pytest

from app import db
from app.errors import Conflict, PayloadTooLarge, ValidationFailed
from app.services import ingestion_service as ing
from app.services.ingestion_service import clean_text, sanitize_filename, validate_pdf
from helpers import make_pdf


# ----- validation -----
def test_validate_pdf_accepts_pdf_header():
    validate_pdf("sample.pdf", b"%PDF-1.7 fake")


def test_validate_pdf_rejects_wrong_extension():
    with pytest.raises(ValidationFailed, match="Only PDF"):
        validate_pdf("sample.txt", b"%PDF-1.7 fake")


def test_validate_pdf_rejects_non_pdf_content():
    with pytest.raises(ValidationFailed, match="valid PDF"):
        validate_pdf("sample.pdf", b"not a pdf")


def test_validate_pdf_rejects_empty_file():
    with pytest.raises(ValidationFailed, match="empty"):
        validate_pdf("sample.pdf", b"")


def test_validate_pdf_rejects_oversized_file():
    with pytest.raises(PayloadTooLarge, match="MB"):
        validate_pdf("big.pdf", b"%PDF-" + b"0" * (2 * 1024 * 1024))   # test limit is 1 MB


# ----- filename sanitisation / text cleaning -----
def test_sanitize_filename_blocks_path_traversal():
    assert sanitize_filename("../../etc/passwd.pdf") == "passwd.pdf"
    assert sanitize_filename("C:\\Users\\me\\notes.pdf") == "notes.pdf"
    assert "/" not in sanitize_filename("a/b/c.pdf")


def test_sanitize_filename_removes_odd_characters_and_handles_empty():
    assert sanitize_filename("my<script>.pdf") == "my_script_.pdf"
    assert sanitize_filename("") == "document.pdf"
    assert sanitize_filename(None) == "document.pdf"
    assert len(sanitize_filename("x" * 500 + ".pdf")) <= 120


def test_clean_text_rejoins_hyphenation_and_collapses_whitespace():
    assert clean_text("informa-\ntion   here\x00") == "information here"
    assert clean_text("a\n\n\n\nb") == "a\n\nb"


# ----- registration, duplicates, processing -----
def test_register_upload_detects_duplicates():
    pdf = make_pdf(["hello world"])
    first, dup1 = ing.register_upload("a.pdf", pdf)
    second, dup2 = ing.register_upload("renamed.pdf", pdf)
    assert dup1 is False and dup2 is True
    assert second["id"] == first["id"]


def test_files_are_stored_under_generated_names():
    from pathlib import Path
    from app.config import settings
    row, _ = ing.register_upload("../../evil name.pdf", make_pdf(["x"]))
    stored = list(Path(settings.upload_dir).iterdir())
    assert [p.name for p in stored] == [f"{row['id']}.pdf"]


def test_process_document_indexes_chunks_with_metadata(store):
    row, _ = ing.register_upload("report.pdf", make_pdf(["Quarterly revenue grew strongly.", "Costs fell in Q3."]))
    ing.process_document(row["id"])
    with db.connect() as conn:
        doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (row["id"],)).fetchone())
    assert doc["status"] == "ready" and doc["chunks"] >= 2 and doc["pages"] == 2
    metas = [m for _, m in store.items.values()]
    assert {m["page_number"] for m in metas} == {1, 2}
    assert all(m["document_id"] == row["id"] and m["filename"] == "report.pdf" for m in metas)


def test_process_document_marks_corrupt_pdf_as_failed(store):
    row, _ = ing.register_upload("broken.pdf", b"%PDF-1.4 this is not really a pdf")
    ing.process_document(row["id"])
    with db.connect() as conn:
        doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (row["id"],)).fetchone())
    assert doc["status"] == "failed" and doc["error"]
    assert store.items == {}


def test_process_document_flags_pdf_without_text(store):
    row, _ = ing.register_upload("scan.pdf", make_pdf([""]))
    ing.process_document(row["id"])
    with db.connect() as conn:
        doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (row["id"],)).fetchone())
    assert doc["status"] == "failed" and "no extractable text" in doc["error"]


def test_ingest_pdf_wrapper_and_duplicate_conflict():
    result = ing.ingest_pdf("a.pdf", make_pdf(["some searchable content"]))
    assert result["chunks"] >= 1
    with pytest.raises(Conflict):
        ing.ingest_pdf("a-again.pdf", make_pdf(["some searchable content"]))
