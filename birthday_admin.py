"""Admin-only birthday editor sharing the Discord registry and transaction."""

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from admin_hub import log_action
from discord_birthdays import (
    REGISTRY_KEY,
    now_moscow,
    parse_date,
    read,
    validate_registry,
)
from roster_settings import write

GENERATION_KEY = "discord_birthdays:web_generation"
MAX_FILE_BYTES = 128 * 1024


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def revision(db, records):
    return digest([records, read(db, GENERATION_KEY, 0)])


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("В JSON повторяется ключ. Исправьте файл.")
        result[key] = value
    return result


def parse_file(source):
    if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("Размер файла — не более 128 КБ.")
    try:
        return validate_registry(
            json.loads(source.lstrip("\ufeff"), object_pairs_hook=unique_object)
        )
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("Некорректный JSON-файл.") from None


def make_router(get_user, get_db, secret):
    router = APIRouter()
    signer = URLSafeTimedSerializer(secret, salt="birthday-web-import-v1")

    def admin(user):
        if not user or not user.is_admin or user.deleted_at:
            raise HTTPException(403, "Доступ только администраторам.")

    def snapshot(db):
        rows = read(db, REGISTRY_KEY, {})
        return {"records": rows, "revision": revision(db, rows)}

    def current(db, expected):
        result = snapshot(db)
        if expected != result["revision"]:
            raise HTTPException(
                409,
                "Данные изменились на сайте или в Discord. Обновите список и повторите действие.",
            )
        return result["records"]

    async def body(request, fields):
        try:
            value = await request.json()
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Ожидается JSON.") from None
        if not isinstance(value, dict) or set(value) != set(fields):
            raise HTTPException(400, "Некорректные поля запроса.")
        return value

    def persist(db, rows, user, action, description):
        write(db, REGISTRY_KEY, rows, str(user.discord_id))
        # Invalidate even a no-op confirmation, making every preview single-use.
        write(db, GENERATION_KEY, read(db, GENERATION_KEY, 0) + 1)
        log_action(
            db, user, action, description
        )  # Never put full birth dates in audit.
        return snapshot(db)

    @router.get("/api/admin/birthdays")
    async def listing(response: Response, user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        response.headers["Cache-Control"] = "private, no-store"
        return {**snapshot(db), "today": now_moscow().date().isoformat()}

    @router.get("/api/admin/birthdays/export")
    async def export(user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        return Response(
            json.dumps(read(db, REGISTRY_KEY, {}), ensure_ascii=False, indent=2) + "\n",
            media_type="application/json",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": 'attachment; filename="birthdays.json"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/api/admin/birthdays/save")
    async def save(request: Request, user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        data = await body(request, ("id", "name", "date", "revision"))
        rows = current(db, data["revision"])
        try:
            if not isinstance(data["id"], str):
                raise ValueError("Discord ID должен быть строкой.")
            incoming = validate_registry(
                {data["id"]: {"name": data["name"], "date": data["date"]}}
            )
            parse_date(data["date"])
            updated = validate_registry({**rows, **incoming})
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None
        return persist(
            db,
            updated,
            user,
            "birthday_save",
            "Сохранена дата рождения Discord ID " + data["id"],
        )

    def plan(db, data):
        rows = current(db, data["revision"])
        try:
            incoming = parse_file(data["source"])
            validate_registry({**rows, **incoming})
        except (ValueError, TypeError, UnicodeError) as exc:
            raise HTTPException(400, str(exc)) from None
        changes = [
            {"id": uid, "before": rows.get(uid), "after": row}
            for uid, row in incoming.items()
            if rows.get(uid) != row
        ]
        future = []
        for uid, row in incoming.items():
            try:
                parse_date(row["date"])
            except ValueError:
                future.append(uid)
        return (
            rows,
            incoming,
            {
                "changes": changes,
                "added": sum(c["before"] is None for c in changes),
                "changed": sum(c["before"] is not None for c in changes),
                "unchanged": len(incoming) - len(changes),
                "paused": future,
            },
        )

    @router.post("/api/admin/birthdays/import/preview")
    async def preview(request: Request, user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        data = await body(request, ("source", "revision"))
        _, incoming, summary = plan(db, data)
        proof = {
            "actor": user.id,
            "revision": data["revision"],
            "records": digest(incoming),
        }
        return {**summary, "confirmation": signer.dumps(proof)}

    @router.post("/api/admin/birthdays/import/apply")
    async def apply(request: Request, user=Depends(get_user), db=Depends(get_db)):
        admin(user)
        data = await body(request, ("source", "revision", "confirmation"))
        rows, incoming, summary = plan(db, data)
        token = data["confirmation"]
        if not isinstance(token, str) or len(token) > 3000:
            raise HTTPException(400, "Сначала выполните предпросмотр.")
        try:
            proof = signer.loads(token, max_age=300)
        except BadSignature:
            raise HTTPException(
                409, "Подтверждение истекло. Повторите предпросмотр."
            ) from None
        if proof != {
            "actor": user.id,
            "revision": data["revision"],
            "records": digest(incoming),
        }:
            raise HTTPException(
                409, "Файл или автор изменились. Повторите предпросмотр."
            )
        return persist(
            db,
            {**rows, **incoming},
            user,
            "birthday_import",
            f"Импорт дат рождения: новых {summary['added']}, изменено {summary['changed']}.",
        )

    return router
