"""Read-only clan groups, historical online intervals and assembly review."""

from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response

from assembly_attendance import (
    AssemblyAssessment,
    AssemblyReportCheck,
    AssemblyWindow,
    _json,
    battlemetrics_coverage,
    voice_coverage,
    presence_for_member,
)
from database import User
from roster_membership import cargo_user_ids, is_cargo_user
from squad_history_admin import (
    MOSCOW,
    group_catalog,
    history_at,
    iso,
    member_groups,
    parse_moment,
)
from squad_play_history import SquadPlaySession
from squad_monitor import SERVER_IDS


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def current_roster(db):
    users = (
        db.query(User)
        .filter(User.deleted_at.is_(None), ~User.id.in_(cargo_user_ids(db)))
        .order_by(User.id)
        .all()
    )
    roles = member_groups(db, {str(u.discord_id) for u in users})
    return users, roles


def latest_assembly(now):
    """Last completed Saturday, always 19:00–22:00 Moscow time."""
    local = now.replace(tzinfo=timezone.utc).astimezone(MOSCOW)
    day = local.date() - timedelta(days=(local.weekday() - 5) % 7)
    end = datetime.combine(day, time(22), MOSCOW)
    if local < end:
        day -= timedelta(days=7)
        end -= timedelta(days=7)
    start = datetime.combine(day, time(19), MOSCOW)
    return (
        day.isoformat(),
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def saved_time(value):
    try:
        if not isinstance(value, str):
            return None
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            moment.astimezone(timezone.utc).replace(tzinfo=None)
            if moment.tzinfo
            else moment
        )
    except (ValueError, TypeError, OverflowError):
        return None


def assembly_membership(event, check, start, end, matching_window, assessments):
    """Use membership observed during the assembly, never today's roster.

    A report-topic refresh after the event lists today's guild members. It is
    valid for checking reports but cannot establish past guild membership.
    Discord voice observations may also prove membership during the window.
    Playing on a game server alone does not establish Discord membership.
    """
    expected = set()
    complete = False

    def ids(values):
        return {str(value) for value in values if str(value).isdigit()}

    if matching_window and event.member_ids is not None:
        expected.update(ids(_json(event.member_ids, [])))
        complete = True
    if matching_window:
        health = _json(event.healthy_servers, {})
        roster = health.get("roster")
        if health.get("roster_basis") == "during_event" and isinstance(roster, dict):
            expected.update(ids(roster))
            complete = True
    if check and check.checked_at and start <= check.checked_at < end:
        expected.update(ids(_json(check.member_ids, [])))
        complete = True
    observed = set(expected)
    if matching_window:
        for key, presence in _json(event.attended, {}).items():
            if not key.startswith("discord:") or not isinstance(presence, dict):
                continue
            moment = saved_time(presence.get("at"))
            if moment and start <= moment < end:
                observed.update(ids([key.removeprefix("discord:")]))
        for row in assessments.values():
            if (
                row.outcome == "present_voice"
                and row.attended_at
                and start <= row.attended_at < end
            ):
                observed.add(str(row.discord_id))
    return expected, observed, complete


def assembly_summary(db, users, now):
    day, start, end = latest_assembly(now)
    event = db.get(AssemblyWindow, day)
    check = db.get(AssemblyReportCheck, day)
    matching_window = bool(event and event.starts_at == start and event.ends_at == end)
    reports_ready = bool(
        check
        and check.status == "ready"
        and check.thread_id
        and check.checked_at
        and check.checked_at >= start
        and check.checked_at <= now
    )
    reports = _json(check.reports, {}) if reports_ready else {}
    frozen = (
        _json(event.healthy_servers, {}).get("roster", {}) if matching_window else {}
    )
    if not isinstance(frozen, dict):
        frozen = {}
    assessments = (
        {
            r.discord_id: r
            for r in db.query(AssemblyAssessment).filter_by(event_date=day)
        }
        if matching_window
        else {}
    )
    expected, observed_members, membership_complete = assembly_membership(
        event, check, start, end, matching_window, assessments
    )
    users = [user for user in users if str(user.discord_id) in observed_members]
    bm_ok, voice_ok = False, False
    if matching_window:
        try:
            bm_ok = battlemetrics_coverage(event)[0]
            voice_ok = voice_coverage(event)[0]
        except (ValueError, TypeError, KeyError):
            pass
    steam_ids = {
        u.id: assessments[str(u.discord_id)].steam_id
        if str(u.discord_id) in assessments and assessments[str(u.discord_id)].steam_id
        else (frozen.get(str(u.discord_id)) or {}).get("steam_id", u.steam_id)
        for u in users
    }
    sessions = (
        db.query(SquadPlaySession)
        .filter(
            SquadPlaySession.steam_id.in_({sid for sid in steam_ids.values() if sid}),
            SquadPlaySession.server_id.in_(SERVER_IDS),
            SquadPlaySession.started_at < end,
            SquadPlaySession.last_seen_at >= start,
        )
        .all()
    )
    positive = {}
    for row in sessions:
        if row.ended_at is not None and row.ended_at < start:
            continue
        positive.setdefault(row.steam_id, []).append(
            {
                "server_id": row.server_id,
                "server_name": row.server_name,
                "source": "BattleMetrics",
            }
        )
    result = []
    counts = {
        key: 0
        for key in (
            "present",
            "absent_with_report",
            "absent_without_report",
            "unknown",
            "exempted",
        )
    }
    for user in users:
        sid = str(user.discord_id)
        assessment = assessments.get(sid)
        presence = presence_for_member(event, sid, user) if matching_window else None
        stamp = saved_time(presence.get("at")) if isinstance(presence, dict) else None
        presence = presence if stamp and start <= stamp < end else None
        evidence = list(positive.get(steam_ids[user.id], []))
        if presence:
            evidence.append(presence)
        if (
            assessment
            and assessment.attended_at
            and start <= assessment.attended_at < end
        ):
            evidence.append({"source": "Учёт сбора", "server_id": assessment.server_id})
        report = saved_time(reports.get(sid))
        if assessment and assessment.reported_at:
            report = assessment.reported_at
        accepted = bool(report and report <= start)
        if evidence:
            status = "present"
        elif accepted:
            status = "absent_with_report"
        elif is_cargo_user(db, user) or (
            assessment and assessment.outcome == "exempted"
        ):
            status = "exempted"
        elif (
            sid in expected
            and reports_ready
            and (voice_ok or (bm_ok and steam_ids[user.id]))
        ):
            status = "absent_without_report"
        else:
            status = "unknown"
        counts[status] += 1
        result.append(
            {
                "user_id": user.id,
                "name": user.display_name or user.username,
                "status": status,
                "has_report": accepted,
                "reported_at": iso(report) if accepted else None,
                "evidence": evidence,
            }
        )
    return {
        "date": day,
        "starts_at": iso(start),
        "ends_at": iso(end),
        "members": result,
        "counts": counts,
        "reports_ready": reports_ready,
        "reports_checked_at": iso(check.checked_at) if check else None,
        "battlemetrics_coverage": bm_ok,
        "voice_coverage": voice_ok,
        "window_matches": matching_window,
        "source_status": event.status if event else "missing",
        "groups_basis": "latest_discord_snapshot",
        "membership_complete": membership_complete,
    }


def make_router(get_user, get_db, may_manage, can_edit_user):
    router = APIRouter()

    def senior(user=Depends(get_user)):
        if not user or not user.is_admin or not may_manage(user):
            raise HTTPException(
                403, "Раздел групп доступен главному и старшим администраторам."
            )
        return user

    def choose(db, group_id):
        catalog = dict(group_catalog())
        if group_id not in catalog and group_id != "all":
            raise HTTPException(404, "Группа не найдена.")
        users, roles = current_roster(db)
        if group_id == "all":
            return users
        return [
            u
            for u in users
            if group_id
            in roles.get(str(u.discord_id), {"group_ids": ["unknown"]})["group_ids"]
        ]

    @router.get("/api/admin/groups")
    async def catalog(response: Response, admin=Depends(senior), db=Depends(get_db)):
        response.headers["Cache-Control"] = "private, no-store"
        users, roles = current_roster(db)
        return {
            "groups": [
                {
                    "id": "all",
                    "name": "Все участники · общая сводка",
                    "members_count": len(users),
                },
                *[
                    {
                        "id": key,
                        "name": name,
                        "members_count": sum(
                            key
                            in roles.get(str(u.discord_id), {"group_ids": ["unknown"]})[
                                "group_ids"
                            ]
                            for u in users
                        ),
                    }
                    for key, name in group_catalog()
                ],
            ],
            "groups_basis": "latest_discord_snapshot",
            "groups_checked_at": min(
                (row["groups_checked_at"] for row in roles.values()), default=None
            ),
            "latest_assembly_date": latest_assembly(utcnow())[0],
        }

    @router.get("/api/admin/groups/{group_id}/online")
    async def online(
        group_id: str,
        response: Response,
        at: str | None = None,
        start: str | None = None,
        end: str | None = None,
        admin=Depends(senior),
        db=Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        users = choose(db, group_id)
        if at is not None and start is None and end is None:
            moment, through = parse_moment(at), None
        elif at is None and start is not None and end is not None:
            moment, through = parse_moment(start), parse_moment(end)
            if moment >= through:
                raise HTTPException(400, "Конец периода должен быть позже начала.")
        else:
            raise HTTPException(400, "Выберите момент или начало и конец периода.")
        result = history_at(
            db, moment, admin, can_edit_user, allow_grant=False, through=through
        )
        allowed = {u.id for u in users}
        servers = []
        found = set()
        for server in result["servers"]:
            members = [m for m in server["members"] if m["user_id"] in allowed]
            if members:
                servers.append({**server, "members": members, "groups": []})
                found.update(m["user_id"] for m in members)
        return {
            **result,
            "servers": servers,
            "members_count": len(found),
            "group_id": group_id,
        }

    @router.get("/api/admin/groups/{group_id}/assembly")
    async def assembly(
        group_id: str, response: Response, admin=Depends(senior), db=Depends(get_db)
    ):
        response.headers["Cache-Control"] = "private, no-store"
        return assembly_summary(db, choose(db, group_id), utcnow())

    return router
