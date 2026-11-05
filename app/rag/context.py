"""Turns retrieved chunks into the context text for the model plus a list of sources.

Chunks from the same page are merged into one source with several passages, so a page
never shows up twice."""
from typing import Any

PASSAGE_PREVIEW_CHARS = 700
MAX_PASSAGES_SHOWN = 3


def _same_text(a: str, b: str) -> bool:
    return a in b or b in a


def build_sources(hits: list[tuple[Any, float]]) -> list[dict[str, Any]]:
    """Group (chunk, score) pairs, best first, into numbered sources."""
    sources: dict[tuple, dict[str, Any]] = {}
    for chunk, score in hits:
        meta = chunk.metadata or {}
        text = (chunk.page_content or "").strip()
        if not text:
            continue
        filename = meta.get("filename") or meta.get("source") or "Unknown"
        key = (meta.get("document_id") or filename, meta.get("page_number"))
        source = sources.get(key)
        if source is None:
            source = sources[key] = {
                "id": len(sources) + 1, "filename": filename, "page": meta.get("page_number"),
                "document_id": meta.get("document_id"), "score": 0.0, "chunk_ids": [], "full_passages": [],
            }
        if any(_same_text(text, existing) for existing in source["full_passages"]):
            continue                                   # overlapping chunks can repeat the same text
        source["full_passages"].append(text)
        source["chunk_ids"].append(meta.get("chunk_id"))
        source["score"] = max(source["score"], float(score))
    return list(sources.values())


def build_context(sources: list[dict[str, Any]], max_chars: int) -> tuple[str, list[dict[str, Any]]]:
    """Build the prompt context. Returns it with the sources that fit under max_chars."""
    parts, kept, total = [], [], 0
    for source in sources:
        body = "\n…\n".join(source["full_passages"])
        block = f"[Source {source['id']}] {source['filename']} | page {source['page'] or 'unknown'}\n{body}"
        if kept and total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
        kept.append(source)
    return "\n\n".join(parts), kept


def public_source(source: dict[str, Any]) -> dict[str, Any]:
    """The trimmed version of a source that gets saved and sent to the browser."""
    passages = [p[:PASSAGE_PREVIEW_CHARS] + ("…" if len(p) > PASSAGE_PREVIEW_CHARS else "")
                for p in source["full_passages"][:MAX_PASSAGES_SHOWN]]
    return {
        "id": source["id"], "filename": source["filename"], "page": source["page"],
        "document_id": source["document_id"], "chunk_ids": source["chunk_ids"],
        "score": round(max(0.0, min(1.0, source["score"])), 3), "passages": passages,
    }
