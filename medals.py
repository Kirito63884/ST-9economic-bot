"""Permanent awards, evidence and notification queue shared by website and bot."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    UniqueConstraint,
)
from database import Base, User, GameHistory

CHANNEL_ID = 1556767291608268840
ROLE_IDS = {
    "iron_cross": 1556774466544210101,
    "gold_star": 1556774763660316672,
    "silver_star": 1556774812439810169,
    "bronze_star": 1556774877741060116,
    "outstanding_service": 1556774958472888482,
    "good_conduct": 1556774982229692461,
    "infantry_blue": 1556775082981072916,
    "infantry_red": 1556775141155930274,
    "infantry_green": 1556775195002404934,
    "merit": 1556775255559774369,
    "purple_heart": 1556775331455836160,
}
ASSET_SUFFIX = {"infantry_red": ".webp"}
MOSCOW = timezone(timedelta(hours=3))
FIRST_WEEK = "2026-09-28"
MANUAL_ROLES = {"iron_cross": 1556774466544210101, "valor": 1556774705082404944}
MANUAL_CATALOG = {
    "valor": (
        "Медаль за отвагу",
        "Выдаётся вручную за проявленный героизм в бою",
        "#f4c95d",
        "✦",
    ),
}
BLOCKED_MEDALS = {}
CATALOG = {
    "iron_cross": (
        "Орден мужества",
        "1000 часов на Protocol и 1000 часов сида за всё время",
        "#cbd5e1",
        "✚",
    ),
    "gold_star": (
        "Золотая звезда",
        "1-е место по убийствам за неделю среди всех игроков Protocol",
        "#f4c95d",
        "★",
    ),
    "silver_star": (
        "Серебряная звезда",
        "2-е место по убийствам за неделю среди всех игроков Protocol",
        "#cbd5e1",
        "★",
    ),
    "bronze_star": (
        "Бронзовая звезда",
        "3-е место по убийствам за неделю среди всех игроков Protocol",
        "#d49463",
        "★",
    ),
    "outstanding_service": (
        "Медаль за выдающиеся заслуги",
        "Топ-3 за неделю по победам командиром стороны",
        "#f3bc70",
        "✦",
    ),
    "good_conduct": (
        "Медаль за отличную службу",
        "500 часов в войсах клана, кроме AFK, и 500 часов на Protocol",
        "#f4dfa2",
        "✓",
    ),
    "infantry_blue": (
        "Медаль за боевые навыки I степени",
        "150 часов на Protocol и K/D не ниже 3,0; также выдаются II и III степени",
        "#60a5fa",
        "I",
    ),
    "infantry_red": (
        "Медаль за боевые навыки II степени",
        "150 часов на Protocol и K/D не ниже 2,0; также выдаётся III степень",
        "#fb7185",
        "II",
    ),
    "infantry_green": (
        "Медаль за боевые навыки III степени",
        "150 часов на Protocol и K/D не ниже 1,5",
        "#6ee7a5",
        "III",
    ),
    "merit": (
        "Медаль за особые заслуги",
        "Недельный топ-10 по убийствам, топ-5 по победам или топ-5 по урону; достаточно одного условия",
        "#e3c56e",
        "✧",
    ),
    "purple_heart": (
        "Пурпурное сердце",
        "Не менее 50 часов на Protocol и K/D ниже 0,5",
        "#c084fc",
        "♥",
    ),
}
from distinctions_catalog import (
    CATALOG as DISTINCTIONS,
    MANUAL_CATALOG as MANUAL_DISTINCTIONS,
    ROLE_IDS as DISTINCTION_ROLES,
    ASSET_SUFFIX as DISTINCTION_SUFFIX,
)

MEDAL_CATALOG = dict(CATALOG)
MANUAL_MEDALS = dict(MANUAL_CATALOG)
CATALOG.update(DISTINCTIONS)
MANUAL_CATALOG.update(MANUAL_DISTINCTIONS)
ROLE_IDS.update(
    {code: role for code, role in DISTINCTION_ROLES.items() if code in DISTINCTIONS}
)
# Import manually assigned distinction roles silently, including unsupported criteria.
MANUAL_ROLES.update(DISTINCTION_ROLES)
ASSET_SUFFIX.update(DISTINCTION_SUFFIX)
from award_rewards import AwardRewardPolicy, AwardRewardPayment
from award_role_names import AwardRoleName, award_name, named_catalog  # noqa: F401 - Register shared ORM tables before schema creation.
from award_role_sync import AwardRoleSync  # noqa: F401 - Register shared ORM tables before schema creation.
from award_reannouncement import AwardReplay, AwardReplayBatch  # noqa: F401 - Register shared ORM tables before schema creation.
from award_revocation import (
    AwardRevocation,  # noqa: F401 - Register shared ORM tables before schema creation.
    active_awards,
    is_revoked,
    revoked_data,
    removal_status,
)
from protocol_week_source import (
    ProtocolPublishedWeek,
    ProtocolPublishedHealth,  # noqa: F401 - Register shared ORM tables before schema creation.
    published_data,
)
from protocol_clan_source import ProtocolClanStats, clan_profile, clan_status  # noqa: F401 - Register shared ORM tables before schema creation.
from protocol_award_exports import (
    ProtocolAwardExport,  # noqa: F401 - Register shared ORM tables before schema creation.
    ProtocolExportHealth,  # noqa: F401 - Register shared ORM tables before schema creation.
    award_exports,
    export_status,
    player_export_progress,
)
from battlemetrics_awards import BattleMetricsPlaytime  # noqa: F401 - Register shared ORM tables before schema creation.
from discord_event_log import DiscordEventLog  # noqa: F401 - Register shared ORM tables before schema creation.
from assembly_attendance import AssemblyWindow, AssemblyAssessment  # noqa: F401 - Register shared ORM tables before schema creation.


def image_path(code, db=None):
    if db is not None:
        from award_design import custom_image_url

        custom = custom_image_url(db, code)
        if custom:
            return custom
    return (
        None
        if code in ("iron_cross", "valor")
        else "/medal-assets/" + code + ASSET_SUFFIX.get(code, ".png")
    )


class MedalAward(Base):
    __tablename__ = "medal_awards"
    __table_args__ = (UniqueConstraint("discord_id", "medal"),)
    id = Column(Integer, primary_key=True)
    discord_id = Column(String(20), nullable=False, index=True)
    steam_id = Column(String(20), nullable=True)
    medal = Column(String(40), nullable=False)
    evidence = Column(Text, nullable=False)
    reason = Column(Text, nullable=False)
    earned_at = Column(DateTime, nullable=False)
    sent_at = Column(DateTime, nullable=True)
    message_id = Column(String(20), nullable=True)
    retry_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    lease_until = Column(DateTime, nullable=True)
    last_error = Column(String(100), nullable=True)


class VoiceTime(Base):
    __tablename__ = "medal_voice_time"
    discord_id = Column(String(20), primary_key=True)
    seconds = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False)


class MedalBotHealth(Base):
    __tablename__ = "medal_bot_health"
    id = Column(Integer, primary_key=True)
    connected = Column(Boolean, nullable=False, default=False)
    active_voice = Column(Integer, nullable=False, default=0)
    last_event_at = Column(DateTime, nullable=True)
    checked_at = Column(DateTime, nullable=False)


class ProtocolProfile(Base):
    __tablename__ = "medal_protocol_profiles"
    steam_id = Column(String(20), primary_key=True)
    payload = Column(Text, nullable=False)
    checked_at = Column(DateTime, nullable=False)


class ProtocolMatch(Base):
    __tablename__ = "medal_protocol_matches"
    match_id = Column(Integer, primary_key=True)
    ended_at = Column(DateTime, nullable=False, index=True)
    payload = Column(Text, nullable=False)


class MedalWeek(Base):
    __tablename__ = "medal_weeks"
    week = Column(String(10), primary_key=True)
    payload = Column(Text, nullable=False)
    finalized_at = Column(DateTime, nullable=False)


class MedalMonitor(Base):
    __tablename__ = "medal_monitor"
    id = Column(Integer, primary_key=True)
    payload = Column(Text, nullable=False, default="{}")
    updated_at = Column(DateTime, nullable=False)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def week_bounds(week):
    start = datetime.strptime(week, "%Y-%m-%d").replace(tzinfo=MOSCOW)
    if start.weekday() != 0:
        raise ValueError("Week must begin on Monday")
    return start, start + timedelta(days=7)


def week_label(week):
    start, end = week_bounds(week)
    return (
        start.strftime("%d.%m.%Y")
        + " — "
        + (end - timedelta(days=1)).strftime("%d.%m.%Y")
    )


def ranks(counts):
    """Competition ranking: equal scores share a place; 1, 1, 3."""
    output = {}
    previous = None
    place = 0
    for index, (sid, value) in enumerate(
        sorted(counts.items(), key=lambda p: (-p[1], p[0])), 1
    ):
        if value <= 0:
            continue
        if value != previous:
            place = index
            previous = value
        output[sid] = {"rank": place, "value": value}
    return output


from distinction_reviews import DistinctionReview  # noqa: F401 - Register review table before migrations.


def grant(db, user, medal, reason, evidence, now=None):
    catalog = {**CATALOG, **MANUAL_CATALOG}
    from automation_control import exception_for
    from admin_hub import weekly_award_allowed

    if (
        medal not in catalog
        or user.deleted_at is not None
        or is_revoked(db, user.discord_id, medal)
        or exception_for(db, user.id, "award", medal)
    ):
        return None
    cached = db.info.get("medals_owned")
    already_owned = (
        medal in cached.get(user.discord_id, set())
        if cached is not None
        else bool(
            db.query(MedalAward.id)
            .filter_by(discord_id=user.discord_id, medal=medal)
            .first()
        )
    )
    if already_owned:
        return None
    moment = now or utcnow()
    if medal in DISTINCTION_ROLES:
        from distinction_reviews import review_allowed

        if not review_allowed(db, user, medal, reason, evidence, moment):
            return None
    elif not weekly_award_allowed(db, user, medal, evidence):
        return None
    award = MedalAward(
        discord_id=user.discord_id,
        steam_id=user.steam_id,
        medal=medal,
        reason=reason,
        evidence=dump(evidence),
        earned_at=now or utcnow(),
    )
    db.add(award)
    db.flush()
    if cached is not None:
        cached.setdefault(user.discord_id, set()).add(medal)
    db.add(
        GameHistory(
            user_id=user.id,
            game_type="medals",
            result="medal_awarded",
            amount=0,
            details=award_name(db, medal) + ": " + reason,
        )
    )
    from award_role_sync import request_sync

    request_sync(db, user.discord_id)
    return award


def sync_manual(db, user, roles):
    """Display manually assigned Discord awards without creating roles or announcements."""
    if not isinstance(roles, list):
        return
    given = {str(value) for value in roles}
    for code, role_id in MANUAL_ROLES.items():
        if str(role_id) not in given:
            continue
        award = grant(
            db,
            user,
            code,
            "Награда назначена вручную в Discord. Подтверждена соответствующая роль.",
            {"source": "Discord role", "role_id": str(role_id), "manual": True},
        )
        if award:
            award.sent_at = utcnow()


def award_week(db, user, week, document, now=None):
    if (
        not user.steam_id
        or document.get("week") != week
        or not document.get("complete")
    ):
        return
    sid = user.steam_id
    label = week_label(week)
    results = document.get("ranks", {})
    kill = results.get("kills", {}).get(sid)
    from automation_control import setting

    star_ranks = {1: "gold_star", 2: "silver_star", 3: "bronze_star"}
    if (
        (week, "kills") not in db.info.get("protocol_export_primary", set())
        and kill
        and kill["rank"] in star_ranks
    ):
        medal = star_ranks[kill["rank"]]
        grant(
            db,
            user,
            medal,
            f"{label}: {kill['rank']}-е место среди всех игроков Protocol, {kill['value']} убийств.",
            {"week": week, "kills": kill, "source": "SQSTAT", "source_role": "reserve"},
            now,
        )
    cmd = results.get("cmd", {}).get(sid)
    # An unresolved match must never be treated as a win or alter the podium silently.
    if (
        (week, "cmdwins") not in db.info.get("protocol_export_primary", set())
        and week not in db.info.get("protocol_week_primary", set())
        and cmd
        and cmd["rank"] <= setting(db, "weekly_cmd_rank")
        and (document.get("cmd_complete") or sid in document.get("cmd_safe", []))
    ):
        grant(
            db,
            user,
            "outstanding_service",
            f"{label}: {cmd['rank']}-е место по победам CMD, {cmd['value']} побед командиром стороны.",
            {"week": week, "cmd": cmd, "source": "SQSTAT", "source_role": "reserve"},
            now,
        )
    revival = results.get("revive", {}).get(sid)
    revive_reserved = (week, "revives") not in db.info.get(
        "protocol_export_primary", set()
    )
    if week in db.info.get("protocol_week_primary", set()):
        # Protocol publishes five entries; SQSTAT may confirm the remaining
        # positions in the configured revive top without overriding that list.
        published = db.get(ProtocolPublishedWeek, week)
        ids = (
            {
                r["steam_id"]
                for r in json.loads(published.payload)["categories"]["revive"]["rows"]
            }
            if published
            else set()
        )
        revive_reserved = (
            revive_reserved
            and published is not None
            and bool(
                revival
                and 6 <= revival["rank"] <= setting(db, "weekly_merit_revive_rank")
                and sid not in ids
            )
        )
    if (
        revive_reserved
        and revival
        and 0 < revival["rank"] <= setting(db, "weekly_merit_revive_rank")
    ):
        grant(
            db,
            user,
            "medical_aid",
            f"{label}: {revival['rank']}-е место по поднятиям, {revival['value']} поднятий.",
            {
                "week": week,
                "metric": "revive",
                "published": revival,
                "source": "SQSTAT",
                "source_role": "reserve",
            },
            now,
        )
    limits = {
        "kills": (setting(db, "weekly_merit_kills_rank"), "убийств"),
        "wins": (setting(db, "weekly_merit_wins_rank"), "побед в матчах"),
        "damage": (setting(db, "weekly_merit_damage_rank"), "единиц урона"),
    }
    proof = {}
    clauses = []
    for metric, (limit, name) in limits.items():
        export_kind = {"kills": "kills", "revive": "revives", "wins": "wins"}.get(
            metric
        )
        if (week, export_kind) in db.info.get("protocol_export_primary", set()):
            continue
        value = results.get(metric, {}).get(sid)
        if (
            metric == "wins"
            and not document.get("wins_complete")
            and sid not in document.get("wins_safe", [])
        ):
            continue
        if value and value["rank"] <= limit:
            proof[metric] = value
            clauses.append(f"{value['rank']}-е место: {value['value']} {name}")
    if clauses:
        grant(
            db,
            user,
            "merit",
            f"{label}: " + "; ".join(clauses) + ".",
            {
                "week": week,
                "achievements": proof,
                "source": "SQSTAT",
                "source_role": "reserve",
            },
            now,
        )


def award_totals(db, user, now=None):
    now = now or utcnow()
    profile = db.get(ProtocolProfile, user.steam_id) if user.steam_id else None
    from automation_control import setting, source_enabled

    sqstat_enabled = source_enabled(db, "sqstat")
    battlemetrics_enabled = source_enabled(db, "battlemetrics")
    protocol_target = setting(db, "medal_good_protocol_hours")
    voice_target = setting(db, "medal_good_voice_hours")
    clan = (
        clan_profile(db, user.steam_id, now) if source_enabled(db, "protocol") else None
    )
    if clan is None and (
        not sqstat_enabled
        or profile is None
        or not timedelta(0) <= now - profile.checked_at <= timedelta(hours=24)
    ):
        if not battlemetrics_enabled:
            return
        from battlemetrics_awards import playtime

        fallback = playtime(db, user.steam_id, now)
        voice = db.get(VoiceTime, user.discord_id)
        seconds = voice.seconds if voice else 0
        if (
            fallback
            and fallback["minutes"] >= protocol_target * 60
            and seconds >= voice_target * 3600
        ):
            grant(
                db,
                user,
                "good_conduct",
                f"BattleMetrics: не менее {fallback['minutes'] // 60} ч на серверах Protocol; войсы клана без AFK: {seconds // 3600} ч.",
                {**fallback, "voice_seconds": seconds},
                now,
            )
        return
    p = clan if clan else json.loads(profile.payload)
    if (
        p.get("steam_id") != user.steam_id
        or (not clan and p.get("season") != "all")
        or not p.get("verified")
    ):
        return
    minutes = p["minutes"]
    kills = p["kills"]
    deaths = p["deaths"]
    if any(type(v) is not int or v < 0 for v in (minutes, kills, deaths)):
        return
    voice = db.get(VoiceTime, user.discord_id)
    seconds = voice.seconds if voice else 0
    evidence = {
        **p,
        "voice_seconds": seconds,
        "checked_at": clan["checked_at"]
        if clan
        else profile.checked_at.isoformat() + "Z",
    }
    hours = f"{minutes // 60} ч {minutes % 60} мин"
    if minutes >= protocol_target * 60 and seconds >= voice_target * 3600:
        grant(
            db,
            user,
            "good_conduct",
            f"Protocol: {hours}; войсы клана без AFK: {seconds // 3600} ч {(seconds % 3600) // 60} мин.",
            evidence,
            now,
        )
    if clan and minutes >= 1000 * 60 and clan["seed_hours"] >= 1000:
        grant(
            db,
            user,
            "iron_cross",
            f"Protocol, статистика состава: {hours}; сид: {clan['seed_hours']:g} ч.",
            evidence,
            now,
        )
    # A K/D with no deaths is undefined; don't substitute a rounded or invented ratio.
    if deaths <= 0:
        return
    ratio = kills / deaths
    kd = f"{ratio:.3f}".replace(".", ",")
    basis = "Protocol, статистика состава" if clan else "Protocol за всё время"
    if minutes >= setting(db, "medal_combat_hours") * 60:
        grades = [
            ("infantry_blue", setting(db, "medal_combat_i_kd")),
            ("infantry_red", setting(db, "medal_combat_ii_kd")),
            ("infantry_green", setting(db, "medal_combat_iii_kd")),
        ]
        for medal, threshold in grades:
            if ratio >= threshold:
                grant(
                    db,
                    user,
                    medal,
                    f"{basis}: {hours}, K/D {kd} ({kills} убийств / {deaths} смертей).",
                    evidence,
                    now,
                )
    if minutes >= setting(db, "medal_purple_hours") * 60 and ratio < setting(
        db, "medal_purple_kd_below"
    ):
        grant(
            db,
            user,
            "purple_heart",
            f"{basis}: {hours}, K/D {kd} ({kills} убийств / {deaths} смертей).",
            evidence,
            now,
        )


def complete_lower_grades(db, user, now):
    """An already permanent higher grade also grants missing lower grades."""
    grades = ("infantry_blue", "infantry_red", "infantry_green")
    owned = {
        a.medal: a
        for a in active_awards(db).filter_by(discord_id=user.discord_id)
        if a.medal in grades
    }
    for index, code in enumerate(grades):
        if code not in owned:
            continue
        parent = owned[code]
        for lower in grades[index + 1 :]:
            grant(
                db,
                user,
                lower,
                "Нижняя степень к уже полученной награде «"
                + award_name(db, code)
                + "». "
                + parent.reason,
                {
                    "source": "Permanent higher grade",
                    "higher_award_id": parent.id,
                    "higher_code": code,
                    "proof": json.loads(parent.evidence),
                },
                now,
            )
        break


def closed_sqstat_weeks(db, now, only_week=None):
    """Only verified complete archives for the exact closed week can back awards."""
    from automation_control import source_enabled
    from protocol_week_source import previous_key

    if not source_enabled(db, "sqstat"):
        return []
    latest = previous_key(now)
    result = []
    rows = db.query(MedalWeek).filter(
        MedalWeek.week >= FIRST_WEEK, MedalWeek.week <= latest
    )
    if only_week is not None:
        rows = rows.filter(MedalWeek.week == only_week)
    for row in rows.order_by(MedalWeek.week):
        try:
            week_bounds(row.week)
            doc = json.loads(row.payload)
        except (ValueError, TypeError):
            continue
        if (
            not isinstance(doc, dict)
            or doc.get("week") != row.week
            or doc.get("complete") is not True
            # Keep previously verified archives valid across SQSTAT's official
            # domain migration; new scans use the canonical host directly.
            or doc.get("source")
            not in ("https://prot.sqstat.ru/", "https://prot.p.sqstat.ru/")
            or type(doc.get("match_count")) is not int
            or doc["match_count"] <= 0
            or not isinstance(doc.get("ranks"), dict)
        ):
            continue
        result.append((row.week, doc))
    return result


def evaluate(db, now=None):
    now = now or utcnow()
    active = db.query(User).filter(User.deleted_at.is_(None)).all()
    known = {}
    for sid, code in db.query(MedalAward.discord_id, MedalAward.medal):
        known.setdefault(sid, set()).add(code)
    db.info["medals_owned"] = known
    from automation_control import source_enabled

    sqstat_enabled = source_enabled(db, "sqstat")
    documents = closed_sqstat_weeks(db, now) if sqstat_enabled else []
    try:
        db.info["protocol_export_primary"] = (
            award_exports(db, active, now) if source_enabled(db, "protocol") else set()
        )
        from protocol_week_source import award_published

        db.info["protocol_week_primary"] = (
            award_published(db, active, now)
            if source_enabled(db, "protocol")
            else set()
        )
        for user in active:
            for week, document in documents:
                award_week(db, user, week, document, now)
            award_totals(db, user, now)
            complete_lower_grades(db, user, now)
        if sqstat_enabled:
            from distinctions import evaluate as evaluate_distinctions

            evaluate_distinctions(db, active, documents, now)
        from distinction_reviews import process_due

        process_due(db, now)
        from award_rewards import pay

        pay(db, now)
    finally:
        db.info.pop("medals_owned", None)
        db.info.pop("protocol_week_primary", None)
        db.info.pop("protocol_export_primary", None)


def visible_awards(db, discord_id):
    awards = (
        active_awards(db)
        .filter_by(discord_id=discord_id)
        .order_by(MedalAward.earned_at, MedalAward.id)
        .all()
    )
    output = []
    from award_rewards import payment_data

    payments = {
        p.medal: payment_data(p)
        for p in db.query(AwardRewardPayment).filter_by(discord_id=discord_id)
    }
    catalog = named_catalog(db, {**CATALOG, **MANUAL_CATALOG})
    for a in awards:
        if a.medal not in catalog:
            continue
        name, rule, color, symbol = catalog[a.medal]
        output.append(
            {
                "id": a.id,
                "code": a.medal,
                "name": name,
                "rule": rule,
                "color": color,
                "image": image_path(a.medal, db),
                "manual": a.medal in MANUAL_CATALOG
                or bool(json.loads(a.evidence).get("manual")),
                "symbol": symbol,
                "reason": a.reason,
                "payment": payments.get(a.medal),
                "date": a.earned_at.replace(tzinfo=timezone.utc)
                .astimezone(MOSCOW)
                .strftime("%d.%m.%Y"),
            }
        )
    return output


def monitor_status(db):
    row = db.get(MedalMonitor, 1)
    data = json.loads(row.payload) if row else {}
    weeks = []
    for week in db.query(MedalWeek).order_by(MedalWeek.week.desc()).limit(8):
        doc = json.loads(week.payload)
        weeks.append(
            {
                "label": week_label(week.week),
                "matches": doc.get("match_count", 0),
                "unresolved": len(doc.get("unresolved", [])),
            }
        )
    from distinctions_catalog import BLOCKED

    rewards = db.query(AwardRewardPayment)
    titles = named_catalog(db, {**CATALOG, **MANUAL_CATALOG})
    voice = db.get(MedalBotHealth, 1)
    bot_alive = bool(
        voice
        and voice.connected
        and timedelta(0) <= utcnow() - voice.checked_at <= timedelta(minutes=3)
    )
    from award_role_sync import status as role_status
    from award_reannouncement import replay_status
    from battlemetrics_awards import status as bm_status

    return {
        **data,
        "weeks": weeks,
        "unsupported_distinctions": {
            code: {"name": titles[code][0], "reason": reason}
            for code, reason in BLOCKED.items()
        },
        "role_sync": role_status(db),
        "reannouncements": replay_status(db),
        "revocations": removal_status(db),
        "protocol_previous": published_data(db),
        "battlemetrics": bm_status(db),
        "protocol_clan": clan_status(db),
        "protocol_exports": export_status(db),
        "unsupported_medals": {
            code: {"name": titles[code][0], "reason": reason}
            for code, reason in BLOCKED_MEDALS.items()
        },
        "voice": {
            "available": bot_alive,
            "active": voice.active_voice if bot_alive else 0,
            "checked_at": voice.checked_at.isoformat() + "Z" if voice else None,
        },
        "updated_at": row.updated_at.isoformat() + "Z" if row else None,
        "awards": active_awards(db).count(),
        "pending": active_awards(db).filter(MedalAward.sent_at.is_(None)).count(),
        "failed": active_awards(db)
        .filter(MedalAward.last_error.is_not(None), MedalAward.sent_at.is_(None))
        .count(),
        "rewards_active": db.get(AwardRewardPolicy, 1) is not None,
        "rewards_paid": rewards.filter(AwardRewardPayment.paid_at.is_not(None)).count(),
        "rewards_pending": rewards.filter(
            AwardRewardPayment.award_id.in_(
                active_awards(db).with_entities(MedalAward.id)
            ),
            AwardRewardPayment.paid_at.is_(None),
            AwardRewardPayment.skipped.is_(False),
        ).count(),
        "rewards_failed": rewards.filter(
            AwardRewardPayment.award_id.in_(
                active_awards(db).with_entities(MedalAward.id)
            ),
            AwardRewardPayment.paid_at.is_(None),
            AwardRewardPayment.skipped.is_(False),
            AwardRewardPayment.last_error.is_not(None),
        ).count(),
    }


def progress(db, user, now=None):
    now = now or utcnow()
    awards = visible_awards(db, user.discord_id)
    owned = {a["code"] for a in awards}
    revoked = revoked_data(db, user.discord_id)
    voice = db.get(VoiceTime, user.discord_id)
    row = db.get(ProtocolProfile, user.steam_id) if user.steam_id else None
    profile = json.loads(row.payload) if row else None
    clan = clan_profile(db, user.steam_id, now, allow_stale=True)
    clan_used = bool(
        clan
        and (
            not clan["stale"]
            or profile is None
            or now - row.checked_at > timedelta(hours=24)
        )
    )
    if clan_used:
        profile = {**(profile or {}), **clan}
    status = monitor_status(db)
    live = status.get("live", {})
    local = now.replace(tzinfo=timezone.utc).astimezone(MOSCOW)
    week = (local - timedelta(days=local.weekday())).date().isoformat()
    # A new Monday cannot inherit last week's ranks during a source outage.
    if live.get("week") != week:
        live = {"week": week, "match_count": 0, "ranks": {}}
    minutes = profile["minutes"] if profile else None
    minutes_source = (
        "Protocol · статистика состава" if clan_used else "SQSTAT" if profile else None
    )
    from battlemetrics_awards import playtime

    fallback = playtime(db, user.steam_id, now)
    if (
        fallback
        and not (clan_used and not clan["stale"])
        and (row is None or now - row.checked_at > timedelta(hours=24))
    ):
        minutes = fallback["minutes"]
        minutes_source = "BattleMetrics (подтверждённый минимум)"
    kills = profile["kills"] if profile else None
    deaths = profile["deaths"] if profile else None
    ratio = profile["kd"] if clan_used else kills / deaths if deaths else None
    voice_seconds = voice.seconds if voice else 0
    metrics = {
        k: live.get("ranks", {})
        .get(k, {})
        .get(user.steam_id, {"rank": None, "value": 0})
        for k in ("kills", "revive", "damage", "vehicle", "wins", "cmd")
    }
    from distinctions import progress_values
    from award_rewards import prices

    reward_prices = prices(db)
    weeks = [
        (w.week, json.loads(w.payload))
        for w in db.query(MedalWeek).order_by(MedalWeek.week)
    ]
    # Avoid counting a cached closed-week live aggregate twice.
    anti_live = {} if any(w == live.get("week") for w, _ in weeks) else live
    published = published_data(db, user.steam_id or "", now)
    exports = player_export_progress(db, user.steam_id, now)
    for kind, metric in {
        "kills": "kills",
        "revives": "revive",
        "wins": "wins",
        "cmdwins": "cmd",
    }.items():
        exported = exports.get(week + ":" + kind)
        if exported:
            metrics[metric] = exported["player"] or {"rank": None, "value": None}
    reviews = {
        row.medal: {"status": row.status, "deadline": row.deadline.isoformat() + "Z"}
        for row in db.query(DistinctionReview).filter_by(discord_id=user.discord_id)
    }
    cards = []
    from automation_control import setting

    thresholds = {
        "infantry_blue": setting(db, "medal_combat_i_kd"),
        "infantry_red": setting(db, "medal_combat_ii_kd"),
        "infantry_green": setting(db, "medal_combat_iii_kd"),
    }
    for code, (name, rule, color, symbol) in named_catalog(
        db, {**MANUAL_MEDALS, **CATALOG, **MANUAL_DISTINCTIONS}
    ).items():
        values = []
        blocked = BLOCKED_MEDALS.get(code)
        if code in ("gold_star", "silver_star", "bronze_star"):
            target = {"gold_star": 1, "silver_star": 2, "bronze_star": 3}[code]
            values = [
                {
                    "label": "Все убийства за неделю",
                    "value": metrics["kills"]["value"],
                    "rank": metrics["kills"]["rank"],
                    "target_rank": target,
                }
            ]
        elif code == "outstanding_service":
            values = [
                {
                    "label": "Победы CMD за неделю",
                    "value": metrics["cmd"]["value"],
                    "rank": metrics["cmd"]["rank"],
                    "target_rank": setting(db, "weekly_cmd_rank"),
                }
            ]
        elif code == "merit":
            limits = {
                "kills": (setting(db, "weekly_merit_kills_rank"), "Убийства"),
                "wins": (setting(db, "weekly_merit_wins_rank"), "Победы"),
                "damage": (setting(db, "weekly_merit_damage_rank"), "Урон"),
            }
            values = [
                {
                    "label": label,
                    "value": metrics[key]["value"],
                    "rank": metrics[key]["rank"],
                    "target_rank": limit,
                }
                for key, (limit, label) in limits.items()
            ]
        elif code == "iron_cross":
            values = [
                {
                    "label": "Часы на Protocol",
                    "value": round(minutes / 60, 2) if minutes is not None else None,
                    "target": 1000,
                },
                {
                    "label": "Часы сида",
                    "value": clan["seed_hours"] if clan else None,
                    "target": 1000,
                },
            ]
            if not clan or clan["stale"]:
                blocked = "Нет свежих часов сида из статистики состава Protocol. Автовыдача ждёт обновления."
        elif code == "good_conduct":
            values = [
                {
                    "label": "Часы на Protocol",
                    "value": round(minutes / 60, 2) if minutes is not None else None,
                    "target": setting(db, "medal_good_protocol_hours"),
                },
                {
                    "label": "Часы в войсах без AFK",
                    "value": round(voice_seconds / 3600, 2),
                    "target": setting(db, "medal_good_voice_hours"),
                },
            ]
        elif code in thresholds:
            values = [
                {
                    "label": "Часы на Protocol",
                    "value": round(minutes / 60, 2) if minutes is not None else None,
                    "target": setting(db, "medal_combat_hours"),
                },
                {
                    "label": "K/D Protocol" if clan_used else "K/D за всё время",
                    "value": round(ratio, 3) if ratio is not None else None,
                    "target": thresholds[code],
                },
            ]
        elif code == "purple_heart":
            values = [
                {
                    "label": "Часы на Protocol",
                    "value": round(minutes / 60, 2) if minutes is not None else None,
                    "target": setting(db, "medal_purple_hours"),
                },
                {
                    "label": "K/D Protocol" if clan_used else "K/D за всё время",
                    "value": round(ratio, 3) if ratio is not None else None,
                    "below": setting(db, "medal_purple_kd_below"),
                },
            ]
        elif code in DISTINCTIONS:
            values, blocked = progress_values(
                code,
                profile,
                anti_live if code == "anti_tank" else live,
                weeks,
                user.steam_id,
            )
        if code == "medical_aid" and values:
            values[0]["target_rank"] = setting(db, "weekly_merit_revive_rank")
        export_kind = {
            "best_machine_gunner": "mg",
            "best_sniper": "sniper",
            "best_engineer": "sapper",
            "best_medic": "medic",
            "medical_aid": "revives",
            "best_demolition": "fob",
            "best_knife": "knife",
            "anti_tank": "handat",
            "best_pilot": "pilot",
            "best_driver": "crew",
        }.get(code)
        if code in ("best_pilot", "best_driver"):
            candidates = [
                v
                for v in exports.values()
                if v.get("weekly_kit_award") == code or v["kind"] == export_kind
            ]
            chosen = (
                max(candidates, key=lambda v: v["week"] or "") if candidates else None
            )
            if chosen:
                export_kind = chosen["kind"]
        if export_kind:
            source = exports.get(
                ("all" if export_kind == "fob" else week) + ":" + export_kind
            )
            if source is None and export_kind != "fob":
                options = [
                    v
                    for v in exports.values()
                    if v["kind"] == export_kind and v["final"]
                ]
                source = max(options, key=lambda v: v["week"]) if options else None
            if source:
                item = source["player"]
                label = (
                    "Уничтоженные вражеские FOB с 07.10.2026"
                    if export_kind == "fob"
                    else source.get("title")
                    or "Статистика Protocol, неделя " + source["week"]
                )
                if source["week"]:
                    label += " · " + source["week"]
                target_rank = (
                    1
                    if code in ("best_driver", "best_pilot")
                    else setting(db, "weekly_merit_revive_rank")
                    if code == "medical_aid"
                    else setting(db, "weekly_best_knife_rank")
                    if code == "best_knife"
                    else 3
                )
                values = [
                    {
                        "label": label,
                        "value": item["value"] if item else None,
                        **(
                            {"target": 50}
                            if export_kind == "fob"
                            else {
                                "rank": item["rank"] if item else None,
                                "target_rank": target_rank,
                            }
                        ),
                        "available": True,
                    }
                ]
                weapon = export_kind in ("knife", "handat") or code == "best_pistol"
                from protocol_award_exports import knife_final_supported

                eligible = (
                    (source.get("exact_weapons") or knife_final_supported(source))
                    if weapon
                    else source.get("exact_kits")
                    if export_kind
                    in ("mg", "sniper", "sapper", "medic", "pilot", "crew")
                    or code == "best_driver"
                    else True
                )
                blocked = (
                    None
                    if eligible
                    else "Автовыдача ждёт завершённую неделю с полным точным учётом источника."
                )
                if export_kind == "knife" and source.get("week", "") < "2026-10-16":
                    values[0]["label"] += " · учёт ножевых убийств с 16.10.2026"
        if (
            code == "best_demolition"
            and "all:fob" in exports
            and not exports["all:fob"]["player"]
        ):
            blocked = "Protocol показывает числовые счётчики первых 20 игроков. Число FOB этого игрока не подтверждено; место в TXT не заменяет счётчик."
        source_kinds = (
            ["kills"]
            if code in ("gold_star", "silver_star", "bronze_star")
            else ["cmdwins"]
            if code == "outstanding_service"
            else ["kills", "wins", None]
            if code == "merit"
            else []
        )
        for value, kind in zip(values, source_kinds):
            if kind and week + ":" + kind in exports:
                value["available"] = True
        cards.append(
            {
                "code": code,
                "name": name,
                "rule": rule,
                "color": color,
                "symbol": symbol,
                "owned": code in owned,
                "higher_owned": False,
                "values": values,
                "revoked": revoked.get(code),
                "review": reviews.get(code),
                "image": image_path(code, db),
                "category": "distinction" if code in DISTINCTION_ROLES else "medal",
                "manual": code in MANUAL_CATALOG,
                "blocked": blocked,
                "reward": reward_prices.get(code),
            }
        )
    return {
        "user_id": user.id,
        "name": user.display_name or user.username,
        "steam_id": user.steam_id,
        "protocol_previous": published,
        "protocol_exports": exports,
        "protocol_exports_health": status.get("protocol_exports", {}),
        "awards": awards,
        "cards": cards,
        "week": week_label(week) if week else None,
        "weekly_available": bool(live.get("match_count")),
        "weekly_matches": live.get("match_count", 0),
        "weekly_unresolved": len(live.get("unresolved", [])),
        "distinctions_unverified": live.get("distinctions", {}).get(
            "unverified_matches", 0
        ),
        "sl_unknown_results": live.get("distinctions", {}).get("sl_unknown_results", 0),
        "weekly_checked_at": status.get("checked_at"),
        "source_status": status.get("status", "warming"),
        "profile_checked_at": clan["checked_at"]
        if clan_used
        else row.checked_at.isoformat() + "Z"
        if row
        else None,
        "profile_stale": clan["stale"]
        if clan_used
        else bool(row and now - row.checked_at > timedelta(hours=24)),
        "voice_checked_at": voice.updated_at.isoformat() + "Z" if voice else None,
        "voice_available": status["voice"]["available"],
        "voice_active": status["voice"]["active"],
        "voice_seconds": voice_seconds,
        "minutes": minutes,
        "kills": kills,
        "deaths": deaths,
        "minutes_source": minutes_source,
        "seed_hours": clan["seed_hours"] if clan else None,
        "clan_stats_stale": bool(clan and clan["stale"]),
        "clan_checked_at": clan["checked_at"] if clan else None,
        "kd_source": "Protocol · статистика состава"
        if clan_used
        else "SQSTAT"
        if profile
        else None,
        "kd": round(ratio, 3) if ratio is not None else None,
        "weekly": metrics,
        "updated_at": now.isoformat() + "Z",
    }
