"""Read-only TXT exports of confirmed absences, never live Discord inference."""

import os
import re
from datetime import date, timezone

from assembly_attendance import (
    MOSCOW,
    AssemblyAssessment,
    AssemblyReportCheck,
    AssemblyWindow,
    _json,
    battlemetrics_coverage,
    bounds,
    presence_for_member,
    utcnow,
    voice_coverage,
)
from database import User
from roster_membership import DiscordMemberRoleSnapshot

GROUPS = (
    ("Высший командный состав", {"1486041117408497704", "1517217078979989564"}),
    ("Служба безопасности", {"1457398926406844417", "1486041548566036540"}),
    ("Разведывательная группа", {"1457398618192351334"}),
    ("Десантно-штурмовая группа", {"1506355940935467098"}),
    ("Бронетанковая группа", {"1467925735674216478"}),
    ("Группа обеспечения", {"1468548988826292409"}),
    ("Учебная группа", {"1402207362126712864", "1446836770141507696"}),
)


class ExportUnavailable(ValueError):
    pass


def absent_txt(db, event_date, now=None):
    now = now or utcnow()
    day = date.fromisoformat(event_date)
    if day.weekday() != 5:
        raise ExportUnavailable("Выберите субботу.")
    event = db.get(AssemblyWindow, event_date)
    if event is None:
        raise ExportUnavailable("За эту субботу нет записей присутствия.")
    if now < event.ends_at:
        raise ExportUnavailable(
            "Сбор ещё не завершён. Итоговый список пока недоступен."
        )
    check = db.get(AssemblyReportCheck, event_date)
    assessments = db.query(AssemblyAssessment).filter_by(event_date=event_date).all()
    # Prepared assessments already incorporate a verified report topic. Before
    # assessment, require a successful report check made after the deadline.
    prepared = event.status in ("completed", "review_pending") and bool(event.thread_id)
    if prepared:
        reports = {
            row.discord_id: row.reported_at for row in assessments if row.reported_at
        }
    elif (
        check
        and check.status == "ready"
        and check.thread_id
        and check.checked_at
        and check.checked_at >= event.starts_at
    ):
        reports = _json(check.reports, {})
    else:
        raise ExportUnavailable(
            "Рапорты за эту субботу ещё не проверены. Нажмите «Проверить рапорты» и дождитесь завершения."
        )
    bm_ok, _ = battlemetrics_coverage(event)
    voice_ok, _ = voice_coverage(event)
    if not bm_ok and not voice_ok:
        raise ExportUnavailable(
            "Недостаточно записей BattleMetrics и Discord за время сбора. Подтвердить отсутствие нельзя."
        )
    health = _json(event.healthy_servers, {})
    frozen = health.get("roster") or {}
    member_ids = set(_json(event.member_ids, []))
    if not member_ids:
        member_ids = set(frozen) or set(_json(check.member_ids, []) if check else [])
    if not member_ids:
        raise ExportUnavailable(
            "Не сохранён список обязательных участников этого сбора."
        )
    users = {
        str(u.discord_id): u
        for u in db.query(User).filter(User.discord_id.in_(member_ids))
    }
    current_roles = {
        row.discord_id: _json(row.role_ids, [])
        for row in db.query(DiscordMemberRoleSnapshot).filter(
            DiscordMemberRoleSnapshot.discord_id.in_(member_ids)
        )
    }
    names = _json(check.member_names, {}) if check else {}
    groups = {name: [] for name, _ in GROUPS}
    groups["Без определённой группы"] = []
    unknown = 0
    absent_count = 0
    assessed_present = {row.discord_id for row in assessments if row.attended_at}
    attendance = _json(event.attended, {})
    for sid in member_ids:
        user = users.get(sid)
        details = frozen.get(sid) or {}
        steam_id = details.get("steam_id", user.steam_id if user else None)
        if (
            sid in reports
            or sid in assessed_present
            or attendance.get(steam_id)
            or attendance.get("discord:" + sid)
        ):
            continue
        if not voice_ok and not (bm_ok and steam_id):
            unknown += 1
            continue
        roles = set(details.get("roles", current_roles.get(sid, [])))
        group = next(
            (name for name, ids in GROUPS if roles & ids), "Без определённой группы"
        )
        name = (
            details.get("name")
            or names.get(sid)
            or (
                (user.display_name or user.guild_nickname or user.username)
                if user
                else sid
            )
        )
        name = " ".join(str(name).split())
        groups[group].append((name, sid))
        absent_count += 1
    local_start = event.starts_at.replace(tzinfo=timezone.utc).astimezone(MOSCOW)
    local_end = event.ends_at.replace(tzinfo=timezone.utc).astimezone(MOSCOW)
    lines = [
        f"Субботний сбор {day.strftime('%d.%m.%Y')}",
        f"{local_start:%H:%M}–{local_end:%H:%M} МСК",
        "Подтверждённо отсутствовавшие без принятого рапорта",
        f"Всего: {absent_count}",
        "Учитываются появления на Protocol и в голосовых каналах Discord, кроме AFK.",
    ]
    if health.get("roster_basis") != "during_event":
        lines.append(
            "Группы и имена: данные на момент последней проверки; исторический состав не сохранён."
        )
    if unknown:
        lines.append(f"Не включены из-за недостатка персональных данных: {unknown}.")
    for group, people in groups.items():
        if people:
            lines += ["", group.upper()] + [
                f"{i}. {name} — Discord ID: {sid}"
                for i, (name, sid) in enumerate(
                    sorted(people, key=lambda p: (p[0].casefold(), int(p[1]))), 1
                )
            ]
    if not absent_count:
        lines += ["", "Подтверждённых отсутствовавших без рапорта нет."]
    return "\r\n".join(lines) + "\r\n"


def review_txt(db, event_date, now=None):
    """Candidates for human review, never proof of absence or a fine input."""
    from discord_event_log import (
        DiscordChannelCatalog,
        DiscordEventLog,
        DiscordVoicePresence,
    )
    from squad_play_history import SquadPlaySession

    now = now or utcnow()
    day = date.fromisoformat(event_date)
    if day.weekday() != 5:
        raise ExportUnavailable("Выберите субботу.")
    event = db.get(AssemblyWindow, event_date)
    start, end = (event.starts_at, event.ends_at) if event else bounds(day)
    if now < end:
        raise ExportUnavailable("Сбор ещё не завершён.")
    check = db.get(AssemblyReportCheck, event_date)
    if not check or check.status != "ready":
        raise ExportUnavailable("Сначала проверьте рапорты за выбранную субботу.")
    ids = set(_json(event.member_ids, []) if event else []) or set(
        _json(check.member_ids, [])
    )
    reports = _json(check.reports, {})
    users = {
        str(u.discord_id): u for u in db.query(User).filter(User.discord_id.in_(ids))
    }
    groups = {name: [] for name, _ in GROUPS}
    groups["Без определённой группы"] = []
    role_map = {
        r.discord_id: _json(r.role_ids, [])
        for r in db.query(DiscordMemberRoleSnapshot).filter(
            DiscordMemberRoleSnapshot.discord_id.in_(ids)
        )
    }
    frozen = _json(event.healthy_servers, {}).get("roster", {}) if event else {}
    names = _json(check.member_names, {})
    positive = {
        sid for sid in ids if event and presence_for_member(event, sid, users.get(sid))
    }
    guild_id = str(os.getenv("REQUIRED_GUILD_ID") or "")
    # Historical logs are used only as positive evidence, never as proof of absence.
    channels = (
        {
            r.channel_id: r.name
            for r in db.query(DiscordChannelCatalog).filter_by(guild_id=guild_id)
        }
        if guild_id
        else {}
    )

    def non_afk(cid, name):
        value = name or channels.get(str(cid), "")
        return bool(
            cid
            and value
            and value != "Неизвестный голосовой канал"
            and not re.search(r"(?<!\w)(?:afk|афк)(?!\w)", value, re.IGNORECASE)
        )

    if guild_id:
        events = db.query(DiscordEventLog).filter(
            DiscordEventLog.guild_id == guild_id,
            DiscordEventLog.target_id.in_(ids),
            DiscordEventLog.occurred_at >= start,
            DiscordEventLog.occurred_at < end,
            DiscordEventLog.kind.in_(("voice_join", "voice_move", "voice_leave")),
        )
        for row in events:
            detail = _json(row.payload, {})
            if non_afk(row.channel_to, detail.get("channel_to_name")) or non_afk(
                row.channel_from, detail.get("channel_from_name")
            ):
                positive.add(row.target_id)
        # A channel's remembered join during the interval also proves presence.
        for row in db.query(DiscordVoicePresence).filter(
            DiscordVoicePresence.guild_id == guild_id,
            DiscordVoicePresence.target_id.in_(ids),
            DiscordVoicePresence.joined_at >= start,
            DiscordVoicePresence.joined_at < end,
        ):
            if non_afk(row.channel_id, row.channel_name):
                positive.add(row.target_id)
    for sid, user in users.items():
        steam = (frozen.get(sid) or {}).get("steam_id", user.steam_id)
        if (
            steam
            and db.query(SquadPlaySession.id)
            .filter(
                SquadPlaySession.steam_id == steam,
                SquadPlaySession.started_at < end,
                SquadPlaySession.last_seen_at >= start,
            )
            .first()
        ):
            positive.add(sid)
    for sid in ids - reports.keys() - positive:
        user = users.get(sid)
        details = frozen.get(sid) or {}
        roles = set(details.get("roles", role_map.get(sid, [])))
        group = next(
            (name for name, values in GROUPS if roles & values),
            "Без определённой группы",
        )
        name = (
            details.get("name")
            or names.get(sid)
            or (
                (user.display_name or user.guild_nickname or user.username)
                if user
                else sid
            )
        )
        groups[group].append((" ".join(str(name).split()), sid))
    count = sum(map(len, groups.values()))
    lines = [
        f"Сбор {day:%d.%m.%Y} — СПИСОК ДЛЯ РУЧНОЙ ПРОВЕРКИ",
        "Это НЕ подтверждённый список отсутствовавших. Автоматические штрафы по нему не начисляются.",
        "У перечисленных участников нет принятого рапорта по сохранённой проверке и не найдены положительные записи присутствия.",
        "Неполные журналы не доказывают отсутствие. Проверьте каждого участника перед решением о штрафе.",
        f"Дата проверки рапортов (UTC): {check.checked_at}",
        "Группы и имена: сохранённые данные, при их отсутствии — текущие.",
        f"Кандидатов на проверку: {count}",
        f"Найдено положительных записей присутствия для участников: {len(positive)}",
    ]
    for group, people in groups.items():
        if people:
            lines += ["", group.upper()] + [
                f"{i}. {name} — Discord ID: {sid}"
                for i, (name, sid) in enumerate(
                    sorted(people, key=lambda x: (x[0].casefold(), int(x[1]))), 1
                )
            ]
    if not count:
        lines += ["", "Кандидатов на ручную проверку нет."]
    return "\r\n".join(lines) + "\r\n"
