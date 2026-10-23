"""Durable, per-player distinction review with a fixed 24-hour deadline.

All callers use the runtime wallet transaction. Evidence is frozen when a valid
candidate first appears; repeated polling never extends its review window.
"""

import json
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from database import Base, User
from distinctions_catalog import ROLE_IDS

REVIEW_WINDOW = timedelta(hours=24)


class DistinctionReview(Base):
    __tablename__ = "distinction_reviews"
    __table_args__ = (UniqueConstraint("discord_id", "medal"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    discord_id = Column(String(20), nullable=False)
    steam_id = Column(String(50), nullable=True)
    medal = Column(String(50), nullable=False)
    reason = Column(Text, nullable=False)
    evidence = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="pending", index=True)
    created_at = Column(DateTime, nullable=False)
    deadline = Column(DateTime, nullable=False, index=True)
    decided_at = Column(DateTime, nullable=True)
    decided_by = Column(String(20), nullable=True)
    decision_comment = Column(Text, nullable=True)
    award_id = Column(Integer, nullable=True)
    blocked_reason = Column(String(250), nullable=True)


def utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


def can_review(actor):
    if not actor or not actor.is_admin or actor.deleted_at is not None:
        return False
    from main import is_senior_or_super

    return bool(is_senior_or_super(actor))


def exclusion(db, user, code, evidence):
    from admin_hub import AwardCandidateExclusion

    week = (evidence or {}).get("week")
    return bool(
        week
        and db.query(AwardCandidateExclusion.id)
        .filter_by(week=week, discord_id=user.discord_id, medal=code)
        .first()
    )


def review_allowed(db, user, code, reason, evidence, now):
    """Called only after eligibility, revocation and duplicate checks in grant."""
    if exclusion(db, user, code, evidence):
        return False
    row = (
        db.query(DistinctionReview)
        .filter_by(discord_id=user.discord_id, medal=code)
        .first()
    )
    if row and row.status == "rejected":
        return False
    if db.info.get("distinction_preview"):
        return True
    if row and db.info.get("issuing_distinction_review") == row.id:
        return row.status == "approved"
    if row and row.status != "cancelled":
        return False
    if row is None:
        row = DistinctionReview(discord_id=user.discord_id, medal=code)
        db.add(row)
    row.user_id, row.steam_id = user.id, user.steam_id
    row.reason = reason
    row.evidence = json.dumps(evidence, ensure_ascii=False, allow_nan=False)
    row.status, row.created_at, row.deadline = "pending", now, now + REVIEW_WINDOW
    row.decided_at = row.decided_by = row.decision_comment = row.blocked_reason = None
    db.flush()
    return False


def sync_manual_request(db, row, decision, now):
    from admin_hub import ManualAwardRequest

    requests = (
        db.query(ManualAwardRequest)
        .filter_by(user_id=row.user_id, medal=row.medal, status="pending")
        .all()
    )
    for request in requests:
        request.status = decision
        request.decided_at, request.decided_by = now, row.decided_by or "automatic"
        request.decision_comment, request.award_id = row.decision_comment, row.award_id


def issue(db, row, now, actor=None):
    """Use the ordinary award/role/payment machinery, once, in one transaction."""
    from medals import MedalAward, grant
    from award_revocation import is_revoked
    from automation_control import exception_for

    user = db.get(User, row.user_id)
    if not user or user.discord_id != row.discord_id or user.steam_id != row.steam_id:
        row.status, row.blocked_reason = (
            "cancelled",
            "Привязка игрока изменилась; нужно новое подтверждённое основание.",
        )
        sync_manual_request(db, row, "cancelled", now)
        return None
    proof = json.loads(row.evidence)
    if user.deleted_at is not None:
        row.blocked_reason = "Игрок неактивен. Выдача ждёт восстановления доступа."
        return None
    if (
        is_revoked(db, user.discord_id, row.medal)
        or exception_for(db, user.id, "award", row.medal)
        or exclusion(db, user, row.medal, proof)
    ):
        row.status, row.blocked_reason = (
            "cancelled",
            "Выдача запрещена отзывом награды или исключением.",
        )
        sync_manual_request(db, row, "cancelled", now)
        return None
    existing = (
        db.query(MedalAward)
        .filter_by(discord_id=row.discord_id, medal=row.medal)
        .first()
    )
    if existing:
        row.status, row.award_id = "issued", existing.id
        sync_manual_request(db, row, "approved", now)
        return existing
    row.status, row.decided_at = "approved", now
    row.decided_by = str(actor.discord_id) if actor else "automatic"
    enriched = {
        **proof,
        "review": {
            "id": row.id,
            "created_at": row.created_at.isoformat() + "Z",
            "deadline": row.deadline.isoformat() + "Z",
            "decision": "manual" if actor else "automatic",
            "decided_by": row.decided_by,
            "decided_at": now.isoformat() + "Z",
        },
    }
    previous = db.info.get("issuing_distinction_review")
    db.info["issuing_distinction_review"] = row.id
    try:
        award = grant(db, user, row.medal, row.reason, enriched, now)
    finally:
        if previous is None:
            db.info.pop("issuing_distinction_review", None)
        else:
            db.info["issuing_distinction_review"] = previous
    if award is None:
        row.status = "pending"
        row.blocked_reason = "Условия выдачи не подтверждены."
        return None
    # An existing Discord award is imported silently, as before the review queue.
    if proof.get("source") == "Discord role":
        award.sent_at = now
    row.status, row.award_id, row.blocked_reason = "issued", award.id, None
    sync_manual_request(db, row, "approved", now)
    from admin_hub import AdminActionLog, log_action

    if actor:
        log_action(
            db,
            actor,
            "distinction_approved",
            f"Заявка #{row.id}: {row.reason}",
            user.id,
        )
    else:
        db.add(
            AdminActionLog(
                actor_id="automatic",
                actor_name="Автовыдача через 24 часа",
                action="distinction_auto_issued",
                target_user_id=user.id,
                details=f"Заявка #{row.id}: {row.reason}",
                created_at=now,
            )
        )
    return award


def enqueue_legacy_manual_requests(db, now):
    """Existing pending requests get a fresh window at first post-update detection."""
    from admin_hub import ManualAwardRequest
    from medals import grant

    for request in db.query(ManualAwardRequest).filter_by(status="pending"):
        if request.medal not in ROLE_IDS:
            continue
        user = db.get(User, request.user_id)
        if user and user.deleted_at is None:
            grant(
                db,
                user,
                request.medal,
                request.comment,
                {
                    "source": "Ручная очередь сайта",
                    "manual": True,
                    "request_id": request.id,
                    "evidence_url": request.evidence_url,
                    "comment": request.comment,
                },
                now,
            )


def process_due(db, now=None):
    now = now or utcnow()
    enqueue_legacy_manual_requests(db, now)
    rows = (
        db.query(DistinctionReview)
        .filter(
            DistinctionReview.status == "pending", DistinctionReview.deadline <= now
        )
        .order_by(DistinctionReview.deadline, DistinctionReview.id)
        .all()
    )
    count = 0
    for row in rows:
        if issue(db, row, now):
            count += 1
    return count


def decide(db, row, actor, decision, comment="", now=None):
    if not can_review(actor):
        raise HTTPException(403, "Доступ только главным и старшим администраторам.")
    if not row or row.status != "pending":
        raise HTTPException(409, "Заявка уже обработана или не найдена.")
    if decision not in ("approved", "rejected"):
        raise HTTPException(400, "Неизвестное решение.")
    if not isinstance(comment, str) or len(comment) > 1000:
        raise HTTPException(400, "Комментарий: не более 1000 символов.")
    now = now or utcnow()
    # After the deadline, the automatic decision wins even if the next timer tick
    # has not run yet. Returning success keeps issuance in the outer transaction.
    if now >= row.deadline:
        award = issue(db, row, now)
        if award is None:
            raise HTTPException(409, row.blocked_reason or "Выдача сейчас невозможна.")
        from award_rewards import pay

        pay(db, now, award_ids=[award.id])
        return award
    row.decision_comment = comment.strip() or None
    if decision == "rejected":
        row.status, row.decided_at, row.decided_by = (
            "rejected",
            now,
            str(actor.discord_id),
        )
        sync_manual_request(db, row, "rejected", now)
        from admin_hub import log_action

        log_action(
            db,
            actor,
            "distinction_rejected",
            f"Заявка #{row.id}: {row.decision_comment or 'без комментария'}",
            row.user_id,
        )
        return None
    award = issue(db, row, now, actor)
    if award is None:
        raise HTTPException(409, row.blocked_reason or "Выдача сейчас невозможна.")
    from award_rewards import pay

    pay(db, now, award_ids=[award.id])
    return award


def record(db, row):
    from award_role_names import award_name

    user = db.get(User, row.user_id)
    return {
        "id": row.id,
        "user_id": row.user_id,
        "name": (user.display_name or user.username) if user else row.discord_id,
        "medal": row.medal,
        "award_name": award_name(db, row.medal),
        "reason": row.reason,
        "evidence": json.loads(row.evidence),
        "status": row.status,
        "created_at": row.created_at.isoformat() + "Z",
        "deadline": row.deadline.isoformat() + "Z",
        "decided_by": row.decided_by,
        "decision_comment": row.decision_comment,
        "blocked_reason": row.blocked_reason,
        "award_id": row.award_id,
    }


def make_router(get_user, get_db):
    router = APIRouter()

    def reviewer(user=Depends(get_user)):
        if not user:
            raise HTTPException(401)
        if not can_review(user):
            raise HTTPException(403)
        return user

    @router.get("/api/admin/distinction-reviews")
    def listing(user=Depends(reviewer), db=Depends(get_db)):
        pending = (
            db.query(DistinctionReview)
            .filter_by(status="pending")
            .order_by(DistinctionReview.deadline)
            .all()
        )
        history = (
            db.query(DistinctionReview)
            .filter(DistinctionReview.status != "pending")
            .order_by(DistinctionReview.id.desc())
            .limit(100)
            .all()
        )
        return {
            "items": [record(db, row) for row in pending + history],
            "pending": len(pending),
            "review_hours": 24,
            "checked_at": utcnow().isoformat() + "Z",
        }

    @router.post("/api/admin/distinction-reviews/{review_id}/decision")
    def decision(
        review_id: int,
        body: dict = Body(...),
        user=Depends(reviewer),
        db=Depends(get_db),
    ):
        choice, comment = body.get("decision"), body.get("comment", "")
        if not isinstance(comment, str) or len(comment) > 1000:
            raise HTTPException(400, "Комментарий: не более 1000 символов.")
        row = db.get(DistinctionReview, review_id)
        decide(db, row, user, choice, comment)
        return {"ok": True, "item": record(db, row)}

    return router
