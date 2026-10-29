"""Read-only medal catalog, awards and private live progress."""

import asyncio
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Body
from fastapi.responses import FileResponse
from database import User, SessionLocal
from medals import (
    CATALOG,
    MEDAL_CATALOG,
    MANUAL_MEDALS,
    MANUAL_CATALOG,
    BLOCKED_MEDALS,
    ASSET_SUFFIX,
    evaluate,
    progress,
    monitor_status,
    image_path,
)
from award_rewards import prices
from award_role_names import named_catalog
from distinctions_catalog import (
    CATALOG as DISTINCTIONS,
    MANUAL_CATALOG as MANUAL_DISTINCTIONS,
    BLOCKED,
)
from runtime import metadata_transaction
from award_role_names import sync_names, role_ids

role_refresh_lock = asyncio.Lock()

logger = logging.getLogger("strike_team9.medals")


async def medal_checker():
    while True:
        # Persist elapsed review decisions independently of source evaluation.
        # A broken statistics snapshot must not roll back a due award/payment.
        try:
            from distinction_reviews import process_due
            from award_rewards import pay

            async with metadata_transaction() as db:
                process_due(db)
                pay(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Distinction review processing will retry")
        try:
            async with metadata_transaction() as db:
                evaluate(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Medal evaluation will retry")
        await asyncio.sleep(60)


def catalog_context(db):
    from automation_control import setting

    medals = named_catalog(db, {**MANUAL_MEDALS, **MEDAL_CATALOG})
    distinctions = named_catalog(db, {**DISTINCTIONS, **MANUAL_DISTINCTIONS})
    rules = {
        "gold_star": f"{setting(db, 'weekly_gold_rank')}-е место по убийствам за прошлую неделю среди всех игроков Protocol",
        "silver_star": f"{setting(db, 'weekly_silver_rank')}-е место по убийствам за прошлую неделю среди всех игроков Protocol",
        "bronze_star": f"{setting(db, 'weekly_bronze_rank')}-е место по убийствам за прошлую неделю среди всех игроков Protocol",
        "outstanding_service": f"Топ-{setting(db, 'weekly_cmd_rank')} за прошлую неделю по победам командиром стороны",
        "merit": f"Прошлая неделя: топ-{setting(db, 'weekly_merit_kills_rank')} по убийствам, топ-{setting(db, 'weekly_merit_wins_rank')} по победам или топ-{setting(db, 'weekly_merit_damage_rank')} по урону",
        "good_conduct": f"{setting(db, 'medal_good_voice_hours')} часов в войсах клана без AFK и {setting(db, 'medal_good_protocol_hours')} часов на Protocol",
        "infantry_blue": f"{setting(db, 'medal_combat_hours')} часов на Protocol и K/D не ниже {setting(db, 'medal_combat_i_kd')}",
        "infantry_red": f"{setting(db, 'medal_combat_hours')} часов на Protocol и K/D не ниже {setting(db, 'medal_combat_ii_kd')}",
        "infantry_green": f"{setting(db, 'medal_combat_hours')} часов на Protocol и K/D не ниже {setting(db, 'medal_combat_iii_kd')}",
        "purple_heart": f"Не менее {setting(db, 'medal_purple_hours')} часов на Protocol и K/D ниже {setting(db, 'medal_purple_kd_below')}",
        "best_pilot": "1-е место завершённой недели Protocol в разделе «Пилот»",
        "squad_leader": f"Топ-{setting(db, 'weekly_squad_leader_rank')} за прошлую неделю по победам сквадным среди всех игроков Protocol",
        "anti_tank": f"Топ-{setting(db, 'weekly_anti_tank_rank')} за прошлую неделю по уничтожению техники трубами и тандемами",
        "best_crew": f"Топ-{setting(db, 'weekly_best_crew_rank')} по урону за прошлую неделю",
        "medical_aid": f"Топ-{setting(db, 'weekly_merit_revive_rank')} за прошлую неделю по поднятиям",
        "best_medic": "Топ-3 за прошлую неделю по часам на ките медика Medic",
        "best_knife": f"Топ-{setting(db, 'weekly_best_knife_rank')} за прошлую неделю по ножевым убийствам",
    }
    rules["best_driver"] = (
        "1-е место завершённой недели Protocol в разделе «Экипаж» (мехвод)"
    )
    for target in (medals, distinctions):
        for code, rule in rules.items():
            if code in target:
                target[code] = (target[code][0], rule, *target[code][2:])
    return {
        "catalog": medals,
        "manual_medals": MANUAL_MEDALS,
        "distinctions": distinctions,
        "manual_distinctions": MANUAL_DISTINCTIONS,
        "blocked": {**BLOCKED, **BLOCKED_MEDALS},
        "asset_suffix": ASSET_SUFFIX,
        "award_images": {
            code: image_path(code, db) for code in {**CATALOG, **MANUAL_CATALOG}
        },
        "rewards": prices(db),
    }


def make_router(get_user, get_db, templates):
    router = APIRouter()
    from award_design import make_router as make_design_router

    router.include_router(make_design_router())
    from protocol_exports_admin import make_router as make_exports_router

    router.include_router(make_exports_router(get_user, get_db))

    def target(db, viewer, uid):
        if not viewer:
            raise HTTPException(401)
        result = (
            db.query(User).filter(User.id == uid, User.deleted_at.is_(None)).first()
        )
        if not result:
            raise HTTPException(404)
        if result.id != viewer.id and not viewer.is_admin:
            raise HTTPException(403, "Прогресс доступен игроку и администраторам")
        return result

    @router.get("/medals")
    async def catalog(request: Request, user=Depends(get_user), db=Depends(get_db)):
        return templates.TemplateResponse(
            request=request,
            name="medals.html",
            context={"user": user, **catalog_context(db)},
        )

    @router.get("/medal-assets/{filename}")
    async def asset(filename: str):
        if filename not in {
            Path(image_path(code)).name
            for code in {**CATALOG, **MANUAL_CATALOG}
            if image_path(code)
        }:
            raise HTTPException(404)
        path = Path(__file__).resolve().parent / "medal-assets" / filename
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(
            path,
            media_type={
                ".webp": "image/webp",
                ".jpg": "image/jpeg",
                ".png": "image/png",
            }[path.suffix],
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @router.get("/medals/progress/{uid}")
    async def player_progress(
        uid: int, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        person = target(db, user, uid)
        return templates.TemplateResponse(
            request=request,
            name="medals_progress.html",
            context={"user": user, "target": person},
        )

    @router.get("/api/medals/progress/{uid}")
    async def data(uid: int, user=Depends(get_user), db=Depends(get_db)):
        return {
            **progress(db, target(db, user, uid)),
            "can_revoke": bool(user.is_admin),
        }

    @router.get("/api/admin/medals/status")
    async def status(user=Depends(get_user), db=Depends(get_db)):
        if not user:
            raise HTTPException(401)
        if not user.is_admin:
            raise HTTPException(403)
        return monitor_status(db)

    @router.post("/api/admin/medals/discord-names")
    async def discord_names(request: Request):
        # This route is excluded from the global wallet transaction: Discord I/O
        # must not pause games. Recheck current permissions before applying names.
        import main

        def require_chief(db):
            actor = (
                db.query(User)
                .filter(
                    User.id == request.session.get("user_id"), User.deleted_at.is_(None)
                )
                .first()
            )
            if not actor or not actor.is_admin or not main.is_super_admin(actor):
                raise HTTPException(
                    403, "Сопоставлять роли могут только главные администраторы."
                )
            return actor

        with SessionLocal() as db:
            require_chief(db)
        if request.headers.get("X-ST9-Action") != "discord-names":
            raise HTTPException(403, "Обновите страницу и повторите запрос.")
        if not main.REQUIRED_GUILD_ID or not main.DISCORD_BOT_TOKEN:
            raise HTTPException(503, "Доступ Discord не настроен.")
        if role_refresh_lock.locked():
            raise HTTPException(409, "Сверка уже выполняется. Дождитесь её окончания.")
        async with role_refresh_lock:
            try:
                async with main.get_discord_client(timeout=20) as client:
                    response = await client.get(
                        main.get_discord_api_url(
                            f"/guilds/{main.REQUIRED_GUILD_ID}/roles"
                        ),
                        headers={"Authorization": f"Bot {main.DISCORD_BOT_TOKEN}"},
                    )
                if response.status_code != 200:
                    raise ValueError("Discord unavailable")
                if len(response.content) > 2_000_000:
                    raise ValueError("Role response too large")
                roles = response.json()
                if not isinstance(roles, list) or any(
                    not isinstance(r, dict)
                    or not isinstance(r.get("name"), str)
                    or not r["name"].strip()
                    or len(r["name"]) > 100
                    or not str(r.get("id", "")).isdigit()
                    for r in roles
                ):
                    raise ValueError("Invalid roles")
                found = {str(r["id"]) for r in roles} & set(role_ids().values())
                if not found:
                    raise ValueError("Award roles missing")
            except Exception as exc:
                raise HTTPException(
                    503,
                    "Не удалось получить названия из Discord. Сохранены предыдущие названия; повторите позже.",
                ) from exc
            async with metadata_transaction() as db:
                require_chief(db)
                sync_names(db, roles)
            return {
                "ok": True,
                "matched": len(found),
                "total": len(role_ids()),
                "missing": len(role_ids()) - len(found),
                "message": f"Сопоставлено наград: {len(found)} из {len(role_ids())}. Названия обновлены.",
            }

    @router.post("/api/admin/medals/restore-roles")
    async def restore_roles(
        request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        import main

        if not user or not user.is_admin or not main.is_super_admin(user):
            raise HTTPException(403)
        if request.headers.get("X-ST9-Action") != "restore-roles":
            raise HTTPException(403)
        from award_role_sync import queue_all

        count = queue_all(db)
        return {
            "ok": True,
            "players": count,
            "message": f"Восстановление заработанных ролей поставлено в очередь для {count} игроков. Коины повторно не начисляются.",
        }

    @router.post("/api/admin/medals/{uid}/{code}/revoke", status_code=202)
    async def revoke_medal(
        uid: int,
        code: str,
        request: Request,
        body: dict = Body(...),
        user=Depends(get_user),
        db=Depends(get_db),
    ):
        if not user or not user.is_admin:
            raise HTTPException(403, "Снимать награды могут только администраторы.")
        if request.headers.get("X-ST9-Action") != "revoke-award":
            raise HTTPException(403)
        person = target(db, user, uid)
        if code not in {**CATALOG, **MANUAL_CATALOG}:
            raise HTTPException(404)
        reason = body.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 300:
            raise HTTPException(400, "Причина должна содержать не более 300 символов.")
        from award_revocation import revoke

        row, changed = revoke(db, person, user, code, reason.strip())
        if row is None:
            raise HTTPException(404, "У игрока нет этой награды.")
        return {
            "ok": True,
            "changed": changed,
            "role_pending": row.removed_at is None,
            "message": "Награда снята на сайте. Удаление роли Discord поставлено в очередь. Баланс не изменён.",
        }

    return router
