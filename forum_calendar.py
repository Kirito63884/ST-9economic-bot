"""Authenticated personal projection of forum events; never exposes full rosters."""

import calendar
from datetime import date, datetime
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

import forum_events as events

MONTHS = (
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


def month_info(value, today):
    try:
        if value is None:
            day = today.replace(day=1)
        else:
            if not isinstance(value, str) or len(value) != 7:
                raise ValueError()
            day = date.fromisoformat(value + "-01")
    except ValueError:
        raise HTTPException(400, "Месяц должен быть в формате ГГГГ-ММ.") from None
    year, month = day.year, day.month
    previous = (
        None
        if (year, month) == (1, 1)
        else f"{year - 1 if month == 1 else year:04}-{12 if month == 1 else month - 1:02}"
    )
    following = (
        None
        if (year, month) == (9999, 12)
        else f"{year + 1 if month == 12 else year:04}-{1 if month == 12 else month + 1:02}"
    )
    weekday, days = calendar.monthrange(year, month)
    return {
        "key": f"{year:04}-{month:02}",
        "label": f"{MONTHS[month]} {year}",
        "first_weekday": weekday,
        "days": days,
        "previous": previous,
        "next": following,
    }


def personal_events(db, discord_id, month=None, mode="month"):
    moment = events.now().astimezone(events.ZONE)
    selected = month_info(month, moment.date())
    items = []
    uid = str(discord_id)
    guild = os.getenv("REQUIRED_GUILD_ID", "")
    for event in events.all_events(db):
        answer = event["responses"].get(uid)
        if not answer or answer["choice"] not in {"yes", "maybe"}:
            continue
        cfg = event["config"]
        start = datetime.fromisoformat(cfg["starts_at"]).astimezone(events.ZONE)
        if mode == "month" and f"{start.year:04}-{start.month:02}" != selected["key"]:
            continue
        if mode == "upcoming" and start < moment:
            continue
        status = (
            "cancelled"
            if cfg["status"] == "cancelled"
            else "finished"
            if event.get("result")
            else "started"
            if start <= moment
            else cfg["status"]
        )
        thread = str(event.get("thread_id") or "")
        # Deliberately construct an allowlisted projection. No recipient IDs,
        # rosters, delivery tokens, creator identity or admin revision data.
        items.append(
            {
                "id": event["id"],
                "number": event["number"],
                "title": cfg["title"],
                "starts_at": cfg["starts_at"],
                "date": start.date().isoformat(),
                "when": start.strftime("%d.%m.%Y %H:%M"),
                "status": status,
                "response": answer["choice"],
                "in_lineup": uid in event["lineup"],
                "lineup_selected": bool(event["lineup"]),
                "teams": cfg["teams"],
                "map": cfg["map"],
                "server": cfg["server"],
                "rules": cfg["rules"],
                "discord_url": f"https://discord.com/channels/{guild}/{thread}"
                if guild.isdigit() and thread.isdigit()
                else None,
                "result": {
                    "summary": event["result"]["summary"],
                    "score": event["result"]["score"],
                }
                if event.get("result")
                else None,
            }
        )
    items.sort(key=lambda item: (item["starts_at"], item["number"]))
    total = len(items)
    if mode == "upcoming":
        items = items[:50]
    return {
        "events": items,
        "total": total,
        "month": selected,
        "mode": mode,
        "today": moment.date().isoformat(),
        "timezone": "Europe/Moscow",
    }


def make_router(get_user, get_db):
    router = APIRouter()

    @router.get("/calendar")
    async def page(request: Request, user=Depends(get_user)):
        if not user:
            return RedirectResponse(
                "/login",
                status_code=303,
                headers={"Cache-Control": "private, no-store"},
            )
        from main import templates

        return templates.TemplateResponse(
            request=request,
            name="calendar.html",
            context={"user": user},
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/api/calendar")
    async def listing(
        response: Response,
        month: str | None = None,
        mode: Literal["month", "upcoming"] = "month",
        user=Depends(get_user),
        db=Depends(get_db),
    ):
        if not user:
            raise HTTPException(401, "Войдите на сайт, чтобы открыть свой календарь.")
        response.headers["Cache-Control"] = "private, no-store"
        return personal_events(db, user.discord_id, month, mode)

    return router
