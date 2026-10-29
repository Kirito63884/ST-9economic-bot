"""Senior-admin lookup and reviewed credits against saved Squad sessions.

An unfinished session ends at its last observation for this lookup. Neither an
open connection nor match participation proves presence after that observation.
All wallet writes use the existing transactional bulk confirmation mechanism.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, or_

from bulk_operations import bulk_preview
from database import User
from squad_play_history import SquadPlaySession
from squad_routes import amount_cents, apply_credit

MOSCOW = ZoneInfo("Europe/Moscow")
GRANT_PATH = "/api/admin/squad/history/grant"


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_moment(value):
    """Require an explicit offset; browser inputs are submitted as Moscow time."""
    try:
        if not isinstance(value, str) or len(value) > 64 or "T" not in value:
            raise ValueError
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(400, "Укажите дату и время с часовым поясом.")
    if moment > utcnow():
        raise HTTPException(400, "Для просмотра истории выберите прошедшее время.")
    return moment


def iso(value):
    return value.isoformat() + "Z" if value else None


def history_at(db, moment, admin, can_edit_user):
    """Return linked active accounts whose saved intervals cover the moment.

    Overlapping/imported rows are merged per user/server. Across servers the
    same account remains one recipient. Empty results mean unknown coverage,
    never a confirmed offline roster.
    """
    base = (
        db.query(SquadPlaySession, User)
        .join(User, User.steam_id == SquadPlaySession.steam_id)
        .filter(User.deleted_at.is_(None))
    )
    first, last = base.with_entities(
        func.min(SquadPlaySession.started_at), func.max(SquadPlaySession.last_seen_at)
    ).one()
    rows = (
        base.filter(
            SquadPlaySession.started_at <= moment,
            SquadPlaySession.last_seen_at >= moment,
            or_(
                SquadPlaySession.ended_at.is_(None), SquadPlaySession.ended_at >= moment
            ),
        )
        .order_by(SquadPlaySession.started_at, SquadPlaySession.id)
        .all()
    )
    servers = {}
    accounts = set()
    for session, user in rows:
        server = servers.setdefault(
            session.server_id,
            {
                "id": session.server_id,
                "name": session.server_name,
                "members": {},
            },
        )
        server["name"] = session.server_name
        server["members"][user.id] = {
            "user_id": user.id,
            "name": user.display_name or user.username,
            "steam_id": user.steam_id,
            "can_select": can_edit_user(admin, user),
            "started_at": iso(session.started_at),
            "last_seen_at": iso(session.last_seen_at),
            "ended_at": iso(session.ended_at),
        }
        accounts.add(user.id)
    return {
        "at": iso(moment),
        "at_msk": moment.replace(tzinfo=timezone.utc).astimezone(MOSCOW).isoformat(),
        "timezone": "Europe/Moscow",
        "source": "Сохранённая история подключений BattleMetrics",
        "history_from": iso(first),
        "history_through": iso(last),
        "members_count": len(accounts),
        "servers": [
            {
                **s,
                "members": sorted(
                    s["members"].values(),
                    key=lambda u: (u["name"].casefold(), u["user_id"]),
                ),
            }
            for s in sorted(
                servers.values(), key=lambda s: (s["name"].casefold(), s["id"])
            )
        ],
    }


def make_router(
    get_user, get_db, may_grant, can_edit_user, read_body, log_game, max_balance
):
    router = APIRouter()

    def require_senior(user=Depends(get_user)):
        if not user or not user.is_admin or not may_grant(user):
            raise HTTPException(
                403, "История онлайна доступна главному и старшим администраторам."
            )
        return user

    @router.get("/api/admin/squad/history")
    async def lookup(
        at: str, response: Response, admin=Depends(require_senior), db=Depends(get_db)
    ):
        response.headers["Cache-Control"] = "private, no-store"
        return history_at(db, parse_moment(at), admin, can_edit_user)

    @router.post(GRANT_PATH)
    @bulk_preview
    async def grant(
        request: Request, admin=Depends(require_senior), db=Depends(get_db)
    ):
        body = await read_body(request)
        moment = parse_moment(body.get("at"))
        cents = amount_cents(body.get("amount"))
        reason = body.get("reason")
        if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500:
            raise HTTPException(400, "Укажите причину начисления от 3 до 500 символов.")
        server_ids = body.get("server_ids")
        if (
            not isinstance(server_ids, list)
            or not 1 <= len(server_ids) <= 100
            or any(
                not isinstance(sid, str) or not 1 <= len(sid) <= 32
                for sid in server_ids
            )
        ):
            raise HTTPException(400, "Выберите серверы из результатов поиска.")
        roster = history_at(db, moment, admin, can_edit_user)
        by_server = {s["id"]: s for s in roster["servers"]}
        if not set(server_ids) <= by_server.keys():
            raise HTTPException(409, "История серверов изменилась. Повторите поиск.")
        selected = set(body["user_ids"])
        members = {
            member["user_id"]: member
            for sid in set(server_ids)
            for member in by_server[sid]["members"]
        }
        if not selected <= members.keys():
            raise HTTPException(
                409, "Для выбранного игрока нет истории на это время и сервер."
            )
        if any(not members[uid]["can_select"] for uid in selected):
            raise HTTPException(403, "Нельзя изменять баланс владельца.")
        users = db.query(User).filter(User.id.in_(selected)).order_by(User.id).all()
        servers = [by_server[sid] for sid in sorted(set(server_ids))]
        label = (
            moment.replace(tzinfo=timezone.utc)
            .astimezone(MOSCOW)
            .strftime("%d.%m.%Y %H:%M:%S МСК")
        )
        context = f"Онлайн на {label}. Причина: {reason.strip()}"
        actor = admin.display_name or admin.username

        def history_log(db, user_id, game_type, result, amount, details):
            player_servers = ", ".join(
                s["name"]
                for s in servers
                if any(m["user_id"] == user_id for m in s["members"])
            )
            log_game(
                db,
                user_id,
                game_type,
                result,
                amount,
                f"Squad: начисление по истории от {actor}; {player_servers}; {context}",
            )

        result = apply_credit(
            db,
            users,
            servers,
            cents,
            actor,
            "история " + iso(moment),
            history_log,
            max_balance,
        )
        # Included in the preview hash: a changed Steam binding, server or saved
        # interval invalidates confirmation even if recipient balances match.
        result["evidence"] = [
            {
                "server_id": s["id"],
                "name": s["name"],
                "members": [m for m in s["members"] if m["user_id"] in selected],
            }
            for s in servers
        ]
        result["context"] = (
            context + ". Серверы: " + ", ".join(s["name"] for s in servers)
        )
        db.flush()
        return result

    return router
