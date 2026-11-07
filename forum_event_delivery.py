"""Durable reminder batches and editable results in an event's existing thread."""

import asyncio
from datetime import datetime, timedelta
import hashlib
import logging
import secrets

import discord

from database import SessionLocal
from runtime import metadata_transaction
import forum_events as events

STAGES = (
    (259200, "за 3 дня"),
    (86400, "за 1 день"),
    (43200, "за 12 часов"),
    (10800, "за 3 часа"),
    (3600, "за 1 час"),
    (1800, "за 30 минут"),
    (600, "за 10 минут"),
    (0, "начало события"),
)
GRACE_SECONDS = 300
BATCH_SIZE = 50
logger = logging.getLogger(__name__)


def generation(event):
    return hashlib.sha256(event["config"]["starts_at"].encode()).hexdigest()[:16]


def reminder_due(event, seconds, moment):
    if (
        not event.get("thread_id")
        or event["config"]["status"] == "cancelled"
        or event.get("result")
    ):
        return False
    due = datetime.fromisoformat(event["config"]["starts_at"]) - timedelta(
        seconds=seconds
    )
    return (
        datetime.fromisoformat(event["created_at"])
        <= due
        <= moment
        < due + timedelta(seconds=GRACE_SECONDS)
    )


def has_work(event):
    if event.get("result"):
        return bool(
            event.get("thread_id")
            and event.get("result_delivery", {}).get("published_revision", 0)
            < event["result"]["revision"]
        )
    if event["config"]["status"] == "cancelled":
        return False
    return any(
        reminder_due(event, seconds, events.now()) for seconds, _ in STAGES
    ) or any(
        plan["generation"] == generation(event)
        and any(part["status"] == "pending" for part in plan["parts"])
        for plan in event.get("reminder_plans", {}).values()
    )


def safe(value):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def result_embed(event):
    result = event["result"]
    embed = discord.Embed(
        title="🏁 Итоги: " + event["config"]["title"],
        description=safe(result["summary"]),
        color=0x5865F2,
    )
    if result["score"]:
        embed.add_field(
            name="Счёт / результат", value=safe(result["score"]), inline=False
        )
    if result["images"]:
        embed.set_image(url=result["images"][0])
        for index, url in enumerate(result["images"][1:], 2):
            embed.add_field(name=f"Скриншот {index}", value=safe(url), inline=False)
    embed.set_footer(text=f"Итоги события #{event['number']}")
    return embed


class Announcements:
    def __init__(self, worker):
        self.worker = worker

    async def thread(self, event):
        thread = await self.worker.bot.fetch_channel(int(event["thread_id"]))
        if (
            thread.parent_id != events.FORUM_ID
            or thread.guild.id != self.worker.guild_id
            or thread.owner_id != self.worker.bot.user.id
        ):
            raise events.EventError("Неверная тема события.")
        if thread.archived:
            await thread.edit(archived=False)
        return thread

    async def find_message(self, thread, footer, since):
        async for message in thread.history(
            limit=None,
            after=datetime.fromisoformat(since) - timedelta(minutes=1),
            oldest_first=False,
        ):
            if message.author.id == self.worker.bot.user.id and any(
                embed.footer.text == footer for embed in message.embeds
            ):
                return message
        return None

    async def plan(self, eid):
        async with metadata_transaction() as db:
            event = events.get(db, eid)
            plans = event.setdefault("reminder_plans", {})
            changed = False
            for seconds, label in STAGES:
                key = f"{generation(event)}:{seconds}"
                if key in plans or not reminder_due(event, seconds, events.now()):
                    continue
                uids = sorted(
                    uid
                    for uid, row in event["responses"].items()
                    if row["choice"] == "yes"
                )
                plans[key] = {
                    "generation": generation(event),
                    "starts_at": event["config"]["starts_at"],
                    "seconds": seconds,
                    "label": label,
                    "parts": [
                        {"uids": uids[i : i + BATCH_SIZE], "status": "pending"}
                        for i in range(0, len(uids), BATCH_SIZE)
                    ],
                }
                changed = True
            if changed:
                events.save(db, event)
            return [
                (key, index)
                for key, plan in plans.items()
                if plan["generation"] == generation(event)
                for index, part in enumerate(plan["parts"])
                if part["status"] == "pending"
            ]

    async def claim_reminder(self, eid, key, index):
        async with metadata_transaction() as db:
            event = events.get(db, eid)
            plan = event.get("reminder_plans", {}).get(key)
            if (
                not plan
                or plan["generation"] != generation(event)
                or event["config"]["status"] == "cancelled"
                or event.get("result")
            ):
                return None
            part = plan["parts"][index]
            if (
                part["status"] != "pending"
                or part.get("until", 0) > events.now().timestamp()
            ):
                return None
            recovering = bool(part.get("first_at"))
            token = secrets.token_hex(16)
            part.update(
                token=token,
                until=events.now().timestamp() + 180,
                first_at=part.get("first_at", events.now().isoformat()),
            )
            events.save(db, event)
            return event, token, recovering

    async def reminder(self, eid, key, index):
        claim = await self.claim_reminder(eid, key, index)
        if not claim:
            return
        event, token, recovering = claim
        plan = event["reminder_plans"][key]
        part = plan["parts"][index]
        start = datetime.fromisoformat(event["config"]["starts_at"]).astimezone(
            events.ZONE
        )
        footer = f"Событие #{event['number']} · {plan['label']} · {start:%d.%m.%Y %H:%M:%S} МСК · {index + 1}/{len(plan['parts'])}"
        try:
            async with asyncio.timeout(60):
                thread = await self.thread(event)
                message = (
                    await self.find_message(thread, footer, part["first_at"])
                    if recovering
                    else None
                )
                with SessionLocal() as db:
                    latest = events.get(db, eid)
                uids = [
                    uid
                    for uid in part["uids"]
                    if latest["responses"].get(uid, {}).get("choice") == "yes"
                ]
                if (
                    message is None
                    and generation(latest) == plan["generation"]
                    and uids
                    and reminder_due(latest, plan["seconds"], events.now())
                ):
                    embed = discord.Embed(
                        title="⏰ " + latest["config"]["title"],
                        description=f"Напоминание: **{plan['label']}**.\nНачало: **{start:%d.%m.%Y %H:%M} МСК**.\nСервер: {safe(latest['config']['server']) or 'уточните у организатора'}",
                        color=0xFEE75C,
                    )
                    embed.set_footer(text=footer)
                    message = await thread.send(
                        " ".join(f"<@{uid}>" for uid in uids),
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(
                            users=[discord.Object(id=int(uid)) for uid in uids],
                            everyone=False,
                            roles=False,
                            replied_user=False,
                        ),
                    )
            async with metadata_transaction() as db:
                latest = events.get(db, eid)
                current = latest["reminder_plans"][key]["parts"][index]
                if current.get("token") == token:
                    current.update(
                        status="sent" if message else "skipped",
                        message_id=str(message.id) if message else None,
                        error="",
                    )
                    events.save(db, latest)
        except Exception:
            logger.exception("Event reminder pending; reconcile before retry")
            async with metadata_transaction() as db:
                latest = events.get(db, eid)
                current = latest["reminder_plans"][key]["parts"][index]
                if current.get("token") == token:
                    current["error"] = (
                        "Не удалось подтвердить напоминание; повторная проверка через 3 минуты."
                    )
                    events.save(db, latest)

    async def result(self, eid):
        async with metadata_transaction() as db:
            event = events.get(db, eid)
            if not event.get("thread_id") or not event.get("result"):
                return
            state = event.setdefault("result_delivery", {})
            if (
                state.get("published_revision", 0) >= event["result"]["revision"]
                or state.get("until", 0) > events.now().timestamp()
            ):
                return
            recovering = bool(state.get("first_at"))
            token = secrets.token_hex(16)
            state.update(
                token=token,
                until=events.now().timestamp() + 180,
                first_at=state.get("first_at", events.now().isoformat()),
            )
            events.save(db, event)
        try:
            async with asyncio.timeout(60):
                thread = await self.thread(event)
                embed = result_embed(event)
                message = (
                    await thread.fetch_message(int(state["message_id"]))
                    if state.get("message_id")
                    else await self.find_message(
                        thread, embed.footer.text, state["first_at"]
                    )
                    if recovering
                    else None
                )
                if message:
                    await message.edit(
                        embed=embed, allowed_mentions=discord.AllowedMentions.none()
                    )
                else:
                    message = await thread.send(
                        embed=embed, allowed_mentions=discord.AllowedMentions.none()
                    )
            async with metadata_transaction() as db:
                latest = events.get(db, eid)
                state = latest["result_delivery"]
                if state.get("token") == token:
                    state.update(
                        message_id=str(message.id),
                        published_revision=event["result"]["revision"],
                        until=0,
                        error="",
                    )
                    events.save(db, latest)
        except Exception:
            logger.exception("Event result pending; reconcile before retry")
            async with metadata_transaction() as db:
                latest = events.get(db, eid)
                if latest["result_delivery"].get("token") == token:
                    latest["result_delivery"]["error"] = (
                        "Итоги ожидают подтверждения Discord; проверка через 3 минуты."
                    )
                    events.save(db, latest)

    async def tick(self, eid):
        with SessionLocal() as db:
            event = events.get(db, eid)
        if event.get("result"):
            await self.result(eid)
            return
        if event["config"]["status"] == "cancelled":
            return
        for key, index in await self.plan(eid):
            await self.reminder(eid, key, index)
