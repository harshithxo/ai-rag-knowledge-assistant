"""Ollama embeddings, with a small cache for query vectors."""
from collections import OrderedDict
from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_ollama import OllamaEmbeddings

from app.config import settings

QUERY_CACHE_SIZE = 128


class CachedEmbeddings(Embeddings):
    """Wraps OllamaEmbeddings and remembers recent query vectors.

    Asking the same thing twice (a retry, or the same follow-up chip) skips the embedding call.
    Document chunks aren't cached because they are only embedded once, at upload time."""

    def __init__(self, inner: Embeddings):
        self.inner = inner
        self._queries: OrderedDict[str, list[float]] = OrderedDict()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.inner.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        if text in self._queries:
            self._queries.move_to_end(text)
            return self._queries[text]
        vector = self.inner.embed_query(text)
        self._queries[text] = vector
        if len(self._queries) > QUERY_CACHE_SIZE:
            self._queries.popitem(last=False)      # drop the oldest one
        return vector


@lru_cache(maxsize=1)
def get_embeddings() -> CachedEmbeddings:
    if settings.embedding_provider.lower() == "openai-compatible":
        from langchain_openai import OpenAIEmbeddings
        inner = OpenAIEmbeddings(
            model=settings.embedding_model,
            base_url=settings.embedding_base_url or settings.ai_base_url,
            api_key=settings.embedding_api_key or settings.ai_api_key,
            dimensions=settings.vector_dimensions,
        )
    else:
        inner = OllamaEmbeddings(model=settings.embedding_model, base_url=settings.ollama_base_url)
    return CachedEmbeddings(inner)
