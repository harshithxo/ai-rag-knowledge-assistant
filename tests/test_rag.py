"""Tests for the retrieval helpers: source grouping, context building and the query-embedding cache."""
from langchain_core.documents import Document

from app.rag.context import build_context, build_sources, public_source
from app.rag.embeddings import CachedEmbeddings


def hit(text, page=1, score=0.8, doc_id="d1", name="a.pdf", chunk="d1:0"):
    return (Document(page_content=text, metadata={"filename": name, "page_number": page, "document_id": doc_id,
                                                  "chunk_id": chunk}), score)


def test_chunks_on_the_same_page_are_grouped_and_best_score_wins():
    sources = build_sources([hit("first part", score=0.9, chunk="d1:0"), hit("second part", score=0.7, chunk="d1:1"),
                             hit("other page", page=2, score=0.6, chunk="d1:2")])
    assert [(s["id"], s["page"]) for s in sources] == [(1, 1), (2, 2)]
    assert sources[0]["full_passages"] == ["first part", "second part"] and sources[0]["score"] == 0.9


def test_repeated_text_is_not_listed_twice():
    sources = build_sources([hit("the same sentence here"), hit("the same sentence here", chunk="d1:1"),
                             hit("sentence here", chunk="d1:2")])
    assert len(sources[0]["full_passages"]) == 1


def test_same_page_number_in_different_documents_stays_separate():
    sources = build_sources([hit("x", doc_id="d1"), hit("y", doc_id="d2", name="b.pdf")])
    assert len(sources) == 2


def test_context_stops_at_the_character_limit_but_keeps_the_first_source():
    sources = build_sources([hit("a" * 500, page=1), hit("b" * 500, page=2), hit("c" * 500, page=3)])
    context, kept = build_context(sources, max_chars=700)
    assert len(kept) == 1 and "[Source 1]" in context and "[Source 2]" not in context
    _, kept_one = build_context(sources, max_chars=10)
    assert len(kept_one) == 1                                     # never returns an empty context


def test_public_source_clamps_score_and_shortens_passages():
    source = build_sources([hit("z" * 2000, score=1.7)])[0]
    public = public_source(source)
    assert public["score"] == 1.0 and len(public["passages"][0]) <= 701 and "full_passages" not in public
    assert public_source(build_sources([hit("q", score=-0.2)])[0])["score"] == 0.0


def test_query_embedding_cache_avoids_repeat_calls_and_stays_small():
    class Inner:
        calls = 0
        def embed_query(self, text):
            Inner.calls += 1
            return [float(len(text))]
        def embed_documents(self, texts):
            return [[1.0] for _ in texts]
    cache = CachedEmbeddings(Inner())
    assert cache.embed_query("hello") == cache.embed_query("hello") and Inner.calls == 1
    for i in range(200):
        cache.embed_query(f"question {i}")
    assert len(cache._queries) <= 128
