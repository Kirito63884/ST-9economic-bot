"""Read-only source freshness: successful data and latest attempts are distinct."""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from database import PROJECT_DIR

from protocol_award_exports import ProtocolAwardExport, ProtocolExportHealth
from protocol_clan_source import ProtocolClanStats
from squad_routes import load_snapshot


def freshness(stamp, now, ttl):
    if stamp is None:
        return {"checked_at": None, "age_seconds": None, "state": "missing"}
    if isinstance(stamp, str):
        try:
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return {"checked_at": None, "age_seconds": None, "state": "missing"}
    if isinstance(stamp, datetime):
        stamp = (
            stamp.replace(tzinfo=timezone.utc).timestamp()
            if stamp.tzinfo is None
            else stamp.timestamp()
        )
    age = now - stamp
    return {
        "checked_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        "age_seconds": max(0, int(age)),
        "state": "fresh" if 0 <= age <= ttl else "stale",
    }


def source_status(db, now=None):
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    clan = db.get(ProtocolClanStats, 1)
    sources = [
        {
            "name": "Protocol · состав",
            **freshness(clan.checked_at if clan else None, now, 86400),
            "status": clan.status if clan else "pending",
            "attempted_at": clan.attempted_at.isoformat() + "Z" if clan else None,
        }
    ]
    health = db.get(ProtocolExportHealth, 1)
    snapshots = db.query(ProtocolAwardExport).all()
    # A recent success in one category must not make a stale category look fresh.
    for row in sorted(snapshots, key=lambda row: row.key):
        sources.append(
            {
                "name": "Protocol · " + row.key,
                **freshness(row.checked_at, now, 86400),
                "status": health.status if health else "pending",
                "historical": bool(row.week),
                "attempted_at": health.attempted_at.isoformat() + "Z"
                if health
                else None,
            }
        )
    if not snapshots:
        sources.append(
            {
                "name": "Protocol · выгрузки",
                **freshness(None, now, 86400),
                "status": health.status if health else "pending",
            }
        )
    from medals import MedalMonitor, MedalBotHealth

    monitor = db.get(MedalMonitor, 1)
    data = json.loads(monitor.payload) if monitor else {}
    sources.append(
        {
            "name": "SQSTAT · статистика матчей",
            **freshness(data.get("checked_at"), now, 3600),
            "status": data.get("status", "pending"),
        }
    )
    voice = db.get(MedalBotHealth, 1)
    sources.append(
        {
            "name": "Discord · голосовая активность",
            **freshness(voice.checked_at if voice else None, now, 120),
            "status": "ready" if voice and voice.connected else "unavailable",
        }
    )
    for row in load_snapshot(now):
        sources.append(
            {
                "name": "BattleMetrics · " + row["name"],
                **freshness(row["updated_at"], now, 90),
                "status": row["status"],
            }
        )
    return sources


def may_view_statistics(user):
    """One policy for page, API and navigation; roles are read from the database."""
    if not user or user.deleted_at is not None or not user.is_admin:
        return False
    from main import is_senior_or_super

    return bool(is_senior_or_super(user))


def make_router(get_user, get_db):
    router = APIRouter()

    def require_statistics_admin(user=Depends(get_user)):
        if not user:
            raise HTTPException(401, "Войдите на сайт для просмотра статистики")
        if not may_view_statistics(user):
            raise HTTPException(
                403, "Статистика доступна только главным и старшим администраторам"
            )
        return user

    @router.get("/statistics", response_class=HTMLResponse)
    def page(request: Request, user=Depends(require_statistics_admin)):
        templates = Jinja2Templates(directory=str(PROJECT_DIR / "templates"))
        templates.env.globals["may_view_statistics"] = may_view_statistics
        return templates.TemplateResponse(
            request=request, name="statistics.html", context={"user": user}
        )

    @router.get("/api/statistics/freshness")
    def status(user=Depends(require_statistics_admin), db=Depends(get_db)):
        return JSONResponse(
            {"sources": source_status(db)}, headers={"Cache-Control": "no-store"}
        )

    return router
