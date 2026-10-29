"""Hourly Discord roster import and permanent read-only Cargo 200 archive."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from discord.http import Route
from sqlalchemy import Column, DateTime, Integer, String, Text

from database import (
    AppRuntime,
    Base,
    DiscordSyncPending,
    GameHistory,
    LoginLink,
    User,
)
from runtime import metadata_transaction

logger = logging.getLogger("strike_team9.roster")
SYNC_SECONDS = 60 * 60
RETRY_SECONDS = 5 * 60
CARGO_ROLE_NAME = "Груз 200"
FILTER_ROLE_IDS = (
    "1457398618192351334",
    "1506355940935467098",
    "1467925735674216478",
    "1468548988826292409",
    "1511019958426009670",
    "1485577945316720780",
    "1458403639377793118",
    "1447307527846101222",
    "1458519514470682654",
    "1446836770141507696",
    "1402207362126712864",
    "1457398926406844417",
)
# The roster filters retain their existing order. Saved membership also needs
# command/security leadership roles for the per-group online history view.
SNAPSHOT_ROLE_IDS = (
    *FILTER_ROLE_IDS,
    "1486041117408497704",
    "1517217078979989564",
    "1486041548566036540",
)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RosterMembership(Base):
    __tablename__ = "roster_memberships"
    user_id = Column(Integer, primary_key=True)
    discord_id = Column(String(20), unique=True, nullable=False, index=True)
    status = Column(String(24), nullable=False, default="active", index=True)
    first_participant_at = Column(DateTime, nullable=False)
    cargo200_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False)


class RosterSyncHealth(Base):
    __tablename__ = "roster_sync_health"
    id = Column(Integer, primary_key=True, default=1)
    status = Column(String(24), nullable=False, default="pending")
    checked_at = Column(DateTime, nullable=True)
    retry_at = Column(DateTime, nullable=True)
    participant_role_id = Column(String(20), nullable=True)
    cargo_role_id = Column(String(20), nullable=True)
    member_count = Column(Integer, nullable=False, default=0)
    created_count = Column(Integer, nullable=False, default=0)
    cargo_count = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)


class DiscordRoleCatalog(Base):
    __tablename__ = "discord_role_catalog"
    role_id = Column(String(20), primary_key=True)
    name = Column(String(128), nullable=False)
    sort_order = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class DiscordMemberRoleSnapshot(Base):
    __tablename__ = "discord_member_role_snapshots"
    discord_id = Column(String(20), primary_key=True)
    role_ids = Column(Text, nullable=False, default="[]")
    updated_at = Column(DateTime, nullable=False)


def _role_name(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def cargo_role_id(db):
    row = db.get(RosterSyncHealth, 1)
    return str(row.cargo_role_id) if row and row.cargo_role_id else None


def is_cargo_user(db, user_or_id):
    uid = user_or_id.id if isinstance(user_or_id, User) else user_or_id
    row = db.get(RosterMembership, uid) if uid else None
    return bool(row and row.status == "cargo200")


def cargo_user_ids(db):
    return {
        row[0]
        for row in db.query(RosterMembership.user_id).filter_by(status="cargo200")
    }


def role_filter_catalog(db):
    rows = {row.role_id: row for row in db.query(DiscordRoleCatalog).all()}
    return [
        {
            "id": role_id,
            "name": rows[role_id].name if role_id in rows else "Роль " + role_id,
            "order": index,
        }
        for index, role_id in enumerate(FILTER_ROLE_IDS)
    ]


def member_filter_roles(db):
    result = {}
    for row in db.query(DiscordMemberRoleSnapshot).all():
        try:
            values = json.loads(row.role_ids)
        except (TypeError, ValueError):
            values = []
        result[row.discord_id] = [
            str(value) for value in values if str(value) in FILTER_ROLE_IDS
        ]
    return result


def user_role_context(db, users):
    snapshots = member_filter_roles(db)
    catalog = role_filter_catalog(db)
    names = {row["id"]: row["name"] for row in catalog}
    fallback = len(FILTER_ROLE_IDS)
    role_ids = {user.id: snapshots.get(str(user.discord_id), []) for user in users}
    return (
        catalog,
        role_ids,
        {
            uid: [names[rid] for rid in values if rid in names]
            for uid, values in role_ids.items()
        },
        {
            uid: min([FILTER_ROLE_IDS.index(rid) for rid in values], default=fallback)
            for uid, values in role_ids.items()
        },
    )


def _next_user_id(db):
    runtime = db.get(AppRuntime, 1)
    if runtime is None:
        raise RuntimeError("runtime_missing")
    highest = max(
        db.query(User.id).order_by(User.id.desc()).limit(1).scalar() or 0,
        db.query(GameHistory.user_id)
        .order_by(GameHistory.user_id.desc())
        .limit(1)
        .scalar()
        or 0,
    )
    uid = max(runtime.next_user_id, highest + 1)
    runtime.next_user_id = uid + 1
    return uid


def _queue_membership_check(db, sid, now):
    row = db.get(DiscordSyncPending, sid)
    if row is None:
        db.add(
            DiscordSyncPending(discord_id=sid, version=1, updated_at=now, retry_at=now)
        )
    else:
        row.version += 1
        row.updated_at = now
        row.retry_at = now


def _state(db, user, now):
    row = db.get(RosterMembership, user.id)
    if row is None:
        row = RosterMembership(
            user_id=user.id,
            discord_id=user.discord_id,
            status="active",
            first_participant_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.discord_id = user.discord_id
        row.updated_at = now
    return row


def sync_snapshot(db, roles, members, required_role_id=None, now=None):
    """Apply one complete Discord snapshot. Network access stays outside this call."""
    now = now or utcnow()
    role_map = {
        str(row.get("id")): str(row.get("name") or "")
        for row in roles
        if isinstance(row, dict) and str(row.get("id", "")).isdigit()
    }
    participant = str(required_role_id or os.getenv("REQUIRED_ROLE_ID") or "")
    if not participant.isdigit() or participant not in role_map:
        raise RuntimeError("participant_role_missing")
    cargo_matches = [
        rid
        for rid, name in role_map.items()
        if _role_name(name) == _role_name(CARGO_ROLE_NAME)
    ]
    if len(cargo_matches) != 1:
        raise RuntimeError(
            "cargo200_role_missing" if not cargo_matches else "cargo200_role_ambiguous"
        )
    cargo = cargo_matches[0]
    normalized = []
    for member in members:
        if not isinstance(member, dict):
            continue
        raw_user = member.get("user") if isinstance(member.get("user"), dict) else {}
        sid = str(raw_user.get("id") or "")
        member_roles = {str(value) for value in member.get("roles", [])}
        if not sid.isdigit() or raw_user.get("bot"):
            continue
        username = str(raw_user.get("username") or sid)[:128]
        display = str(member.get("nick") or raw_user.get("global_name") or username)[
            :128
        ]
        normalized.append((sid, username, display, member_roles))

    for index, role_id in enumerate(FILTER_ROLE_IDS):
        row = db.get(DiscordRoleCatalog, role_id)
        name = role_map.get(role_id)
        if row is None:
            row = DiscordRoleCatalog(
                role_id=role_id,
                name=name or "Роль " + role_id,
                sort_order=index,
                updated_at=now,
            )
            db.add(row)
        else:
            if name:
                row.name = name
            row.sort_order = index
            row.updated_at = now
    db.query(DiscordMemberRoleSnapshot).delete(synchronize_session=False)
    for sid, _, _, member_roles in normalized:
        values = [role_id for role_id in SNAPSHOT_ROLE_IDS if role_id in member_roles]
        db.add(
            DiscordMemberRoleSnapshot(
                discord_id=sid, role_ids=json.dumps(values), updated_at=now
            )
        )

    users = {str(row.discord_id): row for row in db.query(User).all()}
    created = locked = restored = 0
    for sid, username, display, member_roles in normalized:
        has_participant = participant in member_roles
        has_cargo = cargo in member_roles
        user = users.get(sid)
        if has_participant and not has_cargo:
            if user is None:
                user = User(
                    id=_next_user_id(db),
                    discord_id=sid,
                    username=username,
                    display_name=display,
                    guild_nickname=display,
                    registered_at=now,
                )
                db.add(user)
                db.flush()
                users[sid] = user
                created += 1
            else:
                user.username = username
                user.guild_nickname = display
                if not user.display_name:
                    user.display_name = display
            state = _state(db, user, now)
            was_cargo = state.status == "cargo200"
            state.status = "active"
            if user.deleted_at is not None and was_cargo:
                user.deleted_at = None
                restored += 1
            _queue_membership_check(db, sid, now)
        elif has_cargo and user is not None:
            state = _state(db, user, now)
            if state.status != "cargo200":
                state.cargo200_at = now
            state.status = "cargo200"
            user.username = username
            user.guild_nickname = display
            if user.deleted_at is None:
                user.deleted_at = now
                locked += 1
            db.query(LoginLink).filter_by(discord_id=sid).delete(
                synchronize_session=False
            )
            _queue_membership_check(db, sid, now)

    health = db.get(RosterSyncHealth, 1)
    if health is None:
        health = RosterSyncHealth(id=1)
        db.add(health)
    health.status = "ready"
    health.checked_at = now
    health.retry_at = now + timedelta(seconds=SYNC_SECONDS)
    health.participant_role_id = participant
    health.cargo_role_id = cargo
    health.member_count = sum(
        1 for *_, member_roles in normalized if participant in member_roles
    )
    health.created_count = created
    health.cargo_count = sum(
        1 for row in db.query(RosterMembership).filter_by(status="cargo200")
    )
    health.last_error = None
    return {
        "members": health.member_count,
        "created": created,
        "locked": locked,
        "restored": restored,
        "cargo": health.cargo_count,
        "participant_role_id": participant,
        "cargo_role_id": cargo,
    }


class RosterWorker:
    def __init__(self, bot):
        self.bot = bot
        self.guild_id = str(os.getenv("REQUIRED_GUILD_ID") or "")
        self.required_role_id = str(os.getenv("REQUIRED_ROLE_ID") or "")
        self.task = None

    async def members(self):
        result = []
        after = "0"
        for _ in range(250):
            page = await asyncio.wait_for(
                self.bot.http.request(
                    Route("GET", "/guilds/{guild_id}/members", guild_id=self.guild_id),
                    params={"limit": 1000, "after": after},
                ),
                timeout=30,
            )
            if not isinstance(page, list):
                raise RuntimeError("invalid_member_list")
            result.extend(page)
            if len(page) < 1000:
                return result
            after = str(page[-1].get("user", {}).get("id", ""))
            if not after.isdigit():
                raise RuntimeError("invalid_member_page")
        raise RuntimeError("member_list_too_large")

    async def sync_once(self):
        if not self.guild_id.isdigit() or not self.required_role_id.isdigit():
            raise RuntimeError("discord_roster_not_configured")
        guild = self.bot.get_guild(int(self.guild_id))
        if guild is None:
            raise RuntimeError("guild_unavailable")
        roles, members = await asyncio.gather(
            asyncio.wait_for(guild.fetch_roles(), timeout=30), self.members()
        )
        role_data = [{"id": str(role.id), "name": role.name} for role in roles]
        async with metadata_transaction() as db:
            result = sync_snapshot(
                db, role_data, members, self.required_role_id, utcnow()
            )
            from automation_control import resolve_alerts

            resolve_alerts(db, "roster_sync")
        logger.info("Discord roster synced: %s", result)
        return result

    async def ready(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.loop(), name="st9-roster-sync")

    async def loop(self):
        delay = 0
        while not self.bot.is_closed():
            if delay:
                await asyncio.sleep(delay)
            try:
                if not self.bot.is_ready():
                    delay = 60
                    continue
                await self.sync_once()
                delay = SYNC_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Discord roster sync will retry")
                async with metadata_transaction() as db:
                    health = db.get(RosterSyncHealth, 1)
                    if health is None:
                        health = RosterSyncHealth(id=1)
                        db.add(health)
                    health.status = "error"
                    health.checked_at = utcnow()
                    health.retry_at = utcnow() + timedelta(seconds=RETRY_SECONDS)
                    health.last_error = str(exc)[:500]
                    from automation_control import queue_alert

                    queue_alert(
                        db,
                        "roster_sync",
                        "discord_roster",
                        "Не удалось синхронизировать участников Discord: "
                        + str(exc)[:160],
                    )
                delay = RETRY_SECONDS


def register_roster_sync(bot):
    worker = RosterWorker(bot)
    bot.add_listener(worker.ready, "on_ready")
    bot.add_listener(worker.ready, "on_resumed")
    bot.st9_roster_sync = worker
    return worker
