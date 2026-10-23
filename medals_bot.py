"""Voice checkpoints and durable Discord award announcements."""

import asyncio
import json
import logging
import os
import time
from datetime import timedelta, timezone
import discord
from discord.http import Route
from database import User, SessionLocal
from runtime import metadata_transaction
from medals import (
    CHANNEL_ID,
    ROLE_IDS,
    MANUAL_ROLES,
    CATALOG,
    MANUAL_CATALOG,
    MedalAward,
    VoiceTime,
    MedalBotHealth,
    utcnow,
)
from award_rewards import AwardRewardPayment
from distinctions_catalog import ROLE_IDS as DISTINCTION_ROLES
from award_role_names import award_name, sync_names, name_revision
from award_design import attachment_for
from award_role_sync import queue_all, member_event, claim, finish, NETWORK_SECONDS
from award_reannouncement import claim_replay, finish_replay
from award_revocation import active_awards, is_revoked, claim_removal, finish_removal

logger = logging.getLogger("strike_team9.medals")
AWARD_NAMES_REFRESH_SECONDS = 24 * 60 * 60
AWARD_NAMES_RETRY_SECONDS = 60


class MedalBot:
    def __init__(self, bot, clock=time.monotonic):
        self.bot = bot
        self.clock = clock
        self.guild_id = str(os.getenv("REQUIRED_GUILD_ID", ""))
        self.afk_id = None
        self.channels = {}
        self.bots = set()
        self.active = {}
        self.lock = asyncio.Lock()
        self.task = None
        self.names_lock = asyncio.Lock()
        self.names_fetch_lock = asyncio.Lock()
        self.names_version = 0
        self.next_names_refresh = 0
        self.connected = False
        self.last_event_at = None
        self.role_task = None
        self.notification_task = None
        self.started = False

    async def refresh_names(self):
        """Repair missed rename events without network requests inside the DB write lock."""
        if not self.guild_id:
            return False
        guild = self.bot.get_guild(int(self.guild_id))
        if not guild or str(guild.id) != self.guild_id:
            return False
        async with self.names_fetch_lock:
            version = self.names_version
            with SessionLocal() as db:
                revision = name_revision(db)
            try:
                roles = await asyncio.wait_for(guild.fetch_roles(), timeout=20)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Discord award names unavailable; cached names retained")
                return False
            async with self.names_lock:
                # A Gateway rename received during the HTTP request is newer.
                if version != self.names_version:
                    return False
                async with metadata_transaction() as db:
                    if revision != name_revision(db):
                        return False
                    sync_names(db, [{"id": str(r.id), "name": r.name} for r in roles])
        return True

    async def checkpoint(self, ids=None):
        now = self.clock()
        changes = {}
        for sid in list(self.active) if ids is None else ids:
            if sid in self.active:
                previous, fraction = self.active[sid]
                elapsed = max(0, now - previous) + fraction
                changes[sid] = (int(elapsed), elapsed % 1)
        async with metadata_transaction() as db:
            for sid, (seconds, _) in changes.items():
                row = db.get(VoiceTime, sid)
                if row is None:
                    db.add(
                        VoiceTime(discord_id=sid, seconds=seconds, updated_at=utcnow())
                    )
                else:
                    row.seconds += seconds
                    row.updated_at = utcnow()
            health = db.get(MedalBotHealth, 1)
            if health is None:
                health = MedalBotHealth(id=1, checked_at=utcnow())
                db.add(health)
            health.connected = self.connected
            health.active_voice = len(self.active)
            health.checked_at = utcnow()
            health.last_event_at = self.last_event_at
        for sid, (_, fraction) in changes.items():
            if sid in self.active:
                self.active[sid] = (now, fraction)

    def eligible(self, sid, channel):
        return bool(
            channel
            and channel != self.afk_id
            and sid not in self.bots
            and sid != str(getattr(self.bot.user, "id", ""))
        )

    async def raw_packet(self, raw):
        from discord_sync import gateway_packet

        packet = gateway_packet(raw)
        if packet is not None:
            await self.packet(packet)

    async def packet(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("d"), dict):
            return
        event = payload.get("t")
        data = payload["d"]
        gid = data.get("guild_id", data.get("id"))
        if not self.guild_id or str(gid) != self.guild_id:
            return
        self.last_event_at = utcnow()
        if event in ("GUILD_MEMBER_UPDATE", "GUILD_MEMBER_ADD"):
            async with metadata_transaction() as db:
                member_event(db, data)
        if event in ("GUILD_CREATE", "GUILD_ROLE_CREATE", "GUILD_ROLE_UPDATE"):
            roles = data.get("roles") if event == "GUILD_CREATE" else [data.get("role")]
            async with self.names_lock:
                self.names_version += 1
                async with metadata_transaction() as db:
                    sync_names(db, roles)
        async with self.lock:
            if event == "GUILD_CREATE":
                self.connected = True
                await self.checkpoint()
                self.active.clear()
                self.channels.clear()
                self.afk_id = (
                    str(data["afk_channel_id"]) if data.get("afk_channel_id") else None
                )
                self.bots = {
                    str(m.get("user", {}).get("id"))
                    for m in data.get("members", [])
                    if m.get("user", {}).get("bot")
                }
                for v in data.get("voice_states", []):
                    sid = str(v.get("user_id", ""))
                    ch = str(v["channel_id"]) if v.get("channel_id") else None
                    self.channels[sid] = ch
                    if self.eligible(sid, ch):
                        self.active[sid] = (self.clock(), 0)
            elif event == "VOICE_STATE_UPDATE":
                sid = str(data.get("user_id", ""))
                if not sid.isdigit():
                    return
                await self.checkpoint([sid])
                member = data.get("member", {}).get("user", {})
                if member.get("bot"):
                    self.bots.add(sid)
                ch = str(data["channel_id"]) if data.get("channel_id") else None
                self.channels[sid] = ch
                if self.eligible(sid, ch):
                    self.active.setdefault(sid, (self.clock(), 0))
                else:
                    self.active.pop(sid, None)
            elif event == "GUILD_UPDATE" and "afk_channel_id" in data:
                await self.checkpoint()
                self.afk_id = (
                    str(data["afk_channel_id"]) if data.get("afk_channel_id") else None
                )
                for sid, ch in self.channels.items():
                    if self.eligible(sid, ch):
                        self.active.setdefault(sid, (self.clock(), 0))
                    else:
                        self.active.pop(sid, None)
            elif event == "GUILD_DELETE":
                self.connected = False
                await self.checkpoint()
                self.active.clear()
                self.channels.clear()

    async def disconnect(self):
        async with self.lock:
            self.connected = False
            await self.checkpoint()
            self.active.clear()

    async def ready(self):
        self.connected = True
        if not self.started:
            async with metadata_transaction() as db:
                count = queue_all(db)
            self.started = True
            logger.info("Earned award role checks queued for %s players", count)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.loop(), name="st9-medals-bot")
        if self.role_task is None or self.role_task.done():
            self.role_task = asyncio.create_task(
                self.role_loop(), name="st9-award-role-sync"
            )
        if self.notification_task is None or self.notification_task.done():
            self.notification_task = asyncio.create_task(
                self.notification_loop(), name="st9-award-announcements"
            )
        # After RESUMED no new GUILD_CREATE is sent; resume from the actual cache.
        guild = self.bot.get_guild(int(self.guild_id)) if self.guild_id else None
        if guild:
            async with self.lock:
                self.afk_id = str(guild.afk_channel.id) if guild.afk_channel else None
                states = {}
                # Guild has no public voice_states property in discord.py 2.x.
                # Voice/Stage channels expose their actual cached voice states.
                for channel in [*guild.voice_channels, *guild.stage_channels]:
                    states.update(channel.voice_states)
                for sid, state in states.items():
                    sid = str(sid)
                    member = guild.get_member(int(sid))
                    if member and member.bot:
                        self.bots.add(sid)
                    ch = str(state.channel.id) if state.channel else None
                    self.channels[sid] = ch
                    if self.eligible(sid, ch):
                        self.active.setdefault(sid, (self.clock(), 0))
                await self.checkpoint()

    async def deliver_announcement(self, record):
        message = None
        with SessionLocal() as db:
            if is_revoked(db, record["discord_id"], record["medal"]):
                raise RuntimeError("award_revoked")
        channel = self.bot.get_channel(CHANNEL_ID) or await self.bot.fetch_channel(
            CHANNEL_ID
        )
        if str(getattr(getattr(channel, "guild", None), "id", "")) != self.guild_id:
            raise RuntimeError("wrong_guild")
        # PUT is idempotent: a lost response can be retried without extra roles.
        try:
            await self.bot.http.add_role(
                int(self.guild_id),
                int(record["discord_id"]),
                {**ROLE_IDS, **MANUAL_ROLES}[record["medal"]],
                reason="Награда Strike Team 9: " + record["name"],
            )
        except discord.Forbidden:
            raise RuntimeError("role_permissions")
        except discord.NotFound:
            raise RuntimeError("role_or_member_missing")
        marker = record.get("marker", f"ST9-MEDAL-{record['id']}")
        if record["attempts"] > 1:
            history_options = (
                {
                    "limit": None,
                    "after": record["requested_at"].replace(tzinfo=timezone.utc),
                }
                if record.get("repeat")
                else {"limit": 100}
            )
            async for old in channel.history(**history_options):
                if old.author.id == self.bot.user.id and any(
                    e.footer.text == marker for e in old.embeds
                ):
                    message = str(old.id)
                    break
        if message is None:
            with SessionLocal() as db:
                if is_revoked(db, record["discord_id"], record["medal"]):
                    raise RuntimeError("award_revoked")
                current_name = award_name(db, record["medal"])
                current_image = attachment_for(db, record["medal"])
            _, _, color, _ = {**CATALOG, **MANUAL_CATALOG}[record["medal"]]
            name = current_name
            embed = discord.Embed(
                title="🏅 " + name,
                description=record["reason"],
                color=int(color[1:], 16),
            )
            if record["reward"] is not None:
                embed.add_field(
                    name="Награда на сайте",
                    value=f"+{record['reward']} бишопкоинов",
                    inline=False,
                )
            if record.get("repeat"):
                embed.add_field(
                    name="Повторное награждение",
                    value="Ранее заработанная награда. Повторного начисления бишопкоинов нет.",
                    inline=False,
                )
            embed.set_footer(text=marker)
            image, filename, content_type = (
                current_image if current_image else (None, None, None)
            )
            if image is not None:
                embed.set_image(url="attachment://" + filename)
            payload = {
                "content": f"<@{record['discord_id']}> получает "
                + (
                    "знак отличия"
                    if record["medal"] in DISTINCTION_ROLES
                    else "орден"
                    if record["medal"] == "iron_cross"
                    else "медаль"
                )
                + f" **{discord.utils.escape_markdown(discord.utils.escape_mentions(name))}**!",
                "embeds": [embed.to_dict()],
                "allowed_mentions": {
                    "parse": [],
                    "users": [record["discord_id"]],
                    "replied_user": False,
                },
                "nonce": record.get("nonce", "m" + str(record["id"])),
                "enforce_nonce": True,
                "attachments": [{"id": 0, "filename": filename}]
                if image is not None
                else [],
            }
            route = Route(
                "POST", "/channels/{channel_id}/messages", channel_id=CHANNEL_ID
            )
            if image is not None:
                attachment = discord.File(image, filename=filename)
                try:
                    data = await self.bot.http.request(
                        route,
                        files=[attachment],
                        form=[
                            {"name": "payload_json", "value": json.dumps(payload)},
                            {
                                "name": "files[0]",
                                "value": attachment.fp,
                                "filename": filename,
                                "content_type": content_type,
                            },
                        ],
                    )
                finally:
                    attachment.close()
            else:
                data = await self.bot.http.request(route, json=payload)
            message = str(data["id"])
        return message

    async def announce_one(self):
        now = utcnow()
        async with metadata_transaction() as db:
            row = (
                active_awards(db)
                .filter(
                    MedalAward.sent_at.is_(None),
                    (MedalAward.retry_at.is_(None) | (MedalAward.retry_at <= now)),
                    (
                        MedalAward.lease_until.is_(None)
                        | (MedalAward.lease_until <= now)
                    ),
                )
                .order_by(MedalAward.id)
                .first()
            )
            if not row:
                return False
            user = (
                db.query(User)
                .filter(User.discord_id == row.discord_id, User.deleted_at.is_(None))
                .first()
            )
            if not user:
                row.retry_at = now + timedelta(hours=1)
                return False
            row.lease_until = now + timedelta(minutes=5)
            row.attempts += 1
            record = {
                "id": row.id,
                "discord_id": row.discord_id,
                "medal": row.medal,
                "reason": row.reason,
                "attempts": row.attempts,
                "name": award_name(db, row.medal),
            }
            payment = db.get(AwardRewardPayment, row.id)
            record["reward"] = payment.amount if payment and payment.paid_at else None
        message = None
        error = None
        try:
            message = await asyncio.wait_for(
                self.deliver_announcement(record), timeout=NETWORK_SECONDS
            )
        except asyncio.TimeoutError:
            error = "discord_timeout"
        except discord.Forbidden:
            error = "channel_permissions"
        except discord.NotFound:
            error = "channel_not_found"
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, RuntimeError)
                and str(exc)
                in (
                    "wrong_guild",
                    "asset_missing",
                    "role_permissions",
                    "role_or_member_missing",
                )
                else "discord_unavailable"
            )
            logger.warning("Medal notification %s failed: %s", record["id"], error)
        async with metadata_transaction() as db:
            row = db.get(MedalAward, record["id"])
            if row and row.sent_at is None:
                row.lease_until = None
                if message:
                    row.sent_at = utcnow()
                    row.message_id = message
                    row.last_error = None
                    row.retry_at = None
                else:
                    row.last_error = error
                    row.retry_at = utcnow() + timedelta(
                        seconds=min(3600, 30 * 2 ** min(record["attempts"], 7))
                    )
                    from automation_control import queue_alert

                    queue_alert(
                        db,
                        "award_announcements",
                        "Discord",
                        "Не удалось выдать роль или отправить объявление о награде: "
                        + str(error)
                        + ". Бот повторит попытку.",
                    )
                if message:
                    from automation_control import resolve_alerts

                    resolve_alerts(db, "award_announcements")
        return message is not None

    async def loop(self):
        last_voice = self.clock()
        while not self.bot.is_closed():
            try:
                if self.clock() - last_voice >= 60:
                    async with self.lock:
                        await self.checkpoint()
                    last_voice = self.clock()
                if self.bot.is_ready():
                    if self.clock() >= self.next_names_refresh:
                        ok = await self.refresh_names()
                        self.next_names_refresh = self.clock() + (
                            AWARD_NAMES_REFRESH_SECONDS
                            if ok
                            else AWARD_NAMES_RETRY_SECONDS
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Medal background task will retry")
            await asyncio.sleep(5)

    async def eligible_member_roles(self, sid):
        member = await self.bot.http.get_member(int(self.guild_id), int(sid))
        if (
            not isinstance(member, dict)
            or not isinstance(member.get("roles"), list)
            or any(not str(r).isdigit() for r in member["roles"])
        ):
            raise RuntimeError("discord_unavailable")
        identity = member.get("user", {})
        if str(identity.get("id", "")) != sid:
            raise RuntimeError("discord_unavailable")
        required = os.getenv("REQUIRED_ROLE_ID")
        present = {str(r) for r in member["roles"]}
        if identity.get("bot") or (
            required
            and required not in present
            and sid != os.getenv("ADMIN_DISCORD_ID")
        ):
            raise RuntimeError("member_ineligible")
        # Membership/website deletion may have changed during the HTTP request.
        with SessionLocal() as db:
            if (
                not db.query(User.id)
                .filter(User.discord_id == sid, User.deleted_at.is_(None))
                .first()
            ):
                raise RuntimeError("member_ineligible")
        return present

    async def restore_roles(self, job):
        present = await self.eligible_member_roles(job["sid"])
        role_error = None
        for role in sorted(job["roles"] - present):
            # The administrator may have revoked a role while GET was running.
            with SessionLocal() as db:
                from award_revocation import AwardRevocation

                mapping = {**ROLE_IDS, **MANUAL_ROLES}
                blocked = {
                    str(mapping.get(r.medal, ""))
                    for r in db.query(AwardRevocation).filter_by(discord_id=job["sid"])
                }
                if role in blocked:
                    continue
            try:
                await self.bot.http.add_role(
                    int(self.guild_id),
                    int(job["sid"]),
                    int(role),
                    reason="Восстановление ранее заработанной награды ST-9",
                )
            except discord.Forbidden:
                role_error = "role_permissions"
                continue
            except discord.NotFound:
                role_error = "role_or_member_missing"
                continue
            job["restored"] += 1
        if role_error:
            raise RuntimeError(role_error)

    async def reconcile_one(self):
        if not self.guild_id:
            return False
        async with metadata_transaction() as db:
            job = claim(db, utcnow())
        if job is None:
            return False
        job["restored"] = 0
        error = None
        try:
            await asyncio.wait_for(self.restore_roles(job), timeout=NETWORK_SECONDS)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            error = "discord_timeout"
        except discord.Forbidden:
            error = "role_permissions"
        except discord.NotFound:
            error = "member_missing"
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, RuntimeError)
                and str(exc)
                in ("member_ineligible", "role_or_member_missing", "role_permissions")
                else "discord_unavailable"
            )
        async with metadata_transaction() as db:
            finish(db, job, utcnow(), job["restored"], error)
        if error:
            logger.warning(
                "Award role restoration for %s deferred: %s", job["sid"], error
            )
        elif job["restored"]:
            logger.info(
                "Restored %s earned award roles for %s", job["restored"], job["sid"]
            )
        return error is None

    async def role_loop(self):
        while not self.bot.is_closed():
            try:
                if self.bot.is_ready():
                    await self.remove_one()
                    await self.remove_extra_one()
                    await self.reconcile_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Award role checks will retry")
            await asyncio.sleep(3)

    async def remove_one(self):
        if not self.guild_id:
            return False
        async with metadata_transaction() as db:
            job = claim_removal(db, utcnow())
        if job is None:
            return False

        async def remove():
            if not job["role"]:
                raise RuntimeError("role_unknown")
            # Revoked roles must also be removed from people who lost clan membership.
            try:
                member = await self.bot.http.get_member(
                    int(self.guild_id), int(job["sid"])
                )
                if not isinstance(member, dict) or not isinstance(
                    member.get("roles"), list
                ):
                    raise RuntimeError("discord_unavailable")
                if str(member.get("user", {}).get("id", "")) != job["sid"] or any(
                    not str(r).isdigit() for r in member["roles"]
                ):
                    raise RuntimeError("discord_unavailable")
                if str(job["role"]) not in {str(r) for r in member["roles"]}:
                    return
                await self.bot.http.remove_role(
                    int(self.guild_id),
                    int(job["sid"]),
                    job["role"],
                    reason="Награда ST-9 снята администратором на сайте",
                )
            except discord.NotFound:
                return

        error = None
        try:
            await asyncio.wait_for(remove(), timeout=NETWORK_SECONDS)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            error = "discord_timeout"
        except discord.Forbidden:
            error = "role_permissions"
        except Exception:
            error = "discord_unavailable"
        async with metadata_transaction() as db:
            finish_removal(db, job, utcnow(), error)
        if error:
            logger.warning("Award role removal %s will retry: %s", job["id"], error)
        else:
            logger.info("Revoked award role removed: %s", job["id"])
        return error is None

    async def notification_loop(self):
        while not self.bot.is_closed():
            try:
                if self.bot.is_ready():
                    await self.announce_one()
                    await self.replay_one()
                    await self.alert_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Award announcements will retry")
            await asyncio.sleep(5)

    async def remove_extra_one(self):
        from automation_control import DiscordRoleRemoval

        now = utcnow()
        async with metadata_transaction() as db:
            row = (
                db.query(DiscordRoleRemoval)
                .filter(
                    DiscordRoleRemoval.removed_at.is_(None),
                    (DiscordRoleRemoval.retry_at.is_(None))
                    | (DiscordRoleRemoval.retry_at <= now),
                )
                .order_by(DiscordRoleRemoval.requested_at)
                .first()
            )
            if row is None:
                return False
            row.attempts += 1
            row.retry_at = now + timedelta(minutes=5)
            job = {
                "id": row.id,
                "sid": row.discord_id,
                "role": int(row.role_id),
                "attempts": row.attempts,
            }
        error = None
        try:
            member = await asyncio.wait_for(
                self.bot.http.get_member(int(self.guild_id), int(job["sid"])),
                timeout=NETWORK_SECONDS,
            )
            if str(job["role"]) in {str(role) for role in member.get("roles", [])}:
                await asyncio.wait_for(
                    self.bot.http.remove_role(
                        int(self.guild_id),
                        int(job["sid"]),
                        job["role"],
                        reason="Сверка автоматических наград ST-9",
                    ),
                    timeout=NETWORK_SECONDS,
                )
        except discord.NotFound:
            pass
        except discord.Forbidden:
            error = "role_permissions"
        except asyncio.TimeoutError:
            error = "discord_timeout"
        except Exception:
            error = "discord_unavailable"
        async with metadata_transaction() as db:
            row = db.get(DiscordRoleRemoval, job["id"])
            if row:
                row.last_error = error
                if error:
                    row.retry_at = utcnow() + timedelta(
                        seconds=min(3600, 30 * 2 ** min(job["attempts"], 7))
                    )
                else:
                    row.removed_at = utcnow()
                    row.retry_at = None
                    row.attempts = 0
        return error is None

    async def alert_one(self):
        from automation_control import SystemAlert, AUTOMATION_ALERT_CHANNEL_ID

        now = utcnow()
        async with metadata_transaction() as db:
            row = (
                db.query(SystemAlert)
                .filter(
                    SystemAlert.resolved_at.is_(None),
                    SystemAlert.sent_at.is_(None),
                    (SystemAlert.retry_at.is_(None)) | (SystemAlert.retry_at <= now),
                )
                .order_by(SystemAlert.id)
                .first()
            )
            if row is None:
                return False
            row.attempts += 1
            row.retry_at = now + timedelta(minutes=5)
            job = {
                "id": row.id,
                "source": row.source,
                "level": row.level,
                "message": row.message,
                "attempts": row.attempts,
                "channel": AUTOMATION_ALERT_CHANNEL_ID,
            }
        error = None
        message_id = None
        try:
            if not str(job["channel"]).isdigit():
                raise RuntimeError("channel_missing")
            payload = {
                "embeds": [
                    {
                        "title": "⚠️ Сбой автоматизации ST-9",
                        "description": job["message"],
                        "color": 0xD7193F if job["level"] == "error" else 0xD88912,
                        "footer": {"text": "Источник: " + job["source"]},
                    }
                ],
                "allowed_mentions": {"parse": []},
            }
            result = await asyncio.wait_for(
                self.bot.http.request(
                    Route(
                        "POST",
                        "/channels/{channel_id}/messages",
                        channel_id=int(job["channel"]),
                    ),
                    json=payload,
                ),
                timeout=NETWORK_SECONDS,
            )
            message_id = str(result.get("id", ""))
            if not message_id:
                raise RuntimeError("invalid_message")
        except discord.Forbidden:
            error = "channel_permissions"
        except discord.NotFound:
            error = "channel_missing"
        except asyncio.TimeoutError:
            error = "discord_timeout"
        except Exception:
            error = "discord_unavailable"
        async with metadata_transaction() as db:
            row = db.get(SystemAlert, job["id"])
            if row:
                row.last_error = error
                if error:
                    row.retry_at = utcnow() + timedelta(
                        seconds=min(3600, 30 * 2 ** min(job["attempts"], 7))
                    )
                else:
                    row.sent_at = utcnow()
                    row.message_id = message_id
                    row.retry_at = None
        return error is None

    async def replay_one(self):
        if not self.guild_id:
            return False
        async with metadata_transaction() as db:
            job = claim_replay(db, utcnow())
        if job is None:
            return False

        async def deliver():
            await self.eligible_member_roles(job["discord_id"])
            return await self.deliver_announcement(job)

        message = None
        error = None
        try:
            message = await asyncio.wait_for(deliver(), timeout=NETWORK_SECONDS)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            error = "discord_timeout"
        except discord.Forbidden:
            error = "channel_permissions"
        except discord.NotFound:
            error = "channel_or_member_missing"
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, RuntimeError)
                and str(exc)
                in (
                    "wrong_guild",
                    "asset_missing",
                    "role_permissions",
                    "role_or_member_missing",
                    "member_ineligible",
                )
                else "discord_unavailable"
            )
        async with metadata_transaction() as db:
            finish_replay(db, job, utcnow(), message, error)
        if error:
            logger.warning("Award replay %s will retry: %s", job["replay_id"], error)
        else:
            logger.info("Repeated award announcement sent: %s", job["replay_id"])
        return message is not None


def register_medals(bot):
    worker = MedalBot(bot)
    bot.add_listener(worker.raw_packet, "on_socket_raw_receive")
    bot.add_listener(worker.ready, "on_ready")
    bot.add_listener(worker.ready, "on_resumed")
    bot.add_listener(worker.disconnect, "on_disconnect")
    bot.st9_medals = worker
    return worker
