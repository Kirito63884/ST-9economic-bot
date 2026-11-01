"""Authenticated horse-racing page and wallet-backed pari-mutuel bet API."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from horse_racing import HorseRacingError


async def read_body(request, allowed):
    try:
        data = await request.json()
    except (ValueError, UnicodeError):
        raise HorseRacingError("Ожидается JSON")
    if not isinstance(data, dict) or set(data) != allowed:
        raise HorseRacingError("Некорректные поля запроса")
    return data


def build_router(service, get_user, get_db, templates):
    router = APIRouter()

    @router.get("/games/horse-racing")
    async def page(request: Request, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        return templates.TemplateResponse(
            request=request,
            name="horse_racing.html",
            context={"user": user},
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/api/horse-racing/state")
    async def state(response: Response, user=Depends(get_user), db=Depends(get_db)):
        if not user:
            raise HorseRacingError("Войдите в свой профиль", 401)
        if user.deleted_at:
            raise HorseRacingError("Профиль участника недоступен", 401)
        response.headers["Cache-Control"] = "private, no-store"
        return service.state(user, db)

    @router.post("/api/horse-racing/bet")
    async def bet(
        request: Request,
        response: Response,
        user=Depends(get_user),
        db=Depends(get_db),
    ):
        if not user:
            raise HorseRacingError("Войдите в свой профиль", 401)
        if user.deleted_at:
            raise HorseRacingError("Профиль участника недоступен", 401)
        response.headers["Cache-Control"] = "private, no-store"
        data = await read_body(request, {"race_number", "horse_id", "amount_cents"})
        result = service.place_bet(
            user,
            data.get("horse_id"),
            data.get("amount_cents"),
            db,
            data.get("race_number"),
        )
        return {
            "ok": True,
            "duplicate": result["duplicate"],
            "state": service.state(user, db),
        }

    return router

