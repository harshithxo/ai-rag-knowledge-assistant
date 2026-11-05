"""HTTP tests using FastAPI's TestClient. The vector store and LLM are fakes (see conftest.py),
so these cover routing, validation, status codes and the shape of the JSON."""
import io
import json

from fastapi.testclient import TestClient

from app.main import app
from helpers import make_pdf

client = TestClient(app)

import pytest
from argon2 import PasswordHasher
from app import db

@pytest.fixture(autouse=True)
def authenticated_session():
    client.cookies.clear()
    with db.connect() as conn:
        conn.execute("INSERT INTO users(id,full_name,email,password_hash,is_verified,is_active,role,created_at,updated_at) VALUES(?,?,?,?,1,1,'user',?,?)",
                     ("test-user", "Test User", "test@example.com", PasswordHasher().hash("test-password-123"), db.now_iso(), db.now_iso()))
    from app.services.auth_service import login
    user, token, csrf = login("test@example.com", "test-password-123")
    client.cookies.set("docmind_session", token)
    client.cookies.set("docmind_csrf", csrf)
    client.headers.update({"X-CSRF-Token": csrf})
    yield
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)


def test_registration_verification_login_and_logout_flow(monkeypatch):
    import re
    from app.services import auth_service
    sent = []
    monkeypatch.setattr(auth_service, "_send_email", lambda to, subject, body: sent.append(body))
    browser = TestClient(app)

    created = browser.post("/auth/register", json={
        "full_name": "New User", "email": "new-user@example.com",
        "password": "secure-password-123", "confirm_password": "secure-password-123",
    })
    assert created.status_code == 201
    match = re.search(r"verify_email=([^\s]+)", sent[-1])
    assert match
    token = match.group(1)

    verified = browser.post("/auth/verify-email", json={"token": token})
    assert verified.status_code == 200
    login = browser.post("/auth/login", json={
        "email": "new-user@example.com", "password": "secure-password-123",
    })
    assert login.status_code == 200
    csrf = login.json()["csrf_token"]
    browser.headers.update({"X-CSRF-Token": csrf})
    assert browser.get("/auth/me").json()["user"]["email"] == "new-user@example.com"
    assert browser.post("/auth/logout").json()["success"] is True
    assert browser.get("/auth/me").status_code == 401


def test_private_endpoints_reject_anonymous_clients():
    anonymous = TestClient(app)
    assert anonymous.get("/documents").status_code == 401
    assert anonymous.get("/conversations").status_code == 401
    assert anonymous.get("/settings").status_code == 401


def upload(*files):
    return client.post("/documents/upload", files=[("files", f) for f in files])


def pdf_file(name="notes.pdf", text="Interns receive a monthly stipend of 5000 rupees."):
    return (name, io.BytesIO(make_pdf([text])), "application/pdf")


# ----- basics -----
def test_authenticated_post_routes_require_csrf_token():
    csrf = client.headers.pop("X-CSRF-Token")
    try:
        response = client.post("/chat/follow-ups", json={"message_id": "not-a-real-message"})
        assert response.status_code == 403
    finally:
        client.headers.update({"X-CSRF-Token": csrf})


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "ollama" in response.json()


def test_chat_validation():
    response = client.post("/chat", json={"question": ""})
    assert response.status_code == 422


# ----- documents -----
def test_upload_list_preview_delete_roundtrip(store):
    res = upload(pdf_file())
    assert res.status_code == 200
    body = res.json()
    assert body["errors"] == [] and body["results"][0]["duplicate"] is False
    doc_id = body["results"][0]["document"]["id"]

    # TestClient runs background tasks before returning, so indexing has finished.
    docs = client.get("/documents").json()["documents"]
    assert docs[0]["status"] == "ready" and docs[0]["chunks"] >= 1 and docs[0]["filename"] == "notes.pdf"
    assert store.items
    # Uploaded chunks must carry the authenticated account ID for Chroma filtering.
    assert all(meta.get("user_id") == "test-user" for _, meta in store.items.values())

    preview = client.get(f"/documents/{doc_id}/file")
    assert preview.status_code == 200 and preview.headers["content-type"] == "application/pdf"
    assert preview.content.startswith(b"%PDF-") and "inline" in preview.headers["content-disposition"]

    assert client.delete(f"/documents/{doc_id}").status_code == 200
    assert client.get("/documents").json()["documents"] == []
    assert store.items == {}                        # removed from the vector database too
    assert client.get(f"/documents/{doc_id}/file").status_code == 404




def test_accounts_cannot_access_each_others_documents_or_conversations(store):
    upload(pdf_file("private.pdf", "private account A content"))
    doc = client.get("/documents").json()["documents"][0]
    conversation = client.post("/conversations", json={"title": "Private chat"}).json()

    # Create a second verified account and authenticate a separate browser session.
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO users(id,full_name,email,password_hash,is_verified,is_active,role,created_at,updated_at) "
            "VALUES(?,?,?,?,1,1,'user',?,?)",
            ("user-b", "User B", "user-b@example.com", PasswordHasher().hash("another-password-123"),
             db.now_iso(), db.now_iso()),
        )
    from app.services.auth_service import login
    _, token, csrf = login("user-b@example.com", "another-password-123")
    other = TestClient(app)
    other.cookies.set("docmind_session", token)
    other.cookies.set("docmind_csrf", csrf)
    other.headers.update({"X-CSRF-Token": csrf})

    assert other.get("/documents").json()["documents"] == []
    assert other.get(f"/documents/{doc['id']}").status_code == 404
    assert other.get(f"/documents/{doc['id']}/file").status_code == 404
    assert other.delete(f"/documents/{doc['id']}").status_code == 404
    assert other.get(f"/conversations/{conversation['id']}").status_code == 404
    assert other.post("/conversations", json={"title": "Invalid scope", "document_id": doc["id"]}).status_code == 404
    # The original account still owns and can access its document and conversation.
    assert client.get(f"/documents/{doc['id']}").status_code == 200
    assert client.get(f"/conversations/{conversation['id']}").status_code == 200


def test_upload_multiple_files_at_once():
    res = upload(pdf_file("a.pdf", "first document text"), pdf_file("b.pdf", "second document text"))
    assert len(res.json()["results"]) == 2
    assert len(client.get("/documents").json()["documents"]) == 2


def test_duplicate_upload_is_not_reprocessed(store):
    upload(pdf_file())
    count = len(store.items)
    res = upload(pdf_file("same-content-other-name.pdf"))
    assert res.json()["results"][0]["duplicate"] is True
    assert len(client.get("/documents").json()["documents"]) == 1 and len(store.items) == count


def test_upload_rejects_bad_files_with_clear_messages():
    res = upload(("notes.txt", io.BytesIO(b"hello"), "text/plain"))
    assert res.json()["errors"][0]["detail"] == "Only PDF files are supported"
    res = upload(("fake.pdf", io.BytesIO(b"not a pdf"), "application/pdf"))
    assert "valid PDF" in res.json()["errors"][0]["detail"]
    res = upload(("empty.pdf", io.BytesIO(b""), "application/pdf"))
    assert "empty" in res.json()["errors"][0]["detail"]
    big = ("big.pdf", io.BytesIO(b"%PDF-" + b"0" * (2 * 1024 * 1024)), "application/pdf")
    assert "MB" in upload(big).json()["errors"][0]["detail"]
    assert client.get("/documents").json()["documents"] == []


def test_one_bad_file_does_not_block_the_others():
    res = upload(("bad.txt", io.BytesIO(b"x"), "text/plain"), pdf_file())
    assert len(res.json()["results"]) == 1 and len(res.json()["errors"]) == 1


def test_corrupt_pdf_is_reported_as_failed_document_and_can_be_deleted():
    res = upload(("broken.pdf", io.BytesIO(b"%PDF-1.4 garbage"), "application/pdf"))
    doc = client.get("/documents").json()["documents"][0]
    assert doc["status"] == "failed" and doc["error"]
    assert client.delete(f"/documents/{doc['id']}").status_code == 200


def test_unknown_document_returns_404():
    assert client.get("/documents/nope").status_code == 404
    assert client.delete("/documents/nope").status_code == 404
    assert client.get("/documents/nope/file").status_code == 404


def test_path_traversal_attempts_do_not_leak_files():
    assert client.get("/documents/..%2F..%2Fetc%2Fpasswd/file").status_code in (404, 422)


def test_document_search_endpoint():
    upload(pdf_file())
    res = client.get("/documents/search", params={"q": "monthly stipend"})
    assert res.status_code == 200 and res.json()["results"][0]["filename"] == "notes.pdf"
    assert client.get("/documents/search", params={"q": "a"}).status_code == 422


# ----- chat + conversations -----
def test_chat_returns_answer_sources_and_persists():
    upload(pdf_file())
    res = client.post("/chat", json={"question": "What stipend do interns receive?"})
    assert res.status_code == 200
    body = res.json()
    assert body["message"]["sources"][0]["filename"] == "notes.pdf"
    convo = client.get(f"/conversations/{body['conversation_id']}").json()
    assert [m["role"] for m in convo["messages"]] == ["user", "assistant"]
    assert convo["title"].startswith("What stipend")


def test_chat_unknown_conversation_is_404():
    assert client.post("/chat", json={"question": "hi there", "conversation_id": "nope"}).status_code == 404


def test_chat_rejects_oversized_and_blank_questions():
    assert client.post("/chat", json={"question": "x" * 4001}).status_code == 422
    assert client.post("/chat", json={"question": "   "}).status_code == 422


def test_llm_outage_returns_friendly_503(monkeypatch):
    from app.errors import LLMUnavailable
    from app.services import llm_service
    upload(pdf_file())
    def down(messages, temperature=None, **kwargs):
        raise LLMUnavailable("Cannot reach Ollama at http://localhost:11434. Start it with 'ollama serve' and try again.")
    monkeypatch.setattr(llm_service, "generate", down)
    monkeypatch.setattr(llm_service, "stream", down)
    res = client.post("/chat", json={"question": "What stipend do interns receive?"})
    assert res.status_code == 503 and "ollama serve" in res.json()["detail"]


def test_conversation_crud_export_and_clear():
    created = client.post("/conversations", json={"title": "Planning"})
    assert created.status_code == 201
    cid = created.json()["id"]
    assert client.patch(f"/conversations/{cid}", json={"title": "Renamed"}).json()["title"] == "Renamed"
    client.post("/chat", json={"question": "hello", "conversation_id": cid})
    assert "# Renamed" in client.get(f"/conversations/{cid}/export").text
    assert client.post(f"/conversations/{cid}/clear").status_code == 200
    assert client.get(f"/conversations/{cid}").json()["messages"] == []
    assert any(c["id"] == cid for c in client.get("/conversations").json()["conversations"])
    assert client.delete(f"/conversations/{cid}").status_code == 200
    assert client.get(f"/conversations/{cid}").status_code == 404


def test_conversation_validation():
    assert client.post("/conversations", json={"title": ""}).status_code == 422
    cid = client.post("/conversations", json={}).json()["id"]
    assert client.patch(f"/conversations/{cid}", json={"title": "x" * 200}).status_code == 422
    assert client.patch(f"/conversations/{cid}", json={"document_id": "missing"}).status_code == 404


def test_document_scoped_conversation():
    upload(pdf_file("one.pdf", "alpha policy about travel"), pdf_file("two.pdf", "alpha policy about hiring"))
    docs = {d["filename"]: d["id"] for d in client.get("/documents").json()["documents"]}
    cid = client.post("/conversations", json={"document_id": docs["two.pdf"]}).json()["id"]
    body = client.post("/chat", json={"question": "alpha policy", "conversation_id": cid}).json()
    assert {s["filename"] for s in body["message"]["sources"]} == {"two.pdf"}


# ----- settings -----
def test_settings_endpoints():
    current = client.get("/settings").json()
    assert "api_key" not in str(current).lower()
    assert client.patch("/settings", json={"retrieval_top_k": 7}).json()["retrieval_top_k"] == 7
    assert client.patch("/settings", json={"retrieval_top_k": 500}).status_code == 422
    assert client.patch("/settings", json={"retrieval_min_score": 2}).status_code == 422


def test_advanced_settings_roundtrip_and_read_only_values():
    saved = client.patch("/settings", json={"chunk_size": 900, "chunk_overlap": 90, "max_upload_size_mb": 3,
                                            "llm_temperature": 0.4}).json()
    assert saved["chunk_size"] == 900 and saved["max_upload_size_mb"] == 3
    current = client.get("/settings").json()                      # persisted, not just echoed
    assert current["chunk_overlap"] == 90 and current["llm_temperature"] == 0.4
    assert {"llm_model", "embedding_model", "ollama_base_url"} <= current.keys()
    assert client.patch("/settings", json={"chunk_overlap": 950, "chunk_size": 900}).status_code == 400
    assert client.patch("/settings", json={"ollama_base_url": "http://evil"}).status_code == 200   # unknown field ignored
    assert client.get("/settings").json()["ollama_base_url"] != "http://evil"


def test_frontend_is_served():
    page = client.get("/").text
    assert "DocMind" in page
    for path in ("/markdown.js", "/js/core.js", "/js/chat.js", "/js/library.js",
                 "/js/settings.js", "/js/main.js", "/style.css"):
        assert client.get(path).status_code == 200, path
    # App scripts are intentionally loaded after authentication by auth.js.
    loader = client.get("/auth.js").text
    assert "markdown.js" in loader
    for path in ("js/core.js", "js/chat.js", "js/library.js", "js/settings.js", "js/main.js"):
        assert path in loader
    assert "/auth.js" in page


# ----- streaming -----
def read_events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_chat_stream_sends_ndjson_events_and_saves_the_conversation():
    upload(pdf_file())
    res = client.post("/chat/stream", json={"question": "What stipend do interns receive?"})
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/x-ndjson")
    events = read_events(res)
    assert events[0]["type"] == "status" and events[-1]["type"] == "done"
    assert sum(e["type"] == "token" for e in events) > 1
    done = events[-1]
    assert done["message"]["sources"][0]["filename"] == "notes.pdf" and done["message"]["metrics"]["llm_calls"] == 1
    saved = client.get(f"/conversations/{done['conversation_id']}").json()["messages"]
    assert saved[1]["content"] == done["message"]["content"]


def test_chat_stream_reports_errors_as_an_event(monkeypatch):
    from app.errors import LLMUnavailable
    from app.services import llm_service
    upload(pdf_file())
    def down(messages, temperature=None, **kwargs):
        raise LLMUnavailable("Cannot reach Ollama. Start it with 'ollama serve'.")
    monkeypatch.setattr(llm_service, "stream", down)
    events = read_events(client.post("/chat/stream", json={"question": "What stipend do interns receive?"}))
    assert events[-1]["type"] == "error" and "ollama serve" in events[-1]["detail"]
    assert client.get("/conversations").json()["conversations"] == []        # nothing half-saved


def test_chat_stream_with_attachment_searches_only_the_attached_file():
    upload(pdf_file("one.pdf", "alpha policy about travel"), pdf_file("two.pdf", "alpha policy about hiring"))
    docs = {d["filename"]: d["id"] for d in client.get("/documents").json()["documents"]}
    events = read_events(client.post("/chat/stream", json={"question": "alpha policy", "attachment_ids": [docs["two.pdf"]]}))
    done = events[-1]
    assert {s["filename"] for s in done["message"]["sources"]} == {"two.pdf"}
    assert done["message"]["follow_ups"] == []
    user = client.get(f"/conversations/{done['conversation_id']}").json()["messages"][0]
    assert user["attachments"][0]["filename"] == "two.pdf"


def test_chat_stream_validation_and_follow_up_endpoint():
    assert client.post("/chat/stream", json={"question": ""}).status_code == 422
    upload(pdf_file())
    done = read_events(client.post("/chat/stream", json={"question": "What stipend do interns receive?"}))[-1]
    res = client.post("/chat/follow-ups", json={"message_id": done["message"]["id"]})
    assert res.status_code == 200 and len(res.json()["follow_ups"]) == 3
    assert client.post("/chat/follow-ups", json={"message_id": "nope"}).status_code == 404
