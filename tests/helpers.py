"""Test helpers: a tiny PDF builder, an in-memory vector store and a fake LLM.
The tests need no Ollama, no network and no extra packages."""
import re

from langchain_core.documents import Document

from app.prompts.prompts import RAG_SYSTEM_PROMPT


def make_pdf(pages: list[str]) -> bytes:
    """Build a small valid PDF with one line of text per page."""
    objects: list[bytes] = []
    n_pages = len(pages)
    font_id = 3 + 2 * n_pages
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode()
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


class FakeStore:
    """In-memory pgvector stand-in used by tests."""

    def __init__(self):
        self.items: dict[str, tuple[str, dict]] = {}

    def add_documents(self, docs, ids=None):
        for doc, doc_id in zip(docs, ids):
            self.items[doc_id] = (doc.page_content, dict(doc.metadata))
        return ids

    def _matches(self, meta, where):
        if not where:
            return True
        for key, cond in where.items():
            if key == "$and":
                if not all(self._matches(meta, clause) for clause in cond): return False
                continue
            value = meta.get(key)
            if isinstance(cond, dict):
                if "$in" in cond and value not in cond["$in"]: return False
                elif "$in" not in cond and value != cond: return False
            elif value != cond:
                return False
        return True

    def get(self, where=None, include=None):
        rows = [(i, t, m) for i, (t, m) in self.items.items() if self._matches(m, where)]
        return {"ids": [r[0] for r in rows], "documents": [r[1] for r in rows], "metadatas": [r[2] for r in rows]}

    def delete(self, ids=None):
        for i in ids or []:
            self.items.pop(i, None)

    def similarity_search_with_relevance_scores(self, query, k=4, filter=None):
        q = _words(query)
        scored = []
        for _id, (text, meta) in self.items.items():
            if not self._matches(meta, filter):
                continue
            overlap = len(q & _words(text)) / max(1, len(q))
            scored.append((Document(page_content=text, metadata=meta), overlap))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]

    def search(self, query, k, user_id, document_ids=None):
        if not user_id:
            raise ValueError("user_id required")
        scored = self.similarity_search_with_relevance_scores(query, k, None)
        return [(d, s) for d, s in scored if d.metadata.get("user_id") == user_id
                and (document_ids is None or d.metadata.get("document_id") in document_ids)]

    def chunks(self, document_id, user_id):
        return [Document(page_content=text, metadata=meta) for text, meta in self.items.values()
                if user_id and meta.get("user_id") == user_id and meta.get("document_id") == document_id]

    def delete_document(self, document_id, user_id):
        ids = [i for i, (_, m) in self.items.items() if m.get("document_id") == document_id and
               (m.get("user_id") == user_id if user_id else m.get("user_id") in (None, "legacy"))]
        for i in ids:
            del self.items[i]
        return len(ids)


class FakeLLM:
    """Replaces llm_service.generate and llm_service.stream. It records every call, so tests can
    look at the prompts and count how many LLM calls a question needed."""

    def __init__(self):
        self.calls: list[list] = []
        self.answer = "The documents say so [Source 1]."

    def stream(self, messages, temperature=None, stats=None):
        text = self(messages, temperature)
        pieces = re.findall(r"\S+\s*", text)
        if stats is not None:
            stats["tokens"] = len(pieces)
            stats["tokens_per_second"] = 100.0
        yield from pieces

    def __call__(self, messages, temperature=None, max_tokens=None):
        self.calls.append(list(messages))
        system = messages[0].content
        user = messages[-1].content
        if system == RAG_SYSTEM_PROMPT:          # checked first: this prompt also mentions "follow-up questions"
            return self.answer
        if "standalone search question" in system:
            return "standalone: " + user.split("Latest message:")[-1].strip()
        if "follow-up questions" in system:
            return 'Sure! ["What is next?", "Why does it matter?", "Who is involved?"]'
        return "Hello! I can help with your documents."
