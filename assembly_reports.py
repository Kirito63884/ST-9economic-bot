"""Update one private Discord report per completed Saturday without duplicate sends."""

import asyncio
import json
import logging
from datetime import timedelta

import discord

from automation_control import AutomationSetting, utcnow
from database import SessionLocal
from groups_admin import assembly_summary, current_roster, latest_assembly
from runtime import metadata_transaction
from squad_history_admin import group_catalog

CHANNEL_ID = 1565382350999920680
logger = logging.getLogger("discord.st9.assembly_reports")
LABELS = {
    "present": "Присутствовали",
    "absent_with_report": "Отсутствовали с рапортом",
    "absent_without_report": "Отсутствовали без рапорта",
    "unknown": "Недостаточно данных",
    "exempted": "Освобождены",
}


def report_pages(db, now):
    users, roles = current_roster(db)
    summary = assembly_summary(db, users, now)
    day = summary["date"]
    intro = [
        f"Субботний сбор {day} · 19:00–22:00 МСК",
        "Группы — по последней сохранённой синхронизации Discord. Один участник может входить в несколько групп.",
    ]
    intro.extend(f"{label}: {summary['counts'][key]}" for key, label in LABELS.items())
    if not summary["reports_ready"]:
        intro.append("Рапорты пока не проверены полностью. Сводка предварительная.")
    if not summary["membership_complete"]:
        intro.append(
            "Состав Discord на момент сбора сохранён не полностью. Показаны только участники с подтверждённым присутствием в Discord на ту дату."
        )
    indexed = {u.id: str(u.discord_id) for u in users}
    blocks = ["\n".join(intro)]
    for key, name in group_catalog():
        rows = [
            r
            for r in summary["members"]
            if key
            in roles.get(indexed[r["user_id"]], {"group_ids": ["unknown"]})["group_ids"]
        ]
        if not rows:
            continue
        lines = [f"**{name}**"]
        for status, label in LABELS.items():
            names = [
                discord.utils.escape_markdown(" ".join(r["name"].split()))
                for r in rows
                if r["status"] == status
            ]
            lines.append(f"{label} ({len(names)}):")
            lines.extend("• " + value for value in names)
            if not names:
                lines.append("—")
        blocks.append("\n".join(lines))
    pages = []
    for block in blocks:
        chunk = ""
        for line in block.splitlines():
            # Display names are bounded in the schema; keep every participant.
            if len(chunk) + len(line) + 1 > 3400:
                pages.append(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            pages.append(chunk)
    return day, pages


class AssemblyReportPublisher:
    def __init__(self, bot):
        self.bot, self.task = bot, None

    async def save(self, key, pages):
        async with metadata_transaction() as db:
            row = db.get(AutomationSetting, key)
            if row is None:
                row = AutomationSetting(key=key)
                db.add(row)
            row.value, row.updated_at = json.dumps(pages, ensure_ascii=False), utcnow()

    async def sync_once(self, now=None):
        now = now or utcnow()
        _, _, end = latest_assembly(now)
        if now < end + timedelta(minutes=15):
            return False
        channel = self.bot.get_channel(CHANNEL_ID) or await self.bot.fetch_channel(
            CHANNEL_ID
        )
        guild = channel.guild
        import os

        if str(guild.id) != os.getenv("REQUIRED_GUILD_ID"):
            raise ValueError("Канал отчётов находится на другом Discord-сервере")
        if channel.permissions_for(guild.default_role).view_channel:
            raise ValueError(
                "Канал отчётов открыт для @everyone: публикация остановлена"
            )
        with SessionLocal() as db:
            day, contents = report_pages(db, now)
            key = f"assembly_report:{day}:{CHANNEL_ID}:{self.bot.user.id}"
            row = db.get(AutomationSetting, key)
            saved = json.loads(row.value) if row else []
        while len(saved) < len(contents):
            saved.append({})
        # Keep old page IDs stable if a later corrected summary needs fewer pages.
        contents.extend(
            [
                "Сводка обновлена. Актуальные данные находятся в предыдущих частях отчёта."
            ]
            * (len(saved) - len(contents))
        )
        history = None
        changed = False
        for index, content in enumerate(contents):
            slot = saved[index]
            if slot.get("content") == content and slot.get("id"):
                continue
            footer = f"Сбор {day} · часть {index + 1}"
            embed = discord.Embed(
                title="Итоги субботнего сбора",
                description=content,
                colour=discord.Colour.dark_blue(),
            )
            embed.set_footer(text=footer)
            message = None
            if slot.get("id"):
                try:
                    message = await channel.fetch_message(int(slot["id"]))
                except discord.NotFound:
                    pass
                if message and (
                    message.author.id != self.bot.user.id or message.webhook_id
                ):
                    raise ValueError("Сохранённый отчёт не принадлежит боту")
            if message is None:
                if history is None:

                    async def read():
                        return [m async for m in channel.history(limit=None)]

                    history = await asyncio.wait_for(read(), timeout=60)
                found = [
                    m
                    for m in history
                    if m.author.id == self.bot.user.id
                    and not m.webhook_id
                    and any(e.footer.text == footer for e in m.embeds)
                ]
                if len(found) > 1:
                    raise ValueError("Найдены дубли отчёта; публикация остановлена")
                message = found[0] if found else None
            # If POST succeeds but saving fails, the footer recovers it next time.
            await self.save(key, saved)
            if message:
                await message.edit(
                    embed=embed, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                message = await channel.send(
                    embed=embed, allowed_mentions=discord.AllowedMentions.none()
                )
            changed = True
            slot.update(id=str(message.id), content=content)
            await self.save(key, saved)
        if changed:
            logger.info(
                "Assembly report ready: date=%s channel=%s pages=%s",
                day,
                CHANNEL_ID,
                len(contents),
            )
        return True

    async def ready(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.loop(), name="st9-assembly-reports")

    async def loop(self):
        while not self.bot.is_closed():
            try:
                if self.bot.is_ready():
                    await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Assembly report publication failed; retry in five minutes"
                )
            await asyncio.sleep(300)


def register_assembly_reports(bot):
    publisher = AssemblyReportPublisher(bot)
    bot.add_listener(publisher.ready, "on_ready")
    bot.add_listener(publisher.ready, "on_resumed")
    return publisher
