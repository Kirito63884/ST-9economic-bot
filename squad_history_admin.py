"""Senior-admin lookup and reviewed credits against saved Squad sessions.

An unfinished session ends at its last observation for this lookup. Neither an
open connection nor match participation proves presence after that observation.
All wallet writes use the existing transactional bulk confirmation mechanism.
"""

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, or_

from bulk_operations import bulk_preview
from database import User
from squad_play_history import SquadPlaySession
from squad_routes import amount_cents, apply_credit
from roster_membership import DiscordMemberRoleSnapshot
from assembly_export import GROUPS as ASSEMBLY_GROUPS

# Website classification only. Discord roster publication and role assignment
# are intentionally independent of this training-group membership role.
GROUPS = tuple(
    (name, roles | {"1485577945316720780"} if name == "Учебная группа" else roles)
    for name, roles in ASSEMBLY_GROUPS
)

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


def group_catalog():
    return [(f"group_{index}", name) for index, (name, _) in enumerate(GROUPS)] + [
        ("unassigned", "Без определённой группы"),
        ("unknown", "Нет данных о группе"),
    ]


def member_groups(db, discord_ids):
    """Classify saved Discord roles without claiming they describe past roles."""
    result = {}
    for row in db.query(DiscordMemberRoleSnapshot).filter(
        DiscordMemberRoleSnapshot.discord_id.in_(discord_ids)
    ):
        try:
            roles = json.loads(row.role_ids)
            if not isinstance(roles, list) or any(
                type(role) not in (str, int) for role in roles
            ):
                continue
            roles = set(map(str, roles))
        except (ValueError, TypeError):
            continue
        groups = [
            f"group_{index}" for index, (_, ids) in enumerate(GROUPS) if roles & ids
        ]
        result[row.discord_id] = {
            "group_ids": groups or ["unassigned"],
            "groups_checked_at": iso(row.updated_at),
        }
    return result


def history_at(db, moment, admin, can_edit_user, *, allow_grant=True, through=None):
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
            SquadPlaySession.started_at < through
            if through
            else SquadPlaySession.started_at <= moment,
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
    roles = member_groups(db, {str(user.discord_id) for _, user in rows})
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
            "can_select": bool(allow_grant and can_edit_user(admin, user)),
            **roles.get(
                str(user.discord_id),
                {"group_ids": ["unknown"], "groups_checked_at": None},
            ),
            "started_at": iso(session.started_at),
            "last_seen_at": iso(session.last_seen_at),
            "ended_at": iso(session.ended_at),
        }
        accounts.add(user.id)
    for server in servers.values():
        server["groups"] = [
            {
                "id": key,
                "name": name,
                "members": sorted(
                    (m for m in server["members"].values() if key in m["group_ids"]),
                    key=lambda m: (m["name"].casefold(), m["user_id"]),
                ),
            }
            for key, name in group_catalog()
        ]
    return {
        "at": iso(moment),
        "through": iso(through),
        "at_msk": moment.replace(tzinfo=timezone.utc).astimezone(MOSCOW).isoformat(),
        "timezone": "Europe/Moscow",
        "source": "Сохранённая история подключений BattleMetrics",
        "history_from": iso(first),
        "history_through": iso(last),
        "members_count": len(accounts),
        "can_grant": allow_grant,
        "groups_basis": "latest_discord_snapshot",
        "groups_checked_at": min(
            (r["groups_checked_at"] for r in roles.values()), default=None
        ),
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

    def require_admin(user=Depends(get_user)):
        if not user or not user.is_admin:
            raise HTTPException(403, "История онлайна доступна администраторам.")
        return user

    def require_senior(user=Depends(require_admin)):
        if not may_grant(user):
            raise HTTPException(
                403, "История онлайна доступна главному и старшим администраторам."
            )
        return user

    @router.get("/api/admin/squad/history")
    async def lookup(
        at: str, response: Response, admin=Depends(require_admin), db=Depends(get_db)
    ):
        response.headers["Cache-Control"] = "private, no-store"
        return history_at(
            db,
            parse_moment(at),
            admin,
            can_edit_user,
            allow_grant=bool(may_grant(admin)),
        )

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
        choices = body.get("server_selections")
        if "server_selections" in body:
            if not isinstance(choices, dict) or set(choices) != set(server_ids):
                raise HTTPException(
                    400, "Выбор игроков должен соответствовать выбранным серверам."
                )
            for ids in choices.values():
                if (
                    not isinstance(ids, list)
                    or not 1 <= len(ids) <= 1000
                    or any(type(uid) is not int or uid <= 0 for uid in ids)
                ):
                    raise HTTPException(400, "Неверный список игроков сервера.")
            if set().union(*(set(ids) for ids in choices.values())) != selected:
                raise HTTPException(
                    400, "Общий список игроков не совпадает с выбором по серверам."
                )
        servers = []
        for sid in sorted(set(server_ids)):
            server = by_server[sid]
            ids = set(choices[sid]) if choices is not None else selected
            if choices is not None and not ids <= {
                m["user_id"] for m in server["members"]
            }:
                raise HTTPException(
                    409,
                    "Для игрока нет истории на выбранном сервере и в указанное время.",
                )
            servers.append(
                {
                    **server,
                    "members": [m for m in server["members"] if m["user_id"] in ids],
                }
            )
        members = {
            member["user_id"]: member
            for server in servers
            for member in server["members"]
        }
        if not selected <= members.keys():
            raise HTTPException(
                409, "Для выбранного игрока нет истории на это время и сервер."
            )
        if any(not members[uid]["can_select"] for uid in selected):
            raise HTTPException(403, "Нельзя изменять баланс владельца.")
        users = db.query(User).filter(User.id.in_(selected)).order_by(User.id).all()
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
