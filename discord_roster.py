"""Publish two durable, role-driven ST-9 roster messages without member caching."""

import asyncio
import json
import logging
import os
import re
import secrets

import discord
from automation_control import AutomationSetting, queue_alert, resolve_alerts, utcnow
from database import SessionLocal
from discord.http import Route
from runtime import metadata_transaction

CHANNEL_ID = 1555118430858780712
SYNC_SECONDS = 600
ROLES = {
    "security": "1457398926406844417",
    "security_head": "1486041548566036540",
    "field": "1517217078979989564",
    "founder": "1486041117408497704",
    "chief_instructor": "1402207362126712864",
    "instructor": "1446836770141507696",
    "subleader": "1458519514470682654",
    "leader": "1447307527846101222",
    "recon": "1457398618192351334",
    "assault": "1506355940935467098",
    "armor": "1467925735674216478",
    "support": "1468548988826292409",
}
FIRST_HEADER = "<:cmd:1555118632860913684> **ВЫСШИЙ КОМАНДНЫЙ СОСТАВ**"
SECOND_HEADER = "<:TANK:1555118756596949062> **БРОНЕТАНКОВАЯ ГРУППА**"
SEPARATOR = "━━━━━━━━━━━━━━━━━━"
UNITS = (
    (
        "recon",
        "➊",
        "<:RECON:1555118700364046366> **РАЗВЕДЫВАТЕЛЬНАЯ ГРУППА**",
        (
            "выявление и уничтожение FOB/HAB;",
            "работа на флангах и в тылу противника, организация засад на логистические маршруты противника, минирование дорог;",
            "при необходимости уничтожение отдельных небольших групп (дрг противника);",
        ),
    ),
    (
        "assault",
        "➋",
        "<:helicopter:1555118664917848095> **ДЕСАНТНО-ШТУРМОВАЯ ГРУППА**",
        (
            "захват  ключевых точек;",
            "штурм вражеских FOB/HAB и укреплённых позиций;",
            "зачистка объектов;",
            "проведение атак на переднем крае;",
            "взаимодействие с бронетанковой группой;",
            "создание давления на флангах противника;",
            "удержание захваченных объектов до подхода основных сил;",
            "проведение контратак;",
            "усиление других подразделений в наиболее сложных участках.",
        ),
    ),
    (
        "armor",
        "➌",
        SECOND_HEADER,
        (
            "уничтожение вражеской техники;",
            "транспортировка пехоты к месту к актуальным позициям;",
            "сопровождение пехоты при наступлении;",
            "разведка с использованием техники;",
            "уничтожение вражеских позиций и укреплений;",
        ),
    ),
    (
        "support",
        "➍",
        "<:SUPPORT:1555118727924551721> **ГРУППА ОБЕСПЕЧЕНИЯ**",
        (
            "логистика и доставка боеприпасов;",
            "строительство и защита FOB/HAB, защита актуальной точки;",
            "восстановление и обслуживание техники при необходимости (ремонтная станция);",
            "огневая поддержка других групп минометами;",
        ),
    ),
)

logger = logging.getLogger("strike_team9.roster_publication")


class RosterError(ValueError):
    pass


def message_length(value):
    # Conservatively account for emoji represented by UTF-16 surrogate pairs.
    return len(value.encode("utf-16-le")) // 2


def clean_name(value):
    name = " ".join(str(value or "").split()).strip("\u200b\ufeffㅤ ")
    # Rebuild the unit prefix once, even when it is already in the Discord nick.
    name = re.sub(r"^[✪➊➋➌➍❹➒①②③④⑨\s]*【ST-9】\s*", "", name, flags=re.IGNORECASE)
    if not name:
        raise RosterError("У участника отсутствует отображаемое имя.")
    return discord.utils.escape_mentions(discord.utils.escape_markdown(name))


def normalize_members(raw):
    members, seen = [], set()
    for item in raw:
        user = item.get("user", {})
        uid = str(user.get("id") or "")
        roles = item.get("roles")
        if not uid.isdigit() or uid in seen or not isinstance(roles, list):
            raise RosterError(
                "Неполный или повторяющийся участник Discord; старый состав сохранён."
            )
        seen.add(uid)
        if user.get("bot"):
            continue
        name = item.get("nick") or user.get("global_name") or user.get("username")
        members.append(
            {"id": uid, "name": clean_name(name), "roles": set(map(str, roles))}
        )
    if not raw:
        raise RosterError("Discord вернул пустой состав; сообщения не изменены.")
    return sorted(members, key=lambda m: (m["name"].casefold(), int(m["id"])))


def render_roster(raw):
    members = normalize_members(raw)

    def has(member, role):
        return ROLES[role] in member["roles"]

    def names(selected, prefix):
        return "\n".join(prefix + "【ST-9】" + m["name"] for m in selected) or "—"

    def position(title, role, prefix="✪"):
        return (
            "__" + title + ":__\n" + names([m for m in members if has(m, role)], prefix)
        )

    command = "\n\n".join(
        (
            FIRST_HEADER,
            position("Основатель и куратор ST-9", "founder"),
            position("Полевой командир ST-9", "field"),
            "<:__:1555218232049410089> **СЛУЖБА БЕЗОПАСНОСТИ**",
            position("Руководитель СБ", "security_head"),
            "__Помощники СБ:__\n"
            + names(
                [
                    m
                    for m in members
                    if has(m, "security") and not has(m, "security_head")
                ],
                "✪",
            ),
        )
    )
    units = []
    for role, prefix, heading, tasks in UNITS:
        unit = [m for m in members if has(m, role)]
        leaders = [m for m in unit if has(m, "leader")]
        subleaders = [m for m in unit if has(m, "subleader") and not has(m, "leader")]
        regulars = [m for m in unit if not has(m, "leader") and not has(m, "subleader")]
        task_heading = "Основные задачи:" if role == "armor" else "__Основные задачи:__"
        units.append(
            "\n\n".join(
                (
                    heading,
                    "__Командир группы:__\n" + names(leaders, prefix),
                    task_heading + "\n" + "\n".join("• " + task for task in tasks),
                    "__Рядовой состав:__\n" + names(regulars, prefix),
                    "__Командиры подгрупп:__\n" + names(subleaders, prefix),
                    SEPARATOR,
                )
            )
        )
    training = "\n\n".join(
        (
            "<:img_1:1555118789920559175> **УЧЕБНАЯ ГРУППА**",
            position("Главный инструктор", "chief_instructor", "➒"),
            "__Основные задачи:__\n• проведение тренировок;\n• подготовка игроков к распределению в основные группы;",
            "__Инструкторы:__\n"
            + names(
                [
                    m
                    for m in members
                    if has(m, "instructor") and not has(m, "chief_instructor")
                ],
                "➒",
            ),
            SEPARATOR,
        )
    )
    first = "ㅤ\n" + command + "\n\n" + units[0] + "\nㅤ\n" + units[1]
    second = "ㅤ\n" + units[2] + "\nㅤ\n" + units[3] + "\nㅤ\n" + training + "\nㅤ"
    if message_length(first) <= 2000 and message_length(second) <= 2000:
        return [first, second]
    # Keep every line intact and exactly two messages. Prefer section boundaries.
    text = first + "\n" + second
    lines = text.split("\n")
    candidates, offset = [], 0
    for line in lines[:-1]:
        offset += len(line) + 1
        left, right = text[: offset - 1], text[offset:]
        if message_length(left) <= 2000 and message_length(right) <= 2000:
            boundary = line == SEPARATOR
            candidates.append((not boundary, abs(len(left) - len(right)), left, right))
    if not candidates:
        raise RosterError(
            f"Состав не помещается в два сообщения по 2000 символов ({message_length(text)} символов). Старые сообщения сохранены; нужно согласовать дополнительное сообщение или сократить текст."
        )
    _, _, left, right = min(candidates)
    return [left, right]


class RosterPublisher:
    def __init__(self, bot):
        self.bot = bot
        self.guild_id = str(os.getenv("REQUIRED_GUILD_ID") or "")
        self.task = None
        self.lock = asyncio.Lock()

    @property
    def state_key(self):
        return f"discord_roster:{CHANNEL_ID}:{self.bot.user.id}"

    def load_state(self):
        with SessionLocal() as db:
            row = db.get(AutomationSetting, self.state_key)
            if row is None:
                return None
            state = json.loads(row.value)
        if (
            not isinstance(state, list)
            or len(state) != 2
            or not all(isinstance(s, dict) for s in state)
        ):
            raise RosterError(
                "Повреждены сохранённые ID сообщений состава; новые сообщения не создаются."
            )
        return state

    async def save_state(self, state):
        async with metadata_transaction() as db:
            row = db.get(AutomationSetting, self.state_key)
            value = json.dumps(state, ensure_ascii=False)
            if row is None:
                db.add(
                    AutomationSetting(
                        key=self.state_key, value=value, updated_at=utcnow()
                    )
                )
            else:
                row.value, row.updated_at = value, utcnow()

    async def own_history(self, channel):
        async def read():
            return [
                m
                async for m in channel.history(limit=None)
                if m.author.id == self.bot.user.id and not m.webhook_id
            ]

        return await asyncio.wait_for(read(), timeout=60)

    async def initialize(self, channel):
        configured = os.getenv("DISCORD_ROSTER_MESSAGE_IDS", "").strip()
        if configured:
            ids = [part.strip() for part in configured.split(",")]
            if len(ids) != 2 or not all(i.isdigit() for i in ids) or ids[0] == ids[1]:
                raise RosterError(
                    "DISCORD_ROSTER_MESSAGE_IDS должен содержать два разных ID через запятую."
                )
            state = [{"id": value} for value in ids]
        else:
            history = await self.own_history(channel)
            state = []
            for header in (FIRST_HEADER, SECOND_HEADER):
                matches = [
                    m for m in history if m.content.lstrip("ㅤ\n ").startswith(header)
                ]
                if len(matches) > 1:
                    raise RosterError(
                        "Найдено несколько сообщений состава бота; задайте два ID в DISCORD_ROSTER_MESSAGE_IDS."
                    )
                state.append({"id": str(matches[0].id)} if matches else {})
            if state[0].get("id") and state[0].get("id") == state[1].get("id"):
                raise RosterError("Нужны два отдельных сообщения состава.")
        await self.save_state(state)
        return state

    async def publish(self, channel, texts):
        async with self.lock:
            state = self.load_state()
            if state is None:
                state = await self.initialize(channel)
            # Validate both old messages before editing either one.
            messages = {}
            for index, slot in enumerate(state):
                if slot.get("id"):
                    message = await channel.fetch_message(int(slot["id"]))
                    if message.author.id != self.bot.user.id or message.webhook_id:
                        raise RosterError(
                            "Бот не может редактировать чужое сообщение состава."
                        )
                    messages[index] = message
            for index, (slot, text) in enumerate(zip(state, texts)):
                if not slot.get("id"):
                    # Persist intent before POST; recover an interrupted send from history.
                    if slot.get("pending"):
                        matches = [
                            m
                            for m in await self.own_history(channel)
                            if m.content == slot["pending"]
                        ]
                        if len(matches) > 1:
                            raise RosterError(
                                "Неоднозначное восстановление сообщения состава; публикация остановлена."
                            )
                        if matches:
                            slot["id"] = str(matches[0].id)
                            messages[index] = matches[0]
                    else:
                        slot.update(pending=text, nonce=secrets.token_hex(12))
                        await self.save_state(state)
                    if not slot.get("id"):
                        response = await self.bot.http.request(
                            Route(
                                "POST",
                                "/channels/{channel_id}/messages",
                                channel_id=CHANNEL_ID,
                            ),
                            json={
                                "content": slot["pending"],
                                "allowed_mentions": {"parse": []},
                                "nonce": slot["nonce"],
                                "enforce_nonce": True,
                            },
                        )
                        slot["id"] = str(response["id"])
                    slot.pop("pending", None)
                    slot.pop("nonce", None)
                    await self.save_state(state)
                message = messages.get(index) or await channel.fetch_message(
                    int(slot["id"])
                )
                if message.content != text:
                    await message.edit(
                        content=text, allowed_mentions=discord.AllowedMentions.none()
                    )

    async def sync_once(self):
        if not self.guild_id.isdigit():
            raise RosterError("Не задан REQUIRED_GUILD_ID.")
        channel = self.bot.get_channel(CHANNEL_ID) or await self.bot.fetch_channel(
            CHANNEL_ID
        )
        if str(getattr(getattr(channel, "guild", None), "id", "")) != self.guild_id:
            raise RosterError("Канал состава находится на другом Discord-сервере.")
        guild = channel.guild
        roles, raw = await asyncio.gather(
            asyncio.wait_for(guild.fetch_roles(), timeout=30),
            self.bot.st9_roster_sync.members(),
        )
        available = {str(role.id) for role in roles}
        missing = [name for name, rid in ROLES.items() if rid not in available]
        if missing:
            raise RosterError(
                "Не найдены роли состава: "
                + ", ".join(missing)
                + ". Старый текст сохранён."
            )
        texts = render_roster(raw)
        await self.publish(channel, texts)
        async with metadata_transaction() as db:
            resolve_alerts(db, "discord_roster_publication")

    async def ready(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.loop(), name="st9-roster-publication")

    async def loop(self):
        while not self.bot.is_closed():
            try:
                if self.bot.is_ready():
                    await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "Roster publication failed; existing messages retained"
                )
                async with metadata_transaction() as db:
                    queue_alert(
                        db,
                        "discord_roster_publication",
                        "discord_roster",
                        "Не удалось обновить сообщения личного состава: "
                        + str(exc)[:350],
                    )
            await asyncio.sleep(SYNC_SECONDS)


def register_roster_publication(bot):
    worker = RosterPublisher(bot)
    bot.add_listener(worker.ready, "on_ready")
    bot.add_listener(worker.ready, "on_resumed")
    bot.st9_roster_publication = worker
    return worker
