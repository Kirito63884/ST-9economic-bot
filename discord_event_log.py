"""Durable voice and moderation event log for the clan Discord server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import discord
from discord.http import Route
from sqlalchemy import Column, DateTime, Integer, String, Text, or_

from database import Base
from runtime import metadata_transaction


CHANNEL_ID = 1295011686436044873
MOSCOW = timezone(timedelta(hours=3))
DISCORD_EPOCH_MS = 1420070400000
NETWORK_SECONDS = 30
logger = logging.getLogger("strike_team9.discord_events")


class DiscordEventLog(Base):
    __tablename__ = "discord_event_logs"
    id = Column(Integer, primary_key=True)
    event_key = Column(String(100), nullable=False, unique=True)
    kind = Column(String(30), nullable=False)
    guild_id = Column(String(20), nullable=False)
    target_id = Column(String(20), nullable=False)
    actor_id = Column(String(20), nullable=True)
    channel_from = Column(String(20), nullable=True)
    channel_to = Column(String(20), nullable=True)
    payload = Column(Text, nullable=False)
    occurred_at = Column(DateTime, nullable=False)
    not_before = Column(DateTime, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    retry_at = Column(DateTime, nullable=True)
    lease_until = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    ignored_at = Column(DateTime, nullable=True)
    message_id = Column(String(20), nullable=True)
    last_error = Column(String(100), nullable=True)


class DiscordChannelCatalog(Base):
    """Last known Discord voice-channel names, retained after a rename/delete."""

    __tablename__ = "discord_channel_catalog"
    channel_id = Column(String(20), primary_key=True)
    guild_id = Column(String(20), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    updated_at = Column(DateTime, nullable=False)


class DiscordVoicePresence(Base):
    """Current voice state shared by the Discord worker and the website."""

    __tablename__ = "discord_voice_presence"
    target_id = Column(String(20), primary_key=True)
    guild_id = Column(String(20), nullable=False, index=True)
    channel_id = Column(String(20), nullable=False, index=True)
    channel_name = Column(String(100), nullable=False)
    joined_at = Column(DateTime, nullable=False)
    observed_at = Column(DateTime, nullable=False)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _text(value, limit=300):
    return value[:limit] if isinstance(value, str) else ""


def _user(data):
    if not isinstance(data, dict):
        return {}
    return {
        "id": str(data.get("id", "")),
        "name": _text(
            data.get("global_name") or data.get("username") or "Участник", 100
        ),
        "avatar": _text(data.get("avatar"), 100),
        "bot": bool(data.get("bot")),
    }


def _avatar(user):
    if not user.get("avatar") or not user.get("id", "").isdigit():
        return None
    suffix = "gif" if user["avatar"].startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.{suffix}?size=128"


def _event_key(payload, kind, target):
    sequence = payload.get("s")
    if isinstance(sequence, int):
        return f"{kind}:{sequence}:{target}"
    return f"{kind}:{target}:{time.time_ns()}"


def _add(
    db,
    key,
    kind,
    guild_id,
    target_id,
    payload,
    occurred_at,
    delay=0,
    channel_from=None,
    channel_to=None,
):
    if db.query(DiscordEventLog.id).filter_by(event_key=key).first():
        return False
    db.add(
        DiscordEventLog(
            event_key=key,
            kind=kind,
            guild_id=guild_id,
            target_id=target_id,
            channel_from=channel_from,
            channel_to=channel_to,
            payload=json.dumps(payload, ensure_ascii=False),
            occurred_at=occurred_at,
            not_before=occurred_at + timedelta(seconds=delay),
        )
    )
    return True


def _remember_channel(db, guild_id, channel_id, name, moment):
    channel_id = str(channel_id or "")
    name = _text(name, 100)
    if not channel_id.isdigit() or not name:
        return
    row = db.get(DiscordChannelCatalog, channel_id)
    if row is None:
        db.add(
            DiscordChannelCatalog(
                channel_id=channel_id,
                guild_id=str(guild_id),
                name=name,
                updated_at=moment,
            )
        )
    else:
        row.guild_id = str(guild_id)
        row.name = name
        row.updated_at = moment


def _set_voice_presence(db, guild_id, target_id, channel_id, channel_name, moment):
    target_id = str(target_id or "")
    channel_id = str(channel_id or "") if channel_id else ""
    row = db.get(DiscordVoicePresence, target_id)
    if not channel_id:
        if row is not None:
            db.delete(row)
        return
    channel_name = _text(channel_name, 100) or "Неизвестный голосовой канал"
    if row is None:
        db.add(
            DiscordVoicePresence(
                target_id=target_id,
                guild_id=str(guild_id),
                channel_id=channel_id,
                channel_name=channel_name,
                joined_at=moment,
                observed_at=moment,
            )
        )
    else:
        if row.channel_id != channel_id:
            row.joined_at = moment
        row.guild_id = str(guild_id)
        row.channel_id = channel_id
        row.channel_name = channel_name
        row.observed_at = moment


def claim(db, now):
    row = (
        db.query(DiscordEventLog)
        .filter(
            DiscordEventLog.sent_at.is_(None),
            DiscordEventLog.ignored_at.is_(None),
            DiscordEventLog.not_before <= now,
            or_(DiscordEventLog.retry_at.is_(None), DiscordEventLog.retry_at <= now),
            or_(
                DiscordEventLog.lease_until.is_(None),
                DiscordEventLog.lease_until <= now,
            ),
        )
        .order_by(DiscordEventLog.id)
        .first()
    )
    if row is None:
        return None
    row.attempts += 1
    row.lease_until = now + timedelta(seconds=NETWORK_SECONDS + 15)
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def covered_by_ban(db, job):
    start = job["occurred_at"] - timedelta(seconds=60)
    end = job["occurred_at"] + timedelta(seconds=60)
    return (
        db.query(DiscordEventLog.id)
        .filter_by(kind="ban", target_id=job["target_id"])
        .filter(
            DiscordEventLog.occurred_at >= start, DiscordEventLog.occurred_at <= end
        )
        .first()
        is not None
    )


def finish(
    db, job, now, *, message=None, error=None, ignored=False, actor=None, reason=None
):
    row = db.get(DiscordEventLog, job["id"])
    if row is None or row.sent_at or row.ignored_at:
        return
    row.kind = job["kind"]
    row.lease_until = None
    if actor is not None:
        row.actor_id = str(actor) if str(actor).isdigit() else None
        data = json.loads(row.payload)
        data["reason"] = _text(reason, 512)
        data["audit_resolved"] = True
        if row.kind in ("voice_move", "voice_leave"):
            data["forced"] = bool(row.actor_id)
        row.payload = json.dumps(data, ensure_ascii=False)
    if ignored:
        row.ignored_at = now
        row.last_error = error
    elif message:
        row.sent_at = now
        row.message_id = str(message)
        row.last_error = None
        row.retry_at = None
    elif error:
        row.last_error = error
        row.retry_at = now + timedelta(
            seconds=min(300, max(5, 2 ** min(row.attempts, 8)))
        )


def snowflake_time(value):
    try:
        return datetime.fromtimestamp(
            ((int(value) >> 22) + DISCORD_EPOCH_MS) / 1000, timezone.utc
        ).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        return None


def matching_audit(document, job):
    """Return (actor, reason), False when more delay may help, None for a normal leave."""
    entries = document.get("audit_log_entries") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise ValueError("invalid_audit_log")
    action = {
        "kick": 20,
        "ban": 22,
        "timeout": 24,
        "voice_move": 26,
        "voice_leave": 27,
    }[job["kind"]]
    candidates = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("action_type") != action:
            continue
        target = str(entry.get("target_id") or "")
        if target and target != job["target_id"]:
            continue
        at = snowflake_time(entry.get("id"))
        window = 15 if job["kind"] in ("voice_move", "voice_leave") else 45
        if at is None or abs((at - job["occurred_at"]).total_seconds()) > window:
            continue
        if job["kind"] == "voice_move":
            options = (
                entry.get("options") if isinstance(entry.get("options"), dict) else {}
            )
            if str(options.get("channel_id") or "") != str(job.get("channel_to") or ""):
                continue
        if job["kind"] == "timeout":
            changes = entry.get("changes", [])
            if not isinstance(changes, list) or not any(
                isinstance(c, dict)
                and c.get("key") == "communication_disabled_until"
                and c.get("new_value")
                for c in changes
            ):
                continue
        actor = str(entry.get("user_id", ""))
        if actor.isdigit():
            candidates.append((at, actor, _text(entry.get("reason"), 512)))
    if candidates:
        _, actor, reason = max(candidates, key=lambda item: item[0])
        return actor, reason
    # Voice audit entries can omit the moved member, so correlate a very recent
    # entry with the gateway event and destination. With no match, log it as a
    # normal self-directed voice change after short retries for audit-log lag.
    if job["kind"] in ("voice_move", "voice_leave"):
        return ("", "") if job["attempts"] >= 3 else False
    # Member removal without a matching kick entry is an ordinary server leave.
    if job["kind"] == "kick" and job["attempts"] >= 5:
        return None
    if job["attempts"] >= 5:
        if job["kind"] == "timeout" and json.loads(job.get("payload", "{}")).get(
            "require_audit"
        ):
            return None
        return "", ""
    return False


def embed_for(job):
    data = json.loads(job["payload"])
    target = data.get("user", {})
    mention = f"<@{job['target_id']}>"
    actor = f"<@{job['actor_id']}>" if job.get("actor_id") else "не удалось определить"
    reason = _text(data.get("reason"), 512) or "не указана"
    descriptions = {
        "voice_join": f"{mention} присоединился к голосовому каналу <#{job['channel_to']}>.",
        "voice_leave": (
            f"{mention} принудительно отключён от голосового канала <#{job['channel_from']}> · модератор {actor}"
            if data.get("forced")
            else f"{mention} вышел из голосового канала <#{job['channel_from']}>."
        ),
        "voice_move": (
            f"{mention} принудительно перемещён из <#{job['channel_from']}> в <#{job['channel_to']}> · модератор {actor}"
            if data.get("forced")
            else f"{mention} перешёл из <#{job['channel_from']}> в <#{job['channel_to']}>."
        ),
        "ban": f"{mention} забанен · модератор {actor} · причина: {reason}",
        "kick": f"{mention} выгнан · модератор {actor} · причина: {reason}",
        "member_leave": f"{mention} вышел с Discord-сервера.",
        "timeout": f"{mention} получил таймаут до <t:{data.get('until_unix', 0)}:F> · модератор {actor} · причина: {reason}",
    }
    colors = {
        "voice_join": 0x3A8D0B,
        "voice_leave": 0xD7193F,
        "voice_move": 0xD88912,
        "ban": 0x8B0000,
        "kick": 0xE05A00,
        "member_leave": 0x687386,
        "timeout": 0x7B2CBF,
    }
    local = job["occurred_at"].replace(tzinfo=timezone.utc).astimezone(MOSCOW)
    embed = {
        "description": descriptions[job["kind"]],
        "color": colors[job["kind"]],
        "footer": {"text": "ST-9 • " + local.strftime("%d.%m.%Y %H:%M МСК")},
        "author": {"name": _text(target.get("name"), 100) or "Участник"},
    }
    icon = _avatar(target)
    if icon:
        embed["author"]["icon_url"] = icon
    return embed


class DiscordEventWorker:
    def __init__(self, bot):
        self.bot = bot
        self.guild_id = str(os.getenv("REQUIRED_GUILD_ID", ""))
        self.channels = {}
        self.channel_names = {}
        self.timeouts = {}
        self.bots = set()
        self.task = None

    def channel_name(self, channel_id):
        sid = str(channel_id or "")
        if not sid:
            return ""
        if sid in self.channel_names:
            return self.channel_names[sid]
        getter = getattr(self.bot, "get_channel", None)
        channel = getter(int(sid)) if getter and sid.isdigit() else None
        name = _text(getattr(channel, "name", ""), 100) if channel else ""
        if name:
            self.channel_names[sid] = name
        return name

    async def raw_packet(self, raw):
        from discord_sync import gateway_packet

        payload = gateway_packet(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("d"), dict):
            return
        event = payload.get("t")
        data = payload["d"]
        gid = str(data.get("guild_id", data.get("id", "")))
        if not self.guild_id or gid != self.guild_id:
            return
        now = utcnow()
        if event == "GUILD_CREATE":
            self.channels = {
                str(v.get("user_id")): str(v.get("channel_id"))
                for v in data.get("voice_states", [])
                if v.get("user_id") and v.get("channel_id")
            }
            self.channel_names = {
                str(c.get("id")): _text(c.get("name"), 100)
                for c in data.get("channels", [])
                if str(c.get("id", "")).isdigit()
                and c.get("type") in (2, 13)
                and _text(c.get("name"), 100)
            }
            self.bots = {
                str(m.get("user", {}).get("id"))
                for m in data.get("members", [])
                if m.get("user", {}).get("bot")
            }
            self.timeouts = {
                str(m.get("user", {}).get("id")): m.get("communication_disabled_until")
                for m in data.get("members", [])
                if m.get("user", {}).get("id")
            }
            async with metadata_transaction() as db:
                for channel_id, name in self.channel_names.items():
                    _remember_channel(db, gid, channel_id, name, now)
                current = set(self.channels) - self.bots
                for row in db.query(DiscordVoicePresence).filter_by(guild_id=gid).all():
                    if row.target_id not in current:
                        db.delete(row)
                for target_id, channel_id in self.channels.items():
                    if target_id in self.bots:
                        continue
                    _set_voice_presence(
                        db,
                        gid,
                        target_id,
                        channel_id,
                        self.channel_names.get(channel_id, ""),
                        now,
                    )
            return
        if event in ("CHANNEL_CREATE", "CHANNEL_UPDATE") and data.get("type") in (
            2,
            13,
        ):
            sid = str(data.get("id", ""))
            name = _text(data.get("name"), 100)
            if sid.isdigit() and name:
                self.channel_names[sid] = name
                async with metadata_transaction() as db:
                    _remember_channel(db, gid, sid, name, now)
                    for row in (
                        db.query(DiscordVoicePresence).filter_by(channel_id=sid).all()
                    ):
                        row.channel_name = name
                        row.observed_at = now
            return
        if event == "CHANNEL_DELETE":
            self.channel_names.pop(str(data.get("id", "")), None)
            return
        if event == "GUILD_MEMBERS_CHUNK":
            for member in data.get("members", []):
                user = _user(member.get("user", {}))
                sid = user.get("id", "")
                if not sid.isdigit():
                    continue
                self.timeouts[sid] = member.get("communication_disabled_until")
                if user.get("bot"):
                    self.bots.add(sid)
            return
        if event == "VOICE_STATE_UPDATE":
            sid = str(data.get("user_id", ""))
            user = _user(data.get("member", {}).get("user", {}))
            if not sid.isdigit() or user.get("bot") or sid in self.bots:
                return
            old = self.channels.get(sid)
            new = str(data.get("channel_id")) if data.get("channel_id") else None
            if old == new:
                return
            old_name = self.channel_name(old)
            new_name = self.channel_name(new)
            if new:
                self.channels[sid] = new
            else:
                self.channels.pop(sid, None)
            kind = (
                "voice_join" if not old else "voice_leave" if not new else "voice_move"
            )
            delay = 2 if kind in ("voice_move", "voice_leave") else 0
            async with metadata_transaction() as db:
                if new_name:
                    _remember_channel(db, gid, new, new_name, now)
                _set_voice_presence(db, gid, sid, new, new_name, now)
                _add(
                    db,
                    _event_key(payload, kind, sid),
                    kind,
                    gid,
                    sid,
                    {
                        "user": user,
                        "channel_from_name": old_name,
                        "channel_to_name": new_name,
                    },
                    now,
                    delay,
                    channel_from=old,
                    channel_to=new,
                )
            return
        if event == "GUILD_BAN_ADD":
            user = _user(data.get("user", {}))
            sid = user.get("id", "")
            if sid.isdigit():
                async with metadata_transaction() as db:
                    _add(
                        db,
                        _event_key(payload, "ban", sid),
                        "ban",
                        gid,
                        sid,
                        {"user": user},
                        now,
                        2,
                    )
            return
        if event == "GUILD_MEMBER_REMOVE":
            user = _user(data.get("user", {}))
            sid = user.get("id", "")
            if sid.isdigit() and not user.get("bot"):
                async with metadata_transaction() as db:
                    _set_voice_presence(db, gid, sid, None, "", now)
                    _add(
                        db,
                        _event_key(payload, "kick", sid),
                        "kick",
                        gid,
                        sid,
                        {"user": user},
                        now,
                        2,
                    )
            self.channels.pop(sid, None)
            self.timeouts.pop(sid, None)
            self.bots.discard(sid)
            return
        if event == "GUILD_MEMBER_UPDATE":
            user = _user(data.get("user", {}))
            sid = user.get("id", "")
            new = data.get("communication_disabled_until")
            known = sid in self.timeouts
            old = self.timeouts.get(sid)
            self.timeouts[sid] = new
            if not sid.isdigit() or not new or new == old:
                return
            try:
                until = datetime.fromisoformat(new.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return
            if until <= datetime.now(timezone.utc):
                return
            details = {
                "user": user,
                "until": new,
                "until_unix": int(until.timestamp()),
                "require_audit": not known,
            }
            async with metadata_transaction() as db:
                _add(
                    db,
                    _event_key(payload, "timeout", sid),
                    "timeout",
                    gid,
                    sid,
                    details,
                    now,
                    2,
                )

    async def ready(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.loop(), name="st9-discord-event-log")

    async def resolve_audit(self, job):
        route = Route(
            "GET", "/guilds/{guild_id}/audit-logs", guild_id=int(job["guild_id"])
        )
        action = {
            "kick": 20,
            "ban": 22,
            "timeout": 24,
            "voice_move": 26,
            "voice_leave": 27,
        }[job["kind"]]
        document = await self.bot.http.request(
            route, params={"limit": 10, "action_type": action}
        )
        return matching_audit(document, job)

    async def process_one(self):
        async with metadata_transaction() as db:
            job = claim(db, utcnow())
        if job is None:
            return False
        details = json.loads(job["payload"])
        if job["kind"] in (
            "kick",
            "ban",
            "timeout",
            "voice_move",
            "voice_leave",
        ) and not details.get("audit_resolved"):
            try:
                matched = await asyncio.wait_for(
                    self.resolve_audit(job), timeout=NETWORK_SECONDS
                )
            except asyncio.CancelledError:
                raise
            except discord.Forbidden:
                logger.warning(
                    "View Audit Log permission is required for Discord moderation logs"
                )
                if job["kind"] == "kick":
                    async with metadata_transaction() as db:
                        finish(db, job, utcnow(), error="audit_permissions")
                    return False
                matched = ("", "")
            except Exception:
                async with metadata_transaction() as db:
                    finish(db, job, utcnow(), error="audit_unavailable")
                return False
            if matched is False:
                async with metadata_transaction() as db:
                    finish(db, job, utcnow(), error="audit_pending")
                return False
            if matched is None:
                if job["kind"] == "kick":
                    async with metadata_transaction() as db:
                        if covered_by_ban(db, job):
                            finish(
                                db, job, utcnow(), ignored=True, error="covered_by_ban"
                            )
                            return True
                    matched = ("", "")
                    job["kind"] = "member_leave"
                else:
                    async with metadata_transaction() as db:
                        finish(
                            db,
                            job,
                            utcnow(),
                            ignored=True,
                            error="unconfirmed_moderation",
                        )
                    return True
            actor, reason = matched
            job["actor_id"] = actor
            data = json.loads(job["payload"])
            data["reason"] = reason
            data["audit_resolved"] = True
            if job["kind"] in ("voice_move", "voice_leave"):
                data["forced"] = bool(str(actor).isdigit())
            job["payload"] = json.dumps(data, ensure_ascii=False)
            async with metadata_transaction() as db:
                finish(db, job, utcnow(), actor=actor, reason=reason)
        try:
            payload = {"embeds": [embed_for(job)], "allowed_mentions": {"parse": []}}
            result = await asyncio.wait_for(
                self.bot.http.request(
                    Route(
                        "POST", "/channels/{channel_id}/messages", channel_id=CHANNEL_ID
                    ),
                    json=payload,
                ),
                timeout=NETWORK_SECONDS,
            )
            message = str(result.get("id", "")) if isinstance(result, dict) else ""
            if not message:
                raise RuntimeError("invalid_message")
            async with metadata_transaction() as db:
                finish(db, job, utcnow(), message=message)
            return True
        except asyncio.CancelledError:
            raise
        except discord.Forbidden:
            error = "channel_permissions"
        except discord.NotFound:
            error = "channel_missing"
        except Exception:
            error = "discord_unavailable"
        async with metadata_transaction() as db:
            finish(db, job, utcnow(), error=error)
        return False

    async def loop(self):
        while not self.bot.is_closed():
            try:
                if self.bot.is_ready():
                    await self.process_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Discord event log will retry")
            await asyncio.sleep(1)


def register_discord_event_log(bot):
    worker = DiscordEventWorker(bot)
    bot.add_listener(worker.raw_packet, "on_socket_raw_receive")
    bot.add_listener(worker.ready, "on_ready")
    bot.add_listener(worker.ready, "on_resumed")
    bot.st9_discord_event_log = worker
    return worker
