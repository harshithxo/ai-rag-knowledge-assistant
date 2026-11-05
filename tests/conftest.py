"""Shared fixtures. The environment variables are set before the app is imported, so every
test uses throw-away storage and never touches real data."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="rag_tests_")
os.environ["UPLOAD_DIR"] = str(Path(_TMP) / "uploads")
os.environ["CHROMA_PERSIST_DIR"] = str(Path(_TMP) / "chroma")
os.environ["DATABASE_PATH"] = str(Path(_TMP) / "test.db")
os.environ["DOCMIND_TEST_SQLITE"] = "1"
os.environ["MAX_UPLOAD_SIZE_MB"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from helpers import FakeLLM, FakeStore  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Give every test an empty database, upload folder and vector store, plus the fake LLM."""
    from app import db
    from app.config import settings

    with db.connect() as conn:
        for table in ("response_metrics", "messages", "conversations", "document_chunks", "documents", "email_verification_tokens", "password_reset_tokens", "sessions", "auth_rate_limits", "users", "app_settings"):
            conn.execute(f"DELETE FROM {table}")
    upload_dir = Path(settings.upload_dir)
    shutil.rmtree(upload_dir, ignore_errors=True)
    upload_dir.mkdir(parents=True, exist_ok=True)

    store, llm = FakeStore(), FakeLLM()
    import app.rag.retriever as retriever
    import app.services.ingestion_service as ingestion
    monkeypatch.setattr(retriever, "get_vector_store", lambda: store)
    monkeypatch.setattr(ingestion, "get_vector_store", lambda: store)
    from app.services import llm_service
    monkeypatch.setattr(llm_service, "generate", llm)
    monkeypatch.setattr(llm_service, "stream", llm.stream)
    return store, llm


@pytest.fixture
def store(clean_state):
    return clean_state[0]


@pytest.fixture
def llm(clean_state):
    return clean_state[1]
