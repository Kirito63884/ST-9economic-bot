"""Read-only presentation of every Protocol list already collected by the site."""

import json
import math

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from database import User
from protocol_award_exports import (
    EXTRA_KINDS,
    KINDS,
    ProtocolAwardExport,
)
from protocol_clan_source import CLAN, ProtocolClanStats
from protocol_clan_source import URL as CLAN_URL
from protocol_exports_admin import document, summary
from protocol_week_source import (
    EXTRA_LABELS,
    LABELS,
    ProtocolPublishedWeek,
    previous_key,
)
from protocol_week_source import (
    URL as WEEK_URL,
)

CATEGORIES = {**KINDS, **EXTRA_KINDS, "fob": (0, "FOB за всё время")}
ICONS = {
    "kills": "◎",
    "revives": "✚",
    "wins": "★",
    "cmdwins": "⚑",
    "slwins": "⚐",
    "mg": "◆",
    "sniper": "⌖",
    "sapper": "✹",
    "medic": "✚",
    "veh": "▣",
    "fobweek": "▰",
    "knife": "◇",
    "handat": "➶",
    "pilot": "✈",
    "crew": "◈",
    "rifleman": "◎",
    "atgm15": "➶",
    "fob": "▰",
}


def parse(value):
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def stamp(value):
    return value.isoformat() + "Z" if value else None


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def preview_rows(rows):
    return [
        {
            "name": str(r.get("name") or r.get("steam_id") or "Имя не опубликовано"),
            "rank": r.get("rank"),
            "value": number(r.get("value")),
            "steam_id": r.get("steam_id"),
        }
        for r in rows[:3]
        if isinstance(r, dict)
    ]


def saved_lists(db):
    items = []
    for row in db.query(ProtocolAwardExport).order_by(
        ProtocolAwardExport.week.desc(), ProtocolAwardExport.kind
    ):
        data = document(row)
        if data is None:
            continue
        items.append(
            {
                **summary(row, data),
                "id": "export:" + row.key,
                "group": "achievements",
                "icon": ICONS.get(row.kind, "◈"),
                "preview": preview_rows(data.get("rows", [])),
                "endpoint": "/api/admin/protocol-exports/" + row.key,
            }
        )
    for row in db.query(ProtocolPublishedWeek).order_by(
        ProtocolPublishedWeek.week.desc()
    ):
        data = parse(row.payload)
        if (
            data.get("source") != WEEK_URL
            or data.get("week") != row.week
            or not isinstance(data.get("categories"), dict)
        ):
            continue
        for kind, category in data["categories"].items():
            if not isinstance(category, dict) or not isinstance(
                category.get("rows"), list
            ):
                continue
            ident = "published:" + row.week + ":" + kind
            items.append(
                {
                    "id": ident,
                    "key": ident,
                    "group": "weekly",
                    "kind": kind,
                    "week": row.week,
                    "title": category.get("name", kind),
                    "count": len(category["rows"]),
                    "final": bool(data.get("final")),
                    "checked_at": stamp(row.checked_at),
                    "source": WEEK_URL,
                    "unit": "hours" if kind == "driver_hours" else "count",
                    "preview": preview_rows(category["rows"]),
                    "icon": "⚑",
                    "endpoint": "/api/admin/protocol-lists/" + ident,
                }
            )
    clan = db.get(ProtocolClanStats, 1)
    data = parse(clan.payload) if clan else {}
    if (
        data.get("schema") == 1
        and data.get("source") == CLAN_URL
        and data.get("clan") == CLAN
        and isinstance(data.get("players"), dict)
    ):
        rows = list(data["players"].values())
        items.append(
            {
                "id": "clan:roster",
                "key": "clan:roster",
                "group": "clan",
                "kind": "roster",
                "week": None,
                "title": "Состав STRIKE TEAM 9",
                "count": len(rows),
                "final": False,
                "checked_at": stamp(clan.checked_at),
                "status": clan.status,
                "source": CLAN_URL,
                "unit": "roster",
                "icon": "◉",
                "preview": [],
                "endpoint": "/api/admin/protocol-lists/clan:roster",
            }
        )
    return items


def make_router(get_user, get_db):
    router = APIRouter()

    def admin(user=Depends(get_user)):
        if not user:
            raise HTTPException(401)
        if not user.is_admin:
            raise HTTPException(403)
        return user

    @router.get("/api/admin/protocol-lists")
    async def index(response: Response, user=Depends(admin), db=Depends(get_db)):
        response.headers["Cache-Control"] = "private, no-store"
        items = saved_lists(db)
        return {
            "items": items,
            "default_week": previous_key(),
            "categories": [
                {
                    "kind": kind,
                    "title": value[1],
                    "group": "achievements",
                    "icon": ICONS.get(kind, "◈"),
                    "all_time": kind == "fob",
                }
                for kind, value in CATEGORIES.items()
            ]
            + [
                {
                    "kind": kind,
                    "title": name,
                    "group": "weekly",
                    "icon": "⚑",
                    "all_time": False,
                }
                for kind, name in {**LABELS, **EXTRA_LABELS}.items()
            ]
            + [
                {
                    "kind": "roster",
                    "title": "Состав STRIKE TEAM 9",
                    "group": "clan",
                    "icon": "◉",
                    "all_time": True,
                }
            ],
            "weeks": sorted({r["week"] for r in items if r["week"]}, reverse=True),
        }

    @router.get("/api/admin/protocol-lists/{list_id}")
    async def detail(
        list_id: str,
        response: Response,
        q: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
        user=Depends(admin),
        db=Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        meta = next(
            (
                r
                for r in saved_lists(db)
                if r["id"] == list_id and r["group"] != "achievements"
            ),
            None,
        )
        if meta is None:
            raise HTTPException(404, "Сохранённый список не найден.")
        if meta["group"] == "clan":
            data = parse(db.get(ProtocolClanStats, 1).payload)
            rows = list(data["players"].values())
            rows.sort(
                key=lambda r: (
                    -(number(r.get("hours")) or 0),
                    str(r.get("steam_id", "")),
                )
            )
        else:
            _, week, kind = list_id.split(":", 2)
            rows = parse(db.get(ProtocolPublishedWeek, week).payload)["categories"][
                kind
            ]["rows"]
        ids = {r.get("steam_id") for r in rows if isinstance(r, dict)}
        users = {
            u.steam_id: u for u in db.query(User).filter(User.steam_id.in_(ids)).all()
        }
        result = []
        term = q.strip().casefold()[:200]
        for position, raw in enumerate(rows, 1):
            if not isinstance(raw, dict):
                continue
            sid = str(raw.get("steam_id", ""))
            if not (
                len(sid) == 17
                and sid.isascii()
                and sid.isdigit()
                and sid.startswith("7656")
            ):
                continue
            member = users.get(sid)
            name = str(
                raw.get("name")
                or ((member.display_name or member.username) if member else sid)
            )
            if term and term not in (name + " " + sid).casefold():
                continue
            result.append(
                {
                    "steam_id": sid,
                    "name": name,
                    "position": position,
                    "rank": raw.get("rank"),
                    "value": number(raw.get("value")),
                    "matches": raw.get("matches"),
                    "user_id": member.id if member else None,
                    "metrics": {
                        key: number(raw.get(key))
                        for key in ("hours", "seed_hours", "kills", "deaths", "kd")
                    }
                    if meta["group"] == "clan"
                    else None,
                }
            )
        return {
            **meta,
            "total": len(result),
            "offset": offset,
            "rows": result[offset : offset + limit],
        }

    return router
