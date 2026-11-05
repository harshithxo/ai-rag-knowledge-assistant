"""PostgreSQL/pgvector document chunk storage."""
import math

from langchain_core.documents import Document

from app import db
from app.config import settings
from app.rag.embeddings import get_embeddings


class PgVectorStore:
    def add_documents(self, docs, ids=None):
        docs, ids = list(docs), list(ids or [])
        if len(docs) != len(ids):
            raise ValueError("Each document chunk must have an ID")
        vectors = get_embeddings().embed_documents([d.page_content for d in docs])
        for doc, chunk_id, embedding in zip(docs, ids, vectors):
            if len(embedding) != settings.vector_dimensions:
                raise ValueError(f"Embedding dimension {len(embedding)} does not match VECTOR_DIMENSIONS={settings.vector_dimensions}")
            user_id = doc.metadata.get("user_id")
            if not user_id or user_id == "legacy":
                raise ValueError("A real user_id is required to index searchable document chunks")
            with db.connect() as conn:
                owner = conn.execute("SELECT 1 FROM documents WHERE id=? AND user_id=?", (doc.metadata["document_id"], user_id)).fetchone()
                if owner is None:
                    raise ValueError("Chunk document does not belong to the supplied user")
                if not settings.database_url:
                    conn.execute("""INSERT INTO document_chunks
                        (id, document_id, user_id, content, metadata, embedding)
                        VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                        content=excluded.content, metadata=excluded.metadata, embedding=excluded.embedding
                        WHERE document_chunks.user_id=excluded.user_id""",
                        (chunk_id, doc.metadata["document_id"], user_id, doc.page_content,
                         db.dumps(doc.metadata), db.dumps(embedding)))
                else:
                    conn.execute("""INSERT INTO document_chunks
                    (id, document_id, user_id, content, metadata, embedding)
                    VALUES (?, ?, ?, ?, ?::jsonb, ?::vector)
                    ON CONFLICT (id) DO UPDATE SET content=EXCLUDED.content,
                    metadata=EXCLUDED.metadata, embedding=EXCLUDED.embedding
                    WHERE document_chunks.user_id=EXCLUDED.user_id""",
                    (chunk_id, doc.metadata["document_id"], user_id, doc.page_content,
                     db.dumps(doc.metadata), _vector_literal(embedding)))
        return ids

    def search(self, query, k, user_id, document_ids=None):
        if not user_id:
            raise ValueError("user_id is mandatory for vector retrieval")
        vector = get_embeddings().embed_query(query)
        if len(vector) != settings.vector_dimensions:
            raise ValueError("Query embedding dimension does not match the stored vector dimension")
        ids = list(document_ids or [])
        if not settings.database_url:
            with db.connect() as conn:
                rows = conn.execute("SELECT content, metadata, embedding FROM document_chunks WHERE user_id=?", (user_id,)).fetchall()
            candidates = []
            for row in rows:
                meta = db.loads(row["metadata"], {})
                if document_ids is not None and meta.get("document_id") not in ids:
                    continue
                stored = db.loads(row["embedding"], [])
                denom = math.sqrt(sum(v*v for v in vector) * sum(v*v for v in stored))
                score = sum(a*b for a, b in zip(vector, stored)) / denom if denom else 0.0
                candidates.append((Document(page_content=row["content"], metadata=meta), score))
            return sorted(candidates, key=lambda item: item[1], reverse=True)[:k]
        with db.connect() as conn:
            rows = conn.execute("""SELECT content, metadata,
                1 - (embedding <=> ?::vector) AS score
                FROM document_chunks WHERE user_id=? AND (?::text[] IS NULL OR document_id=ANY(?::text[]))
                ORDER BY embedding <=> ?::vector LIMIT ?""",
                (_vector_literal(vector), user_id, ids if document_ids is not None else None,
                 ids if document_ids is not None else None,
                 _vector_literal(vector), k)).fetchall()
        return [(Document(page_content=r["content"], metadata=db.loads(r["metadata"], {})), float(r["score"])) for r in rows]

    def chunks(self, document_id, user_id):
        if not user_id:
            return []
        with db.connect() as conn:
            if settings.database_url:
                rows = conn.execute("SELECT content, metadata FROM document_chunks WHERE user_id=? AND document_id=? ORDER BY (metadata->>'chunk_index')::int", (user_id, document_id)).fetchall()
            else:
                rows = conn.execute("SELECT content, metadata FROM document_chunks WHERE user_id=? AND document_id=? ORDER BY CAST(json_extract(metadata, '$.chunk_index') AS INTEGER)", (user_id, document_id)).fetchall()
        return [Document(page_content=r["content"], metadata=db.loads(r["metadata"], {})) for r in rows]

    def delete_document(self, document_id, user_id):
        with db.connect() as conn:
            if user_id:
                cur = conn.execute("DELETE FROM document_chunks WHERE user_id=? AND document_id=?", (user_id, document_id))
            else:
                cur = conn.execute("DELETE FROM document_chunks WHERE user_id IS NULL AND document_id=?", (document_id,))
            return cur.rowcount


def _vector_literal(values):
    return "[" + ",".join(str(float(v)) for v in values) + "]"


_store = PgVectorStore()


def get_vector_store() -> PgVectorStore:
    return _store
