"""User-scoped semantic search over PostgreSQL/pgvector."""
import logging
from typing import Any

from app.rag.vector_store import get_vector_store
from app.services import settings_service

logger = logging.getLogger(__name__)


def retrieve(query: str, k: int | None = None, document_ids: list[str] | None = None,
             min_score: float | None = None, user_id: str | None = None) -> list[tuple[Any, float]]:
    """Return (chunk, score) pairs above the relevance threshold, best first."""
    k = k or settings_service.get_int("retrieval_top_k")
    threshold = settings_service.get_float("retrieval_min_score") if min_score is None else min_score
    if not user_id:
        raise ValueError("user_id is required for document retrieval")
    store = get_vector_store()
    results = store.search(query, k * 2, user_id, document_ids)
    kept = [(doc, score) for doc, score in results if score >= threshold]
    kept.sort(key=lambda pair: pair[1], reverse=True)
    logger.info("Retrieval: %d candidates, %d above threshold %.2f", len(results), len(kept), threshold)
    return kept[:k]


def get_document_chunks(document_id: str, user_id: str | None = None) -> list[Any]:
    """All stored chunks of one document, in page order."""
    from langchain_core.documents import Document

    return get_vector_store().chunks(document_id, user_id)


def delete_document_vectors(document_id: str, user_id: str | None = None) -> int:
    return get_vector_store().delete_document(document_id, user_id)
