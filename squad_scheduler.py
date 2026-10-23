"""Durable one-off Squad credits. Wallets and job results commit together."""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import Column, String, Integer, BigInteger, Float, Text
from database import Base, User, engine
from squad_routes import (
    amount_cents,
    validate_key,
    validate_servers,
    selected_recipients,
    apply_credit,
)

logger = logging.getLogger(__name__)
GRACE_SECONDS = 600
MAX_PENDING = 200


class SquadScheduleBatch(Base):
    __tablename__ = "squad_schedule_batches"
    id = Column(String(36), primary_key=True)
    admin_id = Column(Integer, nullable=False)
    payload = Column(Text, nullable=False)


class SquadSchedule(Base):
    __tablename__ = "squad_schedules"
    id = Column(String(36), primary_key=True)
    batch_id = Column(String(36), nullable=False)
    admin_id = Column(Integer, nullable=False)
    amount_cents = Column(BigInteger, nullable=False)
    servers = Column(Text, nullable=False)
    due_at = Column(Float, nullable=False, index=True)
    created_at = Column(Float, nullable=False)
    finished_at = Column(Float, nullable=True)
    state = Column(String(12), nullable=False, default="pending", index=True)
    error = Column(Text, nullable=True)
    result = Column(Text, nullable=True)


def validate_dates(values, now):
    if not isinstance(values, list) or not 1 <= len(values) <= 50:
        raise HTTPException(400, "Добавьте от одной до 50 дат.")
    stamps = []
    for value in values:
        try:
            if not isinstance(value, str) or len(value) > 40:
                raise ValueError()
            date = datetime.fromisoformat(value)
            if date.tzinfo is None or date.utcoffset() is None:
                raise ValueError()
            stamp = date.astimezone(timezone.utc).timestamp()
            if not now + 30 <= stamp <= now + 366 * 86400 or stamp % 60:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise HTTPException(
                400,
                "Укажите даты с часовым поясом, с точностью до минуты: от 30 секунд до года вперёд.",
            )
        stamps.append(stamp)
    if len(set(stamps)) != len(stamps):
        raise HTTPException(400, "Одна и та же дата указана несколько раз.")
    return sorted(stamps)


def schedule_json(row, db):
    author = db.get(User, row.admin_id)
    result = json.loads(row.result) if row.result else None
    # Do not return wallet details or private Steam IDs in the history list.
    return {
        "id": row.id,
        "admin_id": row.admin_id,
        "author": (author.display_name or author.username)
        if author
        else "Удалённый администратор",
        "amount": f"{row.amount_cents / 100:.2f}",
        "server_ids": json.loads(row.servers),
        "due_at": row.due_at,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
        "state": row.state,
        "error": row.error,
        "count": result.get("count") if result else None,
        "total": result.get("total") if result else None,
    }


def add_schedule_routes(router, require_senior, get_db, read_body):
    SquadScheduleBatch.__table__.create(engine, checkfirst=True)
    SquadSchedule.__table__.create(engine, checkfirst=True)

    @router.get("/api/admin/squad/schedules")
    async def list_jobs(
        response: Response, admin=Depends(require_senior), db=Depends(get_db)
    ):
        response.headers["Cache-Control"] = "private, no-store"
        active = (
            db.query(SquadSchedule)
            .filter(SquadSchedule.state.in_(["pending", "waiting"]))
            .order_by(SquadSchedule.due_at)
            .all()
        )
        history = (
            db.query(SquadSchedule)
            .filter(~SquadSchedule.state.in_(["pending", "waiting"]))
            .order_by(SquadSchedule.finished_at.desc())
            .limit(100)
            .all()
        )
        return {
            "jobs": [schedule_json(row, db) for row in active + history],
            "timezone": "Europe/Moscow",
            "grace_seconds": GRACE_SECONDS,
        }

    @router.post("/api/admin/squad/schedules")
    async def create_jobs(
        request: Request,
        response: Response,
        admin=Depends(require_senior),
        db=Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        body = await read_body(request)
        key = validate_key(body.get("request_id"))
        ids = validate_servers(body.get("server_ids"))
        cents = amount_cents(body.get("amount"))
        # Replays must still work after the original dates have passed.
        dates = body.get("dates")
        existing = db.get(SquadScheduleBatch, key)
        if existing:
            saved = json.loads(existing.payload)
            if existing.admin_id != admin.id or saved != {
                "server_ids": ids,
                "amount_cents": cents,
                "dates": dates,
            }:
                raise HTTPException(
                    409, "Этот запрос уже используется для другого расписания."
                )
            rows = (
                db.query(SquadSchedule)
                .filter(SquadSchedule.batch_id == key)
                .order_by(SquadSchedule.due_at)
                .all()
            )
            return {"jobs": [schedule_json(row, db) for row in rows], "replayed": True}
        now = time.time()
        stamps = validate_dates(dates, now)
        active = (
            db.query(SquadSchedule)
            .filter(SquadSchedule.state.in_(["pending", "waiting"]))
            .count()
        )
        if active + len(stamps) > MAX_PENDING:
            raise HTTPException(
                400, "Можно хранить не более 200 ожидающих заданий. Отмените ненужные."
            )
        batch = SquadScheduleBatch(
            id=key,
            admin_id=admin.id,
            payload=json.dumps(
                {"server_ids": ids, "amount_cents": cents, "dates": dates}
            ),
        )
        db.add(batch)
        rows = []
        for stamp in stamps:
            row = SquadSchedule(
                id=str(uuid.uuid5(uuid.UUID(key), str(stamp))),
                batch_id=key,
                admin_id=admin.id,
                amount_cents=cents,
                servers=json.dumps(ids),
                due_at=stamp,
                created_at=now,
                state="pending",
            )
            db.add(row)
            rows.append(row)
        db.flush()
        return {"jobs": [schedule_json(row, db) for row in rows], "replayed": False}

    @router.post("/api/admin/squad/schedules/{job_id}/cancel")
    async def cancel_job(
        job_id: str,
        response: Response,
        admin=Depends(require_senior),
        db=Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        row = db.get(SquadSchedule, job_id)
        if not row:
            raise HTTPException(404, "Задание не найдено.")
        if row.state == "cancelled":
            return schedule_json(row, db)
        if row.state not in ("pending", "waiting"):
            raise HTTPException(
                409, "Задание уже завершено. Отмена не изменяет баланс."
            )
        row.state, row.finished_at = "cancelled", time.time()
        row.error = f"Отменил: {admin.display_name or admin.username}"
        db.flush()
        return schedule_json(row, db)


def run_due(db, may_grant, log_game, max_balance, now=None, can_edit_user=None):
    """Call only inside the website's shared runtime transaction / write lock."""
    now = time.time() if now is None else now
    jobs = (
        db.query(SquadSchedule)
        .filter(
            SquadSchedule.state.in_(["pending", "waiting"]), SquadSchedule.due_at <= now
        )
        .order_by(SquadSchedule.due_at, SquadSchedule.id)
        .limit(20)
        .all()
    )
    for row in jobs:
        admin = db.get(User, row.admin_id)
        if not admin or admin.deleted_at or not admin.is_admin or not may_grant(admin):
            row.state, row.finished_at, row.error = (
                "failed",
                now,
                "Создатель задания больше не имеет прав старшего или главного администратора.",
            )
            continue
        if now > row.due_at + GRACE_SECONDS:
            row.state, row.finished_at, row.error = (
                "failed",
                now,
                "Окно ожидания 10 минут истекло. Деньги не начислены.",
            )
            continue
        try:
            users, servers = selected_recipients(
                db,
                json.loads(row.servers),
                now=now,
                not_before=row.due_at,
                allow_empty=True,
            )
        except HTTPException as error:
            row.state, row.error = "waiting", str(error.detail)
            continue
        try:
            # apply_credit checks EVERY balance before changing the first wallet.
            if can_edit_user:
                users = [u for u in users if can_edit_user(admin, u)]
            result = apply_credit(
                db,
                users,
                servers,
                row.amount_cents,
                admin.display_name or admin.username,
                row.id,
                log_game,
                max_balance,
                scheduled=True,
            )
        except HTTPException as error:
            row.state, row.finished_at, row.error = "failed", now, str(error.detail)
            continue
        row.state, row.finished_at, row.error = "completed", now, None
        row.result = json.dumps(result, ensure_ascii=False, allow_nan=False)
    db.flush()


async def schedule_checker(
    transaction,
    serialize,
    restore,
    may_grant,
    log_game,
    max_balance,
    can_edit_user=None,
):
    while True:
        try:
            async with transaction(serialize, restore) as db:
                run_due(
                    db, may_grant, log_game, max_balance, can_edit_user=can_edit_user
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Squad schedule processing failed; transaction rolled back"
            )
        await asyncio.sleep(10)
