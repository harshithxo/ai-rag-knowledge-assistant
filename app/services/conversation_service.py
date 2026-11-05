"""Chat history: conversations and messages stored in SQLite."""
from app import db
from app.config import settings
from app.errors import NotFound

TITLE_MAX = 80


def create_conversation(title: str = "New chat", document_id: str | None = None, user_id: str | None = None) -> dict:
    cid, now = db.new_id(), db.now_iso()
    if document_id:
        ensure_document_exists(document_id, user_id)
    with db.connect() as conn:
        conn.execute("INSERT INTO conversations(id, title, document_id, created_at, updated_at, user_id) "
                     "VALUES(?, ?, ?, ?, ?, ?)", (cid, title[:TITLE_MAX], document_id, now, now, user_id))
    return get_conversation(cid, user_id)


def ensure_document_exists(document_id: str, user_id: str | None = None) -> None:
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM documents WHERE id = ? AND (? IS NULL OR user_id = ?)", (document_id, user_id, user_id)).fetchone() is None:
            raise NotFound("Document not found")


def get_conversation(conversation_id: str, user_id: str | None = None) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id = ? AND (? IS NULL OR user_id = ?)", (conversation_id, user_id, user_id)).fetchone()
    if row is None:
        raise NotFound("Conversation not found")
    return dict(row)


def list_conversations(user_id: str | None = None) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
            "FROM conversations c WHERE (? IS NULL OR c.user_id = ?) ORDER BY c.updated_at DESC", (user_id, user_id)).fetchall()
    return [dict(r) for r in rows]


def rename_conversation(conversation_id: str, title: str, user_id: str | None = None) -> dict:
    get_conversation(conversation_id, user_id)
    with db.connect() as conn:
        conn.execute("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND (? IS NULL OR user_id = ?)",
                     (title.strip()[:TITLE_MAX], db.now_iso(), conversation_id, user_id, user_id))
    return get_conversation(conversation_id, user_id)


def set_document_scope(conversation_id: str, document_id: str | None, user_id: str | None = None) -> dict:
    get_conversation(conversation_id, user_id)
    if document_id:
        ensure_document_exists(document_id, user_id)
    with db.connect() as conn:
        conn.execute("UPDATE conversations SET document_id = ? WHERE id = ? AND (? IS NULL OR user_id = ?)", (document_id, conversation_id, user_id, user_id))
    return get_conversation(conversation_id, user_id)


def delete_conversation(conversation_id: str, user_id: str | None = None) -> None:
    get_conversation(conversation_id, user_id)
    with db.connect() as conn:
        conn.execute("DELETE FROM conversations WHERE id = ? AND (? IS NULL OR user_id = ?)", (conversation_id, user_id, user_id))


def clear_messages(conversation_id: str, user_id: str | None = None) -> None:
    get_conversation(conversation_id, user_id)
    with db.connect() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("UPDATE conversations SET title = 'New chat', updated_at = ?, message_seq=0 WHERE id = ?",
                     (db.now_iso(), conversation_id))


def add_message(conversation_id: str, role: str, content: str, sources: list | None = None,
                follow_ups: list | None = None, grounded: bool | None = None,
                attachments: list | None = None, metrics: dict | None = None) -> dict:
    mid, now = db.new_id(), db.now_iso()
    with db.connect() as conn:
        # Updating the conversation row serializes concurrent writers and allocates a stable order.
        conn.execute("UPDATE conversations SET message_seq=message_seq+1 WHERE id=?", (conversation_id,))
        seq = conn.execute("SELECT message_seq FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if seq is None:
            raise NotFound("Conversation not found")
        message_order = seq["message_seq"]
        conn.execute(
            "INSERT INTO messages(id, conversation_id, role, content, sources, follow_ups, grounded, "
            "attachments, created_at, message_order) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, conversation_id, role, content, db.dumps(sources or []), db.dumps(follow_ups or []),
             None if grounded is None else int(grounded), db.dumps(attachments or []), now, message_order))
        if metrics:
            conn.execute(
                "INSERT INTO response_metrics(message_id, retrieval_ms, generation_ms, total_ms, llm_calls, "
                "passages, tokens, tokens_per_second, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, metrics.get("retrieval_ms"), metrics.get("generation_ms"), metrics.get("total_ms"),
                 metrics.get("llm_calls"), metrics.get("passages"), metrics.get("tokens"),
                 metrics.get("tokens_per_second"), now))
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
    return {"id": mid, "role": role, "content": content, "sources": sources or [],
            "follow_ups": follow_ups or [], "grounded": grounded, "attachments": attachments or [],
            "metrics": metrics or None, "created_at": now}


_METRIC_FIELDS = ("retrieval_ms", "generation_ms", "total_ms", "llm_calls", "passages", "tokens", "tokens_per_second")


def get_messages(conversation_id: str, user_id: str | None = None) -> list[dict]:
    get_conversation(conversation_id, user_id)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT m.*, r.retrieval_ms, r.generation_ms, r.total_ms, r.llm_calls, r.passages, r.tokens, "
            "r.tokens_per_second FROM messages m LEFT JOIN response_metrics r ON r.message_id = m.id "
            "WHERE m.conversation_id = ? ORDER BY m.message_order", (conversation_id,)).fetchall()
    return [{
        "id": r["id"], "role": r["role"], "content": r["content"],
        "sources": db.loads(r["sources"], []), "follow_ups": db.loads(r["follow_ups"], []),
        "grounded": None if r["grounded"] is None else bool(r["grounded"]),
        "attachments": db.loads(r["attachments"], []),
        "metrics": {k: r[k] for k in _METRIC_FIELDS} if r["total_ms"] is not None else None,
        "created_at": r["created_at"],
    } for r in rows]


def get_answer_with_question(message_id: str, user_id: str | None = None) -> dict:
    """An assistant message together with the user question that came right before it."""
    with db.connect() as conn:
        msg = conn.execute("SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id = ? AND (? IS NULL OR c.user_id = ?)", (message_id, user_id, user_id)).fetchone()
        if msg is None or msg["role"] != "assistant":
            raise NotFound("Message not found")
        question = conn.execute(
            "SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' AND message_order < ? "
            "ORDER BY message_order DESC LIMIT 1", (msg["conversation_id"], msg["message_order"])).fetchone()
    return {"id": msg["id"], "answer": msg["content"], "question": question["content"] if question else "",
            "sources": db.loads(msg["sources"], []), "follow_ups": db.loads(msg["follow_ups"], [])}


def save_follow_ups(message_id: str, follow_ups: list[str], user_id: str | None = None) -> None:
    get_answer_with_question(message_id, user_id)
    with db.connect() as conn:
        conn.execute("UPDATE messages SET follow_ups = ? WHERE id = ?", (db.dumps(follow_ups), message_id))


def recent_history(conversation_id: str, user_id: str | None = None) -> list[dict[str, str]]:
    """The last few messages as plain role/content dicts."""
    messages = get_messages(conversation_id, user_id)[-settings.max_history_messages:]
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def export_markdown(conversation_id: str, user_id: str | None = None) -> str:
    convo = get_conversation(conversation_id, user_id)
    lines = [f"# {convo['title']}", ""]
    for m in get_messages(conversation_id, user_id):
        lines.append(f"## {'You' if m['role'] == 'user' else 'Assistant'}")
        lines.append(m["content"])
        if m["sources"]:
            refs = "; ".join(f"{s['filename']}" + (f", p. {s['page']}" if s.get("page") else "")
                             for s in m["sources"])
            lines.append(f"\n*Sources: {refs}*")
        lines.append("")
    return "\n".join(lines)
