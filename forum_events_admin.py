"""Administrative event API; Discord delivery runs separately in the bot."""

import os
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from admin_hub import log_action
from forum_events import EventError, all_events, create, update


def make_router(get_user, get_db, may_manage):
    router = APIRouter()

    def check(user):
        if not user or not user.is_admin or not may_manage(user):
            raise HTTPException(
                403, "События доступны старшим и главным администраторам."
            )

    @router.get("/api/admin/forum-events")
    async def listing(response: Response, user=Depends(get_user), db=Depends(get_db)):
        check(user)
        response.headers["Cache-Control"] = "private, no-store"
        return {
            "events": all_events(db),
            "guild_id": os.getenv("REQUIRED_GUILD_ID", ""),
        }

    @router.post("/api/admin/forum-events")
    async def save_event(request: Request, user=Depends(get_user), db=Depends(get_db)):
        check(user)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {
                "id",
                "revision",
                "config",
                "lineup",
            }:
                raise EventError("Некорректные поля запроса.")
            if body["revision"] is None:
                if body["lineup"] != []:
                    raise EventError("У нового события пока нет участников.")
                event = create(db, body["id"], user.discord_id, body["config"])
            else:
                event = update(
                    db, body["id"], body["revision"], body["config"], body["lineup"]
                )
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None
        log_action(db, user, "forum_event_save", "Сохранено событие " + event["id"])
        return {"event": event}

    router.forum_events_registered = True
    return router
