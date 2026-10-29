"""Monthly group activity from observed sessions, assemblies and roster snapshots."""

import json
from datetime import datetime, time, timedelta, timezone
from itertools import pairwise

from fastapi import HTTPException
from sqlalchemy import Column, DateTime, Text

from database import Base
from squad_history_admin import GROUPS, MOSCOW, iso
from squad_monitor import SERVER_IDS
from squad_play_history import SquadPlaySession


class GroupRosterSnapshot(Base):
    __tablename__ = "group_roster_history"
    observed_at = Column(DateTime, primary_key=True)
    payload = Column(Text, nullable=False)


def capture_groups(db, members, participant_role, cargo_role, now):
    """Called only after a complete, successful Discord roster import."""
    groups = {
        "all": [],
        **{f"group_{i}": [] for i in range(len(GROUPS))},
        "unassigned": [],
    }
    for sid, _, _, role_ids in members:
        if participant_role not in role_ids or cargo_role in role_ids:
            continue
        groups["all"].append(sid)
        matched = [f"group_{i}" for i, (_, ids) in enumerate(GROUPS) if ids & role_ids]
        for group in matched or ["unassigned"]:
            groups[group].append(sid)
    row = db.get(GroupRosterSnapshot, now)
    if row is None:
        db.add(GroupRosterSnapshot(observed_at=now, payload=json.dumps(groups)))


def month_bounds(value, now):
    try:
        if len(value) != 7:
            raise ValueError
        local = datetime.strptime(value, "%Y-%m").replace(tzinfo=MOSCOW)
        if local.strftime("%Y-%m") != value:
            raise ValueError
        following = (local.replace(day=28) + timedelta(days=4)).replace(day=1)
        start = local.astimezone(timezone.utc).replace(tzinfo=None)
        end = following.astimezone(timezone.utc).replace(tzinfo=None)
        if start > now:
            raise ValueError
        return start, min(end, now)
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(
            400, "Укажите прошедший или текущий месяц в формате ГГГГ-ММ."
        )


def merged_seconds(intervals):
    """Union observations so overlapping sessions on different servers count once."""
    result, last = 0, None
    for start, end in sorted(intervals):
        if last is None or start > last:
            result += max(0, (end - start).total_seconds())
            last = end
        elif end > last:
            result += (end - last).total_seconds()
            last = end
    return result


def roster_dynamics(db, group_id, start, end):
    before = (
        db.query(GroupRosterSnapshot)
        .filter(GroupRosterSnapshot.observed_at <= start)
        .order_by(GroupRosterSnapshot.observed_at.desc())
        .first()
    )
    rows = (
        db.query(GroupRosterSnapshot)
        .filter(
            GroupRosterSnapshot.observed_at > start,
            GroupRosterSnapshot.observed_at <= end,
        )
        .order_by(GroupRosterSnapshot.observed_at)
        .all()
    )
    if before and start - before.observed_at > timedelta(hours=2):
        before = None
    if before:
        rows.insert(0, before)
    if not rows:
        return {"available": False, "started_at": None, "ended_at": None}
    sets = [set(json.loads(row.payload).get(group_id, [])) for row in rows]
    joined, left = set(), set()
    for previous, current in pairwise(sets):
        joined.update(current - previous)
        left.update(previous - current)
    return {
        "available": True,
        "started_at": iso(rows[0].observed_at),
        "ended_at": iso(rows[-1].observed_at),
        "start_count": len(sets[0]),
        "end_count": len(sets[-1]),
        "joined": len(joined),
        "left": len(left),
        "has_month_start": before is not None,
        "observations": len(rows),
    }


def monthly_statistics(db, users, group_id, month, now):
    from groups_admin import assembly_summary

    start, end = month_bounds(month, now)
    linked = {u.steam_id: u for u in users if u.steam_id}
    intervals = {u.id: [] for u in users}
    seen = set()
    servers = {}
    rows = (
        db.query(SquadPlaySession)
        .filter(
            SquadPlaySession.steam_id.in_(linked),
            SquadPlaySession.server_id.in_(SERVER_IDS),
            SquadPlaySession.started_at < end,
            SquadPlaySession.last_seen_at >= start,
        )
        .all()
    )
    for row in rows:
        first = max(start, row.started_at)
        last = min(end, row.last_seen_at, row.ended_at or row.last_seen_at)
        if last < first:
            continue
        uid = linked[row.steam_id].id
        seen.add(uid)
        intervals[uid].append((first, last))
        server = servers.setdefault(
            row.server_id,
            {
                "id": row.server_id,
                "name": row.server_name,
                "intervals": {},
                "members": set(),
            },
        )
        server["intervals"].setdefault(uid, []).append((first, last))
        server["members"].add(uid)
    totals = {
        key: 0
        for key in (
            "present",
            "absent_with_report",
            "absent_without_report",
            "unknown",
            "exempted",
        )
    }
    per_user = {u.id: {key: 0 for key in totals} for u in users}
    assemblies = []
    day = start.replace(tzinfo=timezone.utc).astimezone(MOSCOW).date()
    while (
        datetime.combine(day, time.min, MOSCOW)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
        < end
    ):
        finish = (
            datetime.combine(day, time(22), MOSCOW)
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
        if day.weekday() == 5 and finish <= now and finish <= end:
            summary = assembly_summary(db, users, now, event_date=day.isoformat())
            assemblies.append(
                {
                    k: summary[k]
                    for k in (
                        "date",
                        "counts",
                        "reports_ready",
                        "membership_complete",
                        "battlemetrics_coverage",
                        "voice_coverage",
                    )
                }
            )
            for row in summary["members"]:
                per_user[row["user_id"]][row["status"]] += 1
                totals[row["status"]] += 1
        day += timedelta(days=1)
    denominator = totals["present"] + totals["absent_without_report"]
    return {
        "month": month,
        "group_id": group_id,
        "starts_at": iso(start),
        "ends_at": iso(end),
        "groups_basis": "latest_discord_snapshot",
        "members_count": len(users),
        "linked_members": len(linked),
        "observed_members": len(seen),
        "observed_hours": round(
            sum(merged_seconds(v) for v in intervals.values()) / 3600, 2
        ),
        "attendance": totals,
        "attendance_known_cases": denominator,
        "incomplete_assemblies": sum(
            not a["membership_complete"]
            or not a["reports_ready"]
            or not (a["battlemetrics_coverage"] or a["voice_coverage"])
            for a in assemblies
        ),
        "attendance_percent": round(100 * totals["present"] / denominator, 1)
        if denominator
        else None,
        "assemblies": assemblies,
        "roster": roster_dynamics(db, group_id, start, end),
        "members": [
            {
                "user_id": u.id,
                "name": u.display_name or u.username,
                "steam_linked": bool(u.steam_id),
                "observed": u.id in seen,
                "hours": round(merged_seconds(intervals[u.id]) / 3600, 2),
                "attendance": per_user[u.id],
            }
            for u in users
        ],
        "servers": [
            {
                "id": s["id"],
                "name": s["name"],
                "members_count": len(s["members"]),
                "hours": round(
                    sum(merged_seconds(v) for v in s["intervals"].values()) / 3600, 2
                ),
            }
            for s in servers.values()
        ],
        "notice": "Активность и сборы распределены по текущим группам Discord. Часы отражают только сохранённые наблюдения; отсутствие записей не доказывает отсутствие игрока. Рапорты, освобождённые и неизвестные случаи исключены из процента посещаемости. Динамика состава доступна только с начала её записи.",
    }
