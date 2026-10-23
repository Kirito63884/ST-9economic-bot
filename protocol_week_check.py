"""Durable explicit Protocol rechecks; HTTP never holds a wallet write lock."""

import json
import logging
import os
import secrets
from datetime import datetime, timedelta

from admin_hub import AwardWeekControl, log_action
from automation_control import AutomationSetting, source_enabled, utcnow
from database import SessionLocal, User
from runtime import lock_runtime_row

KEY = "protocol_manual_week_check"
LEASE = timedelta(minutes=20)
logger = logging.getLogger("strike_team9.protocol_week_check")


def chief(user):
    return bool(
        user
        and user.deleted_at is None
        and user.is_admin
        and (
            user.is_chief_admin or str(user.discord_id) == os.getenv("ADMIN_DISCORD_ID")
        )
    )


def status(db):
    row = db.get(AutomationSetting, KEY)
    return json.loads(row.value) if row else {"status": "idle"}


def store(db, data):
    row = db.get(AutomationSetting, KEY)
    if row is None:
        db.add(
            AutomationSetting(
                key=KEY, value=json.dumps(data, ensure_ascii=False), updated_at=utcnow()
            )
        )
    else:
        row.value = json.dumps(data, ensure_ascii=False)
        row.updated_at = utcnow()


def request_check(db, actor, now=None):
    from protocol_week_source import previous_key

    now = now or utcnow()
    if not chief(actor):
        raise PermissionError("Доступ только для главного администратора.")
    if not source_enabled(db, "protocol"):
        raise ValueError("Источник Protocol отключён в настройках автоматизации.")
    old = status(db)
    if old.get("status") in ("queued", "running"):
        return old
    data = {
        "id": secrets.token_hex(12),
        "status": "queued",
        "week": previous_key(now),
        "requested_at": now.isoformat(),
        "actor_id": str(actor.discord_id),
        "message": "Проверка поставлена в очередь. Сборщик начнёт её в течение пяти минут.",
    }
    store(db, data)
    log_action(
        db,
        actor,
        "protocol_week_requested",
        f"Запрошена проверка и довыдача Protocol за {data['week']}",
    )
    return data


def fetch_public(now):
    from protocol_clan_source import make_client, request_page
    from protocol_week_source import URL, parse_previous

    with make_client() as client:
        return parse_previous(request_page(client, URL), now)


def finish_error(job, message, now):
    with SessionLocal.begin() as db:
        lock_runtime_row(db)
        current = status(db)
        if current.get("id") == job["id"]:
            current.update(status="error", message=message, finished_at=now.isoformat())
            store(db, current)


def process_requested(now=None, fetch=None, public_fetch=None):
    from award_rewards import pay
    from medals import MedalAward, award_week, closed_sqstat_weeks
    from protocol_award_exports import (
        ClanAuthRequired,
        ClanUnavailable,
        award_exports,
        collect_exports,
        fetch_exports,
    )
    from protocol_week_source import award_published, collect_previous, previous_key

    now = now or utcnow()
    fetch = fetch or fetch_exports
    public_fetch = public_fetch or fetch_public
    with SessionLocal.begin() as db:
        lock_runtime_row(db)
        job = status(db)
        if job.get("status") == "running":
            if now - datetime.fromisoformat(job["started_at"]) < LEASE:
                return False
        elif job.get("status") != "queued":
            return False
        job.update(
            status="running",
            started_at=now.isoformat(),
            message="Читаем топы Protocol и проверяем TXT-выгрузки…",
        )
        store(db, job)
    try:
        if job["week"] != previous_key(now):
            raise ValueError("Неделя сменилась. Запустите проверку заново.")
        with SessionLocal() as db:
            if not source_enabled(db, "protocol"):
                raise ValueError("Источник Protocol отключён.")
        # No open database transaction or runtime lock during external HTTP.
        protocol_error = None
        try:
            documents, failed = fetch(now)
        except Exception as exc:
            logger.warning(
                "Protocol exports unavailable during manual check: %s",
                type(exc).__name__,
            )
            protocol_error = exc
            documents = {}
            failed = []
        valid_keys = {
            key
            for key, doc in documents.items()
            if doc.get("week") == job["week"]
            and doc.get("final")
            and doc.get("file_verified")
        }
        public = None
        warnings = []
        try:
            public = public_fetch(now)
            if public.get("week") != job["week"] or not public.get("final"):
                public = None
        except Exception:
            logger.warning("Published Protocol week unavailable during manual check")
            public = None
        if public is None:
            warnings.append(
                "Недельная таблица Protocol недоступна; используем доступные подтверждённые источники."
            )
        failed_previous = [key for key in failed if key.startswith(job["week"] + ":")]
        if failed_previous:
            warnings.append("Не подтверждены выгрузки: " + ", ".join(failed_previous))
        if protocol_error is not None:

            def failed_fetch(_):
                if isinstance(protocol_error, ClanAuthRequired):
                    raise protocol_error
                raise ClanUnavailable("Protocol exports unavailable")

            collect_exports(now=now, fetch=failed_fetch, force=True)
            warnings.append(
                "Выгрузки Protocol недоступны"
                + (
                    "; требуется protocol_login.py."
                    if isinstance(protocol_error, ClanAuthRequired)
                    else "."
                )
            )
        else:
            collect_exports(now=now, fetch=lambda _: (documents, failed), force=True)
        if public is not None and not collect_previous(
            fetch=lambda **_: public, now=now
        ):
            public = None
            warnings.append("Недельная таблица не сохранена.")
        with SessionLocal() as db:
            reserve = closed_sqstat_weeks(db, now, only_week=job["week"])
        if not valid_keys and public is None and not reserve:
            raise ValueError(
                "Protocol недоступен, а резерв SQSTAT за нужную завершённую неделю ещё не собран полностью. Награды не выданы; повторите проверку после завершения сбора данных."
            )
        if reserve and (
            protocol_error is not None
            or not valid_keys
            or public is None
            or failed_previous
        ):
            warnings.append(
                "Резерв SQSTAT: используются подтверждённые топы за "
                + job["week"]
                + " для недостающих критериев Protocol."
            )
        with SessionLocal.begin() as db:
            lock_runtime_row(db)
            current = status(db)
            if current.get("id") != job["id"]:
                return False
            actor = db.query(User).filter_by(discord_id=job["actor_id"]).first()
            if not chief(actor):
                raise ValueError(
                    "У инициатора больше нет прав главного администратора."
                )
            if not source_enabled(db, "protocol"):
                raise ValueError("Источник Protocol отключён.")
            control = db.get(AwardWeekControl, job["week"])
            if control is None:
                control = AwardWeekControl(week=job["week"], created_at=now)
                db.add(control)
            control.status = "approved"
            control.approved_at = now
            control.approved_by = actor.discord_id
            db.flush()
            from distinction_reviews import DistinctionReview

            existing_reviews = {i for (i,) in db.query(DistinctionReview.id)}
            existing = {i for (i,) in db.query(MedalAward.id)}
            users = db.query(User).filter(User.deleted_at.is_(None)).all()
            try:
                db.info["protocol_export_primary"] = award_exports(
                    db, users, now, only_week=job["week"], verified_keys=valid_keys
                )
                db.info["protocol_week_primary"] = (
                    award_published(db, users, now, only_week=job["week"])
                    if public is not None
                    else set()
                )
                reserve_weeks = closed_sqstat_weeks(db, now, only_week=job["week"])
                for week, document in reserve_weeks:
                    for target in users:
                        award_week(db, target, week, document, now)
                from distinctions import evaluate as award_distinctions

                award_distinctions(db, users, reserve_weeks, now, include_totals=False)
            finally:
                db.info.pop("protocol_export_primary", None)
                db.info.pop("protocol_week_primary", None)
            db.flush()
            added = (
                db.query(MedalAward).filter(~MedalAward.id.in_(existing)).all()
                if existing
                else db.query(MedalAward).all()
            )
            pay(db, now, award_ids={a.id for a in added})
            new_reviews = (
                db.query(DistinctionReview)
                .filter(DistinctionReview.id.notin_(existing_reviews))
                .count()
            )
            current.update(
                status="partial" if warnings else "ready",
                finished_at=now.isoformat(),
                added=len(added),
                queued_distinctions=new_reviews,
                players=len({a.discord_id for a in added}),
                verified=len(valid_keys),
                warnings=warnings,
                sqstat_added=sum(
                    json.loads(a.evidence).get("source") == "SQSTAT" for a in added
                ),
                message=f"Неделя {job['week']}: добавлено наград — {len(added)}, игроков — {len({a.discord_id for a in added})}. Знаков поставлено на подтверждение (24 часа) — {new_reviews}. Выданные роли и объявления переданы Discord-боту.",
            )
            store(db, current)
            log_action(db, actor, "protocol_week_completed", current["message"])
        return True
    except ClanAuthRequired:
        finish_error(
            job,
            "Protocol требует повторный вход: выполните protocol_login.py. Награды этой проверкой не выданы.",
            now,
        )
    except ValueError as exc:
        finish_error(job, str(exc), now)
    except Exception:
        logger.exception("Manual Protocol check failed; award transaction rolled back")
        finish_error(
            job,
            "Проверка не завершена из-за ошибки источника или базы. Сохранённые награды не удалены. Можно повторить проверку.",
            now,
        )
    return False
