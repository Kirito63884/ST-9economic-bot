"""Read-only source freshness: successful data and latest attempts are distinct."""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

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


def make_router(get_user, get_db):
    router = APIRouter()

    @router.get("/api/statistics/freshness")
    def status(user=Depends(get_user), db=Depends(get_db)):
        if not user:
            raise HTTPException(401)
        return JSONResponse(
            {"sources": source_status(db)}, headers={"Cache-Control": "no-store"}
        )

    return router
