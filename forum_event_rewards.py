"""Manually selected, once-per-player event rewards in the wallet transaction."""

from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from admin_hub import log_action
from database import GameHistory, User
from automation_control import AutomationSetting
from roster_settings import write
import forum_events as events

PREFIX = "forum_event_rewards:"


def payments(db, eid):
    row = db.get(AutomationSetting, PREFIX + eid)
    return json.loads(row.value) if row else {}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def amount_cents(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{1,7}(?:[.,][0-9]{1,2})?", value
    ):
        raise ValueError(
            "Сумма должна быть положительным числом, не более двух знаков после запятой."
        )
    cents = int(Decimal(value.replace(",", ".")) * 100)
    if not 1 <= cents <= 100_000_000:
        raise ValueError("Награда одному игроку: от 0,01 до 1 000 000 БК.")
    return cents


def make_router(get_user, get_db, may_manage, secret):
    router = APIRouter()
    signer = URLSafeTimedSerializer(secret, salt="forum-event-reward-v1")

    def check(user):
        if not user or not user.is_admin or not may_manage(user):
            raise HTTPException(
                403,
                "Награды за события доступны старшим и главным администраторам, техадмину и владельцу.",
            )

    def event_for(db, eid):
        try:
            return events.get(db, eid)
        except events.EventError as exc:
            raise HTTPException(404, str(exc)) from None

    @router.get("/api/admin/forum-events/{eid}/rewards")
    async def listing(
        eid: str, response: Response, user=Depends(get_user), db=Depends(get_db)
    ):
        check(user)
        event = event_for(db, eid)
        response.headers["Cache-Control"] = "private, no-store"
        return {
            "event_revision": event["revision"],
            "paid": payments(db, eid),
            "players": [
                {
                    "id": row.id,
                    "name": row.display_name or row.username,
                    "discord_id": row.discord_id,
                    "response": event["responses"]
                    .get(str(row.discord_id), {})
                    .get("choice"),
                    "in_lineup": str(row.discord_id) in event["lineup"],
                }
                for row in db.query(User)
                .filter(User.deleted_at.is_(None))
                .order_by(User.id)
                .all()
            ],
        }

    async def body(request, fields):
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Ожидается JSON.") from None
        if not isinstance(data, dict) or set(data) != set(fields):
            raise HTTPException(400, "Некорректные поля запроса.")
        return data

    def plan(db, eid, data):
        event = event_for(db, eid)
        if (
            type(data["event_revision"]) is not int
            or data["event_revision"] != event["revision"]
        ):
            raise HTTPException(409, "Событие изменилось. Обновите список получателей.")
        if event["config"]["status"] == "cancelled":
            raise HTTPException(400, "Нельзя начислять награды за отменённое событие.")
        items = data["recipients"]
        if not isinstance(items, list) or not 1 <= len(items) <= 1000:
            raise HTTPException(400, "Выберите от 1 до 1000 получателей.")
        paid = payments(db, eid)
        seen, rows = set(), []
        for item in items:
            if (
                not isinstance(item, dict)
                or set(item) != {"user_id", "amount"}
                or type(item["user_id"]) is not int
                or not 0 < item["user_id"] < 2**63
            ):
                raise HTTPException(400, "Некорректный получатель.")
            uid = item["user_id"]
            if uid in seen:
                raise HTTPException(400, "Один игрок указан несколько раз.")
            seen.add(uid)
            if str(uid) in paid:
                raise HTTPException(
                    409,
                    "Один из игроков уже получил награду за это событие. Обновите список.",
                )
            target = db.get(User, uid)
            if not target or target.deleted_at:
                raise HTTPException(409, "Получатель недоступен. Обновите список.")
            try:
                cents = amount_cents(item["amount"])
                balance = Decimal(str(target.balance))
                if (
                    not balance.is_finite()
                    or abs(balance) > Decimal("1000000000")
                    or balance + Decimal(cents) / 100 > Decimal("1000000000")
                ):
                    raise ValueError(
                        "Начисление превышает допустимый баланс получателя."
                    )
            except (ValueError, InvalidOperation) as exc:
                raise HTTPException(400, str(exc)) from None
            rows.append(
                {
                    "user_id": uid,
                    "name": target.display_name or target.username,
                    "cents": cents,
                }
            )
        rows.sort(key=lambda row: row["user_id"])
        return event, paid, rows

    def proof(user, event, paid, rows):
        return {
            "actor": user.id,
            "event": event["id"],
            "revision": event["revision"],
            "paid": digest(paid),
            "recipients": digest(rows),
        }

    @router.post("/api/admin/forum-events/{eid}/rewards/preview")
    async def preview(
        eid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        check(user)
        data = await body(request, ("event_revision", "recipients"))
        event, paid, rows = plan(db, eid, data)
        return {
            "recipients": rows,
            "total_cents": sum(row["cents"] for row in rows),
            "confirmation": signer.dumps(proof(user, event, paid, rows)),
        }

    @router.post("/api/admin/forum-events/{eid}/rewards/apply")
    async def apply(
        eid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        check(user)
        data = await body(request, ("event_revision", "recipients", "confirmation"))
        event, paid, rows = plan(db, eid, data)
        token = data["confirmation"]
        if not isinstance(token, str) or len(token) > 3000:
            raise HTTPException(400, "Сначала выполните предпросмотр.")
        try:
            saved = signer.loads(token, max_age=300)
        except BadSignature:
            raise HTTPException(
                409, "Предпросмотр истёк. Проверьте начисление заново."
            ) from None
        if saved != proof(user, event, paid, rows):
            raise HTTPException(
                409, "Получатели, суммы или автор изменились. Повторите предпросмотр."
            )
        for row in rows:
            target = db.get(User, row["user_id"])
            target.balance = float(
                Decimal(str(target.balance)) + Decimal(row["cents"]) / 100
            )
            operation = GameHistory(
                user_id=target.id,
                game_type="admin",
                result="admin_adjustment",
                amount=row["cents"] / 100,
                details=f"Награда за событие #{event['number']} «{event['config']['title']}»; администратор {user.discord_id}.",
            )
            db.add(operation)
            db.flush()
            paid[str(target.id)] = {
                "name": row["name"],
                "cents": row["cents"],
                "actor": str(user.discord_id),
                "at": events.now().isoformat(),
                "operation_id": operation.id,
            }
        write(db, PREFIX + eid, paid, str(user.discord_id))
        total = sum(row["cents"] for row in rows)
        log_action(
            db,
            user,
            "forum_event_rewards",
            f"Событие #{event['number']}: {len(rows)} получателей, {total} копеек БК.",
        )
        return {"ok": True, "paid": paid, "total_cents": total}

    return router
