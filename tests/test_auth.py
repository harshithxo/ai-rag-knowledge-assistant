"""Authentication unit tests; email delivery is captured and never reaches a real provider."""
import re
import pytest
from app import db
from app.errors import Conflict, ValidationFailed
from app.services import auth_service

@pytest.fixture(autouse=True)
def clean_auth_db(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "auth-test.db"))
    db._initialized.discard(settings.database_path)
    db.init_db()
    with db.connect() as conn:
        for table in ("email_verification_tokens", "password_reset_tokens", "sessions", "auth_rate_limits", "documents", "conversations", "users"):
            conn.execute(f"DELETE FROM {table}")


def capture_email(monkeypatch):
    sent=[]
    monkeypatch.setattr(auth_service, "_send_email", lambda to, subject, body: sent.append((to,subject,body)))
    return sent


def token_from(sent):
    match=re.search(r"[?](?:verify_email|reset_password)=([^\s]+)",sent[-1][2])
    assert match
    return match.group(1)


def test_registration_hashes_password_and_verification_is_single_use(monkeypatch):
    sent=capture_email(monkeypatch)
    auth_service.register("Test User","TEST@example.com","secure-password-123")
    with db.connect() as conn:
        user=conn.execute("SELECT * FROM users WHERE email='test@example.com'").fetchone()
        token=conn.execute("SELECT * FROM email_verification_tokens WHERE user_id=?",(user["id"],)).fetchone()
    assert user["password_hash"] != "secure-password-123"
    assert token["token_hash"] != token_from(sent)
    auth_service.verify_email(token_from(sent))
    with pytest.raises(ValidationFailed): auth_service.verify_email(token_from(sent))


def test_duplicate_registration_and_weak_password(monkeypatch):
    capture_email(monkeypatch)
    auth_service.register("Test User","test@example.com","secure-password-123")
    with pytest.raises(Conflict): auth_service.register("Other User","TEST@example.com","secure-password-123")
    with pytest.raises(ValidationFailed): auth_service.register("Other User","other@example.com","short")


def test_login_and_logout_create_revocable_opaque_session(monkeypatch):
    capture_email(monkeypatch)
    auth_service.register("Test User","test@example.com","secure-password-123")
    with db.connect() as conn:
        row=conn.execute("SELECT token_hash FROM email_verification_tokens").fetchone()
    # Verify using a captured token from the mocked email body.
    sent=[]
    monkeypatch.setattr(auth_service,"_send_email",lambda to,subject,body:sent.append(body))
    auth_service.resend_verification("test@example.com")
    auth_service.verify_email(token_from([(None,None,sent[-1])]))
    user,session,csrf=auth_service.login("test@example.com","secure-password-123")
    assert auth_service.current_user(session)["id"]==user["id"]
    assert auth_service.csrf_valid(auth_service.current_user(session),csrf)
    auth_service.logout(session)
    assert auth_service.current_user(session) is None


def test_user_scoping_hides_other_users_documents():
    with db.connect() as conn:
        for uid in ("user-a","user-b"):
            conn.execute("INSERT INTO users(id,full_name,email,password_hash,is_verified,is_active,role,created_at,updated_at) VALUES(?,?,?,?,1,1,'user',?,?)",
                         (uid,uid,uid+"@example.com","hash",db.now_iso(),db.now_iso()))
        conn.execute("INSERT INTO documents(id,filename,stored_name,size_bytes,content_hash,status,created_at,user_id) VALUES('doc-b','private.pdf','private.pdf',1,'hash','ready',?,'user-b')",(db.now_iso(),))
    from app.services.document_service import get_document
    with pytest.raises(Exception): get_document("doc-b","user-a")
    assert get_document("doc-b","user-b")["filename"]=="private.pdf"


def test_password_reset_is_single_use_and_revokes_existing_sessions(monkeypatch):
    sent = capture_email(monkeypatch)
    auth_service.register("Test User", "test@example.com", "secure-password-123")
    verify_token = token_from(sent)
    auth_service.verify_email(verify_token)
    _, session, _ = auth_service.login("test@example.com", "secure-password-123")

    auth_service.forgot_password("test@example.com")
    reset_token = token_from(sent)
    auth_service.reset_password(reset_token, "new-secure-password-456")
    assert auth_service.current_user(session) is None
    with pytest.raises(ValidationFailed):
        auth_service.reset_password(reset_token, "another-password-789")
    with pytest.raises(ValidationFailed):
        auth_service.login("test@example.com", "secure-password-123")
    user, _, _ = auth_service.login("test@example.com", "new-secure-password-456")
    assert user["email"] == "test@example.com"


def test_change_password_requires_current_password_and_revokes_sessions(monkeypatch):
    sent = capture_email(monkeypatch)
    auth_service.register("Test User", "test@example.com", "secure-password-123")
    auth_service.verify_email(token_from(sent))
    user, session, _ = auth_service.login("test@example.com", "secure-password-123")

    with pytest.raises(ValidationFailed):
        auth_service.change_password(user["id"], "wrong-password", "new-secure-password-456")
    auth_service.change_password(user["id"], "secure-password-123", "new-secure-password-456")
    assert auth_service.current_user(session) is None
    assert auth_service.login("test@example.com", "new-secure-password-456")[0]["id"] == user["id"]
