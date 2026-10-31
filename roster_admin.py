"""Senior/chief roster publication editor, with an exact signed preview."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from admin_hub import log_action
from roster_settings import (
    CONFIG_KEY,
    LABELS,
    digest,
    load,
    preview,
    snapshot,
    validate,
    version,
    write,
)


def make_router(get_user, get_db, may_manage, secret):
    router = APIRouter()
    signer = URLSafeTimedSerializer(secret, salt="roster-publication-preview-v1")

    def chief(user):
        if not user or not user.is_admin or not may_manage(user):
            raise HTTPException(
                403,
                "Настройки состава доступны старшим и главным администраторам.",
            )

    async def body(request, allowed):
        try:
            value = await request.json()
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Ожидается JSON.")
        if not isinstance(value, dict) or set(value) != set(allowed):
            raise HTTPException(400, "Некорректные поля запроса.")
        return value

    def plan(db, data):
        if not isinstance(data["revision"], str) or data["revision"] != version(db):
            raise HTTPException(
                409, "Другой администратор изменил настройки. Загрузите их заново."
            )
        try:
            config = validate(data["config"])
            result = preview(db, config)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return config, result

    @router.get("/api/admin/roster-publication")
    async def get_settings(
        response: Response, user=Depends(get_user), db=Depends(get_db)
    ):
        chief(user)
        response.headers["Cache-Control"] = "private, no-store"
        try:
            config = load(db)
            saved = snapshot(db)
        except ValueError:
            raise HTTPException(
                409,
                "Сохранённые настройки состава повреждены. Прежние сообщения не изменены.",
            )
        return {
            "config": config,
            "revision": version(db),
            "labels": LABELS,
            "channel_id": "1555118430858780712",
            "roles": saved.get("roles", []) if saved else [],
            "snapshot_at": saved.get("checked_at") if saved else None,
            "applied_revision": saved.get("applied_revision") if saved else None,
        }

    @router.post("/api/admin/roster-publication/preview")
    async def preview_settings(
        request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        chief(user)
        data = await body(request, ["revision", "config"])
        config, result = plan(db, data)
        proof = {
            "actor": user.id,
            "revision": data["revision"],
            "config": digest(config),
            "snapshot": result["snapshot_revision"],
        }
        return {
            **result,
            "confirmation": signer.dumps(proof),
            "lengths": [len(t.encode("utf-16-le")) // 2 for t in result["texts"]],
        }

    @router.post("/api/admin/roster-publication/settings")
    async def save_settings(
        request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        chief(user)
        data = await body(request, ["revision", "config", "confirmation"])
        config, result = plan(db, data)
        if (
            not isinstance(data["confirmation"], str)
            or len(data["confirmation"]) > 3000
        ):
            raise HTTPException(400, "Сначала выполните предпросмотр.")
        try:
            proof = signer.loads(data["confirmation"], max_age=300)
        except (BadSignature, SignatureExpired):
            raise HTTPException(
                409, "Предпросмотр истёк или недействителен. Выполните его заново."
            )
        expected = {
            "actor": user.id,
            "revision": data["revision"],
            "config": digest(config),
            "snapshot": result["snapshot_revision"],
        }
        if proof != expected:
            raise HTTPException(
                409,
                "Настройки или состав отличаются от предпросмотра. Проверьте результат заново.",
            )
        old = load(db)
        write(db, CONFIG_KEY, config, str(user.discord_id))
        changed = [
            LABELS[key]
            for key in LABELS
            if old["role_ids"][key] != config["role_ids"][key]
        ]
        log_action(
            db,
            user,
            "roster_publication_settings",
            "Изменены настройки канала состава. ID: "
            + (", ".join(changed) or "без изменений")
            + f"; дополнительных ролей: {len(config['extra_roles'])}. Оформление сохранено.",
        )
        return {
            "ok": True,
            "revision": version(db),
            "message": "Настройки сохранены. Бот применит их при следующем обновлении состава (обычно до 10 минут).",
        }

    return router
