"""Transactional previews for bulk wallet operations.

The existing operation is evaluated inside a rolled-back savepoint. Confirmation
is bound to the actor, request and exact affected user state, and consumed in the
same outer wallet transaction as the real operation.
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import Column, DateTime, Integer, String

from database import Base, User


class BulkConfirmation(Base):
    __tablename__ = "bulk_confirmations"
    token_hash = Column(String(64), primary_key=True)
    actor_id = Column(Integer, nullable=False)
    request_hash = Column(String(64), nullable=False)
    plan_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime, nullable=True)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def digest(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_request(path, body):
    """Reject malformed/ambiguous collections before invoking legacy handlers."""
    key = {
        "/admin/batch-update": "updates",
        "/admin/mass-adjust": "user_ids",
        "/admin/mass-adjust-steam": "steam_ids",
        "/admin/import": "users",
        "/api/admin/squad/history/grant": "user_ids",
    }[path]
    entries = body.get(key)
    if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
        raise HTTPException(400, "Выберите от 1 до 1000 записей.")
    if key in ("updates", "users"):
        identity = "user_id" if key == "updates" else "discord_id"
        if any(not isinstance(entry, dict) for entry in entries):
            raise HTTPException(400, "Неверная запись операции.")
        identities = [entry.get(identity) for entry in entries]
        if any(type(value) not in (str, int) for value in identities):
            raise HTTPException(400, "Неверный идентификатор.")
        if len(set(map(str, identities))) != len(identities):
            raise HTTPException(400, "Повторяющиеся игроки: удалите дубли.")
    elif any(type(value) not in (str, int) for value in entries):
        raise HTTPException(400, "Неверный идентификатор.")
    return len(entries)


def user_states(db):
    return {
        user.id: {
            column.name: getattr(user, column.name) for column in User.__table__.columns
        }
        for user in db.query(User).filter(User.deleted_at.is_(None))
    }


async def simulate(handler, arguments, db):
    before = user_states(db)
    savepoint = db.begin_nested()
    try:
        result = await handler(**arguments)
        db.flush()
        after = user_states(db)
        changes = []
        for uid, state in before.items():
            changed = {
                key: {"before": value, "after": after[uid][key]}
                for key, value in state.items()
                if value != after[uid][key]
            }
            if changed:
                changes.append(
                    {
                        "user_id": uid,
                        "name": state["display_name"] or state["username"],
                        "old": state["balance"],
                        "new": after[uid]["balance"],
                        "delta": round(after[uid]["balance"] - state["balance"], 2),
                        "changes": changed,
                    }
                )
        return result, changes
    finally:
        savepoint.rollback()


def bulk_preview(handler):
    @wraps(handler)
    async def guarded(request, admin, db):
        import main

        if not main.is_senior_or_super(admin):
            raise HTTPException(
                403, "Массовые операции доступны старшим администраторам."
            )
        body = await main.read_json_object(request)
        requested = validate_request(request.url.path, body)
        try:
            request_hash = digest(
                {
                    "path": request.url.path,
                    "body": {k: v for k, v in body.items() if k != "preview"},
                }
            )
        except (ValueError, TypeError):
            raise HTTPException(400, "Некорректные данные операции.")
        now = utcnow()
        db.query(BulkConfirmation).filter(BulkConfirmation.expires_at < now).delete()
        arguments = {"request": request, "admin": admin, "db": db}
        if body.get("preview") is not True:
            token = request.headers.get("X-ST9-Confirmation", "")
            receipt = db.get(
                BulkConfirmation, hashlib.sha256(token.encode()).hexdigest()
            )
            if (
                not receipt
                or receipt.actor_id != admin.id
                or receipt.used_at is not None
                or receipt.expires_at < now
                or receipt.request_hash != request_hash
            ):
                raise HTTPException(
                    409, "Сначала откройте новый предварительный просмотр."
                )
        result, changes = await simulate(handler, arguments, db)
        plan_hash = digest({"result": result, "changes": changes})
        if body.get("preview") is True:
            token = secrets.token_urlsafe(32)
            db.add(
                BulkConfirmation(
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    actor_id=admin.id,
                    request_hash=request_hash,
                    plan_hash=plan_hash,
                    expires_at=now + timedelta(minutes=5),
                )
            )
            return JSONResponse(
                {
                    "preview": True,
                    "confirmation": token,
                    "expires_in": 300,
                    "requested": requested,
                    "changed": len(changes),
                    "unchanged_or_skipped": max(0, requested - len(changes)),
                    "total_delta": round(sum(row["delta"] for row in changes), 2),
                    "rows": changes,
                    "invalid": result.get("invalid", []),
                    "not_found": result.get("not_found", []),
                    "context": result.get("context"),
                },
                headers={"Cache-Control": "no-store"},
            )
        if receipt.plan_hash != plan_hash:
            raise HTTPException(
                409,
                "Данные игроков изменились. Откройте предварительный просмотр заново.",
            )
        result = await handler(**arguments)
        receipt.used_at = now
        db.flush()
        return result

    return guarded
