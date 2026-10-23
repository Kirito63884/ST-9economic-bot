"""Administrator-only views/downloads of already collected Protocol dumps."""

import json
import re
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from database import User
from protocol_award_exports import ProtocolAwardExport, URL, KINDS, EXTRA_KINDS


def document(row):
    try:
        data = json.loads(row.payload)
        if (
            data.get("source") != URL
            or data.get("schema") != 1
            or data.get("kind") != row.kind
            or data.get("week") != row.week
        ):
            return None
        return data
    except (ValueError, TypeError, AttributeError):
        return None


def ids_for(data):
    ids = data.get("file_ids")
    if ids is None:
        ids = [r.get("steam_id") for r in data.get("rows", [])]
    if (
        not isinstance(ids, list)
        or len(ids) > 5000
        or any(
            not isinstance(s, str) or not re.fullmatch(r"7656\d{13}", s) for s in ids
        )
    ):
        return []
    return list(dict.fromkeys(ids))


def summary(row, data):
    return {
        "key": row.key,
        "kind": row.kind,
        "week": row.week,
        "title": data.get("title")
        or {**KINDS, **EXTRA_KINDS}.get(row.kind, (0, row.kind))[1],
        "final": bool(data.get("final")),
        "checked_at": row.checked_at.isoformat() + "Z",
        "file_verified": bool(data.get("file_verified")),
        "count": data.get("file_count", len(ids_for(data))),
        "numeric_rows": len(data.get("rows", [])),
        "full_file_saved": "file_ids" in data,
        "coverage": data.get("coverage"),
        "candidate_award": data.get("candidate_award"),
        "unit": data.get(
            "unit",
            "hours" if row.kind in ("mg", "sniper", "sapper", "medic") else "count",
        ),
        "source": URL,
    }


def make_router(get_user, get_db):
    router = APIRouter()

    def admin(user):
        if not user:
            raise HTTPException(401)
        if not user.is_admin:
            raise HTTPException(403)

    def get_document(db, key):
        row = db.get(ProtocolAwardExport, key)
        data = document(row) if row else None
        if data is None:
            raise HTTPException(404, "Сохранённая выгрузка не найдена.")
        return row, data

    @router.get("/api/admin/protocol-exports")
    def index(user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        items = []
        for row in db.query(ProtocolAwardExport).order_by(
            ProtocolAwardExport.week.desc(), ProtocolAwardExport.kind
        ):
            data = document(row)
            if data is not None:
                items.append(summary(row, data))
        return {"items": items, "source": URL}

    @router.get("/api/admin/protocol-exports/{key}/download")
    def download(key: str, user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        row, data = get_document(db, key)
        if not data.get("file_verified"):
            raise HTTPException(409, "TXT ещё не подтверждён.")
        if "file_ids" not in data:
            raise HTTPException(
                409,
                "Полный TXT ещё не сохранён новым сборщиком. Дождитесь часового обновления Protocol.",
            )
        filename = "protocol-" + re.sub(r"[^a-z0-9_-]", "-", row.key) + ".txt"
        return Response(
            "".join(s + "\n" for s in ids_for(data)),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="' + filename + '"',
                "Cache-Control": "no-store",
            },
        )

    @router.get("/api/admin/protocol-exports/{key}")
    def detail(
        key: str,
        q: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
        user=Depends(get_user),
        db=Depends(get_db),
    ):
        admin(user)
        row, data = get_document(db, key)
        by_id = {r["steam_id"]: r for r in data.get("rows", [])}
        ids = ids_for(data)
        users = (
            {u.steam_id: u for u in db.query(User).filter(User.steam_id.in_(ids)).all()}
            if ids
            else {}
        )
        entries = []
        term = q.casefold().strip()[:200]
        for position, sid in enumerate(ids, 1):
            numeric = by_id.get(sid, {})
            member = users.get(sid)
            name = numeric.get("name") or (
                (member.display_name or member.username) if member else sid
            )
            if (
                term
                and term
                not in (
                    name
                    + " "
                    + sid
                    + " "
                    + ((member.display_name or member.username) if member else "")
                ).casefold()
            ):
                continue
            entries.append(
                {
                    "steam_id": sid,
                    "name": name,
                    "position": position,
                    "rank": numeric.get("rank"),
                    "value": numeric.get("value"),
                    "user_id": member.id if member else None,
                }
            )
        return {
            **summary(row, data),
            "total": len(entries),
            "offset": offset,
            "rows": entries[offset : offset + limit],
        }

    return router
