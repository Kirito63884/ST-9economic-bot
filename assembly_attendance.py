"""Saturday assembly attendance, Discord reports and idempotent Bishopcoin fines."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import date, datetime, time as clock_time, timedelta, timezone

from discord.http import Route
from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint, text

from database import Base, GameHistory, SessionLocal, User
from runtime import metadata_transaction
from squad_monitor import INTERVAL, SERVER_IDS


FORUM_ID = 1494745219525120112
PENALTY = 800.0
MOSCOW = timezone(timedelta(hours=3))
START = clock_time(19, 0)
END = clock_time(22, 0)
PROCESS_DELAY = timedelta(minutes=5)
REPORT_CHECK_SECONDS = 10 * 60
NETWORK_SECONDS = 30
MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
logger = logging.getLogger("strike_team9.assembly")


class AssemblyWindow(Base):
    __tablename__ = "assembly_windows"
    event_date = Column(String(10), primary_key=True)
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    first_sample = Column(DateTime, nullable=False)
    last_sample = Column(DateTime, nullable=False)
    max_gap_seconds = Column(Integer, nullable=False, default=0)
    sample_count = Column(Integer, nullable=False, default=0)
    healthy_servers = Column(Text, nullable=False, default="{}")
    attended = Column(Text, nullable=False, default="{}")
    member_ids = Column(Text, nullable=True)
    status = Column(String(30), nullable=False, default="collecting")
    thread_id = Column(String(20), nullable=True)
    processed_at = Column(DateTime, nullable=True)
    retry_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(100), nullable=True)


class AssemblyAssessment(Base):
    __tablename__ = "assembly_assessments"
    __table_args__ = (UniqueConstraint("event_date", "discord_id"),)
    id = Column(Integer, primary_key=True)
    event_date = Column(String(10), nullable=False, index=True)
    discord_id = Column(String(20), nullable=False, index=True)
    user_id = Column(Integer, nullable=True)
    steam_id = Column(String(20), nullable=True)
    outcome = Column(String(30), nullable=False)
    reported_at = Column(DateTime, nullable=True)
    attended_at = Column(DateTime, nullable=True)
    server_id = Column(String(20), nullable=True)
    assessed_cents = Column(Integer, nullable=False, default=0)
    charged_cents = Column(Integer, nullable=False, default=0)
    balance_before_cents = Column(Integer, nullable=True)
    balance_after_cents = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False)


class AssemblyReportCheck(Base):
    """Latest Discord report preview for one Saturday."""

    __tablename__ = "assembly_report_checks"
    event_date = Column(String(10), primary_key=True)
    status = Column(String(30), nullable=False, default="pending")
    thread_id = Column(String(20), nullable=True)
    member_ids = Column(Text, nullable=False, default="[]")
    member_names = Column(Text, nullable=False, default="{}")
    reports = Column(Text, nullable=False, default="{}")
    checked_at = Column(DateTime, nullable=True)
    next_check = Column(DateTime, nullable=True)
    last_error = Column(String(200), nullable=True)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def bounds(day):
    if isinstance(day, str):
        day = date.fromisoformat(day)
    from automation_control import setting

    with SessionLocal() as db:
        start_value = setting(db, "assembly_start")
        end_value = setting(db, "assembly_end")
    start_clock = clock_time(*map(int, start_value.split(":")))
    end_clock = clock_time(*map(int, end_value.split(":")))
    start = (
        datetime.combine(day, start_clock, MOSCOW)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    end = (
        datetime.combine(day, end_clock, MOSCOW)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    if end <= start:
        end += timedelta(days=1)
    return start, end


def event_day(moment):
    aware = (
        moment.replace(tzinfo=timezone.utc)
        if moment.tzinfo is None
        else moment.astimezone(timezone.utc)
    )
    local = aware.astimezone(MOSCOW)
    day = local.date()
    if local.weekday() != 5:
        return None
    start, end = bounds(day)
    plain = aware.astimezone(timezone.utc).replace(tzinfo=None)
    return day if start <= plain < end else None


def report_target_day(moment=None):
    """Return the current or next Saturday in Moscow."""
    moment = moment or utcnow()
    aware = (
        moment.replace(tzinfo=timezone.utc)
        if moment.tzinfo is None
        else moment.astimezone(timezone.utc)
    )
    local = aware.astimezone(MOSCOW)
    return local.date() + timedelta(days=(5 - local.weekday()) % 7)


def request_report_check(db, event_date, now=None):
    """Queue an immediate preview refresh for a selected Saturday."""
    now = now or utcnow()
    day = date.fromisoformat(event_date) if isinstance(event_date, str) else event_date
    if day.weekday() != 5:
        raise ValueError("not_saturday")
    key = day.isoformat()
    row = db.get(AssemblyReportCheck, key)
    if row is None:
        row = AssemblyReportCheck(event_date=key, status="pending", next_check=now)
        db.add(row)
    else:
        row.status = "pending"
        row.next_check = now
        row.last_error = None
    return row


def _json(value, fallback):
    try:
        result = json.loads(value)
        return result if isinstance(result, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


@contextmanager
def observation_db():
    """Serialize JSON merges with the independent BattleMetrics process."""
    with SessionLocal.begin() as db:
        if db.bind.dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        yield db


def ensure_window(db, day, moment):
    row = db.get(AssemblyWindow, day.isoformat())
    if row is None:
        start, end = bounds(day)
        row = AssemblyWindow(
            event_date=day.isoformat(),
            starts_at=start,
            ends_at=end,
            first_sample=moment,
            last_sample=moment,
            sample_count=0,
            healthy_servers="{}",
            attended="{}",
            status="collecting",
        )
        db.add(row)
    return row


def observe_snapshot(servers, moment=None):
    """Record positive presence and enough source health to prove an absence."""
    moment = moment or utcnow()
    day = event_day(moment)
    if day is None:
        return False
    server_map = {
        str(item.get("id")): item for item in servers if isinstance(item, dict)
    }
    with observation_db() as db:
        row = ensure_window(db, day, moment)
        health = _json(row.healthy_servers, {})
        attendance = _json(row.attended, {})
        bm_clock = health.get("battlemetrics_clock") or {}
        bm_last = datetime.fromisoformat(bm_clock["last"]) if bm_clock else moment
        health["battlemetrics_clock"] = {
            "first": bm_clock.get("first", moment.isoformat()),
            "last": moment.isoformat(),
            "max_gap": max(
                bm_clock.get("max_gap", 0), int((moment - bm_last).total_seconds())
            ),
        }
        gap = max(0, int((moment - row.last_sample).total_seconds()))
        row.max_gap_seconds = max(row.max_gap_seconds or 0, gap)
        row.last_sample = max(row.last_sample, moment)
        row.first_sample = min(row.first_sample, moment)
        row.sample_count += 1
        for server_id in SERVER_IDS:
            server = server_map.get(server_id)
            if not server or server.get("status") not in ("ready", "offline"):
                continue
            # Unresolved Steam identities cannot prove that an individual was absent.
            if server.get("status") == "offline" or not server.get("pending", 0):
                health[server_id] = int(health.get(server_id, 0)) + 1
            if server.get("status") != "ready":
                continue
            for player in server.get("roster", []):
                if not isinstance(player, dict):
                    continue
                for steam_id in player.get("steam_ids", []):
                    if (
                        isinstance(steam_id, str)
                        and re.fullmatch(r"7656\d{13}", steam_id)
                        and steam_id not in attendance
                    ):
                        attendance[steam_id] = {
                            "at": moment.isoformat(),
                            "server_id": server_id,
                        }
        row.healthy_servers = json.dumps(health, ensure_ascii=False, sort_keys=True)
        row.attended = json.dumps(attendance, ensure_ascii=False, sort_keys=True)
    return True


def observe_voice_snapshot(people, moment=None):
    """Only live, non-AFK Discord states from a connected bot count as evidence."""
    moment = moment or utcnow()
    day = event_day(moment)
    if day is None:
        return False
    with observation_db() as db:
        row = ensure_window(db, day, moment)
        health, attended = _json(row.healthy_servers, {}), _json(row.attended, {})
        voice = health.get("discord_voice") or {}
        last = datetime.fromisoformat(voice["last"]) if voice else moment
        # Do not inflate coverage if two tasks observe the same minute.
        if voice and (moment - last).total_seconds() < 50:
            return False
        voice = {
            "first": voice.get("first", moment.isoformat()),
            "last": moment.isoformat(),
            "count": voice.get("count", 0) + 1,
            "max_gap": max(
                voice.get("max_gap", 0), int((moment - last).total_seconds())
            ),
        }
        for sid, channel in people.items():
            if str(sid).isdigit() and channel.get("id"):
                attended.setdefault(
                    "discord:" + str(sid),
                    {
                        "at": moment.isoformat(),
                        "source": "discord_voice",
                        "channel_id": str(channel["id"]),
                        "channel_name": str(channel.get("name") or "")[:100],
                    },
                )
        health["discord_voice"] = voice
        row.healthy_servers = json.dumps(health, ensure_ascii=False, sort_keys=True)
        row.attended = json.dumps(attended, ensure_ascii=False, sort_keys=True)
    return True


def battlemetrics_coverage(row):
    expected = int((row.ends_at - row.starts_at).total_seconds() // INTERVAL)
    from automation_control import setting

    with SessionLocal() as db:
        minimum = int(expected * setting(db, "assembly_coverage_percent") / 100)
    health = _json(row.healthy_servers, {})
    bm_clock = health.get("battlemetrics_clock") or {}
    first = datetime.fromisoformat(bm_clock["first"]) if bm_clock else row.first_sample
    last = datetime.fromisoformat(bm_clock["last"]) if bm_clock else row.last_sample
    max_gap = bm_clock.get("max_gap", row.max_gap_seconds)
    problems = []
    if first > row.starts_at + timedelta(minutes=3):
        problems.append("late_start")
    if last < row.ends_at - timedelta(minutes=3):
        problems.append("early_end")
    if max_gap > 180:
        problems.append("monitor_gap")
    if row.sample_count < minimum:
        problems.append("too_few_samples")
    if any(int(health.get(server_id, 0)) < minimum for server_id in SERVER_IDS):
        problems.append("server_coverage")
    return not problems, problems


def voice_coverage(row):
    voice = _json(row.healthy_servers, {}).get("discord_voice") or {}
    if not voice:
        return False, ["discord_not_observed"]
    try:
        first, last = (datetime.fromisoformat(voice[key]) for key in ("first", "last"))
        from automation_control import setting

        with SessionLocal() as db:
            minimum = int(
                (row.ends_at - row.starts_at).total_seconds()
                / 60
                * setting(db, "assembly_coverage_percent")
                / 100
            )
        problems = []
        if first > row.starts_at + timedelta(minutes=3):
            problems.append("discord_late_start")
        if last < row.ends_at - timedelta(minutes=3):
            problems.append("discord_early_end")
        if voice["max_gap"] > 180:
            problems.append("discord_monitor_gap")
        if voice["count"] < minimum:
            problems.append("discord_too_few_samples")
        return not problems, problems
    except (TypeError, ValueError, KeyError):
        return False, ["discord_invalid_coverage"]


def coverage(row):
    bm_ok, bm_problems = battlemetrics_coverage(row)
    voice_ok, voice_problems = voice_coverage(row)
    return (True, []) if bm_ok or voice_ok else (False, bm_problems + voice_problems)


def presence_for_member(event, sid, user):
    attendance = _json(event.attended, {})
    return (
        attendance.get(user.steam_id) if user and user.steam_id else None
    ) or attendance.get("discord:" + str(sid))


def roster_details(members, member_ids):
    return {
        str(row["user"]["id"]): {
            "name": str(
                row.get("nick")
                or row["user"].get("global_name")
                or row["user"].get("username")
                or row["user"]["id"]
            )[:128],
            "roles": [str(role) for role in row.get("roles", [])],
        }
        for row in members
        if str(row.get("user", {}).get("id", "")) in member_ids
    }


def thread_matches(name, day):
    if not isinstance(name, str):
        return False
    target = f"{day.day} {MONTHS[day.month]}"
    normalized = re.sub(r"\s+", " ", name.casefold()).strip()
    return (
        re.search(r"(?<!\d)" + re.escape(target) + r"(?![а-яё\d])", normalized)
        is not None
    )


def discord_time(value):
    if not isinstance(value, str):
        return None
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
    except (ValueError, TypeError):
        return None


def accepted_reports(messages, thread_id, starts_at):
    reports = {}
    for message in messages:
        if not isinstance(message, dict) or str(message.get("id", "")) == str(
            thread_id
        ):
            continue
        author = (
            message.get("author") if isinstance(message.get("author"), dict) else {}
        )
        sid = str(author.get("id", ""))
        stamp = discord_time(message.get("timestamp"))
        if sid.isdigit() and not author.get("bot") and stamp and stamp <= starts_at:
            reports[sid] = min(stamp, reports.get(sid, stamp))
    return reports


def _assessment(
    db,
    event,
    sid,
    user,
    outcome,
    now,
    report=None,
    presence=None,
    charged=0,
    before=None,
    assessed=None,
):
    from automation_control import setting

    assessed = (
        setting(db, "assembly_penalty")
        if assessed is None and outcome in ("penalized", "pending_penalty")
        else (assessed or 0)
    )
    db.add(
        AssemblyAssessment(
            event_date=event.event_date,
            discord_id=sid,
            user_id=user.id if user else None,
            steam_id=user.steam_id if user else None,
            outcome=outcome,
            reported_at=report,
            attended_at=datetime.fromisoformat(presence["at"]) if presence else None,
            server_id=presence.get("server_id") if presence else None,
            assessed_cents=int(round(assessed * 100)),
            charged_cents=int(round(charged * 100)),
            balance_before_cents=int(round(before * 100))
            if before is not None
            else None,
            balance_after_cents=int(round(user.balance * 100)) if user else None,
            created_at=now,
        )
    )


def apply_assessments(db, event_date, member_ids, reports, now=None, defer=True):
    """Prepare review only; the legacy defer argument cannot authorize a charge."""
    from roster_membership import is_cargo_user

    now = now or utcnow()
    event = db.get(AssemblyWindow, event_date)
    if event is None or event.status in ("completed", "review_pending"):
        return {"penalized": 0, "charged": 0.0}
    ok, problems = coverage(event)
    if not ok:
        event.status = "source_incomplete"
        event.processed_at = now
        event.last_error = ",".join(problems)
        return {"penalized": 0, "charged": 0.0, "source_incomplete": problems}
    users = {
        u.discord_id: u
        for u in db.query(User).filter(User.discord_id.in_(member_ids)).all()
    }
    total = charged_total = 0.0
    for sid in sorted(member_ids, key=int):
        if (
            db.query(AssemblyAssessment.id)
            .filter_by(event_date=event_date, discord_id=sid)
            .first()
        ):
            continue
        user = users.get(sid)
        report = reports.get(sid)
        presence = presence_for_member(event, sid, user)
        if report:
            _assessment(
                db, event, sid, user, "excused", now, report=report, presence=presence
            )
        elif presence and presence.get("source") == "discord_voice":
            _assessment(db, event, sid, user, "present_voice", now, presence=presence)
        elif user is None:
            _assessment(db, event, sid, None, "unregistered", now)
        elif is_cargo_user(db, user):
            _assessment(db, event, sid, user, "exempted", now)
        elif user.deleted_at is not None:
            _assessment(db, event, sid, user, "unregistered", now)
        elif not user.steam_id:
            _assessment(db, event, sid, user, "steam_unlinked", now)
        elif presence:
            _assessment(db, event, sid, user, "present", now, presence=presence)
        else:
            # A missing Steam ID remains exempt from automated penalties. For a
            # linked player, either complete BM or complete voice observation
            # must support absence; a failed source alone is never evidence.
            from automation_control import exception_for, setting

            exclusion = exception_for(
                db, user.id, "assembly", "absence_penalty", event_date
            )
            from assembly_waivers import active_waiver

            if active_waiver(db, event_date):
                _assessment(db, event, sid, user, "waived", now)
                continue
            if exclusion:
                _assessment(db, event, sid, user, "exempted", now)
                continue
            penalty = float(setting(db, "assembly_penalty"))
            _assessment(db, event, sid, user, "pending_penalty", now, assessed=penalty)
            total += 1
    event.status = "review_pending" if total else "completed"
    event.processed_at = now
    event.retry_at = None
    event.last_error = None
    return {"penalized": int(total), "charged": charged_total}


def finalize_penalties(db, event_date, now=None):
    """Apply reviewed penalties once. Active administrator exceptions win."""
    from automation_control import exception_for

    now = now or utcnow()
    event = db.get(AssemblyWindow, event_date)
    if event is None or event.status != "review_pending":
        return {"penalized": 0, "charged": 0.0}
    total = 0
    charged_total = 0.0
    rows = (
        db.query(AssemblyAssessment)
        .filter_by(event_date=event_date, outcome="pending_penalty")
        .all()
    )
    for row in rows:
        user = db.get(User, row.user_id) if row.user_id else None
        if user is None:
            row.outcome = "unregistered"
            continue
        from roster_membership import is_cargo_user

        if is_cargo_user(db, user):
            row.outcome = "exempted"
            continue
        if user.deleted_at is not None:
            row.outcome = "unregistered"
            continue
        from assembly_waivers import active_waiver

        if active_waiver(db, event_date):
            row.outcome = "waived"
            continue
        if exception_for(db, user.id, "assembly", "absence_penalty", event_date):
            row.outcome = "exempted"
            continue
        before = float(user.balance)
        assessed = row.assessed_cents / 100
        charged = assessed
        user.balance = before - charged
        row.outcome = "penalized"
        row.charged_cents = int(round(charged * 100))
        row.balance_before_cents = int(round(before * 100))
        row.balance_after_cents = int(round(user.balance * 100))
        db.add(
            GameHistory(
                user_id=user.id,
                game_type="admin",
                result="assembly_penalty",
                amount=-charged,
                details=f"📅 Штраф за отсутствие на обязательном сборе {event_date} после проверки администратора: "
                f"начислено {assessed:.0f} БК; списано {charged:.0f} БК.",
            )
        )
        total += 1
        charged_total += charged
    event.status = "completed"
    event.processed_at = now
    event.retry_at = None
    event.last_error = None
    return {"penalized": total, "charged": charged_total}


class AssemblyWorker:
    def __init__(self, bot):
        self.bot = bot
        self.guild_id = str(os.getenv("REQUIRED_GUILD_ID", ""))
        self.role_id = str(os.getenv("REQUIRED_ROLE_ID", ""))
        self.task = None

    async def request(self, route, **kwargs):
        return await asyncio.wait_for(
            self.bot.http.request(route, **kwargs), timeout=NETWORK_SECONDS
        )

    async def members(self):
        result = []
        after = "0"
        while True:
            page = await self.request(
                Route("GET", "/guilds/{guild_id}/members", guild_id=int(self.guild_id)),
                params={"limit": 1000, "after": after},
            )
            if not isinstance(page, list):
                raise ValueError("invalid_member_list")
            result.extend(page)
            if len(page) < 1000:
                return result
            after = str(page[-1].get("user", {}).get("id", ""))
            if not after.isdigit() or len(result) > 200000:
                raise ValueError("invalid_member_page")

    async def threads(self):
        active = await self.request(
            Route(
                "GET", "/guilds/{guild_id}/threads/active", guild_id=int(self.guild_id)
            )
        )
        rows = active.get("threads", []) if isinstance(active, dict) else []
        archived = await self.request(
            Route(
                "GET",
                "/channels/{channel_id}/threads/archived/public",
                channel_id=FORUM_ID,
            ),
            params={"limit": 100},
        )
        if isinstance(archived, dict):
            rows += archived.get("threads", [])
        return [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("parent_id", "")) == str(FORUM_ID)
        ]

    async def messages(self, thread_id):
        result = []
        before = None
        while True:
            params = {"limit": 100}
            if before:
                params["before"] = before
            page = await self.request(
                Route(
                    "GET", "/channels/{channel_id}/messages", channel_id=int(thread_id)
                ),
                params=params,
            )
            if not isinstance(page, list):
                raise ValueError("invalid_messages")
            result.extend(page)
            if len(page) < 100:
                return result
            before = str(page[-1].get("id", ""))
            if not before.isdigit() or len(result) > 10000:
                raise ValueError("invalid_message_page")

    def role_members(self, members):
        from roster_membership import cargo_role_id

        with SessionLocal() as db:
            retired_role = cargo_role_id(db)
        return {
            sid
            for row in members
            for sid in [str(row.get("user", {}).get("id", ""))]
            if sid.isdigit()
            and not row.get("user", {}).get("bot")
            and self.role_id in {str(role) for role in row.get("roles", [])}
            and (
                not retired_role
                or retired_role not in {str(role) for role in row.get("roles", [])}
            )
        }

    async def capture_roster(self, now=None):
        now = now or utcnow()
        day = event_day(now)
        if day is None:
            return False
        with SessionLocal() as db:
            row = db.get(AssemblyWindow, day.isoformat())
            if row is None or row.member_ids:
                return False
        members = await self.members()
        member_ids = self.role_members(members)
        async with metadata_transaction() as db:
            row = db.get(AssemblyWindow, day.isoformat())
            if row and not row.member_ids:
                row.member_ids = json.dumps(sorted(member_ids, key=int))
                health = _json(row.healthy_servers, {})
                health["roster"] = roster_details(members, member_ids)
                steam_ids = {
                    str(u.discord_id): u.steam_id
                    for u in db.query(User).filter(User.discord_id.in_(member_ids))
                }
                for sid, details in health["roster"].items():
                    details["steam_id"] = steam_ids.get(sid)
                health["roster_basis"] = "during_event"
                row.healthy_servers = json.dumps(health, ensure_ascii=False)
        return True

    async def capture_voice(self, now=None):
        now = now or utcnow()
        if event_day(now) is None or not self.bot.is_ready():
            return False
        guild = (
            self.bot.get_guild(int(self.guild_id)) if self.guild_id.isdigit() else None
        )
        if guild is None or getattr(guild, "unavailable", False):
            return False
        people = {}
        afk_id = getattr(getattr(guild, "afk_channel", None), "id", None)
        # VoiceChannel/StageChannel.voice_states is public and works without
        # member caching. Guild has no public voice_states attribute.
        channels = list(guild.voice_channels) + list(guild.stage_channels)
        for channel in channels:
            if channel.id == afk_id or re.search(
                r"(?<!\w)(?:afk|афк)(?!\w)", channel.name, re.IGNORECASE
            ):
                continue
            for sid in channel.voice_states:
                people[str(sid)] = {"id": str(channel.id), "name": channel.name}
        return await asyncio.to_thread(observe_voice_snapshot, people, now)

    async def refresh_report_check(self, event_date, now=None):
        """Read the report topic and retain a preview for the admin panel."""
        now = now or utcnow()
        day = date.fromisoformat(event_date)
        starts_at, _ = bounds(day)
        members, threads = await asyncio.gather(self.members(), self.threads())
        member_ids = self.role_members(members)
        member_names = {}
        for member in members:
            raw_user = member.get("user", {}) if isinstance(member, dict) else {}
            sid = str(raw_user.get("id", ""))
            if sid in member_ids:
                member_names[sid] = str(
                    member.get("nick")
                    or raw_user.get("global_name")
                    or raw_user.get("username")
                    or sid
                )[:128]
        candidates = [row for row in threads if thread_matches(row.get("name"), day)]
        status = "ready"
        thread_id = None
        reports = {}
        error = None
        if len(candidates) != 1:
            status = "thread_missing" if not candidates else "thread_ambiguous"
            error = (
                "report_thread_missing" if not candidates else "report_thread_ambiguous"
            )
        else:
            thread_id = str(candidates[0]["id"])
            reports = accepted_reports(
                await self.messages(thread_id), thread_id, starts_at
            )
        local_today = now.replace(tzinfo=timezone.utc).astimezone(MOSCOW).date()
        next_check = (
            now + timedelta(seconds=REPORT_CHECK_SECONDS)
            if day >= local_today
            else None
        )
        async with metadata_transaction() as db:
            row = db.get(AssemblyReportCheck, event_date)
            if row is None:
                row = AssemblyReportCheck(event_date=event_date)
                db.add(row)
            row.status = status
            row.thread_id = thread_id
            row.member_ids = json.dumps(sorted(member_ids, key=int))
            row.member_names = json.dumps(
                member_names, ensure_ascii=False, sort_keys=True
            )
            row.reports = json.dumps(
                {sid: stamp.isoformat() for sid, stamp in reports.items()},
                sort_keys=True,
            )
            row.checked_at = now
            row.next_check = next_check
            row.last_error = error
        return {
            "event_date": event_date,
            "members": len(member_ids),
            "reports": len(reports),
            "status": status,
        }

    async def process_report_checks(self, now=None):
        if not self.guild_id.isdigit() or not self.role_id.isdigit():
            return False
        now = now or utcnow()
        target = report_target_day(now).isoformat()
        with SessionLocal.begin() as db:
            row = db.get(AssemblyReportCheck, target)
            if row is None:
                row = AssemblyReportCheck(
                    event_date=target, status="pending", next_check=now
                )
                db.add(row)
            due = (
                db.query(AssemblyReportCheck)
                .filter(
                    AssemblyReportCheck.next_check.is_not(None),
                    AssemblyReportCheck.next_check <= now,
                )
                .order_by(
                    AssemblyReportCheck.next_check, AssemblyReportCheck.event_date
                )
                .first()
            )
            if due is None:
                return False
            event_date = due.event_date
            due.next_check = now + timedelta(seconds=REPORT_CHECK_SECONDS)
        try:
            result = await self.refresh_report_check(event_date, now)
            logger.info("Assembly reports checked: %s", result)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Assembly report preview %s will retry: %s",
                event_date,
                type(exc).__name__,
            )
            async with metadata_transaction() as db:
                row = db.get(AssemblyReportCheck, event_date)
                if row is None:
                    row = AssemblyReportCheck(event_date=event_date)
                    db.add(row)
                row.status = "error"
                row.checked_at = now
                row.next_check = now + timedelta(seconds=REPORT_CHECK_SECONDS)
                row.last_error = str(exc)[:200]
            return False

    async def process_event(self, event_date):
        with SessionLocal() as db:
            event = db.get(AssemblyWindow, event_date)
            if not event:
                return False
            ok, problems = coverage(event)
        if not ok:
            async with metadata_transaction() as db:
                apply_assessments(db, event_date, set(), {}, utcnow())
            logger.warning(
                "Assembly %s not fined because attendance coverage is incomplete: %s",
                event_date,
                ",".join(problems),
            )
            return True
        day = date.fromisoformat(event_date)
        members, threads = await asyncio.gather(self.members(), self.threads())
        candidates = [row for row in threads if thread_matches(row.get("name"), day)]
        if len(candidates) != 1:
            raise ValueError(
                "report_thread_missing" if not candidates else "report_thread_ambiguous"
            )
        thread = candidates[0]
        messages = await self.messages(thread["id"])
        stored = set(_json(event.member_ids, []))
        member_ids = stored or self.role_members(members)
        reports = accepted_reports(messages, thread["id"], event.starts_at)
        async with metadata_transaction() as db:
            current = db.get(AssemblyWindow, event_date)
            current.thread_id = str(thread["id"])
            health = _json(current.healthy_servers, {})
            if not health.get("roster"):
                health["roster"] = roster_details(members, member_ids)
                health["roster_basis"] = "after_event"
                current.healthy_servers = json.dumps(health, ensure_ascii=False)
            result = apply_assessments(
                db, event_date, member_ids, reports, utcnow(), defer=True
            )
        logger.warning(
            "Assembly %s prepared for review: %s participants, %s reports, %s possible penalties",
            event_date,
            len(member_ids),
            len(reports),
            result["penalized"],
        )
        return True

    async def process_due(self):
        if not self.guild_id.isdigit() or not self.role_id.isdigit():
            return False
        now = utcnow()
        # Freeze the mandatory-role roster near the beginning of the assembly so
        # removing the role or leaving later cannot evade assessment.
        try:
            await self.capture_roster(now)
        except Exception:
            logger.warning("Assembly member roster will be retried")
        with SessionLocal.begin() as db:
            row = (
                db.query(AssemblyWindow)
                .filter(
                    AssemblyWindow.status.in_(("collecting", "forum_pending")),
                    AssemblyWindow.ends_at <= now - PROCESS_DELAY,
                    (AssemblyWindow.retry_at.is_(None))
                    | (AssemblyWindow.retry_at <= now),
                )
                .order_by(AssemblyWindow.event_date)
                .first()
            )
            if row is None:
                return False
            day = row.event_date
            row.attempts += 1
            row.retry_at = now + timedelta(minutes=min(60, max(5, row.attempts * 5)))
        try:
            return await self.process_event(day)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Assembly %s waits for Discord forum/member data: %s",
                day,
                type(exc).__name__,
            )
            with SessionLocal.begin() as db:
                row = db.get(AssemblyWindow, day)
                if row and row.status != "completed":
                    row.status = "forum_pending"
                    row.last_error = str(exc)[:100]
                from automation_control import queue_alert

                queue_alert(
                    db,
                    "assembly:" + day,
                    "assembly",
                    "Не удалось завершить проверку сбора "
                    + day
                    + ": "
                    + str(exc)[:160],
                )
            return False

    async def ready(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.loop(), name="st9-assembly-attendance")

    async def loop(self):
        while not self.bot.is_closed():
            if self.bot.is_ready():
                for stage in (
                    self.capture_voice,
                    self.process_report_checks,
                    self.process_due,
                ):
                    try:
                        await stage()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Assembly stage %s will retry", stage.__name__)
            await asyncio.sleep(60)


def register_assembly(bot):
    worker = AssemblyWorker(bot)
    bot.add_listener(worker.ready, "on_ready")
    bot.add_listener(worker.ready, "on_resumed")
    bot.st9_assembly = worker
    return worker
