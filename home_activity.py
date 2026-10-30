"""Public activity: current site games and verified cached Squad presence."""

import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Response
from database import User
from squad_routes import public_roster

FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="15" fill="#121d30"/><path d="M32 7 54 16v17c0 13-12 21-22 26C22 54 10 46 10 33V16Z" fill="#253e63" stroke="#a9c9ff" stroke-width="2"/><path d="M38 14 21 34h12l-7 16 19-24H32Z" fill="#f2cc82"/></svg>"""


def activity(
    db, blackjack, crash, poker, durak, billiards=None, domino=None,
    battleship=None, chess=None, *, now=None
):
    now = time.time() if now is None else now
    threshold = datetime.fromtimestamp(now) - timedelta(minutes=5)
    users = {
        u.id: u
        for u in db.query(User)
        .filter(User.deleted_at.is_(None), User.last_seen >= threshold)
        .all()
    }
    groups = {}

    def add(key, label, url, ids):
        members = []
        for uid in sorted(set(ids)):
            u = users.get(uid)
            if u:
                members.append(
                    {
                        "user_id": u.id,
                        "name": u.display_name or u.username,
                        "discord_name": u.guild_nickname or u.username,
                        "avatar": u.avatar_emoji or "👤",
                    }
                )
        if members:
            group = groups.setdefault(
                key,
                {"key": key, "kind": "site", "label": label, "url": url, "members": []},
            )
            by_id = {p["user_id"]: p for p in group["members"] + members}
            group["members"] = sorted(
                by_id.values(), key=lambda p: (p["name"].casefold(), p["user_id"])
            )

    add(
        "blackjack",
        "Блэкджек",
        "/games/blackjack",
        [
            uid
            for uid, g in blackjack.items()
            if g.get("status") in ("playing", "showdown")
        ],
    )
    add(
        "crash",
        "Crash",
        "/games/crash",
        [
            uid
            for uid, g in crash.items()
            if g.status == "running" and g.get_current_multiplier() < g.crash_point
        ],
    )
    for tid, table in sorted(poker.items()):
        if table.phase not in ("preflop", "flop", "turn", "river"):
            continue
        private = table.is_private
        creator = (
            next(
                (
                    p.get("display_name") or p.get("username")
                    for p in table.players
                    if p["user_id"] == table.creator_id
                ),
                None,
            )
            or "сообщества"
        )
        add(
            "poker-private" if private else "poker:" + tid,
            "Покер · закрытые столы" if private else "Покер · стол " + creator,
            "/games/poker/lobby" if private else "/games/poker/table/" + tid,
            [
                p["user_id"]
                for p in table.players
                if p.get("connected") and not p.get("leave_pending")
            ],
        )
    for tid, table in sorted(durak.tables.items()):
        if table.phase in ("waiting", "finished"):
            continue
        private = table.rules["private"]
        creator = next(
            (p["name"] for p in table.players if p["id"] == table.creator), "сообщества"
        )
        add(
            "durak-private" if private else "durak:" + tid,
            "Дурак · закрытые столы" if private else "Дурак · стол " + creator,
            "/games/durak/lobby" if private else "/games/durak/table/" + tid,
            [p["id"] for p in table.players if not p.get("out") and not p.get("left")],
        )
    for tid, table in sorted(billiards.tables.items()) if billiards is not None else []:
        if table.phase != "playing":
            continue
        private = table.rules["private"]
        creator = next(
            (p["name"] for p in table.players if p["id"] == table.creator), "сообщества"
        )
        practice = table.rules["mode"] == "practice"
        add(
            "billiards-private" if private and not practice else "billiards:" + tid,
            "Бильярд · тренировка"
            if practice
            else "Бильярд · закрытые столы"
            if private
            else "Бильярд · стол " + creator,
            "/games/billiards/lobby"
            if private and not practice
            else "/games/billiards/table/" + tid,
            [p["id"] for p in table.players if not p.get("left")],
        )

    for tid, table in sorted(domino.tables.items()) if domino is not None else []:
        if table.phase != "playing":
            continue
        private = table.rules["private"]
        creator = next(
            (p["name"] for p in table.players if p["id"] == table.creator), "сообщества"
        )
        add(
            "domino-private" if private else "domino:" + tid,
            "Домино · закрытые столы" if private else "Домино · стол " + creator,
            "/games/domino/lobby" if private else "/games/domino/table/" + tid,
            [p["id"] for p in table.players if not p["left"]],
        )

    for tid, table in sorted(battleship.tables.items()) if battleship is not None else []:
        if table.phase != "playing":
            continue
        private = table.rules["private"]
        creator = next(
            (p["name"] for p in table.players if p["id"] == table.creator),
            "сообщества",
        )
        add(
            "battleship-private" if private else "battleship:" + tid,
            "Морской бой · закрытые столы" if private else "Морской бой · стол " + creator,
            "/games/battleship/lobby" if private else "/games/battleship/table/" + tid,
            [p["id"] for p in table.players if not p["left"]],
        )

    for tid, table in sorted(chess.tables.items()) if chess is not None else []:
        if table.phase != "playing":
            continue
        private = table.rules["private"]
        creator = next(
            (p["name"] for p in table.players if p["id"] == table.creator),
            "сообщества",
        )
        add(
            "chess-private" if private else "chess:" + tid,
            "Шахматы · закрытые столы" if private else "Шахматы · стол " + creator,
            "/games/chess/lobby" if private else "/games/chess/table/" + tid,
            [p["id"] for p in table.players if not p["left"]],
        )

    squad = public_roster(db, now=now)
    unavailable = 0
    pending = 0
    for server in squad["servers"]:
        if server["status"] != "ready" or server["stale"]:
            unavailable += 1
            continue
        pending += server["pending"]
        if server["members"]:
            groups["squad:" + server["id"]] = {
                "key": "squad:" + server["id"],
                "kind": "squad",
                "label": server["name"],
                "url": "/squad",
                "updated_at": server["updated_at"],
                "members": server["members"],
            }
    result = list(groups.values())
    return {
        "groups": result,
        "online_members": len({p["user_id"] for g in result for p in g["members"]}),
        "squad_unavailable": unavailable,
        "squad_pending": pending,
        "generated_at": now,
        "refresh_seconds": 10,
    }


def make_router(
    get_db, blackjack, crash, poker, durak, billiards=None, domino=None,
    battleship=None, chess=None
):
    router = APIRouter()

    @router.get("/api/home/activity")
    def current_activity(response: Response, db=Depends(get_db)):
        response.headers["Cache-Control"] = "no-store"
        return activity(
            db, blackjack, crash, poker, durak, billiards, domino, battleship, chess
        )

    @router.get("/favicon.svg", include_in_schema=True)
    def favicon():
        return Response(
            FAVICON,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return router
