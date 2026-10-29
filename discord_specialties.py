"""Persistent, allowlisted Discord specialty buttons for the former ProBot panel."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import unicodedata

import discord

from automation_control import AutomationSetting, utcnow
from database import SessionLocal
from runtime import metadata_transaction

CHANNEL_ID = 1448264624242819093
PANEL_MARKER = "st9:specialties:v1"
SPECIALTIES = (
    ("rifleman", "Стрелок", "🎯", 0),
    ("medic", "Медик", "🩺", 0),
    ("machine_gunner", "Пулемётчик", "🔫", 0),
    ("grenadier", "Гренадер", "💥", 0),
    ("anti_tank", "Гранатомётчик", "🚀", 1),
    ("sapper", "Сапёр", "💣", 2),
    ("marksman", "Марксман", "🔭", 2),
    ("mechanic", "Механик", "⚙️", 2),
    ("pilot", "Пилот", "🚁", 2),
)
# Permissions that a self-service specialty must never confer.
PRIVILEGED = discord.Permissions(
    administrator=True,
    manage_guild=True,
    manage_roles=True,
    manage_nicknames=True,
    manage_expressions=True,
    view_audit_log=True,
    manage_channels=True,
    manage_webhooks=True,
    kick_members=True,
    ban_members=True,
    moderate_members=True,
    manage_messages=True,
    manage_threads=True,
    manage_events=True,
    mention_everyone=True,
    mute_members=True,
    deafen_members=True,
    move_members=True,
).value
logger = logging.getLogger("discord.st9.specialties")


class SpecialtyError(ValueError):
    """A safe, actionable configuration error; never contains credentials."""


def normalized_name(name):
    """Ignore decorative icons, whitespace, case and the е/ё spelling variant."""
    text = unicodedata.normalize("NFKC", name).casefold().replace("ё", "е")
    return "".join(char for char in text if char.isalpha())


def validate_mapping(mapping):
    keys = {key for key, *_ in SPECIALTIES}
    if (
        not isinstance(mapping, dict)
        or set(mapping) != keys
        or any(
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdigit()
            or not 0 < int(value) < 2**64
            for value in mapping.values()
        )
        or len(set(mapping.values())) != len(keys)
    ):
        raise SpecialtyError(
            "Нужны девять разных ID ролей в DISCORD_SPECIALTY_ROLE_IDS."
        )
    return mapping


def resolve_roles(roles, saved=None):
    """Pin unique existing roles by ID; never create roles or guess ambiguous names."""
    configured = os.getenv("DISCORD_SPECIALTY_ROLE_IDS", "").strip()
    if configured:
        try:
            return validate_mapping(json.loads(configured))
        except (json.JSONDecodeError, TypeError) as exc:
            raise SpecialtyError("Неверный JSON в DISCORD_SPECIALTY_ROLE_IDS.") from exc
    if saved is not None:
        return validate_mapping(saved)
    mapping = {}
    for key, label, *_ in SPECIALTIES:
        matches = [
            role
            for role in roles
            if normalized_name(role.name) == normalized_name(label)
        ]
        if len(matches) != 1:
            raise SpecialtyError(
                f"Специальность «{label}»: найдено ролей {len(matches)}. "
                "Укажите точные ID в DISCORD_SPECIALTY_ROLE_IDS. Панель не опубликована."
            )
        mapping[key] = str(matches[0].id)
    return validate_mapping(mapping)


def check_role(role, guild, bot_member):
    if (
        role is None
        or role.id == guild.id
        or str(role.id) == os.getenv("REQUIRED_ROLE_ID")
        or role.managed
        or role.permissions.value & PRIVILEGED
        or not bot_member.guild_permissions.manage_roles
        or role >= bot_member.top_role
    ):
        raise SpecialtyError(
            "Роль недоступна для самостоятельной выдачи. Администратору нужно "
            "проверить её права, право бота «Управлять ролями» и положение роли бота выше специальностей."
        )


def panel_embed():
    embed = discord.Embed(
        title="Учётная специальность",
        description=(
            "Это роль, указывающая ваши знания, навыки и опыт в определённой сфере. "
            "Она отображается в профиле и помогает определить вашу роль в команде. "
            "Фактическое назначение на должность зависит от потребностей и решения командования.\n\n"
            "Нажмите кнопку, чтобы получить специальность. Повторное нажатие снимает роль. "
            "Можно выбрать несколько специальностей."
        ),
        colour=discord.Colour.gold(),
    )
    embed.set_footer(text=PANEL_MARKER)
    return embed


class SpecialtyButton(discord.ui.Button):
    def __init__(self, panel, key, label, emoji, row):
        super().__init__(
            label=label,
            emoji=emoji,
            row=row,
            style=discord.ButtonStyle.primary,
            custom_id=f"st9:specialty:{key}:v1",
        )
        self.panel, self.key = panel, key

    async def callback(self, interaction):
        await self.panel.toggle(interaction, self.key)


class SpecialtyView(discord.ui.View):
    def __init__(self, panel):
        super().__init__(timeout=None)
        for key, label, emoji, row in SPECIALTIES:
            self.add_item(SpecialtyButton(panel, key, label, emoji, row))


class SpecialtyPanel:
    def __init__(self, bot):
        self.bot = bot
        self.guild_id = os.getenv("REQUIRED_GUILD_ID", "")
        self.lock = asyncio.Lock()
        self.task = None

    @property
    def state_key(self):
        return f"specialty_panel:{CHANNEL_ID}:{self.bot.user.id}"

    def load_state(self):
        with SessionLocal() as db:
            row = db.get(AutomationSetting, self.state_key)
            if row is None:
                return None
            state = json.loads(row.value)
        if not isinstance(state, dict) or str(state.get("guild_id")) != self.guild_id:
            raise SpecialtyError("Повреждены настройки панели специальностей.")
        validate_mapping(state.get("roles"))
        return state

    async def save_state(self, state):
        async with metadata_transaction() as db:
            row = db.get(AutomationSetting, self.state_key)
            if row is None:
                row = AutomationSetting(key=self.state_key)
                db.add(row)
            row.value = json.dumps(state)
            row.updated_at = utcnow()

    async def sync_once(self):
        async with self.lock:
            if not self.guild_id.isascii() or not self.guild_id.isdigit():
                raise SpecialtyError("Не задан REQUIRED_GUILD_ID.")
            channel = self.bot.get_channel(CHANNEL_ID) or await self.bot.fetch_channel(
                CHANNEL_ID
            )
            guild = channel.guild
            if str(guild.id) != self.guild_id:
                raise SpecialtyError(
                    "Канал специальностей находится на другом сервере."
                )
            roles = await guild.fetch_roles()
            bot_member = await guild.fetch_member(self.bot.user.id)
            state = self.load_state()
            mapping = resolve_roles(roles, state.get("roles") if state else None)
            by_id = {str(role.id): role for role in roles}
            for role_id in mapping.values():
                check_role(by_id.get(role_id), guild, bot_member)
            permissions = channel.permissions_for(bot_member)
            if not all(
                (
                    permissions.view_channel,
                    permissions.send_messages,
                    permissions.embed_links,
                    permissions.read_message_history,
                )
            ):
                raise SpecialtyError(
                    "Боту нужны права просмотра канала, отправки сообщений, вставки ссылок и чтения истории."
                )
            message = None
            if state and state.get("message_id"):
                try:
                    message = await channel.fetch_message(int(state["message_id"]))
                except discord.NotFound:
                    pass  # A deleted panel is recreated, without touching other messages.
                if message and not self.owned(message):
                    raise SpecialtyError(
                        "Сохранённое сообщение не принадлежит этому боту."
                    )
            if message is None:
                # Recover a successful send interrupted before its ID reached SQLite.
                async def find_panel():
                    return [
                        m
                        async for m in channel.history(limit=None)
                        if self.owned(m)
                        and any(e.footer.text == PANEL_MARKER for e in m.embeds)
                    ]

                found = await asyncio.wait_for(find_panel(), timeout=60)
                if len(found) > 1:
                    raise SpecialtyError(
                        "Найдены несколько панелей этого бота; автоматическая публикация остановлена."
                    )
                message = found[0] if found else None
            state = {
                "guild_id": self.guild_id,
                "roles": mapping,
                "message_id": str(message.id) if message else None,
            }
            await self.save_state(state)
            view = SpecialtyView(self)
            if message:
                await message.edit(
                    embed=panel_embed(),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                message = await channel.send(
                    embed=panel_embed(),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            state["message_id"] = str(message.id)
            await self.save_state(state)
            logger.info(
                "Specialty panel ready: channel=%s message=%s", CHANNEL_ID, message.id
            )

    def owned(self, message):
        return message.author.id == self.bot.user.id and not message.webhook_id

    async def toggle(self, interaction, key):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self.lock:
                state = self.load_state()
                if (
                    key not in {item[0] for item in SPECIALTIES}
                    or not state
                    or str(interaction.guild_id) != self.guild_id
                    or interaction.channel_id != CHANNEL_ID
                    or not interaction.message
                    or str(interaction.message.id) != state.get("message_id")
                    or not self.owned(interaction.message)
                ):
                    raise SpecialtyError(
                        "Эта кнопка не относится к действующей панели специальностей."
                    )
                guild = interaction.guild
                # Cacheless bot: fetch current membership, permissions and hierarchy.
                member = await guild.fetch_member(interaction.user.id)
                bot_member = await guild.fetch_member(self.bot.user.id)
                roles = await guild.fetch_roles()
                role = next(
                    (r for r in roles if str(r.id) == state["roles"][key]), None
                )
                check_role(role, guild, bot_member)
                if (
                    member.bot
                    or not interaction.channel.permissions_for(member).view_channel
                ):
                    raise SpecialtyError(
                        "Выбор специальности недоступен этому участнику."
                    )
                remove = any(r.id == role.id for r in member.roles)
                reason = f"Самостоятельный выбор специальности: Discord {member.id}"
                # Discord's single-role endpoints preserve every unrelated role.
                if remove:
                    await member.remove_roles(role, reason=reason, atomic=True)
                else:
                    await member.add_roles(role, reason=reason, atomic=True)
                label = next(item[1] for item in SPECIALTIES if item[0] == key)
                text = f"Специальность «{label}» {'снята' if remove else 'выдана'}."
                logger.info(
                    "Specialty %s: member=%s role=%s",
                    "removed" if remove else "added",
                    member.id,
                    role.id,
                )
        except SpecialtyError as exc:
            text = str(exc)
        except discord.HTTPException as exc:
            logger.warning(
                "Specialty Discord request failed: status=%s code=%s",
                exc.status,
                exc.code,
            )
            text = "Discord не разрешил изменить роль. Проверьте права бота или попробуйте позже."
        except Exception:
            logger.exception("Specialty selection failed")
            text = "Не удалось изменить специальность. Ошибка записана в журнале бота."
        await interaction.followup.send(
            text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    async def ready(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(
                self.publish_with_retry(), name="st9-specialties"
            )

    async def publish_with_retry(self):
        while not self.bot.is_closed():
            try:
                await self.sync_once()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Specialty panel publication failed; retry in five minutes"
                )
                await asyncio.sleep(300)


def register_specialties(bot):
    panel = SpecialtyPanel(bot)
    bot.st9_specialties = panel
    bot.add_listener(panel.ready, "on_ready")
    bot.add_listener(panel.ready, "on_resumed")
    return panel
