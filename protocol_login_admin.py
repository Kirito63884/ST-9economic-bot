"""Protocol code exchange outside the wallet lock. Never persist or log codes."""

import fcntl
import os
import time
from contextlib import contextmanager

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from database import SessionLocal, User
from protocol_clan_source import ClanUnavailable, cookie_path
from protocol_login import login_with_code, refresh_after_login


@contextmanager
def login_slot():
    """One exchange per shared cookie file, including multiple web workers."""
    path = cookie_path().with_suffix(".login.lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(
                409, "Вход уже выполняется. Дождитесь результата."
            ) from None
        previous = os.read(fd, 64).decode("ascii").strip()
        if previous and time.time() - float(previous) < 30:
            raise HTTPException(429, "Подождите 30 секунд перед новой попыткой.")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(time.time()).encode("ascii"))
        yield
    finally:
        os.close(fd)


def make_router(may_login):
    router = APIRouter()

    def require(session):
        with SessionLocal() as db:
            actor = (
                db.get(User, session.get("user_id")) if session.get("user_id") else None
            )
            if not actor:
                raise HTTPException(401, "Войдите в профиль.")
            if actor.deleted_at is not None or not may_login(actor):
                raise HTTPException(
                    403,
                    "Вход Protocol доступен владельцу, техадминам и главным администраторам.",
                )
            if session.get("auth_version", 0) != (actor.auth_version or 0):
                raise HTTPException(403, "Войдите в профиль заново.")

    @router.post("/api/admin/protocol-login")
    async def login(request: Request):
        require(request.session)
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Введите шестизначный код.") from None
        if not isinstance(data, dict) or set(data) != {"code"}:
            raise HTTPException(400, "Ожидается только код из игры.")
        code = data.get("code")
        if (
            not isinstance(code, str)
            or len(code) != 6
            or any(c not in "0123456789" for c in code)
        ):
            raise HTTPException(400, "Нужен шестизначный код из игры.")
        # Cancellation must not close a file lock under a running worker thread.
        # The worker owns the lock and checks the account again before saving.
        from runtime import finish_thread

        session = dict(request.session)
        result = await finish_thread(exchange, code, session)
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"})

    def exchange(code, session):
        try:
            with login_slot():
                require(session)
                document = login_with_code(code, before_save=lambda: require(session))
                del code
                try:
                    stored = refresh_after_login(document)
                except Exception:
                    # The verified session is already saved. Do not tell the user
                    # to spend another one-time code if only statistics failed.
                    return {
                        "ok": True,
                        "message": "Вход выполнен. Сессия сохранена; обновление статистики пока не завершено. Проверьте свежесть источника позже.",
                    }
                return {
                    "ok": True,
                    "message": "Вход выполнен. Сессия сохранена. "
                    + (
                        "Статистика состава обновлена; остальные данные загрузит сборщик."
                        if stored
                        else "Включите источник Protocol в настройках автоматизации для загрузки статистики."
                    ),
                }
        except HTTPException:
            raise
        except ClanUnavailable:
            raise HTTPException(
                400,
                "Protocol не подтвердил вход. Получите новый код через !сайт и повторите попытку. Старая сессия сохранена.",
            ) from None
        except (httpx.HTTPError, OSError, ImportError):
            raise HTTPException(
                503,
                "Не удалось связаться с Protocol или сохранить сессию. Проверьте доступность источника и права службы сайта.",
            ) from None

    return router

