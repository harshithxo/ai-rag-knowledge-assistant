"""Service-layer tests: documents, conversations, chat and settings."""
import pytest
import functools

from app import db
from app.errors import NotFound, PayloadTooLarge, ValidationFailed
from app.services import (chat_service, conversation_service as convos, document_service,
                          ingestion_service as ing, settings_service)
from helpers import make_pdf


TEST_USER_ID = "service-test-user"


@pytest.fixture(autouse=True)
def authenticated_chat_owner(monkeypatch, clean_state):
    """Exercise the authenticated service contract with an owned test account."""
    with db.connect() as conn:
        conn.execute("INSERT INTO users(id,full_name,email,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                     (TEST_USER_ID, "Service Test User", "service-test@example.com", "test", db.now_iso(), db.now_iso()))
    original_chat, original_events = chat_service.chat, chat_service.chat_events

    @functools.wraps(original_chat)
    def owned_chat(*args, **kwargs):
        if len(args) < 5:
            kwargs.setdefault("user_id", TEST_USER_ID)
        return original_chat(*args, **kwargs)

    @functools.wraps(original_events)
    def owned_events(*args, **kwargs):
        if len(args) < 5:
            kwargs.setdefault("user_id", TEST_USER_ID)
        yield from original_events(*args, **kwargs)

    monkeypatch.setattr(chat_service, "chat", owned_chat)
    monkeypatch.setattr(chat_service, "chat_events", owned_events)


def add_doc(name="a.pdf", pages=("Interns receive a monthly stipend of 5000 rupees.", "Leave policy allows twelve days."), user_id=TEST_USER_ID):
    if user_id and user_id != TEST_USER_ID:
        with db.connect() as conn:
            conn.execute("INSERT INTO users(id,full_name,email,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                         (user_id, user_id, f"{user_id}@example.com", "test", db.now_iso(), db.now_iso()))
    row, _ = ing.register_upload(name, make_pdf(list(pages)), user_id)
    ing.process_document(row["id"], user_id)
    return row["id"]


# ----- documents -----
def test_list_and_get_document():
    doc_id = add_doc()
    docs = document_service.list_documents()
    assert len(docs) == 1 and docs[0]["status"] == "ready"
    assert "stored_name" not in docs[0] and "content_hash" not in docs[0]   # internals never leave the service
    assert document_service.get_document(doc_id)["filename"] == "a.pdf"


def test_delete_document_removes_file_vectors_and_record(store):
    from pathlib import Path
    from app.config import settings
    doc_id = add_doc()
    assert store.items and list(Path(settings.upload_dir).iterdir())
    document_service.delete_document(doc_id, TEST_USER_ID)
    assert store.items == {}
    assert not [path for path in Path(settings.upload_dir).rglob("*") if path.is_file()]
    with pytest.raises(NotFound):
        document_service.get_document(doc_id)


def test_delete_only_removes_the_target_documents_vectors(store):
    keep = add_doc("keep.pdf", pages=("different keep content",))
    drop = add_doc("drop.pdf", pages=("content to drop",))
    document_service.delete_document(drop, TEST_USER_ID)
    assert {m["document_id"] for _, m in store.items.values()} == {keep}


def test_delete_missing_document_raises():
    with pytest.raises(NotFound):
        document_service.delete_document("nope")


def test_vector_failure_keeps_document_so_it_can_be_retried(monkeypatch):
    doc_id = add_doc()
    def boom(*_args):
        raise RuntimeError("chroma down")
    monkeypatch.setattr(document_service, "delete_document_vectors", boom)
    with pytest.raises(RuntimeError):
        document_service.delete_document(doc_id)
    assert document_service.get_document(doc_id)["status"] == "ready"


def test_get_file_path_stays_inside_upload_dir():
    doc_id = add_doc()
    path = document_service.get_file_path(doc_id, TEST_USER_ID)
    assert path.suffix == ".pdf" and path.is_file()
    with db.connect() as conn:
        conn.execute("UPDATE documents SET stored_name = '../../etc/passwd' WHERE id = ?", (doc_id,))
    with pytest.raises(NotFound):
        document_service.get_file_path(doc_id, TEST_USER_ID)


def test_search_documents_finds_relevant_passage():
    add_doc(user_id="search-user")
    hits = document_service.search_documents("monthly stipend interns", user_id="search-user")
    assert hits and hits[0]["page"] == 1 and "stipend" in hits[0]["text"]


# ----- conversations -----
def test_conversation_lifecycle():
    c = convos.create_conversation()
    convos.add_message(c["id"], "user", "hi")
    convos.add_message(c["id"], "assistant", "hello", sources=[{"id": 1, "filename": "a.pdf", "page": 2}], follow_ups=["q?"], grounded=True)
    msgs = convos.get_messages(c["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["sources"][0]["page"] == 2 and msgs[1]["follow_ups"] == ["q?"] and msgs[1]["grounded"] is True
    assert convos.rename_conversation(c["id"], "  Budget talk ")["title"] == "Budget talk"
    assert convos.list_conversations()[0]["message_count"] == 2
    assert "## You" in convos.export_markdown(c["id"])
    convos.clear_messages(c["id"])
    assert convos.get_messages(c["id"]) == []
    convos.delete_conversation(c["id"])
    with pytest.raises(NotFound):
        convos.get_conversation(c["id"])


def test_chat_message_order_preserves_turns_and_followup_question():
    convo = convos.create_conversation()
    convos.add_message(convo["id"], "user", "first question")
    first_answer = convos.add_message(convo["id"], "assistant", "first answer", follow_ups=["ask this"])
    convos.add_message(convo["id"], "user", "second question")
    second_answer = convos.add_message(convo["id"], "assistant", "second answer")
    history = convos.get_messages(convo["id"])
    assert [m["content"] for m in history] == [
        "first question", "first answer", "second question", "second answer"]
    assert convos.get_answer_with_question(first_answer["id"])["question"] == "first question"
    assert convos.get_answer_with_question(second_answer["id"])["question"] == "second question"
    assert convos.get_messages(convo["id"])[1]["follow_ups"] == ["ask this"]
    with db.connect() as conn:
        orders = conn.execute("SELECT message_order FROM messages WHERE conversation_id=? ORDER BY message_order",
                              (convo["id"],)).fetchall()
    assert [row["message_order"] for row in orders] == [1, 2, 3, 4]


def test_deleting_conversation_deletes_messages():
    c = convos.create_conversation()
    convos.add_message(c["id"], "user", "hi")
    convos.delete_conversation(c["id"])
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0


def test_scoped_conversation_requires_existing_document():
    with pytest.raises(NotFound):
        convos.create_conversation(document_id="missing")


def test_deleting_document_unscopes_its_conversations():
    doc_id = add_doc()
    c = convos.create_conversation(document_id=doc_id, user_id=TEST_USER_ID)
    document_service.delete_document(doc_id, TEST_USER_ID)
    assert convos.get_conversation(c["id"], TEST_USER_ID)["document_id"] is None


# ----- chat -----
def test_chat_creates_conversation_titles_it_and_persists_messages():
    add_doc()
    result = chat_service.chat("What stipend do interns receive?", user_id=TEST_USER_ID)
    cid = result["conversation_id"]
    assert result["message"]["sources"][0]["filename"] == "a.pdf"
    assert convos.get_conversation(cid)["title"].startswith("What stipend")
    assert len(convos.get_messages(cid)) == 2


def test_chat_follow_up_uses_history(llm):
    add_doc()
    first = chat_service.chat("What stipend do interns receive?", user_id=TEST_USER_ID)
    chat_service.chat("And what about leave?", conversation_id=first["conversation_id"], user_id=TEST_USER_ID)
    rewrite_calls = [c for c in llm.calls if "standalone search question" in c[0].content]
    assert rewrite_calls and "stipend" in rewrite_calls[0][-1].content


def test_chat_with_no_documents_explains_what_to_do():
    result = chat_service.chat("What is in my files?", user_id=TEST_USER_ID)
    assert "no processed documents" in result["message"]["content"].lower()


def test_chat_only_searches_ready_documents(store):
    row, _ = ing.register_upload("pending.pdf", make_pdf(["secret pending text"]))   # still 'processing'
    result = chat_service.chat("secret pending text", user_id=TEST_USER_ID)
    assert "no processed documents" in result["message"]["content"].lower()


def test_chat_scoped_to_one_document():
    one = add_doc("one.pdf", pages=("alpha policy about travel",))
    add_doc("two.pdf", pages=("alpha policy about hiring",))
    result = chat_service.chat("alpha policy", document_id=one, user_id=TEST_USER_ID)
    assert {s["filename"] for s in result["message"]["sources"]} == {"one.pdf"}


def test_chat_rejects_blank_question():
    with pytest.raises(ValidationFailed):
        chat_service.chat("   ")


def test_failed_llm_call_does_not_save_half_a_conversation(monkeypatch):
    from app.errors import LLMUnavailable
    from app.services import llm_service
    add_doc()
    c = convos.create_conversation(user_id=TEST_USER_ID)
    def down(messages, temperature=None, **kwargs):
        raise LLMUnavailable("Cannot reach Ollama")
    monkeypatch.setattr(llm_service, "generate", down)
    monkeypatch.setattr(llm_service, "stream", down)
    with pytest.raises(LLMUnavailable):
        chat_service.chat("What stipend do interns receive?", conversation_id=c["id"])
    assert convos.get_messages(c["id"]) == []



def test_settings_default_update_and_validation():
    assert settings_service.get_int("retrieval_top_k") == 5
    assert settings_service.update({"retrieval_top_k": 8, "retrieval_min_score": 0.35})["retrieval_top_k"] == 8
    assert settings_service.get_float("retrieval_min_score") == 0.35
    with pytest.raises(ValidationFailed):
        settings_service.update({"retrieval_top_k": 99})
    with pytest.raises(ValidationFailed):
        settings_service.update({"api_key": "x"})


def test_threshold_setting_changes_retrieval():
    add_doc()
    settings_service.update({"retrieval_min_score": 0.9})
    assert document_service.search_documents("stipend", user_id=TEST_USER_ID) != []        # search ignores the threshold by design
    result = chat_service.chat("stipend interns policy unrelated words here", user_id=TEST_USER_ID)
    assert result["message"]["sources"] == []


# ----- streaming chat -----
def test_chat_events_stream_status_tokens_then_done_and_save_the_full_answer():
    add_doc()
    events = list(chat_service.chat_events("What stipend do interns receive?", user_id=TEST_USER_ID))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "status" and kinds[-1] == "done" and kinds.count("token") > 1
    done = events[-1]
    streamed = "".join(e["text"] for e in events if e["type"] == "token").strip()
    assert streamed.replace(" [Source 1]", "") == done["message"]["content"] == "The documents say so."
    saved = convos.get_messages(done["conversation_id"])
    assert [m["role"] for m in saved] == ["user", "assistant"] and saved[1]["content"] == done["message"]["content"]
    assert saved[1]["metrics"]["llm_calls"] == 1 and saved[1]["metrics"]["total_ms"] > 0


def test_stopping_a_stream_keeps_the_partial_answer():
    add_doc()
    stream = chat_service.chat_events("What stipend do interns receive?", user_id=TEST_USER_ID)
    for event in stream:
        if event["type"] == "token":
            break
    stream.close()                                    # what happens when the browser aborts the request
    convo = convos.list_conversations()[0]
    last = convos.get_messages(convo["id"])[-1]
    assert last["role"] == "assistant" and "(Response stopped)" in last["content"]


def test_attached_documents_scope_the_question_and_are_saved_with_the_message():
    one = add_doc("one.pdf", pages=("alpha policy about travel",))
    add_doc("two.pdf", pages=("alpha policy about hiring",))
    result = chat_service.chat("alpha policy", attachment_ids=[one], user_id=TEST_USER_ID)
    assert {s["filename"] for s in result["message"]["sources"]} == {"one.pdf"}
    user_message = convos.get_messages(result["conversation_id"])[0]
    assert user_message["attachments"] == [{"id": one, "filename": "one.pdf"}]


def test_unknown_attachment_ids_are_ignored_not_trusted():
    add_doc()
    result = chat_service.chat("What stipend do interns receive?", attachment_ids=["not-a-document"], user_id=TEST_USER_ID)
    assert "no processed documents" in result["message"]["content"].lower()


def test_new_conversation_is_not_created_when_the_llm_fails(monkeypatch):
    from app.errors import LLMUnavailable
    from app.services import llm_service
    add_doc()
    def down(messages, temperature=None, **kwargs):
        raise LLMUnavailable("Cannot reach Ollama")
    monkeypatch.setattr(llm_service, "stream", down)
    with pytest.raises(LLMUnavailable):
        chat_service.chat("What stipend do interns receive?", user_id=TEST_USER_ID)
    assert convos.list_conversations() == []          # no empty "New chat" left behind


# ----- follow-up suggestions (separate call, after the answer) -----
def test_follow_ups_are_generated_once_and_saved(llm):
    add_doc()
    result = chat_service.chat("What stipend do interns receive?", user_id=TEST_USER_ID)
    assert result["message"]["follow_ups"] == []                  # not part of the answer call
    calls_before = len(llm.calls)
    suggestions = chat_service.suggest_follow_ups(result["message"]["id"], user_id=TEST_USER_ID)
    assert suggestions == ["What is next?", "Why does it matter?", "Who is involved?"]
    assert len(llm.calls) == calls_before + 1
    assert chat_service.suggest_follow_ups(result["message"]["id"], user_id=TEST_USER_ID) == suggestions
    assert len(llm.calls) == calls_before + 1                     # cached: no second call
    assert convos.get_messages(result["conversation_id"])[1]["follow_ups"] == suggestions


def test_follow_ups_survive_garbage_and_skip_ungrounded_answers(llm, monkeypatch):
    add_doc()
    grounded = chat_service.chat("What stipend do interns receive?", user_id=TEST_USER_ID)
    original = llm.__call__
    monkeypatch.setattr(chat_service.llm_service, "generate", lambda m, temperature=None, max_tokens=None: "not json at all")
    assert chat_service.suggest_follow_ups(grounded["message"]["id"]) == []
    monkeypatch.setattr(chat_service.llm_service, "generate", original)
    calc = chat_service.chat("What is 20% of 500?")
    assert chat_service.suggest_follow_ups(calc["message"]["id"]) == []      # no sources -> no suggestions




def test_advanced_settings_persist_and_are_validated():
    saved = settings_service.update({"chunk_size": 800, "chunk_overlap": 100, "max_upload_size_mb": 5})
    assert saved["chunk_size"] == 800 and settings_service.get_int("max_upload_size_mb") == 5
    with pytest.raises(ValidationFailed, match="smaller"):
        settings_service.update({"chunk_overlap": 900})
    assert settings_service.get_int("chunk_overlap") == 100       # nothing was half-saved
    with pytest.raises(ValidationFailed):
        settings_service.update({"chunk_size": 50})


def test_upload_limit_and_chunk_size_settings_are_really_used():
    big = b"%PDF-" + b"0" * (int(1.5 * 1024 * 1024))
    with pytest.raises(PayloadTooLarge):
        ing.validate_pdf("big.pdf", big)                          # test default is 1 MB
    settings_service.update({"max_upload_size_mb": 2})
    ing.validate_pdf("big.pdf", big)
    settings_service.update({"chunk_size": 300, "chunk_overlap": 30})
    add_doc("small-chunks.pdf", pages=("word " * 400,))
    assert document_service.list_documents()[0]["chunks"] >= 6


# ----- documents -----
def test_document_deleted_while_indexing_leaves_no_vectors(store, monkeypatch):
    row, _ = ing.register_upload("racing.pdf", make_pdf(["text that will be indexed"]))
    original = store.add_documents
    def add_then_user_deletes(docs, ids=None):
        original(docs, ids=ids)
        with db.connect() as conn:
            conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
    monkeypatch.setattr(store, "add_documents", add_then_user_deletes)
    ing.process_document(row["id"])
    assert store.items == {}


def test_old_database_is_upgraded_without_losing_data():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, document_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                               sources TEXT, follow_ups TEXT, grounded INTEGER, created_at TEXT NOT NULL);
        INSERT INTO conversations VALUES ('c1', 'Old chat', NULL, 'now', 'now');
        INSERT INTO messages VALUES ('m1', 'c1', 'user', 'old question', '[]', '[]', NULL, 'now');""")
    db._prepare(conn)
    assert conn.execute("SELECT content FROM messages WHERE id = 'm1'").fetchone()["content"] == "old question"
    assert "attachments" in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    assert conn.execute("SELECT COUNT(*) FROM response_metrics").fetchone()[0] == 0
    db._prepare(conn)                                             # running it twice is harmless
