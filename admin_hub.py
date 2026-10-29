"""Administrator work queue, weekly review, appeals, evidence, timeline and exports."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from database import Base, GameHistory, User


REVIEW_START_WEEK = "2026-10-12"


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def stamp(value):
    return (
        value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        if value
        else None
    )


class AwardWeekControl(Base):
    __tablename__ = "award_week_controls"
    week = Column(String(10), primary_key=True)
    status = Column(String(16), nullable=False, default="review", index=True)
    created_at = Column(DateTime, nullable=False)
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String(20), nullable=True)


class AwardCandidateExclusion(Base):
    __tablename__ = "award_candidate_exclusions"
    __table_args__ = (UniqueConstraint("week", "discord_id", "medal"),)
    id = Column(Integer, primary_key=True)
    week = Column(String(10), nullable=False, index=True)
    discord_id = Column(String(20), nullable=False, index=True)
    medal = Column(String(50), nullable=False)
    reason = Column(Text, nullable=False)
    actor_id = Column(String(20), nullable=False)
    created_at = Column(DateTime, nullable=False)


class ManualAwardRequest(Base):
    __tablename__ = "manual_award_requests"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    medal = Column(String(50), nullable=False)
    evidence_url = Column(String(1000), nullable=True)
    comment = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="pending", index=True)
    created_at = Column(DateTime, nullable=False)
    created_by = Column(String(20), nullable=False)
    decided_at = Column(DateTime, nullable=True)
    decided_by = Column(String(20), nullable=True)
    decision_comment = Column(Text, nullable=True)
    award_id = Column(Integer, nullable=True)


class AssemblyAppeal(Base):
    __tablename__ = "assembly_appeals"
    assessment_id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    reason = Column(Text, nullable=False)
    evidence_url = Column(String(1000), nullable=True)
    status = Column(String(16), nullable=False, default="pending", index=True)
    created_at = Column(DateTime, nullable=False)
    decided_at = Column(DateTime, nullable=True)
    decided_by = Column(String(20), nullable=True)
    decision_comment = Column(Text, nullable=True)
    refunded_cents = Column(Integer, nullable=False, default=0)


class AdminActionLog(Base):
    __tablename__ = "admin_action_logs"
    id = Column(Integer, primary_key=True)
    actor_id = Column(String(20), nullable=False, index=True)
    actor_name = Column(String(100), nullable=False)
    action = Column(String(60), nullable=False, index=True)
    target_user_id = Column(Integer, nullable=True, index=True)
    details = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, index=True)


def log_action(db, actor, action, details, target_user_id=None):
    db.add(
        AdminActionLog(
            actor_id=str(actor.discord_id),
            actor_name=(actor.display_name or actor.username)[:100],
            action=action[:60],
            target_user_id=target_user_id,
            details=str(details)[:2000],
            created_at=utcnow(),
        )
    )


def _evidence(value):
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def weekly_award_allowed(db, user, medal, evidence):
    """Hold new weekly awards for chief review starting with the next full week."""
    week = str((evidence or {}).get("week") or "")
    if not week or week < REVIEW_START_WEEK:
        return True
    control = db.get(AwardWeekControl, week)
    if control is None:
        control = AwardWeekControl(week=week, status="review", created_at=utcnow())
        db.add(control)
        return False
    if control.status != "approved":
        return False
    return (
        db.query(AwardCandidateExclusion.id)
        .filter_by(week=week, discord_id=str(user.discord_id), medal=medal)
        .first()
        is None
    )


def _url(value):
    value = str(value or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or len(value) > 1000:
        raise HTTPException(400, "Ссылка должна начинаться с http:// или https://.")
    return value


def _award_name(db, code):
    from award_role_names import award_name

    return award_name(db, code)


def _preview(db, week):
    from medals import MedalAward, evaluate

    existing = {
        (a.discord_id, a.medal): a
        for a in db.query(MedalAward).all()
        if _evidence(a.evidence).get("week") == week
    }
    control = db.get(AwardWeekControl, week)
    if control is None:
        control = AwardWeekControl(week=week, status="review", created_at=utcnow())
        db.add(control)
        db.flush()
    original = control.status
    nested = db.begin_nested()
    try:
        control.status = "approved"
        db.flush()
        db.info["distinction_preview"] = True
        try:
            evaluate(db, utcnow())
        finally:
            db.info.pop("distinction_preview", None)
        db.flush()
        candidates = [
            a
            for a in db.query(MedalAward).all()
            if _evidence(a.evidence).get("week") == week
        ]
        rows = []
        users = {u.discord_id: u for u in db.query(User).all()}
        exclusions = {
            (r.discord_id, r.medal): r
            for r in db.query(AwardCandidateExclusion).filter_by(week=week)
        }
        for award in candidates:
            user = users.get(award.discord_id)
            excluded = exclusions.get((award.discord_id, award.medal))
            rows.append(
                {
                    "user_id": user.id if user else None,
                    "discord_id": award.discord_id,
                    "name": (user.display_name or user.username)
                    if user
                    else award.discord_id,
                    "medal": award.medal,
                    "award_name": _award_name(db, award.medal),
                    "reason": award.reason,
                    "evidence": _evidence(award.evidence),
                    "excluded": bool(excluded),
                    "exclusion_reason": excluded.reason if excluded else None,
                    "issued": (award.discord_id, award.medal) in existing,
                }
            )
        return sorted(rows, key=lambda r: (r["award_name"], r["name"].casefold()))
    finally:
        nested.rollback()
        control.status = original


def _csv_cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _csv_response(name, headers, rows):
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_csv_cell(value) for value in row])
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "private, no-store",
        },
    )


def make_router(get_user, get_db, is_super_admin):
    router = APIRouter()

    def admin(user):
        if not user or not user.is_admin:
            raise HTTPException(403)

    def chief(user):
        admin(user)
        if not is_super_admin(user):
            raise HTTPException(403, "Доступ только для главных администраторов.")

    @router.get("/api/admin/hub/attention")
    async def attention(user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        from automation_control import SystemAlert
        from assembly_attendance import AssemblyAssessment
        from award_role_sync import status as role_status

        missing_steam = (
            db.query(User)
            .filter(User.deleted_at.is_(None), User.steam_id.is_(None))
            .count()
        )
        alerts = db.query(SystemAlert).filter(SystemAlert.resolved_at.is_(None)).count()
        appeals = db.query(AssemblyAppeal).filter_by(status="pending").count()
        manual = db.query(ManualAwardRequest).filter_by(status="pending").count()
        fines = (
            db.query(AssemblyAssessment).filter_by(outcome="pending_penalty").count()
        )
        reviews = db.query(AwardWeekControl).filter_by(status="review").count()
        from distinction_reviews import DistinctionReview

        distinction_pending = (
            db.query(DistinctionReview).filter_by(status="pending").count()
        )
        roles = role_status(db)
        items = [
            {
                "key": "distinctions",
                "label": "Знаки ждут подтверждения (24 часа)",
                "count": distinction_pending,
                "section": "hub",
            },
            {
                "key": "alerts",
                "label": "Сбои автоматизации",
                "count": alerts,
                "section": "automation",
            },
            {
                "key": "steam",
                "label": "Нет Steam ID",
                "count": missing_steam,
                "section": "players",
            },
            {
                "key": "roles",
                "label": "Роли ожидают синхронизации",
                "count": roles["pending"] + roles["failed"],
                "section": "automation",
            },
            {
                "key": "awards",
                "label": "Недели ожидают проверки наград",
                "count": reviews,
                "section": "award-review",
            },
            {
                "key": "manual",
                "label": "Ручные награды ожидают решения",
                "count": manual,
                "section": "manual-awards",
            },
            {
                "key": "appeals",
                "label": "Апелляции ожидают решения",
                "count": appeals,
                "section": "appeals",
            },
            {
                "key": "fines",
                "label": "Штрафы ожидают подтверждения",
                "count": fines,
                "section": "assembly",
            },
        ]
        from wallet_history import WalletQuestion

        items.append(
            {
                "key": "wallet_questions",
                "label": "Вопросы по финансовым операциям",
                "count": db.query(WalletQuestion).filter_by(status="pending").count(),
                "section": "wallet-questions",
            }
        )
        return {
            "total": sum(i["count"] for i in items),
            "items": items,
            "checked_at": stamp(utcnow()),
        }

    @router.get("/api/admin/awards/preview")
    async def award_preview(week: str = "", user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        from protocol_week_source import previous_key

        week = week or previous_key(utcnow())
        try:
            datetime.strptime(week, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Некорректная неделя.")
        rows = _preview(db, week)
        control = db.get(AwardWeekControl, week)
        return {
            "week": week,
            "status": control.status,
            "candidates": rows,
            "pending": sum(not r["issued"] and not r["excluded"] for r in rows),
        }

    @router.post("/api/admin/awards/preview/{week}/approve")
    async def approve_week(
        week: str, body: dict = Body(...), user=Depends(get_user), db=Depends(get_db)
    ):
        chief(user)
        try:
            datetime.strptime(week, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Некорректная неделя.")
        exclusions = body.get("exclusions", [])
        if not isinstance(exclusions, list) or len(exclusions) > 500:
            raise HTTPException(400, "Некорректный список исключений.")
        for item in exclusions:
            target = db.get(User, item.get("user_id"))
            medal = str(item.get("medal") or "")
            reason = str(item.get("reason") or "").strip()
            if not target or not medal or not reason:
                raise HTTPException(
                    400, "Для исключения нужны игрок, награда и причина."
                )
            row = (
                db.query(AwardCandidateExclusion)
                .filter_by(week=week, discord_id=target.discord_id, medal=medal)
                .first()
            )
            if row is None:
                db.add(
                    AwardCandidateExclusion(
                        week=week,
                        discord_id=target.discord_id,
                        medal=medal,
                        reason=reason[:500],
                        actor_id=user.discord_id,
                        created_at=utcnow(),
                    )
                )
        control = db.get(AwardWeekControl, week)
        if control is None:
            control = AwardWeekControl(week=week, status="review", created_at=utcnow())
            db.add(control)
        control.status = "approved"
        control.approved_at = utcnow()
        control.approved_by = user.discord_id
        from medals import evaluate
        from award_rewards import pay

        evaluate(db, utcnow())
        pay(db)
        log_action(
            db,
            user,
            "award_week_approved",
            f"Неделя {week}; исключений: {len(exclusions)}",
        )
        return {"ok": True, "week": week, "excluded": len(exclusions)}

    @router.get("/api/admin/manual-awards")
    async def manual_list(user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        rows = (
            db.query(ManualAwardRequest)
            .order_by(ManualAwardRequest.id.desc())
            .limit(200)
            .all()
        )
        from medals import MANUAL_CATALOG
        from distinctions_catalog import MANUAL_CATALOG as D_MANUAL

        catalog = {
            code: _award_name(db, code) for code in {**MANUAL_CATALOG, **D_MANUAL}
        }
        users = (
            {
                u.id: u
                for u in db.query(User)
                .filter(User.id.in_({r.user_id for r in rows}))
                .all()
            }
            if rows
            else {}
        )
        return {
            "catalog": catalog,
            "items": [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    "name": (users[r.user_id].display_name or users[r.user_id].username)
                    if r.user_id in users
                    else str(r.user_id),
                    "medal": r.medal,
                    "award_name": _award_name(db, r.medal),
                    "evidence_url": r.evidence_url,
                    "comment": r.comment,
                    "status": r.status,
                    "created_at": stamp(r.created_at),
                    "decision_comment": r.decision_comment,
                }
                for r in rows
            ],
        }

    @router.post("/api/admin/manual-awards")
    async def manual_create(
        body: dict = Body(...), user=Depends(get_user), db=Depends(get_db)
    ):
        admin(user)
        target = db.get(User, body.get("user_id"))
        medal = str(body.get("medal") or "")
        comment = str(body.get("comment") or "").strip()
        url = _url(body.get("evidence_url"))
        from medals import MANUAL_CATALOG
        from distinctions_catalog import MANUAL_CATALOG as D_MANUAL

        if not target or target.deleted_at is not None:
            raise HTTPException(404, "Игрок не найден.")
        from distinction_reviews import can_review

        if medal in D_MANUAL and not can_review(user):
            raise HTTPException(
                403, "Ручные знаки могут предлагать главные и старшие администраторы."
            )
        if medal not in {**MANUAL_CATALOG, **D_MANUAL}:
            raise HTTPException(400, "Эта награда не выдаётся вручную.")
        if not comment or len(comment) > 2000:
            raise HTTPException(400, "Укажите обоснование до 2000 символов.")
        if medal in D_MANUAL:
            from distinction_reviews import DistinctionReview
            from medals import MedalAward

            review = (
                db.query(DistinctionReview)
                .filter_by(discord_id=target.discord_id, medal=medal)
                .first()
            )
            existing = (
                db.query(MedalAward.id)
                .filter_by(discord_id=target.discord_id, medal=medal)
                .first()
            )
            if existing or (review and review.status != "cancelled"):
                raise HTTPException(
                    409,
                    "Для этого игрока знак уже выдан, ожидает решения или отклонён.",
                )
        row = ManualAwardRequest(
            user_id=target.id,
            medal=medal,
            evidence_url=url,
            comment=comment,
            status="pending",
            created_at=utcnow(),
            created_by=user.discord_id,
        )
        db.add(row)
        db.flush()
        log_action(
            db,
            user,
            "manual_award_requested",
            f"{_award_name(db, medal)}: {comment}",
            target.id,
        )
        if medal in D_MANUAL:
            from distinction_reviews import enqueue_legacy_manual_requests

            enqueue_legacy_manual_requests(db, utcnow())
        return {"ok": True, "id": row.id}

    @router.post("/api/admin/manual-awards/{request_id}/decision")
    async def manual_decision(
        request_id: int,
        body: dict = Body(...),
        user=Depends(get_user),
        db=Depends(get_db),
    ):
        admin(user)
        row = db.get(ManualAwardRequest, request_id)
        from distinction_reviews import (
            DistinctionReview,
            can_review,
            decide,
            enqueue_legacy_manual_requests,
        )
        from distinctions_catalog import ROLE_IDS as distinction_codes

        if row and row.medal in distinction_codes:
            if row.status != "pending":
                raise HTTPException(409, "Заявка уже обработана.")
            if not can_review(user):
                raise HTTPException(403)
            enqueue_legacy_manual_requests(db, utcnow())
            target = db.get(User, row.user_id)
            review = (
                db.query(DistinctionReview)
                .filter_by(discord_id=target.discord_id, medal=row.medal)
                .first()
                if target
                else None
            )
            decide(db, review, user, body.get("decision"), body.get("comment", ""))
            return {"ok": True, "status": row.status, "award_id": row.award_id}
        chief(user)
        if not row or row.status != "pending":
            raise HTTPException(409, "Заявка уже обработана или не найдена.")
        decision = str(body.get("decision") or "")
        comment = str(body.get("comment") or "").strip()[:1000]
        if decision not in ("approved", "rejected"):
            raise HTTPException(400, "Неизвестное решение.")
        row.status = decision
        row.decided_at = utcnow()
        row.decided_by = user.discord_id
        row.decision_comment = comment or None
        if decision == "approved":
            from medals import grant
            from award_rewards import pay

            target = db.get(User, row.user_id)
            award = grant(
                db,
                target,
                row.medal,
                row.comment,
                {
                    "source": "Ручная очередь сайта",
                    "manual": True,
                    "evidence_url": row.evidence_url,
                    "comment": row.comment,
                    "request_id": row.id,
                },
                utcnow(),
            )
            if award is None:
                raise HTTPException(
                    409, "Награда уже выдана, снята или запрещена исключением."
                )
            row.award_id = award.id
            pay(db)
        log_action(
            db,
            user,
            "manual_award_" + decision,
            f"Заявка #{row.id}: {comment}",
            row.user_id,
        )
        return {"ok": True, "status": row.status, "award_id": row.award_id}

    @router.get("/api/admin/awards/{award_id}/evidence")
    async def award_evidence(award_id: int, user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        from medals import MedalAward

        row = db.get(MedalAward, award_id)
        if not row:
            raise HTTPException(404)
        target = db.query(User).filter_by(discord_id=row.discord_id).first()
        return {
            "id": row.id,
            "name": _award_name(db, row.medal),
            "player": (target.display_name or target.username)
            if target
            else row.discord_id,
            "reason": row.reason,
            "earned_at": stamp(row.earned_at),
            "evidence": _evidence(row.evidence),
        }

    @router.get("/api/admin/assembly/appeals")
    async def appeal_list(user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        from assembly_attendance import AssemblyAssessment

        rows = (
            db.query(AssemblyAppeal)
            .order_by(AssemblyAppeal.created_at.desc())
            .limit(200)
            .all()
        )
        assessments = (
            {
                r.id: r
                for r in db.query(AssemblyAssessment)
                .filter(AssemblyAssessment.id.in_({a.assessment_id for a in rows}))
                .all()
            }
            if rows
            else {}
        )
        users = (
            {
                u.id: u
                for u in db.query(User)
                .filter(User.id.in_({a.user_id for a in rows}))
                .all()
            }
            if rows
            else {}
        )
        return {
            "items": [
                {
                    "assessment_id": a.assessment_id,
                    "user_id": a.user_id,
                    "name": (users[a.user_id].display_name or users[a.user_id].username)
                    if a.user_id in users
                    else str(a.user_id),
                    "event_date": assessments[a.assessment_id].event_date
                    if a.assessment_id in assessments
                    else None,
                    "reason": a.reason,
                    "evidence_url": a.evidence_url,
                    "status": a.status,
                    "created_at": stamp(a.created_at),
                    "decision_comment": a.decision_comment,
                    "refunded": a.refunded_cents / 100,
                }
                for a in rows
            ]
        }

    @router.post("/api/assembly/appeals")
    async def appeal_create(
        body: dict = Body(...), user=Depends(get_user), db=Depends(get_db)
    ):
        if not user:
            raise HTTPException(401)
        from assembly_attendance import AssemblyAssessment

        assessment = db.get(AssemblyAssessment, body.get("assessment_id"))
        reason = str(body.get("reason") or "").strip()
        url = _url(body.get("evidence_url"))
        if not assessment or assessment.user_id != user.id:
            raise HTTPException(404, "Штраф не найден.")
        if assessment.outcome not in ("penalized", "pending_penalty"):
            raise HTTPException(409, "Это решение нельзя обжаловать.")
        if db.get(AssemblyAppeal, assessment.id):
            raise HTTPException(409, "Апелляция уже отправлена.")
        if not reason or len(reason) > 2000:
            raise HTTPException(400, "Укажите причину до 2000 символов.")
        db.add(
            AssemblyAppeal(
                assessment_id=assessment.id,
                user_id=user.id,
                reason=reason,
                evidence_url=url,
                status="pending",
                created_at=utcnow(),
            )
        )
        return {"ok": True}

    @router.post("/api/admin/assembly/appeals/{assessment_id}/decision")
    async def appeal_decision(
        assessment_id: int,
        body: dict = Body(...),
        user=Depends(get_user),
        db=Depends(get_db),
    ):
        chief(user)
        appeal = db.get(AssemblyAppeal, assessment_id)
        if not appeal or appeal.status != "pending":
            raise HTTPException(409, "Апелляция уже обработана или не найдена.")
        decision = str(body.get("decision") or "")
        comment = str(body.get("comment") or "").strip()[:1000]
        if decision not in ("approved", "rejected"):
            raise HTTPException(400, "Неизвестное решение.")
        from assembly_attendance import AssemblyAssessment

        assessment = db.get(AssemblyAssessment, assessment_id)
        target = db.get(User, appeal.user_id)
        if not assessment or not target:
            raise HTTPException(404)
        if decision == "approved":
            refund = max(0, assessment.charged_cents or 0)
            if refund:
                target.balance += refund / 100
                db.add(
                    GameHistory(
                        user_id=target.id,
                        game_type="admin",
                        result="admin_adjustment",
                        amount=refund / 100,
                        details=f"Возврат штрафа за сбор {assessment.event_date} по одобренной апелляции",
                    )
                )
            appeal.refunded_cents = refund
            assessment.outcome = "appeal_approved"
        appeal.status = decision
        appeal.decided_at = utcnow()
        appeal.decided_by = user.discord_id
        appeal.decision_comment = comment or None
        log_action(
            db,
            user,
            "assembly_appeal_" + decision,
            f"Сбор {assessment.event_date}; возврат {appeal.refunded_cents / 100:.2f} БК",
            target.id,
        )
        return {"ok": True, "status": decision, "refunded": appeal.refunded_cents / 100}

    @router.get("/api/appeals/mine")
    async def my_appeals(user=Depends(get_user), db=Depends(get_db)):
        if not user:
            raise HTTPException(401)
        from assembly_attendance import AssemblyAssessment

        rows = (
            db.query(AssemblyAssessment)
            .filter_by(user_id=user.id)
            .order_by(AssemblyAssessment.event_date.desc())
            .limit(30)
            .all()
        )
        appeals = {
            a.assessment_id: a
            for a in db.query(AssemblyAppeal).filter_by(user_id=user.id).all()
        }
        return {
            "items": [
                {
                    "assessment_id": r.id,
                    "event_date": r.event_date,
                    "outcome": r.outcome,
                    "charged": (r.charged_cents or 0) / 100,
                    "appeal_status": appeals[r.id].status if r.id in appeals else None,
                    "reason": appeals[r.id].reason if r.id in appeals else None,
                }
                for r in rows
            ]
        }

    @router.get("/api/admin/player-timeline/{user_id}")
    async def timeline(user_id: int, user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        target = db.get(User, user_id)
        if not target:
            raise HTTPException(404)
        from audit_logs import present
        from medals import MedalAward
        from squad_play_history import sessions_for
        from discord_event_log import DiscordEventLog, DiscordChannelCatalog

        entries = []
        for row in (
            db.query(GameHistory)
            .filter_by(user_id=user_id)
            .order_by(GameHistory.id.desc())
            .limit(100)
        ):
            item = present(row, True)
            entries.append(
                {
                    "at": stamp(row.created_at),
                    "type": "operation",
                    "title": item["label"],
                    "details": item["details"],
                }
            )
        for row in sessions_for(db, target.steam_id, 50) if target.steam_id else []:
            entries.append(
                {
                    "at": stamp(row.started_at),
                    "type": "squad",
                    "title": "Вход на " + row.server_name,
                    "details": "Squad · Protocol",
                }
            )
            if row.ended_at:
                entries.append(
                    {
                        "at": stamp(row.ended_at),
                        "type": "squad",
                        "title": "Выход с " + row.server_name,
                        "details": "Squad · Protocol",
                    }
                )
        channels = {r.channel_id: r.name for r in db.query(DiscordChannelCatalog).all()}
        for row in (
            db.query(DiscordEventLog)
            .filter_by(target_id=target.discord_id)
            .order_by(DiscordEventLog.id.desc())
            .limit(100)
        ):
            before = row.channel_from
            after = row.channel_to
            title = {
                "voice_join": "Вход в войс",
                "voice_leave": "Выход из войса",
                "voice_move": "Переход между войсами",
            }.get(row.kind, row.kind)
            details = " → ".join(
                filter(
                    None,
                    [
                        channels.get(str(before)) if before else None,
                        channels.get(str(after)) if after else None,
                    ],
                )
            )
            entries.append(
                {
                    "at": stamp(row.occurred_at),
                    "type": "discord",
                    "title": title,
                    "details": details,
                }
            )
        for row in db.query(MedalAward).filter_by(discord_id=target.discord_id):
            entries.append(
                {
                    "at": stamp(row.earned_at),
                    "type": "award",
                    "title": "Награда: " + _award_name(db, row.medal),
                    "details": row.reason,
                    "award_id": row.id,
                }
            )
        entries = [e for e in entries if e["at"]]
        entries.sort(key=lambda e: e["at"], reverse=True)
        return {"user_id": user_id, "items": entries[:250]}

    @router.get("/api/admin/weekly-report")
    async def weekly_report(week: str = "", user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        from protocol_week_source import previous_key, ProtocolPublishedWeek
        from medals import MedalAward
        from assembly_attendance import AssemblyAssessment

        week = week or previous_key(utcnow())
        start = datetime.strptime(week, "%Y-%m-%d")
        end = start + timedelta(days=7)
        awards = [
            a
            for a in db.query(MedalAward).all()
            if _evidence(a.evidence).get("week") == week
        ]
        assessments = (
            db.query(AssemblyAssessment)
            .filter(
                AssemblyAssessment.event_date >= week,
                AssemblyAssessment.event_date < end.strftime("%Y-%m-%d"),
            )
            .all()
        )
        protocol = db.get(ProtocolPublishedWeek, week)
        return {
            "week": week,
            "awards": len(awards),
            "award_rows": [
                {
                    "name": _award_name(db, a.medal),
                    "discord_id": a.discord_id,
                    "reason": a.reason,
                }
                for a in awards
            ],
            "assembly": {
                "people": len(assessments),
                "present": sum(a.attended_at is not None for a in assessments),
                "reports": sum(a.reported_at is not None for a in assessments),
                "fines": sum((a.charged_cents or 0) > 0 for a in assessments),
                "charged": sum(a.charged_cents or 0 for a in assessments) / 100,
            },
            "protocol_available": bool(protocol),
            "manual_pending": db.query(ManualAwardRequest)
            .filter_by(status="pending")
            .count(),
            "appeals_pending": db.query(AssemblyAppeal)
            .filter_by(status="pending")
            .count(),
        }

    @router.get("/api/admin/actions")
    async def actions(user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        rows = (
            db.query(AdminActionLog).order_by(AdminActionLog.id.desc()).limit(300).all()
        )
        return {
            "items": [
                {
                    "id": r.id,
                    "actor": r.actor_name,
                    "action": r.action,
                    "target_user_id": r.target_user_id,
                    "details": r.details,
                    "created_at": stamp(r.created_at),
                }
                for r in rows
            ]
        }

    @router.get("/api/admin/export/{kind}")
    async def export(kind: str, user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        if kind == "users":
            rows = db.query(User).order_by(User.id).all()
            return _csv_response(
                "st9-users.csv",
                ["ID", "Discord ID", "Имя", "Steam ID", "Баланс", "Архив"],
                (
                    (
                        r.id,
                        r.discord_id,
                        r.display_name or r.username,
                        r.steam_id or "",
                        f"{r.balance:.2f}",
                        stamp(r.deleted_at) or "",
                    )
                    for r in rows
                ),
            )
        if kind == "awards":
            from medals import MedalAward

            rows = db.query(MedalAward).order_by(MedalAward.id).all()
            return _csv_response(
                "st9-awards.csv",
                ["ID", "Discord ID", "Награда", "Дата", "Причина", "Доказательство"],
                (
                    (
                        r.id,
                        r.discord_id,
                        _award_name(db, r.medal),
                        stamp(r.earned_at),
                        r.reason,
                        r.evidence,
                    )
                    for r in rows
                ),
            )
        if kind == "assembly":
            from assembly_attendance import AssemblyAssessment

            rows = db.query(AssemblyAssessment).order_by(AssemblyAssessment.id).all()
            return _csv_response(
                "st9-assembly.csv",
                ["Дата", "Discord ID", "Steam ID", "Решение", "Начислено", "Списано"],
                (
                    (
                        r.event_date,
                        r.discord_id,
                        r.steam_id or "",
                        r.outcome,
                        (r.assessed_cents or 0) / 100,
                        (r.charged_cents or 0) / 100,
                    )
                    for r in rows
                ),
            )
        if kind == "audit":
            rows = db.query(GameHistory).order_by(GameHistory.id).all()
            return _csv_response(
                "st9-audit.csv",
                ["ID", "Игрок", "Тип", "Результат", "Сумма", "Описание", "Дата"],
                (
                    (
                        r.id,
                        r.user_id,
                        r.game_type,
                        r.result,
                        r.amount,
                        r.details or "",
                        stamp(r.created_at),
                    )
                    for r in rows
                ),
            )
        raise HTTPException(404, "Неизвестный экспорт.")

    return router
