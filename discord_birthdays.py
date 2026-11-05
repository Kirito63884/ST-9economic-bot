"""Private birthday registry and restart-safe Discord announcements (Moscow)."""

import asyncio
import calendar
from datetime import date, datetime, timedelta
import json
import logging
import os
import re
import secrets
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from automation_control import AutomationSetting
from database import SessionLocal
from roster_settings import write
from runtime import metadata_transaction

CHANNEL_ID = 1546906175751192706
REGISTRY_KEY = "discord_birthdays:registry"
ZONE = ZoneInfo("Europe/Moscow")
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
logger = logging.getLogger(__name__)


def now_moscow():
    return datetime.now(ZONE)


def parse_date(value, today=None, *, allow_unusual_year=False):
    today = today or now_moscow().date()
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        raise ValueError("Используй дату в формате ДД.ММ.ГГГГ.")
    try:
        birthday = datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        raise ValueError("Такой даты не существует.") from None
    if not allow_unusual_year and birthday > today:
        raise ValueError("Дата рождения не может быть в будущем.")
    return birthday


def validate_registry(data):
    if not isinstance(data, dict) or len(data) > 10000:
        raise ValueError("Ожидается объект с записями дней рождения.")
    result = {}
    for uid, record in data.items():
        if not isinstance(uid, str) or not re.fullmatch(r"[1-9]\d{14,19}", uid):
            raise ValueError("Некорректный Discord ID.")
        if not isinstance(record, dict) or set(record) != {"name", "date"}:
            raise ValueError("У записи должны быть поля name и date.")
        if not isinstance(record["name"], str) or not 1 <= len(record["name"]) <= 200:
            raise ValueError("Некорректное имя.")
        parse_date(record["date"], allow_unusual_year=True)
        result[uid] = dict(record)
    return result


def read(db, key, default=None):
    row = db.get(AutomationSetting, key)
    return json.loads(row.value) if row else default


def anniversary(birthday, year):
    # February 29 is celebrated on March 1 in non-leap years.
    if birthday.month == 2 and birthday.day == 29 and not calendar.isleap(year):
        return date(year, 3, 1)
    return birthday.replace(year=year)


def age_text(age):
    word = (
        "лет"
        if 11 <= age % 100 <= 14
        else "год"
        if age % 10 == 1
        else "года"
        if 2 <= age % 10 <= 4
        else "лет"
    )
    return f"{age} {word}"


def greeting(member, birthday, moment):
    age = age_text(moment.year - birthday.year)
    embed = discord.Embed(
        title="🎉🎂 С ДНЁМ РОЖДЕНИЯ! 🎂🎉",
        description=(
            f"**{member.mention}** сегодня празднует свой день рождения!\n\n"
            f"📅 **Сегодня исполняется {age}** 🎈\n"
            f"📆 Родился(ась): {birthday.day} {MONTHS[birthday.month]} {birthday.year} года\n\n"
            "🥳 Желаем тебе:\n• Счастья и здоровья\n• Удачи и успехов\n"
            "• Исполнения всех желаний\n• Много подарков и улыбок!\n\n"
            "Счастья, здоровья и успехов! 🍀"
        ),
        color=0xFF69B4,
        timestamp=moment,
    )
    embed.set_thumbnail(url=str(member.display_avatar.url))
    embed.set_footer(text=f"🎊 С наилучшими пожеланиями от бота! • {age}")
    return embed


class BirthdayWorker:
    def __init__(self, bot):
        self.bot = bot
        self.guild_id = int(os.getenv("REQUIRED_GUILD_ID") or 0)
        self.task = None

    async def claim(self, uid, record, moment):
        key = f"discord_birthdays:sent:{moment.year}:{uid}"
        async with metadata_transaction() as db:
            # An edit/removal while Discord was queried must cancel the old plan.
            if read(db, REGISTRY_KEY, {}).get(uid) != record:
                return None
            prior = read(db, key, {})
            if (
                prior.get("status") == "sent"
                or prior.get("until", 0) > moment.timestamp()
            ):
                return None
            token = secrets.token_hex(16)
            started = prior.get("started", moment.isoformat())
            write(
                db,
                key,
                {
                    "status": "pending",
                    "until": moment.timestamp() + 600,
                    "token": token,
                    "started": started,
                },
            )
            return key, token, bool(prior), started

    async def deliver(self, channel, member, uid, record, moment):
        claim = await self.claim(uid, record, moment)
        if not claim:
            return
        key, token, recovering, started = claim
        message = None
        embed = greeting(member, parse_date(record["date"]), moment)
        # After an uncertain send, reconcile history before sending again.
        # Permission/network errors leave the claim pending; never guess success.
        async with asyncio.timeout(90):
            if recovering:
                since = datetime.fromisoformat(started) - timedelta(minutes=1)
                async for old in channel.history(
                    limit=None, after=since, oldest_first=False
                ):
                    if old.author.id == self.bot.user.id and any(
                        previous.title == embed.title
                        and f"**{member.mention}** сегодня празднует"
                        in (previous.description or "")
                        for previous in old.embeds
                    ):
                        message = old
                        break
            if message is None:
                message = await channel.send(
                    f"🎉 ВНИМАНИЕ! {member.mention}",
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(
                        users=[member], roles=False, everyone=False, replied_user=False
                    ),
                )
        async with metadata_transaction() as db:
            current = read(db, key, {})
            if current.get("token") != token:
                raise RuntimeError("Birthday delivery claim changed")
            write(
                db,
                key,
                {
                    "status": "sent",
                    "message_id": str(message.id),
                    "at": moment.isoformat(),
                },
            )
        # Optional decoration never causes another announcement.
        try:
            await message.add_reaction("👍")
            if not getattr(message, "thread", None):
                await message.create_thread(
                    name="🎉 🎂 С ДНЁМ РОЖДЕНИЯ! 🎂 🎉", auto_archive_duration=1440
                )
        except (discord.HTTPException, asyncio.TimeoutError):
            logger.warning("Birthday sent; reaction/thread unavailable")

    async def tick(self, moment=None):
        moment = (moment or now_moscow()).astimezone(ZONE)
        if not self.guild_id or moment.hour < 7:
            return
        with SessionLocal() as db:
            records = read(db, REGISTRY_KEY, {})
        due = []
        for uid, record in records.items():
            try:
                if (
                    anniversary(parse_date(record["date"]), moment.year)
                    == moment.date()
                ):
                    due.append((uid, record))
            except ValueError:
                logger.warning("Invalid birthday record; retained for correction")
        if not due:
            return
        channel = self.bot.get_channel(CHANNEL_ID) or await self.bot.fetch_channel(
            CHANNEL_ID
        )
        if channel.guild.id != self.guild_id:
            raise RuntimeError("Birthday channel belongs to another guild")
        for uid, record in due:
            try:
                with SessionLocal() as db:
                    if (
                        read(db, f"discord_birthdays:sent:{moment.year}:{uid}", {}).get(
                            "status"
                        )
                        == "sent"
                    ):
                        continue
                member = await channel.guild.fetch_member(int(uid))
                if not member.bot:
                    await self.deliver(channel, member, uid, record, moment)
            except discord.NotFound:
                # Leaving the guild does not remove or alter the birthday.
                continue
            except Exception:
                logger.exception("Birthday delivery failed; registry retained")

    async def ready(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.loop(), name="st9-birthdays")

    async def loop(self):
        while not self.bot.is_closed():
            try:
                if self.bot.is_ready():
                    await self.tick()
            except Exception:
                logger.exception("Birthday check failed")
            await asyncio.sleep(60)


def may_edit(interaction, target):
    return (
        target.id == interaction.user.id
        or interaction.user.guild_permissions.manage_guild
    )


MONTH_NAMES = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)
PAGE_SIZE = 10


def upcoming_date(birthday, today):
    result = anniversary(birthday, today.year)
    return result if result >= today else anniversary(birthday, today.year + 1)


def calendar_rows(records):
    rows = []
    for uid, record in records.items():
        try:
            birthday = parse_date(record["date"])
        except (ValueError, KeyError):
            continue
        name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(record["name"].replace("\n", " ")[:60])
        )
        rows.append((birthday, uid, name))
    return rows


def next_birthdays_embed(records, today):
    rows = sorted(
        (
            (upcoming_date(birthday, today), uid, name)
            for birthday, uid, name in calendar_rows(records)
        ),
        key=lambda row: (row[0], row[1]),
    )[:PAGE_SIZE]
    lines = []
    for day, uid, name in rows:
        remaining = (day - today).days
        when = "сегодня" if remaining == 0 else f"через {remaining} дн."
        lines.append(f"**{day:%d.%m}** — {name} · {when}")
    embed = discord.Embed(
        title="🎂 Ближайшие дни рождения",
        description="\n".join(lines) or "Пока нет сохранённых дат.",
        color=0xFF69B4,
    )
    embed.set_footer(
        text="До 10 ближайших дат • Москва • Включая сохранённые записи вышедших"
    )
    return embed


class BirthdayCalendar(discord.ui.View):
    def __init__(self, owner_id, guild_id, month):
        super().__init__(timeout=180)
        self.owner_id, self.guild_id, self.month = owner_id, guild_id, month
        self.page = 0

    async def interaction_check(self, interaction):
        if (
            interaction.user.id == self.owner_id
            and interaction.guild_id == self.guild_id
        ):
            return True
        await interaction.response.send_message(
            "Открой собственный список командой /list_bd.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    def render(self):
        with SessionLocal() as db:
            records = read(db, REGISTRY_KEY, {})
        rows = sorted(
            (row for row in calendar_rows(records) if row[0].month == self.month),
            key=lambda row: (row[0].day, row[1]),
        )
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(max(0, self.page), pages - 1)
        selected = rows[self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE]
        embed = discord.Embed(
            title=f"🎂 {MONTH_NAMES[self.month]}",
            description="\n".join(
                f"**{birthday:%d.%m}** — {name}" for birthday, uid, name in selected
            )
            or "В этом месяце нет сохранённых дат.",
            color=0xFF69B4,
        )
        embed.set_footer(
            text=f"Страница {self.page + 1}/{pages} • Записей: {len(rows)} • Включая вышедших • Кнопки: 3 минуты"
        )
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page == pages - 1
        return embed

    async def move(self, interaction, *, month_delta=0, page_delta=0):
        if not await self.interaction_check(interaction):
            return
        self.month = (self.month - 1 + month_delta) % 12 + 1
        self.page = 0 if month_delta else self.page + page_delta
        await interaction.response.edit_message(
            embed=self.render(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="◀ Месяц", style=discord.ButtonStyle.secondary, row=0)
    async def previous_month(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.move(interaction, month_delta=-1)

    @discord.ui.button(label="Месяц ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_month(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.move(interaction, month_delta=1)

    @discord.ui.button(label="◀ Страница", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.move(interaction, page_delta=-1)

    @discord.ui.button(label="Страница ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.move(interaction, page_delta=1)


def register_birthdays(bot):
    worker = BirthdayWorker(bot)
    bot.st9_birthdays = worker
    bot.add_listener(worker.ready, "on_ready")
    bot.add_listener(worker.ready, "on_resumed")

    async def answer(interaction, text):
        sender = (
            interaction.followup.send
            if interaction.response.is_done()
            else interaction.response.send_message
        )
        await sender(
            text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    async def guard(interaction):
        if not worker.guild_id or interaction.guild_id != worker.guild_id:
            await answer(interaction, "Команда доступна только на сервере ST-9.")
            return False
        return True

    @bot.tree.command(name="add_bd", description="Добавить или изменить день рождения")
    @app_commands.guild_only()
    @app_commands.describe(
        date="Дата ДД.ММ.ГГГГ",
        user="Оставь пустым для себя; чужую дату меняет управляющий сервером",
    )
    async def add_bd(
        interaction: discord.Interaction, date: str, user: discord.Member | None = None
    ):
        if not await guard(interaction):
            return
        target = user or interaction.user
        if target.bot or not may_edit(interaction, target):
            await answer(
                interaction,
                "Можно менять свою дату; чужую — с правом «Управлять сервером».",
            )
            return
        try:
            parse_date(date)
        except ValueError as exc:
            await answer(interaction, str(exc))
            return
        await interaction.response.defer(ephemeral=True)
        async with metadata_transaction() as db:
            records = read(db, REGISTRY_KEY, {})
            records[str(target.id)] = {"name": target.display_name, "date": date}
            write(db, REGISTRY_KEY, records, str(interaction.user.id))
        await answer(
            interaction, f"🎂 Дата {date} сохранена. Поздравление — в 07:00 по Москве."
        )

    @bot.tree.command(name="remove_bd", description="Удалить день рождения вручную")
    @app_commands.guild_only()
    async def remove_bd(
        interaction: discord.Interaction, user: discord.Member | None = None
    ):
        if not await guard(interaction):
            return
        target = user or interaction.user
        if not may_edit(interaction, target):
            await answer(interaction, "Чужую дату удаляет только управляющий сервером.")
            return
        await interaction.response.defer(ephemeral=True)
        async with metadata_transaction() as db:
            records = read(db, REGISTRY_KEY, {})
            found = records.pop(str(target.id), None)
            write(db, REGISTRY_KEY, records, str(interaction.user.id))
        await answer(interaction, "Дата удалена." if found else "Дата не найдена.")

    @bot.tree.command(name="my_bd", description="Посмотреть свой день рождения")
    @app_commands.guild_only()
    async def my_bd(interaction: discord.Interaction):
        if not await guard(interaction):
            return
        with SessionLocal() as db:
            record = read(db, REGISTRY_KEY, {}).get(str(interaction.user.id))
        await answer(
            interaction,
            f"🎂 Твой день рождения: {record['date']}"
            if record
            else "Дата ещё не добавлена: /add_bd",
        )

    @bot.tree.command(
        name="next_bd", description="Ближайшие дни рождения и сколько дней осталось"
    )
    @app_commands.guild_only()
    async def next_bd(interaction: discord.Interaction):
        if not await guard(interaction):
            return
        with SessionLocal() as db:
            records = read(db, REGISTRY_KEY, {})
        await interaction.response.send_message(
            embed=next_birthdays_embed(records, now_moscow().date()),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(name="list_bd", description="Дни рождения по месяцам")
    @app_commands.guild_only()
    @app_commands.describe(month="Номер месяца от 1 до 12; по умолчанию текущий")
    async def list_bd(
        interaction: discord.Interaction,
        month: app_commands.Range[int, 1, 12] | None = None,
    ):
        if not await guard(interaction):
            return
        view = BirthdayCalendar(
            interaction.user.id, worker.guild_id, month or now_moscow().month
        )
        await interaction.response.send_message(
            embed=view.render(),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(
        name="preview_bd", description="Предпросмотр поздравления без публикации"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(user="Именинник; по умолчанию ты")
    async def preview_bd(
        interaction: discord.Interaction, user: discord.Member | None = None
    ):
        if not await guard(interaction):
            return
        if not interaction.user.guild_permissions.manage_guild:
            await answer(
                interaction, "Предпросмотр доступен с правом «Управлять сервером»."
            )
            return
        target = user or interaction.user
        with SessionLocal() as db:
            record = read(db, REGISTRY_KEY, {}).get(str(target.id))
        if not record:
            await answer(
                interaction, "Дата не найдена. Сначала добавь её через /add_bd."
            )
            return
        try:
            birthday = parse_date(record["date"])
        except ValueError as exc:
            await answer(interaction, str(exc))
            return
        day = upcoming_date(birthday, now_moscow().date())
        moment = datetime(day.year, day.month, day.day, 7, tzinfo=ZONE)
        await interaction.response.send_message(
            f"Предпросмотр на {day:%d.%m.%Y}, 07:00 МСК. Видно только тебе.\n🎉 ВНИМАНИЕ! {target.mention}",
            embed=greeting(target, birthday, moment),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(name="bd_help", description="Помощь по дням рождения")
    @app_commands.guild_only()
    async def bd_help(interaction: discord.Interaction):
        if await guard(interaction):
            await answer(
                interaction,
                "/add_bd — сохранить дату\n/remove_bd — удалить вручную\n/my_bd — своя дата\n/next_bd — ближайшие даты\n/list_bd — календарь по месяцам\n/preview_bd — предпросмотр для управляющего сервером\nПоздравления в 07:00 МСК. Даты вышедших участников сохраняются.",
            )

    return worker
