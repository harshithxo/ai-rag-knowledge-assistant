from pathlib import Path
from langchain_core.documents import Document
import app.services.ingestion_service as svc


def test_chunk_metadata(monkeypatch):
    monkeypatch.setattr(svc, "read_pdf_pages", lambda _: [Document(page_content="hello " * 500, metadata={"page": 0})])
    chunks = svc.extract_and_chunk(Path("fake.pdf"), "doc-1", "file.pdf")
    assert chunks
    assert chunks[0].metadata["document_id"] == "doc-1"
    assert chunks[0].metadata["page_number"] == 1
    assert chunks[0].metadata["chunk_id"].startswith("doc-1:")


def test_chunk_indexes_are_sequential_and_size_is_bounded(monkeypatch):
    from app.config import settings
    text = "Sentence number one. " * 400
    monkeypatch.setattr(svc, "read_pdf_pages", lambda _: [Document(page_content=text, metadata={"page": 2})])
    chunks = svc.extract_and_chunk(Path("fake.pdf"), "d", "f.pdf")
    assert [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks)))
    assert all(len(c.page_content) <= settings.chunk_size for c in chunks)
    assert all(c.metadata["page_number"] == 3 for c in chunks)
