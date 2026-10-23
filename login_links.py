"""Single-use website credentials shared by the Discord bot and the website."""

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from database import GameHistory, LoginLink, User

LINK_TTL = timedelta(minutes=10)
ISSUE_INTERVAL = timedelta(seconds=30)


def utcnow():
    # SQLAlchemy DateTime columns are naive; all link timestamps use UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LoginLinkError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def validate_discord_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{17,20}", value):
        raise LoginLinkError("Укажите Discord ID: от 17 до 20 цифр")
    if not 0 < int(value) < 2**64:
        raise LoginLinkError("Некорректный Discord ID")
    return value


def site_origin():
    # Never construct a credential URL from an untrusted request Host header.
    configured = os.getenv("SITE_URL") or os.getenv("DISCORD_REDIRECT_URI") or ""
    parsed = urlsplit(configured.strip())
    try:
        _ = parsed.port
    except ValueError:
        raise LoginLinkError("Некорректный адрес сайта в .env", 503)
    local_http = parsed.scheme == "http" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "[::1]",
        "::1",
    }
    if (
        not parsed.hostname
        or (parsed.scheme != "https" and not local_http)
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in configured)
    ):
        raise LoginLinkError(
            "Настройте HTTPS-адрес SITE_URL или DISCORD_REDIRECT_URI в .env", 503
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def make_login_url(token):
    # The fragment is not sent to HTTP servers, access logs, or link preview bots.
    return f"{site_origin()}/auth/link#{token}"


def token_digest(token):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise LoginLinkError(
            "Ссылка недействительна или истекла. Получите новую через /login.", 400
        )
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def revoke_login_links(db, discord_id, now=None):
    now = now or utcnow()
    return (
        db.query(LoginLink)
        .filter(
            LoginLink.discord_id == discord_id,
            LoginLink.used_at.is_(None),
            LoginLink.revoked_at.is_(None),
            LoginLink.expires_at > now,
        )
        .update({LoginLink.revoked_at: now}, synchronize_session="fetch")
    )


def issue_login_link(db, discord_id, source, issued_by, now=None):
    """Caller must hold the shared runtime transaction and verify the issuer."""
    validate_discord_id(discord_id)
    origin = site_origin()
    now = now or utcnow()
    latest = (
        db.query(LoginLink)
        .filter(LoginLink.discord_id == discord_id)
        .order_by(LoginLink.created_at.desc())
        .first()
    )
    if latest and now - latest.created_at < ISSUE_INTERVAL:
        raise LoginLinkError("Подождите 30 секунд перед выдачей новой ссылки", 429)
    db.query(LoginLink).filter(LoginLink.expires_at < now - timedelta(days=1)).delete(
        synchronize_session=False
    )
    revoke_login_links(db, discord_id, now)
    user = db.query(User).filter(User.discord_id == discord_id).first()
    token = secrets.token_urlsafe(32)
    db.add(
        LoginLink(
            token_hash=token_digest(token),
            discord_id=discord_id,
            user_id=user.id if user else None,
            source=source,
            issued_by=issued_by,
            created_at=now,
            expires_at=now + LINK_TTL,
        )
    )
    if user:
        db.add(
            GameHistory(
                user_id=user.id,
                game_type="admin",
                result="login_link_issued",
                amount=0,
                details=f"Выдана ссылка для входа: {source}; инициатор Discord ID {issued_by}",
            )
        )
    db.flush()
    return token, f"{origin}/auth/link#{token}"


def get_valid_login_link(db, token, now=None):
    now = now or utcnow()
    row = db.get(LoginLink, token_digest(token))
    if row is None or row.used_at or row.revoked_at or row.expires_at <= now:
        raise LoginLinkError(
            "Ссылка недействительна или истекла. Получите новую через /login.", 400
        )
    if row.user_id is not None:
        user = db.get(User, row.user_id)
        if user is None or user.discord_id != row.discord_id:
            raise LoginLinkError(
                "Профиль больше недоступен. Получите новую ссылку.", 400
            )
    return row
