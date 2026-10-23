"""Public clan roster and atomic, idempotent senior-admin Squad grants."""

import json
import math
import re
import time
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import Column, String, Integer, BigInteger, Float, Text
from database import Base, User, engine
import squad_monitor as monitor


class SquadGrant(Base):
    __tablename__ = "squad_grants"
    id = Column(String(36), primary_key=True)
    admin_id = Column(Integer, nullable=False)
    amount_cents = Column(BigInteger, nullable=False)
    servers = Column(Text, nullable=False)
    recipients = Column(Text, nullable=False)
    expires_at = Column(Float, nullable=False)
    state = Column(String(12), nullable=False, default="pending")
    result = Column(Text, nullable=True)


class SquadGrantSelection(Base):
    __tablename__ = "squad_grant_selections"
    id = Column(String(36), primary_key=True)
    user_ids = Column(Text, nullable=False)


ERRORS = {
    "authentication": "Токен BattleMetrics недействителен или истёк.",
    "permissions": "BattleMetrics не разрешает чтение данных этого сервера.",
    "rate_limited": "BattleMetrics ограничил частоту запросов. Обновление будет повторено.",
    "rcon_unavailable": "BattleMetrics потерял соединение RCON с сервером.",
    "source_unavailable": "BattleMetrics временно недоступен.",
    "invalid_roster": "Список игроков не удалось проверить.",
    "invalid_response": "Ответ BattleMetrics не удалось проверить.",
    "incomplete_response": "BattleMetrics вернул неполный список игроков.",
    "not_found": "Сервер не найден в BattleMetrics.",
    "ambiguous_identity": "BattleMetrics связал одного игрока с несколькими Steam ID. Сопоставление не подтверждено.",
    "steam_unavailable": "BattleMetrics не предоставил Steam ID части игроков.",
}


def load_snapshot(now=None):
    now = time.time() if now is None else now
    try:
        document = monitor.read_json(monitor.SNAPSHOT)
        if document.get("version") != 1 or not isinstance(
            document.get("servers"), list
        ):
            raise ValueError("Invalid snapshot")
        values = {
            row["id"]: row
            for row in document["servers"]
            if isinstance(row, dict) and row.get("id") in monitor.SERVER_IDS
        }
    except (OSError, ValueError, TypeError, KeyError):
        values = {}
    result = []
    for sid in monitor.SERVER_IDS:
        row = values.get(sid, {})
        stamp = row.get("updated_at")
        valid_stamp = (
            isinstance(stamp, (int, float))
            and not isinstance(stamp, bool)
            and math.isfinite(stamp)
            and 0 <= now - stamp <= monitor.FRESH_SECONDS
        )
        status = row.get("status", "starting")
        if status not in ("ready", "offline", "error", "starting"):
            status = "error"
        stale = not valid_stamp or status == "error"
        raw = row.get("roster", [])
        roster = []
        malformed = not isinstance(raw, list) or len(raw) > 2000
        for player in raw if isinstance(raw, list) and not malformed else []:
            if (
                not isinstance(player, dict)
                or not isinstance(player.get("steam_ids"), list)
                or len(player["steam_ids"]) > 1
                or any(not monitor.steam_id(sid) for sid in player["steam_ids"])
            ):
                malformed = True
                break
            roster.append(
                {
                    "name": monitor.text(player.get("name")),
                    "steam_ids": list(dict.fromkeys(player["steam_ids"])),
                }
            )
        if malformed:
            status, stale, roster = "error", True, []
        pending = sum(not p["steam_ids"] for p in roster)
        result.append(
            {
                "id": sid,
                "name": monitor.text(row.get("name")) or "Сервер " + sid,
                "status": status,
                "stale": stale,
                "updated_at": stamp
                if isinstance(stamp, (int, float)) and math.isfinite(stamp)
                else None,
                "pending": pending,
                "roster": roster,
                "total_players": monitor.number(row.get("total_players")),
                "max_players": monitor.number(row.get("max_players")),
                "map": monitor.text(row.get("map")),
                "mode": monitor.text(row.get("mode")),
                "queue": row.get("queue")
                if type(row.get("queue")) is int and 0 <= row["queue"] <= 1000
                else None,
                "factions": [
                    monitor.text(v, 80)
                    for v in row.get("factions", [])
                    if isinstance(v, str)
                ][:2]
                if isinstance(row.get("factions"), list)
                else [],
                "source_updated_at": monitor.text(row.get("source_updated_at"), 60),
                "error": (
                    ERRORS.get(row.get("error"), "Не удалось обновить данные.")
                    if status == "error"
                    else ERRORS.get(
                        row.get("identity_error"),
                        "Не удалось проверить часть Steam ID.",
                    )
                    if row.get("identity_error")
                    else None
                ),
                "can_grant": status == "ready" and not stale and pending == 0,
            }
        )
    return result


def public_roster(db, now=None, include_steam=False):
    servers = load_snapshot(now)
    from sqstat_online import display_servers
    from automation_control import source_enabled

    if source_enabled(db, "sqstat"):
        servers = display_servers(servers, time.time() if now is None else now)
    users = (
        db.query(User)
        .filter(User.deleted_at.is_(None), User.steam_id.is_not(None))
        .all()
    )
    by_steam = {u.steam_id: u for u in users if monitor.steam_id(u.steam_id)}
    fresh_ids = set()
    for server in servers:
        matches = {}
        for player in server.pop("roster"):
            for sid in player["steam_ids"]:
                user = by_steam.get(sid)
                if user:
                    matches[user.id] = {
                        "user_id": user.id,
                        "name": user.display_name or user.username,
                        "discord_name": user.guild_nickname or user.username,
                        "game_name": player["name"],
                        "avatar": monitor.text(user.avatar_emoji, 12),
                    }
                    if include_steam:
                        matches[user.id]["steam_id"] = user.steam_id
        server["members"] = sorted(
            matches.values(), key=lambda u: (u["name"].casefold(), u["user_id"])
        )
        server["url"] = "https://www.battlemetrics.com/servers/" + server["id"]
        if server["status"] == "ready" and not server["stale"]:
            fresh_ids.update(matches)
    return {
        "servers": servers,
        "online_members": len(fresh_ids),
        "tracked_members": len(by_steam),
        "refresh_seconds": monitor.INTERVAL,
        "fresh_seconds": monitor.FRESH_SECONDS,
        "generated_at": time.time() if now is None else now,
    }


def selected_recipients(
    db, server_ids, *, now=None, not_before=None, allow_empty=False
):
    selected = [server for server in load_snapshot(now) if server["id"] in server_ids]
    if len(selected) != len(server_ids) or any(
        not row["can_grant"] for row in selected
    ):
        raise HTTPException(
            409,
            "Нужны свежие и полностью сопоставленные списки выбранных серверов. Дождитесь обновления.",
        )
    if not_before is not None and any(
        row["updated_at"] < not_before for row in selected
    ):
        raise HTTPException(409, "Ожидаем список, обновлённый после времени задания.")
    ids = {
        sid
        for row in selected
        for player in row["roster"]
        for sid in player["steam_ids"]
    }
    users = (
        db.query(User)
        .filter(User.deleted_at.is_(None), User.steam_id.in_(ids))
        .order_by(User.id)
        .all()
        if ids
        else []
    )
    if not users and not allow_empty:
        raise HTTPException(
            409, "На выбранных серверах нет участников с привязанным Steam ID."
        )
    return users, selected


def validate_servers(ids):
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > 5
        or any(not isinstance(sid, str) or sid not in monitor.SERVER_IDS for sid in ids)
    ):
        raise HTTPException(400, "Выберите от одного до пяти настроенных серверов.")
    return sorted(set(ids))


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", key
    ):
        raise HTTPException(400, "Некорректный идентификатор запроса.")
    return key


def apply_credit(
    db, users, servers, cents, actor, key, log_game, max_balance, *, scheduled=False
):
    amount = Decimal(cents) / 100
    for user in users:
        if (
            not -Decimal(str(max_balance))
            <= Decimal(str(user.balance)) + amount
            <= Decimal(str(max_balance))
        ):
            raise HTTPException(
                409,
                "Начисление превысит лимит баланса одного из получателей. Никому не начислено.",
            )
    names = ", ".join(row["name"] for row in servers)
    updated = []
    for user in users:
        old = user.balance
        user.balance = float(Decimal(str(old)) + amount)
        label = "автоначисление" if scheduled else "массовое начисление"
        log_game(
            db,
            user.id,
            "admin",
            "admin_adjustment",
            float(amount),
            f"Squad: {label} от {actor}; {names}; пакет {key}",
        )
        updated.append(
            {
                "user_id": user.id,
                "name": user.display_name or user.username,
                "old": old,
                "new": user.balance,
            }
        )
    return {
        "ok": True,
        "grant_id": key,
        "amount": f"{amount:.2f}",
        "count": len(users),
        "total": f"{amount * len(users):.2f}",
        "updated": updated,
        "message": f"Начислено по {amount:.2f} БК для {len(users)} участников.",
    }


def amount_cents(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{1,13}(?:\.[0-9]{1,2})?", value
    ):
        raise HTTPException(400, "Введите положительную сумму с точностью до 0,01 БК.")
    amount = Decimal(value)
    if not Decimal(".01") <= amount <= Decimal("1000000000000"):
        raise HTTPException(400, "Сумма должна быть от 0,01 до 1 000 000 000 000 БК.")
    return int(amount * 100)


def preview_result(grant):
    if grant.state == "completed":
        return {
            "completed": True,
            "grant_id": grant.id,
            "result": json.loads(grant.result),
        }
    users = json.loads(grant.recipients)
    return {
        "grant_id": grant.id,
        "expires_at": grant.expires_at,
        "amount": f"{Decimal(grant.amount_cents) / 100:.2f}",
        "total": f"{Decimal(grant.amount_cents * len(users)) / 100:.2f}",
        "recipients": users,
        "server_ids": json.loads(grant.servers),
    }


def make_router(
    get_user,
    get_db,
    templates,
    may_grant,
    read_body,
    log_game,
    max_balance,
    can_edit_user=None,
):
    # Only this new metadata table is added; existing wallet/runtime tables are preserved.
    SquadGrant.__table__.create(engine, checkfirst=True)
    SquadGrantSelection.__table__.create(engine, checkfirst=True)
    router = APIRouter()

    def require_senior(user=Depends(get_user)):
        if not user or not user.is_admin or not may_grant(user):
            raise HTTPException(
                403, "Начисления Squad доступны главному и старшим администраторам."
            )
        return user

    @router.get("/squad")
    async def page(request: Request, user=Depends(get_user)):
        response = templates.TemplateResponse(
            request=request, name="squad.html", context={"user": user}
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @router.get("/api/squad/online")
    async def online(response: Response, db=Depends(get_db)):
        response.headers["Cache-Control"] = "no-store"
        return public_roster(db)

    @router.get("/api/admin/squad/online")
    async def admin_online(
        response: Response, admin=Depends(require_senior), db=Depends(get_db)
    ):
        response.headers["Cache-Control"] = "private, no-store"
        roster = public_roster(db, include_steam=True)
        for server in roster["servers"]:
            for member in server["members"]:
                member["can_select"] = not can_edit_user or can_edit_user(
                    admin, db.get(User, member["user_id"])
                )
        return roster

    @router.post("/api/admin/squad/preview")
    async def preview(
        request: Request,
        response: Response,
        admin=Depends(require_senior),
        db=Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        body = await read_body(request)
        amount = amount_cents(body.get("amount"))
        ids = validate_servers(body.get("server_ids"))
        key = validate_key(body.get("request_id"))
        selection = body.get("user_ids")
        if "user_ids" in body:
            if (
                not isinstance(selection, list)
                or not selection
                or len(selection) > 2000
                or any(type(uid) is not int or uid <= 0 for uid in selection)
            ):
                raise HTTPException(400, "Выберите хотя бы одного игрока.")
            selection = sorted(set(selection))
        existing = db.get(SquadGrant, key)
        if existing:
            saved_selection = db.get(SquadGrantSelection, key)
            previous = json.loads(saved_selection.user_ids) if saved_selection else None
            if (
                existing.admin_id != admin.id
                or existing.amount_cents != amount
                or json.loads(existing.servers) != ids
                or previous != selection
            ):
                raise HTTPException(
                    409, "Этот запрос уже используется для другого начисления."
                )
            if existing.state != "completed" and existing.expires_at < time.time():
                raise HTTPException(
                    409, "Предпросмотр истёк. Подготовьте новый список."
                )
            return preview_result(existing)
        users, servers = selected_recipients(db, ids)
        if (
            selection is not None
            and can_edit_user
            and any(u.id in selection and not can_edit_user(admin, u) for u in users)
        ):
            raise HTTPException(403, "Нельзя изменять баланс владельца.")
        if can_edit_user:
            users = [u for u in users if can_edit_user(admin, u)]
        if selection is not None:
            if not set(selection) <= {u.id for u in users}:
                raise HTTPException(
                    409,
                    "Выбранный игрок больше не находится на выбранных серверах. Обновите список.",
                )
            users = [u for u in users if u.id in selection]
        if not users:
            raise HTTPException(409, "Нет доступных получателей на выбранных серверах.")
        if len(users) > 2000:
            raise HTTPException(400, "Слишком много получателей.")
        recipients = [
            {
                "id": user.id,
                "name": user.display_name or user.username,
                "steam_id": user.steam_id,
            }
            for user in users
        ]
        grant = SquadGrant(
            id=key,
            admin_id=admin.id,
            amount_cents=amount,
            servers=json.dumps(ids),
            recipients=json.dumps(recipients, ensure_ascii=False),
            expires_at=time.time() + 120,
            state="pending",
        )
        db.add(grant)
        if selection is not None:
            db.add(SquadGrantSelection(id=key, user_ids=json.dumps(selection)))
        db.flush()
        return preview_result(grant)

    @router.post("/api/admin/squad/grants/{grant_id}")
    async def commit(
        grant_id: str,
        response: Response,
        admin=Depends(require_senior),
        db=Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        grant = db.get(SquadGrant, grant_id)
        if not grant or grant.admin_id != admin.id:
            raise HTTPException(404, "Начисление не найдено.")
        if grant.state == "completed":
            return json.loads(grant.result)
        if grant.state != "pending" or grant.expires_at < time.time():
            raise HTTPException(409, "Предпросмотр истёк. Подготовьте новый список.")
        ids = json.loads(grant.servers)
        users, servers = selected_recipients(db, ids)
        if can_edit_user:
            users = [u for u in users if can_edit_user(admin, u)]
        selection = db.get(SquadGrantSelection, grant.id)
        if selection:
            selected_ids = set(json.loads(selection.user_ids))
            users = [u for u in users if u.id in selected_ids]
        expected = {
            (row["id"], row["steam_id"]) for row in json.loads(grant.recipients)
        }
        if {(u.id, u.steam_id) for u in users} != expected:
            raise HTTPException(
                409, "Состав игроков изменился. Подготовьте и подтвердите новый список."
            )
        actor = admin.display_name or admin.username
        result = apply_credit(
            db,
            users,
            servers,
            grant.amount_cents,
            actor,
            grant.id,
            log_game,
            max_balance,
        )
        grant.state, grant.result = (
            "completed",
            json.dumps(result, ensure_ascii=False, allow_nan=False),
        )
        db.flush()
        return result

    from squad_scheduler import add_schedule_routes

    add_schedule_routes(router, require_senior, get_db, read_body)
    return router
