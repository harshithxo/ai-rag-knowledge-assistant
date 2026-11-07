
"""Authentication primitives: Argon2 passwords, hashed one-time tokens and opaque DB sessions."""

import hashlib
import logging
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

from app import db
from app.config import settings
from app.errors import Conflict, NotFound, ValidationFailed


logger = logging.getLogger(__name__)
_hasher = PasswordHasher()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _rate_limit(
    scope: str,
    subject: str,
    limit: int,
    window_minutes: int = 15,
) -> None:
    key = _hash(subject.strip().lower())
    now = datetime.now(timezone.utc)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT window_started,attempts FROM auth_rate_limits "
            "WHERE scope=? AND subject_hash=?",
            (scope, key),
        ).fetchone()

        if (
            not row
            or now - datetime.fromisoformat(row["window_started"])
            >= timedelta(minutes=window_minutes)
        ):
            conn.execute(
                "INSERT INTO auth_rate_limits"
                "(scope,subject_hash,window_started,attempts) "
                "VALUES(?,?,?,1) "
                "ON CONFLICT(scope,subject_hash) DO UPDATE SET "
                "window_started=excluded.window_started,attempts=1",
                (scope, key, now.isoformat()),
            )
        else:
            if row["attempts"] >= limit:
                raise ValidationFailed(
                    "Too many attempts. Please wait and try again later."
                )

            conn.execute(
                "UPDATE auth_rate_limits SET attempts=attempts+1 "
                "WHERE scope=? AND subject_hash=?",
                (scope, key),
            )


def _date(minutes: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=minutes)
    ).isoformat()


def _send_email(to: str, subject: str, body: str) -> None:
    provider = settings.email_provider.lower()

    # Local development email
    if provider == "console" and settings.app_env.lower() != "production":
        logger.info(
            "Development email to %s | %s\n%s",
            to,
            subject,
            body,
        )
        return

    # SMTP provider
    if provider == "smtp":
        if not settings.smtp_host or not settings.email_from:
            raise RuntimeError(
                "SMTP_HOST and EMAIL_FROM are required "
                "when EMAIL_PROVIDER=smtp"
            )

        if bool(settings.smtp_username) != bool(settings.smtp_password):
            raise RuntimeError(
                "Set both SMTP_USERNAME and SMTP_PASSWORD, "
                "or leave both empty for unauthenticated SMTP"
            )

        msg = EmailMessage()
        msg["From"] = settings.email_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        try:
            with smtplib.SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=20,
            ) as server:
                server.ehlo()

                if settings.smtp_starttls:
                    server.starttls(
                        context=ssl.create_default_context()
                    )
                    server.ehlo()

                if settings.smtp_username:
                    server.login(
                        settings.smtp_username,
                        settings.smtp_password,
                    )

                server.send_message(msg)

        except (OSError, smtplib.SMTPException):
            logger.exception(
                "SMTP delivery failed via %s:%s; "
                "check host, port, TLS and credentials",
                settings.smtp_host,
                settings.smtp_port,
            )
            raise

        return

    # Brevo HTTPS API
    if provider == "brevo":
        sender_email = parseaddr(settings.email_from)[1]

        if not sender_email:
            raise RuntimeError(
                "A valid EMAIL_FROM address is required"
            )

        if not settings.brevo_api_key:
            raise RuntimeError(
                "BREVO_API_KEY is required"
            )

        payload = {
            "sender": {
                "name": "DocMind",
                "email": sender_email,
            },
            "to": [
                {"email": to}
            ],
            "subject": subject,
            "textContent": body,
        }

        try:
            response = httpx.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={
                    "api-key": settings.brevo_api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=20,
            )
            response.raise_for_status()

        except httpx.HTTPError:
            logger.exception("Brevo email delivery failed")
            raise RuntimeError(
                "Unable to send email through Brevo"
            ) from None

        return

    # Resend provider
    if provider == "resend":
        import resend

        resend.api_key = settings.resend_api_key

        resend.Emails.send({
            "from": settings.email_from,
            "to": [to],
            "subject": subject,
            "text": body,
        })

        return

    raise RuntimeError("Email provider is not configured")


def _issue_token(
    table: str,
    user_id: str,
    expiry: int,
    purpose: str,
) -> str:
    token = secrets.token_urlsafe(32)

    with db.connect() as conn:
        conn.execute(
            f"UPDATE {table} SET used_at=? "
            "WHERE user_id=? AND used_at IS NULL",
            (db.now_iso(), user_id),
        )

        conn.execute(
            f"INSERT INTO {table}"
            "(id,user_id,token_hash,expires_at,created_at) "
            "VALUES(?,?,?,?,?)",
            (
                db.new_id(),
                user_id,
                _hash(token),
                _date(expiry),
                db.now_iso(),
            ),
        )

    url = (
        f"{settings.frontend_url.rstrip('/')}/"
        f"?{purpose}={token}"
    )

    _send_email(
        _user_email(user_id),
        "DocMind " + purpose.replace("_", " ").title(),
        (
            f"Use this one-time link within {expiry} minutes:\n"
            f"{url}\n"
            "If you did not request this, ignore this email."
        ),
    )

    return token


def _user_email(user_id: str) -> str:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT email FROM users WHERE id=?",
            (user_id,),
        ).fetchone()

    return row["email"]


def register(full_name: str, email: str, password: str) -> None:
    email = email.strip().lower()

    _rate_limit("register", email, 5, 60)

    if len(full_name.strip()) < 2:
        raise ValidationFailed("Enter your full name")

    if (
        len(password) < 10
        or not any(c.isalpha() for c in password)
        or not any(c.isdigit() for c in password)
    ):
        raise ValidationFailed(
            "Password must be at least 10 characters "
            "and include a letter and a number"
        )

    uid = db.new_id()
    now = db.now_iso()

    try:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO users"
                "(id,full_name,email,password_hash,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    uid,
                    full_name.strip()[:100],
                    email,
                    _hasher.hash(password),
                    now,
                    now,
                ),
            )

    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise Conflict(
                "An account with this email already exists"
            ) from None
        raise

    _issue_token(
        "email_verification_tokens",
        uid,
        settings.email_verification_expire_minutes,
        "verify_email",
    )


def verify_email(token: str) -> None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM email_verification_tokens "
            "WHERE token_hash=? AND used_at IS NULL",
            (_hash(token),),
        ).fetchone()

        if (
            not row
            or datetime.fromisoformat(row["expires_at"])
            < datetime.now(timezone.utc)
        ):
            raise ValidationFailed(
                "Verification link is invalid or expired"
            )

        conn.execute(
            "UPDATE users SET is_verified=1,updated_at=? WHERE id=?",
            (db.now_iso(), row["user_id"]),
        )

        conn.execute(
            "UPDATE email_verification_tokens SET used_at=? WHERE id=?",
            (db.now_iso(), row["id"]),
        )


def resend_verification(email: str) -> None:
    _rate_limit("resend_verification", email, 3, 60)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT id,is_verified FROM users WHERE email=?",
            (email.strip().lower(),),
        ).fetchone()

    if row and not row["is_verified"]:
        _issue_token(
            "email_verification_tokens",
            row["id"],
            settings.email_verification_expire_minutes,
            "verify_email",
        )


def login(email: str, password: str) -> tuple[dict, str, str]:
    _rate_limit("login", email, 10, 15)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email=?",
            (email.strip().lower(),),
        ).fetchone()

    if not row:
        # Reduce timing difference for unknown accounts.
        try:
            _hasher.verify(
                _hasher.hash("dummy-password-value"),
                password,
            )
        except Exception:
            pass

        raise ValidationFailed(
            "Email or password is incorrect. "
            "If this is a new account, use the verification link sent by email."
        )

    try:
        valid = _hasher.verify(row["password_hash"], password)
    except (VerifyMismatchError, InvalidHash):
        valid = False

    if not valid or not row["is_active"] or not row["is_verified"]:
        raise ValidationFailed(
            "Email or password is incorrect. "
            "If this is a new account, use the verification link sent by email."
        )

    raw = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    now = db.now_iso()

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sessions"
            "(id,user_id,session_token_hash,csrf_token_hash,"
            "expires_at,created_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                db.new_id(),
                row["id"],
                _hash(raw),
                _hash(csrf),
                _date(settings.session_expire_minutes),
                now,
                now,
            ),
        )

    return (
        {
            "id": row["id"],
            "full_name": row["full_name"],
            "email": row["email"],
            "role": row["role"],
        },
        raw,
        csrf,
    )


def current_user(raw: str | None) -> dict | None:
    if not raw:
        return None

    with db.connect() as conn:
        row = conn.execute(
            "SELECT u.*,s.id AS session_id,s.csrf_token_hash "
            "FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.session_token_hash=? "
            "AND s.revoked_at IS NULL AND s.expires_at>?",
            (_hash(raw), db.now_iso()),
        ).fetchone()

        if row:
            conn.execute(
                "UPDATE sessions SET last_seen_at=? WHERE id=?",
                (db.now_iso(), row["session_id"]),
            )

    return (
        dict(row)
        if row and row["is_active"] and row["is_verified"]
        else None
    )


def logout(raw: str | None) -> None:
    if raw:
        with db.connect() as conn:
            conn.execute(
                "UPDATE sessions SET revoked_at=? "
                "WHERE session_token_hash=? AND revoked_at IS NULL",
                (db.now_iso(), _hash(raw)),
            )


def csrf_valid(user: dict, token: str | None) -> bool:
    return bool(
        token
        and secrets.compare_digest(
            user["csrf_token_hash"],
            _hash(token),
        )
    )


def forgot_password(email: str) -> None:
    _rate_limit("forgot_password", email, 3, 60)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email=? AND is_active=1",
            (email.strip().lower(),),
        ).fetchone()

    if row:
        _issue_token(
            "password_reset_tokens",
            row["id"],
            settings.password_reset_expire_minutes,
            "reset_password",
        )


def reset_password(token: str, password: str) -> None:
    _rate_limit("reset_password", token, 5, 30)

    if (
        len(password) < 10
        or not any(c.isalpha() for c in password)
        or not any(c.isdigit() for c in password)
    ):
        raise ValidationFailed(
            "Password must be at least 10 characters "
            "and include a letter and a number"
        )

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM password_reset_tokens "
            "WHERE token_hash=? AND used_at IS NULL",
            (_hash(token),),
        ).fetchone()

        if (
            not row
            or datetime.fromisoformat(row["expires_at"])
            < datetime.now(timezone.utc)
        ):
            raise ValidationFailed(
                "Reset link is invalid or expired"
            )

        conn.execute(
            "UPDATE users SET password_hash=?,updated_at=? WHERE id=?",
            (
                _hasher.hash(password),
                db.now_iso(),
                row["user_id"],
            ),
        )

        conn.execute(
            "UPDATE password_reset_tokens SET used_at=? WHERE id=?",
            (db.now_iso(), row["id"]),
        )

        conn.execute(
            "UPDATE sessions SET revoked_at=? "
            "WHERE user_id=? AND revoked_at IS NULL",
            (db.now_iso(), row["user_id"]),
        )


def profile(user_id: str) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id,full_name,email,is_verified,created_at "
            "FROM users WHERE id=?",
            (user_id,),
        ).fetchone()

    if not row:
        raise NotFound("Account not found")

    return dict(row)


def update_profile(user_id: str, full_name: str) -> dict:
    if len(full_name.strip()) < 2:
        raise ValidationFailed("Enter your full name")

    with db.connect() as conn:
        conn.execute(
            "UPDATE users SET full_name=?,updated_at=? WHERE id=?",
            (
                full_name.strip()[:100],
                db.now_iso(),
                user_id,
            ),
        )

    return profile(user_id)


def change_password(
    user_id: str,
    current_password: str,
    new_password: str,
) -> None:
    if (
        len(new_password) < 10
        or not any(c.isalpha() for c in new_password)
        or not any(c.isdigit() for c in new_password)
    ):
        raise ValidationFailed(
            "Password must be at least 10 characters "
            "and include a letter and a number"
        )

    with db.connect() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id=?",
            (user_id,),
        ).fetchone()

        if not row:
            raise NotFound("Account not found")

        try:
            valid = _hasher.verify(
                row["password_hash"],
                current_password,
            )
        except Exception:
            valid = False

        if not valid:
            raise ValidationFailed(
                "Current password is incorrect"
            )

        conn.execute(
            "UPDATE users SET password_hash=?,updated_at=? WHERE id=?",
            (
                _hasher.hash(new_password),
                db.now_iso(),
                user_id,
            ),
        )

        conn.execute(
            "UPDATE sessions SET revoked_at=? "
            "WHERE user_id=? AND revoked_at IS NULL",
            (db.now_iso(), user_id),
        )
