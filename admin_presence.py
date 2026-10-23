"""Private, bulk online indicators; no external requests or runtime game lock."""

from datetime import datetime, timedelta, timezone

from database import SessionLocal, User
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from squad_play_history import SquadPlaySession, SquadPresence, presence_for, utcnow


def player_statuses(db, users, *, servers=None, moment=None):
    from squad_routes import load_snapshot

    moment = moment or utcnow()
    servers = (
        load_snapshot(moment.replace(tzinfo=timezone.utc).timestamp())
        if servers is None
        else servers
    )
    steam_ids = {u.steam_id for u in users if u.steam_id}
    cached = (
        {
            r.steam_id: r
            for r in db.query(SquadPresence).filter(
                SquadPresence.steam_id.in_(steam_ids)
            )
        }
        if steam_ids
        else {}
    )
    recent = {}
    if steam_ids:
        rows = (
            db.query(SquadPlaySession)
            .filter(
                SquadPlaySession.steam_id.in_(steam_ids),
                SquadPlaySession.ended_at.is_(None),
                SquadPlaySession.last_seen_at >= moment - timedelta(seconds=90),
                SquadPlaySession.last_seen_at <= moment,
            )
            .order_by(SquadPlaySession.last_seen_at.desc())
        )
        for row in rows:
            recent.setdefault(row.steam_id, row)
    # User.last_seen follows the existing server-local, naive timestamp convention.
    site_now = datetime.now(timezone.utc).astimezone().replace(tzinfo=None)
    result = []
    for user in users:
        game = presence_for(
            db,
            user.steam_id,
            servers,
            moment,
            cached_presence=cached.get(user.steam_id),
            recent_session=recent.get(user.steam_id),
        )
        result.append(
            {
                "id": user.id,
                "site_online": bool(
                    user.last_seen
                    and timedelta(0)
                    <= site_now - user.last_seen
                    <= timedelta(minutes=5)
                ),
                "site_last_seen": user.last_seen.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
                if user.last_seen
                else None,
                "squad_online": game["online"],
                "squad_status": game["presence_status"],
                "server_name": (game.get("current") or {}).get("server_name"),
                "squad_checked_at": game["presence_checked_at"],
            }
        )
    return result


def make_router():
    router = APIRouter()

    @router.get("/api/admin/player-presence")
    def player_presence(request: Request):
        with SessionLocal() as db:
            actor = (
                db.get(User, request.session.get("user_id"))
                if request.session.get("user_id")
                else None
            )
            if not actor:
                raise HTTPException(401)
            if not actor.is_admin or actor.deleted_at:
                raise HTTPException(403)
            users = db.query(User).filter(User.deleted_at.is_(None)).all()
            return JSONResponse(
                {"players": player_statuses(db, users)},
                headers={"Cache-Control": "no-store"},
            )

    @router.post("/api/site-presence")
    async def heartbeat(request: Request):
        from runtime import metadata_transaction

        if request.headers.get("X-ST9-Action") != "site-presence":
            raise HTTPException(403)
        uid = request.session.get("user_id")
        if not uid:
            raise HTTPException(401)
        async with metadata_transaction() as db:
            user = db.get(User, uid)
            if not user or user.deleted_at:
                raise HTTPException(401)
            now = datetime.now(timezone.utc).astimezone().replace(tzinfo=None)
            if not user.last_seen or (now - user.last_seen).total_seconds() >= 60:
                user.last_seen = now
        return {"ok": True}

    return router
