"""Administrator API for automation health, reconciliation, exceptions and policy."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from database import SessionLocal, User
from automation_control import (
    AutomationException,
    DiscordRoleRemoval,
    IdentityLinkHistory,
    SystemAlert,
    add_exception,
    backfill_identity_history,
    settings,
    source_enabled,
    update_settings,
    utcnow,
)


def _stamp(value):
    return (
        value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        if value
        else None
    )


async def _guild_members(main):
    if not main.REQUIRED_GUILD_ID or not main.DISCORD_BOT_TOKEN:
        raise HTTPException(503, "Доступ Discord не настроен.")
    result = []
    after = "0"
    async with main.get_discord_client(timeout=30) as client:
        for _ in range(250):
            response = await client.get(
                main.get_discord_api_url(f"/guilds/{main.REQUIRED_GUILD_ID}/members"),
                params={"limit": 1000, "after": after},
                headers={"Authorization": f"Bot {main.DISCORD_BOT_TOKEN}"},
            )
            if response.status_code != 200:
                raise HTTPException(503, "Discord временно не отдаёт роли участников.")
            page = response.json()
            if not isinstance(page, list):
                raise HTTPException(503, "Discord вернул некорректный список.")
            for member in page:
                user = member.get("user", {}) if isinstance(member, dict) else {}
                roles = member.get("roles") if isinstance(member, dict) else None
                sid = str(user.get("id", ""))
                if sid.isdigit() and not user.get("bot") and isinstance(roles, list):
                    result.append(
                        {
                            "discord_id": sid,
                            "roles": {
                                str(role) for role in roles if str(role).isdigit()
                            },
                            "name": member.get("nick")
                            or user.get("global_name")
                            or user.get("username")
                            or sid,
                            "username": user.get("username") or sid,
                            "global_name": user.get("global_name"),
                            "nick": member.get("nick"),
                        }
                    )
            if len(page) < 1000:
                return result
            after = str(page[-1].get("user", {}).get("id", ""))
            if not after.isdigit():
                raise HTTPException(503, "Discord вернул неполный список.")
    raise HTTPException(503, "Список Discord слишком большой.")


async def _guild_roles(main):
    if not main.REQUIRED_GUILD_ID or not main.DISCORD_BOT_TOKEN:
        raise HTTPException(503, "Доступ Discord не настроен.")
    async with main.get_discord_client(timeout=30) as client:
        response = await client.get(
            main.get_discord_api_url(f"/guilds/{main.REQUIRED_GUILD_ID}/roles"),
            headers={"Authorization": f"Bot {main.DISCORD_BOT_TOKEN}"},
        )
    if response.status_code != 200 or not isinstance(response.json(), list):
        raise HTTPException(503, "Discord временно не отдаёт список ролей.")
    return [
        {"id": str(row.get("id", "")), "name": str(row.get("name") or "")}
        for row in response.json()
        if isinstance(row, dict)
    ]


def make_router(get_user, get_db, is_super_admin):
    router = APIRouter()

    def chief(user):
        if not user or not user.is_admin or not is_super_admin(user):
            raise HTTPException(403, "Доступ только для главных администраторов.")

    @router.get("/api/admin/automation/status")
    async def automation_status(user=Depends(get_user), db=Depends(get_db)):
        if not user or not user.is_admin:
            raise HTTPException(403)
        from medals import MedalAward, MedalBotHealth, MedalMonitor
        from protocol_week_source import ProtocolPublishedHealth
        from award_role_sync import status as role_status
        from award_revocation import removal_status
        from discord_event_log import DiscordEventLog
        from assembly_attendance import AssemblyWindow
        from roster_membership import RosterSyncHealth

        now = utcnow()
        voice = db.get(MedalBotHealth, 1)
        protocol = db.get(ProtocolPublishedHealth, 1)
        monitor = db.get(MedalMonitor, 1)
        roster = db.get(RosterSyncHealth, 1)
        from protocol_week_source import (
            LABELS,
            MAX_EMPTY_RECHECKS,
            ProtocolPublishedWeek,
            empty_categories,
            empty_rechecks,
            previous_key,
        )

        protocol_week = db.get(ProtocolPublishedWeek, previous_key(now))
        try:
            protocol_document = (
                json.loads(protocol_week.payload) if protocol_week else {}
            )
            protocol_empty = empty_categories(protocol_document)
            protocol_rechecks = empty_rechecks(protocol_document)
        except (TypeError, ValueError):
            protocol_empty = []
            protocol_rechecks = {}
        protocol_retry_text = (
            " Повторная проверка раз в 24 часа."
            if any(
                protocol_rechecks.get(code, 0) < MAX_EMPTY_RECHECKS
                for code in protocol_empty
            )
            else " Три дополнительные проверки завершены."
        )
        protocol_detail = (
            (
                "Пустые разделы: "
                + ", ".join(
                    f"{LABELS.get(code, code)} ({protocol_rechecks.get(code, 0)}/{MAX_EMPTY_RECHECKS})"
                    for code in protocol_empty
                )
                + "."
                + protocol_retry_text
            )
            if protocol_empty
            else (
                protocol.last_error
                if protocol and protocol.last_error
                else "Последняя проверка успешна"
            )
        )
        monitor_data = json.loads(monitor.payload) if monitor else {}
        snapshot = Path(__file__).resolve().parent / "squad-data" / "online.json"
        snapshot_at = None
        try:
            snapshot_at = datetime.fromtimestamp(
                snapshot.stat().st_mtime, timezone.utc
            ).replace(tzinfo=None)
        except OSError:
            pass
        roles = role_status(db)
        revoked = removal_status(db)
        removals = db.query(DiscordRoleRemoval)
        logs = db.query(DiscordEventLog).filter(
            DiscordEventLog.sent_at.is_(None), DiscordEventLog.ignored_at.is_(None)
        )
        latest = (
            db.query(AssemblyWindow).order_by(AssemblyWindow.event_date.desc()).first()
        )
        alerts = (
            db.query(SystemAlert)
            .filter(SystemAlert.resolved_at.is_(None))
            .order_by(SystemAlert.id.desc())
            .limit(20)
            .all()
        )
        enabled = {
            key: source_enabled(db, key)
            for key in ("sqstat", "protocol", "battlemetrics")
        }

        def source_service(key, name, ok, checked_at, detail):
            active = enabled[key]
            return {
                "key": key,
                "name": name,
                "enabled": active,
                "ok": bool(ok) if active else True,
                "checked_at": checked_at,
                "detail": detail if active else "Отключено главным администратором",
            }

        services = [
            {
                "key": "discord",
                "name": "Discord-бот",
                "ok": bool(
                    voice
                    and voice.connected
                    and now - voice.checked_at <= timedelta(minutes=3)
                ),
                "checked_at": _stamp(voice.checked_at if voice else None),
                "detail": f"В войсах: {voice.active_voice}"
                if voice
                else "Нет контрольной записи",
            },
            {
                "key": "roster",
                "name": "Состав Discord → сайт",
                "ok": bool(
                    roster
                    and roster.status == "ready"
                    and roster.checked_at
                    and now - roster.checked_at <= timedelta(minutes=70)
                ),
                "checked_at": _stamp(roster.checked_at if roster else None),
                "detail": (
                    f"Участников: {roster.member_count}; добавлено: {roster.created_count}; Груз 200: {roster.cargo_count}"
                    if roster and roster.status == "ready"
                    else (
                        roster.last_error
                        if roster and roster.last_error
                        else "Первая синхронизация ещё не выполнена"
                    )
                ),
            },
            source_service(
                "sqstat",
                "SQSTAT",
                monitor
                and monitor_data.get("status") == "ready"
                and now - monitor.updated_at <= timedelta(minutes=15),
                _stamp(monitor.updated_at if monitor else None),
                monitor_data.get("last_error")
                or ("Профили и матчи обновляются" if monitor else "Нет данных"),
            ),
            source_service(
                "protocol",
                "Protocol · прошлая неделя",
                protocol and protocol.status == "ready",
                _stamp(protocol.checked_at if protocol else None),
                protocol_detail,
            ),
            source_service(
                "battlemetrics",
                "BattleMetrics",
                snapshot_at and now - snapshot_at <= timedelta(minutes=3),
                _stamp(snapshot_at),
                "Онлайн, история игр, часы и посещение сборов",
            ),
            {
                "key": "roles",
                "name": "Роли наград",
                "ok": roles["failed"] == 0 and revoked["failed"] == 0,
                "checked_at": None,
                "detail": f"ожидают {roles['pending']}, ошибок {roles['failed'] + revoked['failed']}",
            },
            {
                "key": "announcements",
                "name": "Объявления о наградах",
                "ok": db.query(MedalAward)
                .filter(
                    MedalAward.sent_at.is_(None), MedalAward.last_error.is_not(None)
                )
                .count()
                == 0,
                "checked_at": None,
                "detail": f"ожидают {db.query(MedalAward).filter(MedalAward.sent_at.is_(None)).count()}",
            },
            {
                "key": "discord_logs",
                "name": "Журнал Discord",
                "ok": logs.filter(DiscordEventLog.last_error.is_not(None)).count() == 0,
                "checked_at": None,
                "detail": f"ожидают {logs.count()}",
            },
        ]
        return {
            "checked_at": _stamp(now),
            "healthy": all(item["ok"] for item in services),
            "services": services,
            "role_sync": roles,
            "revocations": revoked,
            "role_removals": {
                "pending": removals.filter(
                    DiscordRoleRemoval.removed_at.is_(None)
                ).count(),
                "failed": removals.filter(
                    DiscordRoleRemoval.removed_at.is_(None),
                    DiscordRoleRemoval.last_error.is_not(None),
                ).count(),
            },
            "assembly": {
                "event_date": latest.event_date,
                "status": latest.status,
                "processed_at": _stamp(latest.processed_at),
            }
            if latest
            else None,
            "alerts": [
                {
                    "id": r.id,
                    "source": r.source,
                    "level": r.level,
                    "message": r.message,
                    "created_at": _stamp(r.created_at),
                    "sent": bool(r.sent_at),
                    "error": r.last_error,
                }
                for r in alerts
            ],
        }

    async def reconciliation(main, db):
        from medals import MedalAward, ROLE_IDS, MANUAL_ROLES
        from distinctions_catalog import ROLE_IDS as D_ROLES, MANUAL_CATALOG as D_MANUAL
        from award_revocation import active_awards

        mapping = {**ROLE_IDS, **MANUAL_ROLES, **D_ROLES}
        manual_codes = set(MANUAL_ROLES) | set(D_MANUAL)
        automatic_roles = {
            str(role) for code, role in mapping.items() if code not in manual_codes
        }
        users = {
            u.discord_id: u
            for u in db.query(User).filter(User.deleted_at.is_(None)).all()
        }
        expected = {sid: set() for sid in users}
        for sid, code in active_awards(db).with_entities(
            MedalAward.discord_id, MedalAward.medal
        ):
            if sid in expected and code in mapping:
                expected[sid].add(str(mapping[code]))
        members = {row["discord_id"]: row for row in await _guild_members(main)}
        missing = []
        extra = []
        absent = []
        for sid, user in users.items():
            member = members.get(sid)
            if member is None:
                if expected[sid]:
                    absent.append(
                        {
                            "user_id": user.id,
                            "discord_id": sid,
                            "name": user.display_name or user.username,
                            "roles": len(expected[sid]),
                        }
                    )
                continue
            for role in sorted(expected[sid] - member["roles"]):
                missing.append(
                    {
                        "user_id": user.id,
                        "discord_id": sid,
                        "name": member["name"],
                        "role_id": role,
                    }
                )
            for role in sorted((member["roles"] & automatic_roles) - expected[sid]):
                extra.append(
                    {
                        "user_id": user.id,
                        "discord_id": sid,
                        "name": member["name"],
                        "role_id": role,
                    }
                )
        pending = db.query(MedalAward).filter(MedalAward.sent_at.is_(None)).count()
        return {
            "checked_at": _stamp(utcnow()),
            "missing": missing,
            "extra": extra,
            "absent": absent,
            "pending_announcements": pending,
        }

    @router.get("/api/admin/automation/reconcile")
    async def preview_reconciliation(request: Request):
        import main

        with SessionLocal() as db:
            actor = db.get(User, request.session.get("user_id"))
            chief(actor)
            return await reconciliation(main, db)

    @router.post("/api/admin/automation/reconcile", status_code=202)
    async def apply_reconciliation(request: Request):
        import main

        if request.headers.get("X-ST9-Action") != "reconcile-awards":
            raise HTTPException(403)
        with SessionLocal() as read:
            actor = read.get(User, request.session.get("user_id"))
            chief(actor)
            actor_id = actor.discord_id
            preview = await reconciliation(main, read)
        from runtime import metadata_transaction
        from award_role_sync import request_sync

        async with metadata_transaction() as db:
            actor = db.query(User).filter_by(discord_id=actor_id).first()
            chief(actor)
            now = utcnow()
            for item in preview["missing"]:
                request_sync(db, item["discord_id"], now)
            for item in preview["extra"]:
                row = (
                    db.query(DiscordRoleRemoval)
                    .filter_by(discord_id=item["discord_id"], role_id=item["role_id"])
                    .first()
                )
                if row is None:
                    db.add(
                        DiscordRoleRemoval(
                            discord_id=item["discord_id"],
                            role_id=item["role_id"],
                            reason="Лишняя автоматическая роль по сверке сайта",
                            requested_at=now,
                            requested_by=actor_id,
                            retry_at=now,
                        )
                    )
                else:
                    row.removed_at = None
                    row.requested_at = now
                    row.retry_at = now
                    row.last_error = None
            from medals import MedalAward

            for row in db.query(MedalAward).filter(MedalAward.sent_at.is_(None)):
                row.retry_at = now
                row.lease_until = None
        return {
            "ok": True,
            "queued_missing": len(preview["missing"]),
            "queued_extra": len(preview["extra"]),
            "announcements": preview["pending_announcements"],
        }

    @router.post("/api/admin/roster/sync")
    async def sync_roster_now(request: Request):
        """Chief-only complete roster import. Discord I/O happens outside a DB transaction."""
        import main

        if request.headers.get("X-ST9-Action") != "sync-roster":
            raise HTTPException(403)
        with SessionLocal() as read:
            actor = read.get(User, request.session.get("user_id"))
            chief(actor)
            actor_id = actor.discord_id
        roles, members = await asyncio.gather(_guild_roles(main), _guild_members(main))
        raw_members = [
            {
                "user": {
                    "id": row["discord_id"],
                    "username": row["username"],
                    "global_name": row.get("global_name"),
                    "bot": False,
                },
                "nick": row.get("nick"),
                "roles": sorted(row["roles"]),
            }
            for row in members
        ]
        from runtime import metadata_transaction
        from roster_membership import sync_snapshot

        async with metadata_transaction() as db:
            actor = db.query(User).filter_by(discord_id=actor_id).first()
            chief(actor)
            result = sync_snapshot(
                db, roles, raw_members, main.REQUIRED_ROLE_ID, utcnow()
            )
        return {"ok": True, **result}

    @router.get("/api/admin/assembly-reports")
    async def assembly_reports(request: Request):
        from assembly_attendance import AssemblyReportCheck, report_target_day

        raw = request.query_params.get("event_date") or report_target_day().isoformat()
        try:
            day = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "Некорректная дата.")
        if day.weekday() != 5:
            raise HTTPException(400, "Выберите субботу.")
        with SessionLocal() as db:
            actor = db.get(User, request.session.get("user_id"))
            chief(actor)
            row = db.get(AssemblyReportCheck, raw)
            if row is None:
                return {
                    "event_date": raw,
                    "status": "not_requested",
                    "checked_at": None,
                    "next_check": None,
                    "thread_id": None,
                    "excused": [],
                    "without_report": [],
                    "member_count": 0,
                    "report_count": 0,
                }
            try:
                member_ids = json.loads(row.member_ids or "[]")
                names = json.loads(row.member_names or "{}")
                reports = json.loads(row.reports or "{}")
            except (TypeError, ValueError):
                member_ids = []
                names = {}
                reports = {}
            users = (
                {
                    str(user.discord_id): user
                    for user in db.query(User)
                    .filter(User.discord_id.in_(member_ids))
                    .all()
                }
                if member_ids
                else {}
            )

            def item(sid):
                user = users.get(sid)
                return {
                    "discord_id": sid,
                    "user_id": user.id if user else None,
                    "display_name": (
                        user.display_name or user.guild_nickname or user.username
                    )
                    if user
                    else names.get(sid, sid),
                    "username": user.username if user else None,
                    "reported_at": reports.get(sid),
                }

            excused = sorted(
                (item(sid) for sid in member_ids if sid in reports),
                key=lambda x: str(x["display_name"]).casefold(),
            )
            missing = sorted(
                (item(sid) for sid in member_ids if sid not in reports),
                key=lambda x: str(x["display_name"]).casefold(),
            )
            return {
                "event_date": raw,
                "status": row.status,
                "checked_at": _stamp(row.checked_at),
                "next_check": _stamp(row.next_check),
                "thread_id": row.thread_id,
                "last_error": row.last_error,
                "excused": excused,
                "without_report": missing,
                "member_count": len(member_ids),
                "report_count": len(excused),
            }

    @router.get("/api/admin/protocol-week-check")
    async def protocol_check_status(request: Request):
        from fastapi.responses import JSONResponse
        from protocol_week_check import status

        with SessionLocal() as db:
            uid = request.session.get("user_id")
            actor = db.get(User, uid) if uid else None
            chief(actor)
            return JSONResponse(
                status(db), headers={"Cache-Control": "private, no-store"}
            )

    @router.post("/api/admin/protocol-week-check", status_code=202)
    async def protocol_check_request(request: Request):
        from fastapi.responses import JSONResponse
        from protocol_week_check import request_check
        from runtime import metadata_transaction

        async with metadata_transaction() as db:
            uid = request.session.get("user_id")
            actor = db.get(User, uid) if uid else None
            chief(actor)
            try:
                result = request_check(db, actor)
            except PermissionError as exc:
                raise HTTPException(403, str(exc))
            except ValueError as exc:
                raise HTTPException(409, str(exc))
        return JSONResponse(
            result, status_code=202, headers={"Cache-Control": "private, no-store"}
        )

    @router.get("/api/admin/assembly/{event_date}/absent.txt")
    @router.get("/api/admin/assembly/{event_date}/review.txt")
    async def download_assembly_absent(event_date: str, request: Request):
        from fastapi.responses import Response
        from assembly_export import ExportUnavailable, absent_txt, review_txt

        review = request.url.path.endswith("/review.txt")
        with SessionLocal() as db:
            uid = request.session.get("user_id")
            actor = db.get(User, uid) if uid else None
            if not actor or actor.deleted_at is not None or not actor.is_admin:
                raise HTTPException(403, "Доступ только для администраторов.")
            try:
                day = datetime.strptime(event_date, "%Y-%m-%d").date()
                if day.isoformat() != event_date:
                    raise ValueError("format")
            except ValueError:
                raise HTTPException(400, "Некорректная дата.")
            try:
                content = (review_txt if review else absent_txt)(db, event_date)
            except ExportUnavailable as exc:
                raise HTTPException(409, str(exc))
        return Response(
            content=b"\xef\xbb\xbf" + content.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="st9-{"review" if review else "absence"}-{event_date}.txt"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/api/admin/assembly-reports", status_code=202)
    async def queue_assembly_reports(request: Request, body: dict = Body(...)):
        raw = str(body.get("event_date") or "")
        try:
            day = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "Некорректная дата.")
        if day.weekday() != 5:
            raise HTTPException(400, "Выберите субботу.")
        with SessionLocal.begin() as db:
            actor = db.get(User, request.session.get("user_id"))
            chief(actor)
            from assembly_attendance import request_report_check

            request_report_check(db, raw, utcnow())
        return {
            "ok": True,
            "event_date": raw,
            "message": "Проверка рапортов поставлена в очередь.",
        }

    @router.get("/api/admin/automation/settings")
    async def get_settings(user=Depends(get_user), db=Depends(get_db)):
        if not user or not user.is_admin:
            raise HTTPException(403)
        from award_rewards import prices
        from medals import CATALOG, MANUAL_CATALOG
        from award_role_names import named_catalog

        return {
            "settings": {
                key: value
                for key, value in settings(db).items()
                if key != "assembly_grace_hours"
            },
            "rewards": prices(db),
            "awards": {
                code: value[0]
                for code, value in named_catalog(
                    db, {**CATALOG, **MANUAL_CATALOG}
                ).items()
            },
        }

    @router.post("/api/admin/automation/settings")
    async def save_settings(
        body: dict = Body(...), user=Depends(get_user), db=Depends(get_db)
    ):
        chief(user)
        try:
            update_settings(db, body.get("settings", {}), user)
        except ValueError as exc:
            raise HTTPException(400, "Некорректное значение настройки: " + str(exc))
        rewards = body.get("rewards")
        if rewards is not None:
            from award_rewards import AwardRewardPolicy

            row = db.get(AwardRewardPolicy, 1)
            old = json.loads(row.prices) if row else None
            if row is None:
                raise HTTPException(409, "Политика выплат ещё не установлена.")
            if (
                not isinstance(rewards, dict)
                or set(rewards) != set(old)
                or any(
                    type(v) is not int or not 0 <= v <= 100000 for v in rewards.values()
                )
            ):
                raise HTTPException(400, "Некорректная таблица выплат.")
            row.prices = json.dumps(rewards, sort_keys=True)
        return {
            "ok": True,
            "settings": {
                key: value
                for key, value in settings(db).items()
                if key != "assembly_grace_hours"
            },
        }

    @router.get("/api/admin/automation/exceptions")
    async def list_exceptions(user=Depends(get_user), db=Depends(get_db)):
        if not user or not user.is_admin:
            raise HTTPException(403)
        rows = (
            db.query(AutomationException)
            .filter_by(active=True)
            .order_by(AutomationException.id.desc())
            .all()
        )
        users = (
            {
                u.id: u
                for u in db.query(User)
                .filter(User.id.in_({r.user_id for r in rows}))
                .all()
            }
            if rows
            else {}
        )
        return {
            "items": [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    "name": (users[r.user_id].display_name or users[r.user_id].username)
                    if r.user_id in users
                    else str(r.user_id),
                    "scope": r.scope,
                    "target": r.target,
                    "event_date": r.event_date or None,
                    "reason": r.reason,
                    "actor": r.actor_name,
                    "created_at": _stamp(r.created_at),
                }
                for r in rows
            ]
        }

    @router.post("/api/admin/automation/exceptions")
    async def create_exception(
        body: dict = Body(...), user=Depends(get_user), db=Depends(get_db)
    ):
        chief(user)
        target = db.get(User, body.get("user_id"))
        if not target or target.deleted_at is not None:
            raise HTTPException(404)
        reason = body.get("reason")
        scope = body.get("scope")
        code = body.get("target", "")
        event_date = body.get("event_date", "")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise HTTPException(400, "Укажите причину до 500 символов.")
        if scope == "award":
            from medals import CATALOG, MANUAL_CATALOG

            if code not in {**CATALOG, **MANUAL_CATALOG}:
                raise HTTPException(400, "Неизвестная награда.")
        elif scope == "assembly":
            try:
                datetime.strptime(event_date, "%Y-%m-%d")
            except (TypeError, ValueError):
                raise HTTPException(400, "Укажите дату сбора.")
        else:
            raise HTTPException(400, "Неизвестный тип исключения.")
        row = add_exception(db, target, user, scope, code, reason, event_date)
        if scope == "assembly":
            from assembly_attendance import AssemblyAssessment

            assessment = (
                db.query(AssemblyAssessment)
                .filter_by(
                    event_date=event_date, user_id=target.id, outcome="pending_penalty"
                )
                .first()
            )
            if assessment:
                assessment.outcome = "exempted"
        return {"ok": True, "id": row.id}

    @router.post("/api/admin/assembly/{event_date}/finalize")
    async def finalize_assembly(
        event_date: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        chief(user)
        if request.headers.get("X-ST9-Action") != "finalize-assembly":
            raise HTTPException(403)
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Некорректная дата сбора.")
        from assembly_attendance import finalize_penalties, AssemblyWindow

        row = db.get(AssemblyWindow, event_date)
        if not row:
            raise HTTPException(404, "Проверка сбора не найдена.")
        if row.status != "review_pending":
            raise HTTPException(409, "Этот сбор не ожидает подтверждения.")
        result = finalize_penalties(db, event_date)
        from admin_hub import log_action

        log_action(db, user, "assembly_confirmed", {"event_date": event_date, **result})
        return {"ok": True, **result}

    @router.delete("/api/admin/automation/exceptions/{exception_id}")
    async def disable_exception(
        exception_id: int, user=Depends(get_user), db=Depends(get_db)
    ):
        chief(user)
        row = db.get(AutomationException, exception_id)
        if not row:
            raise HTTPException(404)
        row.active = False
        row.disabled_at = utcnow()
        return {"ok": True}

    @router.get("/api/admin/identities/{user_id}")
    async def identity_history(
        user_id: int, user=Depends(get_user), db=Depends(get_db)
    ):
        if not user or not user.is_admin:
            raise HTTPException(403)
        target = db.get(User, user_id)
        if not target:
            raise HTTPException(404)
        backfill_identity_history(db)
        db.flush()
        rows = (
            db.query(IdentityLinkHistory)
            .filter_by(user_id=user_id)
            .order_by(IdentityLinkHistory.valid_from.desc())
            .all()
        )
        return {
            "user_id": user_id,
            "items": [
                {
                    "kind": r.kind,
                    "value": r.value,
                    "valid_from": _stamp(r.valid_from),
                    "valid_to": _stamp(r.valid_to),
                    "actor_id": r.actor_id,
                    "reason": r.reason,
                }
                for r in rows
            ],
        }

    return router
