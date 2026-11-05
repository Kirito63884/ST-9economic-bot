"""Forum delivery, persistent RSVP buttons and staff event commands."""

import asyncio
from datetime import datetime
import hashlib
import logging
import os
import secrets

import discord
from discord import app_commands

from database import SessionLocal, User
from runtime import metadata_transaction
from admin_hub import log_action
import forum_events as events

logger = logging.getLogger(__name__)
CHOICES = {"yes": "Буду", "maybe": "Возможно", "no": "Не буду"}


def safe(value):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def render(event):
    cfg = event["config"]
    moment = datetime.fromisoformat(cfg["starts_at"]).astimezone(events.ZONE)
    start = int(moment.timestamp())
    opened = events.is_open(event)
    status = (
        "Отменено"
        if cfg["status"] == "cancelled"
        else "Регистрация открыта"
        if opened
        else "Регистрация закрыта"
    )
    yes = sum(row["choice"] == "yes" for row in event["responses"].values())
    embed = discord.Embed(
        title=cfg["title"],
        description=f"**{status}**\nНачало: {moment:%d.%m.%Y %H:%M} МСК · <t:{start}:R>\n\nКоманды: {safe(cfg['teams']) or '—'}\nМест: {cfg['capacity']}\nКарта: {safe(cfg['map']) or '—'}\nСервер: {safe(cfg['server']) or '—'}\nПравила: {safe(cfg['rules']) or '—'}\n\n**Регистрация: {yes}/{cfg['capacity']}**\nСвободно: {max(0, cfg['capacity'] - yes)}",
        color=0x3BA55C if opened else 0x747F8D,
    )
    for choice, label in CHOICES.items():
        rows = [
            safe(r["name"][:35])
            for r in event["responses"].values()
            if r["choice"] == choice
        ]
        shown = rows[:10]
        value = "\n".join(f"{i + 1:02}. {name}" for i, name in enumerate(shown)) or "—"
        if len(rows) > 10:
            value += f"\n… ещё {len(rows) - 10}. Полный список — «Участники»."
        embed.add_field(name=f"{label} · {len(rows)}", value=value, inline=False)
    names = [safe(event["responses"][uid]["name"][:35]) for uid in event["lineup"]]
    value = (
        "\n".join(f"{i + 1:02}. {name}" for i, name in enumerate(names[:10]))
        or "Организатор пока не выбрал состав."
    )
    if len(names) > 10:
        value += f"\n… ещё {len(names) - 10}. Полный список — «Участники»."
    embed.add_field(
        name=f"Игровой состав · {len(names)}/{cfg['capacity']}",
        value=value,
        inline=False,
    )
    if cfg["image_url"]:
        embed.set_image(url=cfg["image_url"])
    embed.set_footer(text=f"Событие #{event['number']} • Europe/Moscow")
    return embed


async def reply(interaction, message, **kwargs):
    send = (
        interaction.followup.send
        if interaction.response.is_done()
        else interaction.response.send_message
    )
    await send(
        message,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
        **kwargs,
    )


def staff(db, discord_id):
    user = (
        db.query(User)
        .filter(User.discord_id == str(discord_id), User.deleted_at.is_(None))
        .first()
    )
    allowed = (
        user
        and user.is_admin
        and (
            user.is_senior_admin
            or user.is_chief_admin
            or user.is_tech_admin
            or str(discord_id) == os.getenv("ADMIN_DISCORD_ID")
        )
    )
    if not allowed:
        raise events.EventError(
            "События создают и редактируют старшие/главные администраторы, техадмин и владелец сайта."
        )
    return user


async def member_check(worker, interaction):
    if (
        not worker.guild_id
        or interaction.guild_id != worker.guild_id
        or not interaction.guild
    ):
        raise events.EventError("Доступно только на сервере ST-9.")
    member = await interaction.guild.fetch_member(interaction.user.id)
    required = os.getenv("REQUIRED_ROLE_ID")
    if member.bot or (
        required
        and str(member.id) != os.getenv("ADMIN_DISCORD_ID")
        and required not in {str(role.id) for role in member.roles}
    ):
        raise events.EventError("Для участия нужна роль участника клана.")
    return member


class Participants(discord.ui.View):
    def __init__(self, event, owner):
        super().__init__(timeout=180)
        self.event, self.owner, self.page = event, owner, 0

    def content(self):
        rows = list(self.event["responses"].items())
        pages = max(1, (len(rows) + 14) // 15)
        self.page = max(0, min(self.page, pages - 1))
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page == pages - 1
        lines = [
            f"{CHOICES[row['choice']]} · {safe(row['name'][:60])}"
            + (" · ✅ состав" if uid in self.event["lineup"] else "")
            for uid, row in rows[self.page * 15 : (self.page + 1) * 15]
        ]
        return f"**Участники #{self.event['number']} · {self.page + 1}/{pages}**\n" + (
            "\n".join(lines) or "Пока никого нет."
        )

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner:
            return True
        await reply(interaction, "Откройте свой список кнопкой «Участники».")
        return False

    @discord.ui.button(label="Назад")
    async def previous(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.page -= 1
        await interaction.response.edit_message(
            content=self.content(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Далее")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await interaction.response.edit_message(
            content=self.content(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class EventView(discord.ui.View):
    def __init__(self, worker, eid, closed=False):
        super().__init__(timeout=None)
        self.worker, self.eid = worker, eid
        for choice, label, style in (
            ("yes", "Записаться", discord.ButtonStyle.success),
            ("maybe", "Возможно", discord.ButtonStyle.secondary),
            ("no", "Не буду", discord.ButtonStyle.secondary),
            ("members", "Участники", discord.ButtonStyle.primary),
        ):
            button = discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"st9:event:{eid}:{choice}",
                disabled=closed and choice != "members",
            )

            async def callback(interaction, value=choice):
                await self.action(interaction, value)

            button.callback = callback
            self.add_item(button)

    async def action(self, interaction, choice):
        await interaction.response.defer(ephemeral=True)
        try:
            member = await member_check(self.worker, interaction)
            with SessionLocal() as db:
                event = events.get(db, self.eid)
            if str(interaction.channel_id) != str(event["thread_id"]) or str(
                interaction.message.id
            ) != str(event["thread_id"]):
                raise events.EventError("Кнопка не относится к теме события.")
            if choice == "members":
                view = Participants(event, member.id)
                await reply(interaction, view.content(), view=view)
                return
            async with metadata_transaction() as db:
                events.respond(
                    db,
                    self.eid,
                    member.id,
                    member.display_name,
                    choice,
                    interaction.channel_id,
                )
            self.worker.wake.set()
            await reply(
                interaction,
                "Ответ сохранён: " + CHOICES[choice] + ". Состав выбирает организатор.",
            )
        except (events.EventError, discord.HTTPException) as exc:
            await reply(
                interaction,
                str(exc)
                if isinstance(exc, events.EventError)
                else "Не удалось проверить доступ в Discord. Попробуйте снова.",
            )


class ForumWorker:
    def __init__(self, bot):
        self.bot = bot
        self.guild_id = int(os.getenv("REQUIRED_GUILD_ID") or 0)
        self.task = None
        self.wake = asyncio.Event()

    def restore_views(self):
        with SessionLocal() as db:
            saved = events.all_events(db)
        for event in saved:
            if event["thread_id"]:
                self.bot.add_view(
                    EventView(self, event["id"], not events.is_open(event)),
                    message_id=int(event["thread_id"]),
                )

    async def claim(self, eid):
        async with metadata_transaction() as db:
            event = events.get(db, eid)
            if event["config"]["status"] == "open" and not events.is_open(event):
                event["config"]["status"] = "closed"
                event["revision"] += 1
            if event["published_revision"] == event["revision"]:
                return None
            if event["delivery"].get("until", 0) > events.now().timestamp():
                return None
            token = secrets.token_hex(16)
            event["delivery"] = {
                "token": token,
                "until": events.now().timestamp() + 300,
                "attempted": True,
            }
            events.save(db, event)
            return event, token

    async def find_thread(self, forum, event):
        suffix = f" • #{event['number']}"
        active = await forum.guild.active_threads()
        for thread in active:
            if (
                thread.parent_id == events.FORUM_ID
                and thread.owner_id == self.bot.user.id
                and thread.name.endswith(suffix)
            ):
                return thread
        async for thread in forum.archived_threads(limit=None):
            if thread.owner_id == self.bot.user.id and thread.name.endswith(suffix):
                return thread
        return None

    async def publish(self, eid):
        claim = await self.claim(eid)
        if not claim:
            return
        event, token = claim
        try:
            async with asyncio.timeout(90):
                forum = self.bot.get_channel(
                    events.FORUM_ID
                ) or await self.bot.fetch_channel(events.FORUM_ID)
                if (
                    not isinstance(forum, discord.ForumChannel)
                    or forum.guild.id != self.guild_id
                ):
                    raise events.EventError(
                        "Указанный канал должен быть форумом на сервере ST-9."
                    )
                thread = (
                    await self.bot.fetch_channel(int(event["thread_id"]))
                    if event["thread_id"]
                    else await self.find_thread(forum, event)
                )
                view = EventView(self, eid, not events.is_open(event))
                name = f"{event['config']['title']} • #{event['number']}"
                if thread is None:
                    tags = (
                        list(forum.available_tags[:1])
                        if forum.flags.require_tag
                        else []
                    )
                    result = await forum.create_thread(
                        name=name,
                        embed=render(event),
                        view=view,
                        applied_tags=tags,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    thread = result.thread
                else:
                    if (
                        thread.parent_id != events.FORUM_ID
                        or thread.owner_id != self.bot.user.id
                    ):
                        raise events.EventError(
                            "Тема не принадлежит боту или форуму событий."
                        )
                    if thread.archived:
                        await thread.edit(archived=False)
                    message = await thread.fetch_message(thread.id)
                    await message.edit(
                        embed=render(event),
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    if thread.name != name:
                        await thread.edit(name=name)
                async with metadata_transaction() as db:
                    latest = events.get(db, eid)
                    if latest["delivery"].get("token") != token:
                        raise events.EventError("Изменилось подтверждение публикации.")
                    latest.update(
                        thread_id=str(thread.id),
                        published_revision=event["revision"],
                        delivery={},
                        error="",
                    )
                    events.save(db, latest)
                self.bot.add_view(view, message_id=thread.id)
        except Exception as exc:
            logger.exception("Forum event publication failed")
            async with metadata_transaction() as db:
                latest = events.get(db, eid)
                if latest["delivery"].get("token") == token:
                    latest["error"] = (
                        str(exc)[:250]
                        if isinstance(exc, events.EventError)
                        else "Discord недоступен или не хватает прав. Повтор через 5 минут."
                    )
                    events.save(db, latest)

    async def ready(self):
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.loop(), name="st9-forum-events")

    async def loop(self):
        while not self.bot.is_closed():
            self.wake.clear()
            try:
                if self.guild_id and self.bot.is_ready():
                    with SessionLocal() as db:
                        ids = [
                            e["id"]
                            for e in events.all_events(db)
                            if e["published_revision"] != e["revision"]
                            or (
                                e["config"]["status"] == "open"
                                and not events.is_open(e)
                            )
                        ]
                    for eid in ids:
                        await self.publish(eid)
            except Exception:
                logger.exception("Forum events loop failed")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=30)
            except TimeoutError:
                pass


def register_forum_events(bot):
    worker = ForumWorker(bot)
    bot.st9_forum_events = worker
    bot.add_listener(worker.ready, "on_ready")
    bot.add_listener(worker.ready, "on_resumed")

    async def authorize(interaction):
        await member_check(worker, interaction)
        with SessionLocal() as db:
            staff(db, interaction.user.id)

    @bot.tree.command(name="event_create", description="Создать событие в форуме ST-9")
    @app_commands.guild_only()
    @app_commands.describe(
        title="Название",
        starts="Начало: ДД.ММ.ГГГГ ЧЧ:ММ, Москва",
        capacity="Мест: 1–100",
        teams="Команды",
        map="Карта",
        server="Сервер",
        rules="Правила",
        image="HTTPS-ссылка на изображение",
    )
    async def event_create(
        interaction: discord.Interaction,
        title: str,
        starts: str,
        capacity: app_commands.Range[int, 1, 100],
        teams: str = "",
        map: str = "",
        server: str = "",
        rules: str = "",
        image: str = "",
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            await authorize(interaction)
            start = (
                datetime.strptime(starts, "%d.%m.%Y %H:%M")
                .replace(tzinfo=events.ZONE)
                .isoformat()
            )
            data = dict(
                title=title,
                starts_at=start,
                capacity=capacity,
                teams=teams,
                map=map,
                server=server,
                rules=rules,
                image_url=image,
                status="open",
            )
            eid = hashlib.sha256(str(interaction.id).encode()).hexdigest()[:32]
            async with metadata_transaction() as db:
                actor = staff(db, interaction.user.id)
                event = events.create(db, eid, actor.discord_id, data)
                log_action(db, actor, "forum_event_create", "Создано событие " + eid)
            worker.wake.set()
            await reply(
                interaction,
                f"Событие #{event['number']} сохранено. Тема появится в форуме после публикации ботом. Редактирование и выбор состава — на сайте → Админка → События.",
            )
        except (ValueError, discord.HTTPException) as exc:
            await reply(
                interaction,
                str(exc)
                if isinstance(exc, events.EventError)
                else "Не удалось создать событие. Проверьте доступ и дату ДД.ММ.ГГГГ ЧЧ:ММ.",
            )

    @bot.tree.command(
        name="event_edit", description="Изменить событие по номеру из его карточки"
    )
    @app_commands.guild_only()
    @app_commands.describe(
        number="Номер события",
        starts="ДД.ММ.ГГГГ ЧЧ:ММ, Москва",
        image="HTTPS-ссылка на изображение",
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Открыто", value="open"),
            app_commands.Choice(name="Закрыто", value="closed"),
            app_commands.Choice(name="Отменено", value="cancelled"),
        ]
    )
    async def event_edit(
        interaction: discord.Interaction,
        number: int,
        title: str | None = None,
        starts: str | None = None,
        capacity: app_commands.Range[int, 1, 100] | None = None,
        teams: str | None = None,
        map: str | None = None,
        server: str | None = None,
        rules: str | None = None,
        image: str | None = None,
        status: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            await authorize(interaction)
            changes = {
                key: value
                for key, value in dict(
                    title=title,
                    capacity=capacity,
                    teams=teams,
                    map=map,
                    server=server,
                    rules=rules,
                    image_url=image,
                    status=status,
                ).items()
                if value is not None
            }
            if starts is not None:
                changes["starts_at"] = (
                    datetime.strptime(starts, "%d.%m.%Y %H:%M")
                    .replace(tzinfo=events.ZONE)
                    .isoformat()
                )
            async with metadata_transaction() as db:
                actor = staff(db, interaction.user.id)
                event = next(
                    (
                        item
                        for item in events.all_events(db)
                        if item["number"] == number
                    ),
                    None,
                )
                if event is None:
                    raise events.EventError("Событие с таким номером не найдено.")
                events.update(
                    db,
                    event["id"],
                    event["revision"],
                    {**event["config"], **changes},
                    event["lineup"],
                )
                log_action(
                    db, actor, "forum_event_edit", "Изменено событие " + event["id"]
                )
            worker.wake.set()
            await reply(
                interaction,
                f"Событие #{number} сохранено. Бот обновит ту же тему форума.",
            )
        except (ValueError, discord.HTTPException) as exc:
            await reply(
                interaction,
                str(exc)
                if isinstance(exc, events.EventError)
                else "Проверьте доступ и формат даты ДД.ММ.ГГГГ ЧЧ:ММ.",
            )

    return worker
