"""Date-scoped assembly charge waiver with actor-bound preview and optional refunds."""

import hashlib
import json
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from automation_control import AutomationSetting, utcnow
from bulk_operations import BulkConfirmation, digest
from database import GameHistory, User

PATH = "/api/admin/assembly/waive"
PREFIX = "assembly_waiver:"


def valid_day(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        day = datetime.strptime(value, "%Y-%m-%d").date()
        if day.isoformat() != value or day.weekday() != 5:
            raise ValueError
        return value
    except ValueError:
        raise HTTPException(400, "Укажите дату субботнего сбора.")


def active_waiver(db, event_date):
    row = db.get(AutomationSetting, PREFIX + event_date)
    if row is None:
        return None
    # A malformed administrative policy must not silently authorize a charge.
    data = json.loads(row.value)
    if not isinstance(data, dict) or data.get("active") is not True:
        raise ValueError("Invalid stored assembly waiver")
    return data


def plan_waiver(db, day, refund):
    from admin_hub import AssemblyAppeal
    from assembly_attendance import AssemblyAssessment, AssemblyWindow

    policy = active_waiver(db, day)
    window = db.get(AssemblyWindow, day)
    assessments = (
        db.query(AssemblyAssessment)
        .filter_by(event_date=day)
        .order_by(AssemblyAssessment.id)
        .all()
    )
    appeals = {
        r.assessment_id: r
        for r in db.query(AssemblyAppeal)
        .filter(AssemblyAppeal.assessment_id.in_([a.id for a in assessments]))
        .all()
    }
    rows, skipped = [], []
    for assessment in assessments:
        pending = assessment.outcome == "pending_penalty"
        appeal = appeals.get(assessment.id)
        already_returned = max(0, appeal.refunded_cents or 0) if appeal else 0
        cents = (
            max(0, (assessment.charged_cents or 0) - already_returned) if refund else 0
        )
        if not pending and not cents:
            continue
        user = db.get(User, assessment.user_id) if assessment.user_id else None
        if cents and not user:
            skipped.append(
                {
                    "assessment_id": assessment.id,
                    "discord_id": assessment.discord_id,
                    "cents": cents,
                }
            )
            cents = 0
        if not pending and not cents:
            continue
        rows.append(
            {
                "assessment_id": assessment.id,
                "user_id": user.id if user else None,
                "discord_id": assessment.discord_id,
                "name": (user.display_name or user.username)
                if user
                else assessment.discord_id,
                "outcome": assessment.outcome,
                "assessed_cents": assessment.assessed_cents,
                "charged_cents": assessment.charged_cents,
                "refund_cents": cents,
                "balance": user.balance if user and cents else None,
                "appeal": {
                    "status": appeal.status,
                    "refunded_cents": appeal.refunded_cents,
                }
                if appeal
                else None,
                "cancel_pending": pending,
            }
        )
    return {
        "event_date": day,
        "policy": policy,
        "window_status": window.status if window else None,
        "rows": rows,
        "skipped": skipped,
        "pending_count": sum(r["cancel_pending"] for r in rows),
        "cancelled_cents": sum(
            r["assessed_cents"] for r in rows if r["cancel_pending"]
        ),
        "refund_count": sum(r["refund_cents"] > 0 for r in rows),
        "refund_cents": sum(r["refund_cents"] for r in rows),
        "action_needed": policy is None or bool(rows),
    }


def public_plan(plan):
    return {
        **{
            k: v
            for k, v in plan.items()
            if k not in ("policy", "window_status", "rows")
        },
        "already_waived": plan["policy"] is not None,
        "rows": [
            {
                "name": r["name"],
                "cancel_pending": r["cancel_pending"],
                "cancelled_cents": r["assessed_cents"] if r["cancel_pending"] else 0,
                "refund_cents": r["refund_cents"],
                "balance": r["balance"],
            }
            for r in plan["rows"]
        ],
    }


def apply_waiver(db, plan, actor, reason, now):
    from admin_hub import AssemblyAppeal, log_action
    from assembly_attendance import AssemblyAssessment, AssemblyWindow

    day = plan["event_date"]
    if plan["policy"] is None:
        db.add(
            AutomationSetting(
                key=PREFIX + day,
                value=json.dumps(
                    {
                        "active": True,
                        "reason": reason,
                        "created_by": actor.discord_id,
                        "actor_name": actor.display_name or actor.username,
                        "created_at": now.isoformat() + "Z",
                    },
                    ensure_ascii=False,
                ),
                updated_at=now,
                actor_id=actor.discord_id,
            )
        )
    for item in plan["rows"]:
        row = db.get(AssemblyAssessment, item["assessment_id"])
        if item["refund_cents"]:
            user = db.get(User, item["user_id"])
            cents = item["refund_cents"]
            user.balance += cents / 100
            row.charged_cents = 0
            db.add(
                GameHistory(
                    user_id=user.id,
                    game_type="admin",
                    result="assembly_refund",
                    amount=cents / 100,
                    details=f"Возврат штрафа за сбор {day}: общее освобождение от списаний. Причина: {reason}",
                )
            )
        appeal = db.get(AssemblyAppeal, row.id)
        if appeal and appeal.status == "pending":
            appeal.status, appeal.decided_at, appeal.decided_by = (
                "approved",
                now,
                actor.discord_id,
            )
            appeal.refunded_cents = (appeal.refunded_cents or 0) + item["refund_cents"]
            appeal.decision_comment = "Общее освобождение от списаний: " + reason
        # Separate outcome preserves factual attendance: a waiver is not a report.
        row.outcome = "waived"
    event = db.get(AssemblyWindow, day)
    if event and event.status == "review_pending":
        event.status, event.processed_at, event.retry_at = "completed", now, None
    if plan["action_needed"]:
        log_action(
            db,
            actor,
            "assembly_waived_all",
            {
                "event_date": day,
                "reason": reason,
                "cancelled": plan["pending_count"],
                "refund_count": plan["refund_count"],
                "refund_cents": plan["refund_cents"],
            },
        )
    return {"ok": True, **public_plan(plan)}


def make_router(get_user, get_db, is_chief):
    router = APIRouter()

    def chief(user=Depends(get_user)):
        if not user or not user.is_admin or not is_chief(user):
            raise HTTPException(
                403, "Освобождение всех доступно владельцу и главным администраторам."
            )
        return user

    @router.get("/api/admin/assembly/waiver")
    async def status(
        event_date: str, response: Response, user=Depends(chief), db=Depends(get_db)
    ):
        day = valid_day(event_date)
        response.headers["Cache-Control"] = "private, no-store"
        return {"event_date": day, "waiver": active_waiver(db, day)}

    @router.post(PATH)
    async def waive(
        request: Request, response: Response, user=Depends(chief), db=Depends(get_db)
    ):
        try:
            body = await request.json()
            day = valid_day(body.get("event_date"))
            reason, refund, preview = (
                body.get("reason"),
                body.get("refund", False),
                body.get("preview", False),
            )
            if (
                not isinstance(reason, str)
                or not 3 <= len(reason.strip()) <= 500
                or type(refund) is not bool
                or type(preview) is not bool
            ):
                raise ValueError
            reason = reason.strip()
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(
                400, "Укажите причину от 3 до 500 символов и режим возврата."
            )
        response.headers["Cache-Control"] = "private, no-store"
        request_hash = digest(
            {"path": PATH, "day": day, "reason": reason, "refund": refund}
        )
        now = utcnow()
        receipt = None
        if not preview:
            token = request.headers.get("X-ST9-Confirmation", "")
            receipt = db.get(
                BulkConfirmation, hashlib.sha256(token.encode()).hexdigest()
            )
            if (
                not receipt
                or receipt.actor_id != user.id
                or receipt.request_hash != request_hash
                or receipt.used_at
                or receipt.expires_at <= now
            ):
                raise HTTPException(
                    409, "Сначала откройте новый предварительный просмотр."
                )
        plan = plan_waiver(db, day, refund)
        plan_hash = digest(plan)
        if preview:
            token = secrets.token_urlsafe(32)
            db.add(
                BulkConfirmation(
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    actor_id=user.id,
                    request_hash=request_hash,
                    plan_hash=plan_hash,
                    expires_at=now + timedelta(minutes=5),
                )
            )
            return {"preview": True, "confirmation": token, **public_plan(plan)}
        if receipt.plan_hash != plan_hash:
            raise HTTPException(
                409,
                "Списания или балансы изменились. Откройте предварительный просмотр заново.",
            )
        result = apply_waiver(db, plan, user, reason, now)
        receipt.used_at = now
        return result

    return router
