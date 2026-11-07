
"""Gemini or Ollama embeddings with a small query cache."""
from collections import OrderedDict
from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_ollama import OllamaEmbeddings

from app.config import settings

QUERY_CACHE_SIZE = 128


class CachedEmbeddings(Embeddings):
    """Cache recently used query vectors."""

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
            self._queries.popitem(last=False)

        return vector


@lru_cache(maxsize=1)
def get_embeddings() -> CachedEmbeddings:
    provider = settings.embedding_provider.lower()

    if provider == "gemini":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        inner = GoogleGenerativeAIEmbeddings(
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            output_dimensionality=settings.vector_dimensions,
        )

    elif provider == "openai-compatible":
        from langchain_openai import OpenAIEmbeddings

        inner = OpenAIEmbeddings(
            model=settings.embedding_model,
            base_url=settings.embedding_base_url or settings.ai_base_url,
            api_key=settings.embedding_api_key or settings.ai_api_key,
            dimensions=settings.vector_dimensions,
        )

    else:
        inner = OllamaEmbeddings(
            model=settings.embedding_model,
            base_url=settings.ollama_base_url,
        )

    return CachedEmbeddings(inner)
