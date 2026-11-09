"""Persistent rolling join detector and reversible guest-channel slowmode."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import secrets
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from automation_control import AutomationSetting
from roster_settings import write
from runtime import metadata_transaction

GUEST_CHANNEL = 1513602609976709130
ALERT_CHANNEL = 1553858549879148635
ALERT_ROLE = 1457398926406844417
WINDOW = 600
THRESHOLD = 10
DEFAULT_SETTINGS = {"day_minutes": 15, "night_minutes": 60}
ZONE = ZoneInfo("Europe/Moscow")
QUIET = 7200
LEASE = 120
logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).timestamp()


def load(db, key):
    row = db.get(AutomationSetting, key)
    return (
        json.loads(row.value)
        if row
        else {"joins": {}, "incident": None, "ignore_before": 0}
    )


def settings(db, key):
    row = db.get(AutomationSetting, key)
    return {**DEFAULT_SETTINGS, **(json.loads(row.value) if row else {})}


def scheduled_delay(config, moment=None):
    hour = datetime.fromtimestamp(now() if moment is None else moment, ZONE).hour
    return config["day_minutes" if 6 <= hour < 23 else "night_minutes"] * 60


def delay_text(seconds):
    return f"{seconds // 60} мин." if seconds % 60 == 0 else f"{seconds} сек."


def settings_text(config):
    return (
        f"Задержка при рейде, МСК:\n06:00–23:00 — {config['day_minutes']} мин."
        f"\n23:00–06:00 — {config['night_minutes']} мин."
        "\nВне режима защиты расписание не применяется."
    )


class RaidGuard:
    def __init__(self, bot):
        self.bot = bot
        self.guild_id = int(os.getenv("REQUIRED_GUILD_ID") or 0)
        self.key = f"discord_raid_guard:{self.guild_id}"
        self.settings_key = f"discord_raid_guard_config:{self.guild_id}"
        self.task = None
        self.wake = asyncio.Event()

    async def joined(self, member):
        if not self.guild_id or member.guild.id != self.guild_id or member.bot:
            return
        moment = now()
        joined = member.joined_at.timestamp() if member.joined_at else moment
        # Ignore old gateway replays and malformed future timestamps.
        if joined < moment - WINDOW or joined > moment + 30:
            return
        joined = min(joined, moment)
        async with metadata_transaction() as db:
            state = load(db, self.key)
            if joined <= state["ignore_before"]:
                return
            joins = {
                uid: stamp
                for uid, stamp in state["joins"].items()
                if stamp >= moment - WINDOW
            }
            uid = str(member.id)
            if joined <= joins.get(uid, 0):
                return
            joins[uid] = joined
            state["joins"] = dict(
                sorted(joins.items(), key=lambda item: item[1])[-1000:]
            )
            incident = state["incident"]
            if incident:
                incident["last_join"] = max(joined, incident["last_join"])
            elif len(joins) >= THRESHOLD:
                state["incident"] = {
                    "id": secrets.token_hex(12),
                    "started": moment,
                    "last_join": max(joins.values()),
                    "count": len(joins),
                    "previous": None,
                    "target": scheduled_delay(settings(db, self.settings_key), moment),
                    "pending_target": None,
                    "enabled": False,
                    "alert_attempted": False,
                    "message_id": None,
                    "notified_state": None,
                    "release_requested": False,
                    "lease": {},
                    "retry_at": 0,
                    "error": "",
                }
            write(db, self.key, state)
        self.wake.set()

    async def claim(self):
        async with metadata_transaction() as db:
            state = load(db, self.key)
            incident = state["incident"]
            if (
                not incident
                or incident["lease"].get("until", 0) > now()
                or incident["retry_at"] > now()
            ):
                return None
            token = secrets.token_hex(16)
            incident["lease"] = {"token": token, "until": now() + LEASE}
            write(db, self.key, state)
            return incident["id"], token

    async def owned(self, iid, token, **changes):
        async with metadata_transaction() as db:
            state = load(db, self.key)
            incident = state["incident"]
            if (
                not incident
                or incident["id"] != iid
                or incident["lease"].get("token") != token
            ):
                raise RuntimeError("Raid guard lease changed")
            if changes:
                incident.update(changes)
                write(db, self.key, state)
            return incident

    async def close(self, iid, token, *, force=False):
        async with metadata_transaction() as db:
            state = load(db, self.key)
            incident = state["incident"]
            if (
                not incident
                or incident["id"] != iid
                or incident["lease"].get("token") != token
            ):
                return False
            if not force and not self.due(incident):
                return False
            state.update(incident=None, joins={}, ignore_before=now())
            write(db, self.key, state)
            return True

    @staticmethod
    def due(incident):
        return incident["release_requested"] or now() >= incident["last_join"] + QUIET

    async def channel(self, channel_id):
        channel = await self.bot.fetch_channel(channel_id)
        if (
            not isinstance(channel, discord.TextChannel)
            or channel.guild.id != self.guild_id
        ):
            raise ValueError(
                "Raid guard channel must be a text channel in the configured guild"
            )
        return channel

    async def configure(self, day_minutes=None, night_minutes=None, actor_id=None):
        values = {"day_minutes": day_minutes, "night_minutes": night_minutes}
        if any(
            value is not None and (type(value) is not int or not 1 <= value <= 360)
            for value in values.values()
        ):
            raise ValueError("Задержка должна быть целым числом от 1 до 360 минут.")
        async with metadata_transaction() as db:
            config = settings(db, self.settings_key)
            if any(value is not None for value in values.values()):
                config.update(
                    {key: value for key, value in values.items() if value is not None}
                )
                write(db, self.settings_key, config, actor_id)
        self.wake.set()
        return config

    async def slowmode(self, iid, token):
        channel = await self.channel(GUEST_CHANNEL)
        incident = await self.owned(iid, token)
        if self.due(incident):
            previous = incident["previous"]
            expected = {incident["target"], incident.get("pending_target")}
            # Restore either a confirmed setting or an edit whose reply was lost.
            if (
                previous is not None
                and channel.slowmode_delay in expected
                and channel.slowmode_delay != previous
            ):
                await channel.edit(
                    slowmode_delay=previous, reason="ST-9: снятие защиты от рейда"
                )
                channel.slowmode_delay = previous
            if await self.close(iid, token):
                return False
            incident = await self.owned(
                iid,
                token,
                enabled=False,
                target=channel.slowmode_delay,
                pending_target=None,
            )
        if incident["previous"] is None:
            incident = await self.owned(
                iid,
                token,
                previous=channel.slowmode_delay,
                target=channel.slowmode_delay,
            )
        pending = incident.get("pending_target")
        expected = {incident["target"], pending}
        if not incident["enabled"]:
            expected.add(
                incident["previous"]
            )  # Also accepts an older 5-minute installation's unfinished edit.
        if channel.slowmode_delay not in expected:
            await self.close(iid, token, force=True)
            return False
        if pending is not None and channel.slowmode_delay == pending:
            incident = await self.owned(
                iid, token, target=pending, pending_target=None, enabled=True
            )
        async with metadata_transaction() as db:
            wanted = scheduled_delay(settings(db, self.settings_key))
        target = max(incident["previous"], wanted)
        if channel.slowmode_delay != target:
            # Save intent before I/O so a restart or lost response is recoverable.
            await self.owned(iid, token, pending_target=target)
            await channel.edit(
                slowmode_delay=target,
                reason="ST-9: задержка при рейде по расписанию МСК",
            )
        await self.owned(iid, token, target=target, pending_target=None, enabled=True)
        return True

    @staticmethod
    def notification_state(incident):
        return [incident["enabled"], incident["target"], incident.get("pending_target")]

    def embed(self, incident):
        status = (
            f"Защита включена: подтверждённая задержка между сообщениями — {delay_text(incident['target'])}"
            if incident["enabled"]
            else "Не удалось включить задержку. Проверьте права бота и включите защиту вручную."
        )
        if incident.get("pending_target") is not None:
            status += f"\nОжидается применение новой задержки: {delay_text(incident['pending_target'])}"
        result = discord.Embed(
            title="⚠️ Возможный рейд в гостевом канале",
            description=f"За последние 10 минут на сервер вошли {incident['count']} человек. Возможен потенциальный рейд в <#{GUEST_CHANNEL}>.\n\n{status}\nАвтоснятие — через 2 часа после последнего нового входа. Модератор может снять защиту командой /raid_off.",
            color=0xED4245,
            timestamp=datetime.fromtimestamp(incident["started"], timezone.utc),
        )
        result.set_footer(text=f"Инцидент {incident['id']}")
        return result

    async def notify(self, iid, token):
        incident = await self.owned(iid, token)
        if incident["message_id"] and incident.get(
            "notified_state"
        ) == self.notification_state(incident):
            return
        channel = await self.channel(ALERT_CHANNEL)
        message = None
        if incident["message_id"]:
            message = await channel.fetch_message(int(incident["message_id"]))
        elif incident["alert_attempted"]:
            # An accepted send can lose its response. Reconcile before another ping.
            after = datetime.fromtimestamp(
                incident["started"], timezone.utc
            ) - timedelta(minutes=1)
            async for old in channel.history(limit=None, after=after):
                if old.author.id == self.bot.user.id and any(
                    embed.footer.text == f"Инцидент {iid}" for embed in old.embeds
                ):
                    message = old
                    break
        if message is None:
            incident = await self.owned(iid, token, alert_attempted=True)
            message = await channel.send(
                f"<@&{ALERT_ROLE}>",
                embed=self.embed(incident),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=[discord.Object(id=ALERT_ROLE)],
                    replied_user=False,
                ),
            )
        else:
            await message.edit(
                embed=self.embed(incident),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await self.owned(
            iid,
            token,
            message_id=str(message.id),
            notified_state=self.notification_state(incident),
        )

    async def tick(self):
        claim = await self.claim()
        if not claim:
            return
        iid, token = claim
        failed = False
        try:
            async with asyncio.timeout(90):
                try:
                    if not await self.slowmode(iid, token):
                        return
                except Exception:
                    failed = True
                    logger.exception("Raid guard could not change guest slowmode")
                # Alert moderators even when the channel edit fails.
                incident = await self.owned(iid, token)
                if not self.due(incident):
                    await self.notify(iid, token)
        except Exception:
            failed = True
            logger.exception("Raid guard delivery failed")
        finally:
            async with metadata_transaction() as db:
                state = load(db, self.key)
                incident = state["incident"]
                if (
                    incident
                    and incident["id"] == iid
                    and incident["lease"].get("token") == token
                ):
                    incident.update(
                        lease={},
                        retry_at=now() + 60 if failed else 0,
                        error="Не удалось применить защиту или отправить предупреждение. Проверьте журнал и права бота."
                        if failed
                        else "",
                    )
                    write(db, self.key, state)

    async def request_release(self):
        async with metadata_transaction() as db:
            state = load(db, self.key)
            incident = state["incident"]
            if not incident:
                return False
            incident.update(release_requested=True, retry_at=0)
            write(db, self.key, state)
        self.wake.set()
        return True

    async def ready(self):
        if not self.guild_id:
            logger.error("Raid guard requires REQUIRED_GUILD_ID")
            return
        if not self.bot.intents.members:
            logger.error(
                "Raid guard cannot count joins: enable Server Members Intent in Discord Developer Portal"
            )
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.loop(), name="st9-raid-guard")

    async def loop(self):
        while not self.bot.is_closed():
            self.wake.clear()
            try:
                if self.bot.is_ready():
                    await self.tick()
            except Exception:
                logger.exception("Raid guard loop failed")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=10)
            except TimeoutError:
                pass


def register_raid_guard(bot):
    worker = RaidGuard(bot)
    bot.st9_raid_guard = worker
    bot.add_listener(worker.joined, "on_member_join")
    bot.add_listener(worker.ready, "on_ready")
    bot.add_listener(worker.ready, "on_resumed")

    async def allowed(interaction):
        if (
            interaction.guild_id != worker.guild_id
            or not worker.guild_id
            or not interaction.permissions.manage_channels
        ):
            await interaction.response.send_message(
                "Команда доступна модераторам с правом «Управлять каналами» на сервере ST-9.",
                ephemeral=True,
            )
            return False
        return True

    @bot.tree.command(
        name="raid_off", description="Снять защиту гостевого канала от рейда"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def raid_off(interaction: discord.Interaction):
        if not await allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        requested = await worker.request_release()
        await interaction.followup.send(
            "Запрошено снятие защиты. Бот восстановит прежнюю задержку; состояние — /raid_status."
            if requested
            else "Автоматическая защита сейчас не активна.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(
        name="raid_delay", description="Настроить дневную и ночную задержку при рейде"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(
        day_minutes="Минуты с 06:00 до 23:00 МСК (1–360)",
        night_minutes="Минуты с 23:00 до 06:00 МСК (1–360)",
    )
    async def raid_delay(
        interaction: discord.Interaction,
        day_minutes: app_commands.Range[int, 1, 360] | None = None,
        night_minutes: app_commands.Range[int, 1, 360] | None = None,
    ):
        if not await allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            config = await worker.configure(
                day_minutes, night_minutes, str(interaction.user.id)
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        text = settings_text(config)
        if day_minutes is not None or night_minutes is not None:
            text += "\nНастройки сохранены. Активная защита обновится при следующей проверке."
        await interaction.followup.send(
            text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @bot.tree.command(
        name="raid_status", description="Состояние защиты гостевого канала"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def raid_status(interaction: discord.Interaction):
        if not await allowed(interaction):
            return
        async with metadata_transaction() as db:
            incident = load(db, worker.key)["incident"]
            config = settings(db, worker.settings_key)
        if not incident:
            text = "Автоматическая защита не активна."
        else:
            text = (
                "Снятие запрошено."
                if incident["release_requested"]
                else "Защита включена."
                if incident["enabled"]
                else "Защита ожидает включения."
            )
            deadline = datetime.fromtimestamp(
                incident["last_join"] + QUIET, ZoneInfo("Europe/Moscow")
            )
            text += (
                f"\nАвтоснятие после последнего входа: {deadline:%d.%m.%Y %H:%M} МСК."
            )
            if incident["error"]:
                text += "\n" + incident["error"]
        if incident and incident["enabled"]:
            text += f"\nПодтверждённая задержка: {delay_text(incident['target'])}"
        text += "\n\n" + settings_text(config)
        if not bot.intents.members:
            text += "\nПодсчёт входов недоступен: включите Server Members Intent."
        await interaction.response.send_message(
            text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    return worker
