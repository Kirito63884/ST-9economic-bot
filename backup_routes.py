"""Read-only admin authentication; backup work never holds the game transaction."""

import asyncio
import os
import secrets
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from database import SessionLocal, User
import backup_db


def make_backup_router(is_super_admin, is_owner_admin=None):
    router = APIRouter(prefix="/api/admin/backups")

    def permissions(request: Request):
        with SessionLocal() as db:
            user = (
                db.query(User)
                .filter(
                    User.id == request.session.get("user_id"), User.deleted_at.is_(None)
                )
                .first()
            )
            if (
                not user
                or not user.is_admin
                or not (user.is_senior_admin or is_super_admin(user))
            ):
                raise HTTPException(403, "Недостаточно прав.")
            return bool(is_super_admin(user))

    @router.get("")
    async def status(response: Response, owner: bool = Depends(permissions)):
        response.headers["Cache-Control"] = "private, no-store"
        try:
            result = await asyncio.to_thread(backup_db.backup_status)
            result["can_manage"] = owner
            return result
        except (backup_db.BackupError, OSError) as exc:
            raise HTTPException(503, backup_db._safe_error(exc)) from exc

    @router.post("/create", status_code=202)
    async def create(owner: bool = Depends(permissions)):
        if not owner:
            raise HTTPException(
                403, "Создавать копии может только главный администратор."
            )
        try:
            job = await asyncio.to_thread(backup_db.request_backup)
            return {
                "request_id": job,
                "message": "Копия создаётся. Можно продолжать пользоваться сайтом.",
            }
        except backup_db.BackupBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except (backup_db.BackupError, OSError) as exc:
            raise HTTPException(503, backup_db._safe_error(exc)) from exc

    @router.get("/download/{name}")
    async def download(name: str, request: Request, owner: bool = Depends(permissions)):
        if not owner:
            raise HTTPException(
                403, "Скачивать базу может только главный администратор."
            )
        with SessionLocal() as db:
            actor = db.get(User, request.session.get("user_id"))
            raw_allowed = is_owner_admin is None or bool(is_owner_admin(actor))
        temporary = None

        def open_copy():
            # Open before releasing the lock: rotation cannot race the open.
            with backup_db.backup_lock():
                backup_db._manifest(name)
                source = backup_db._file_path(name)
                if raw_allowed:
                    return source.open("rb"), None
                # Delegated chiefs receive a usable DB copy without authentication
                # secrets that could be used to impersonate the protected owner.
                descriptor, filename = tempfile.mkstemp(
                    prefix="st9-backup-download-", suffix=".db"
                )
                os.close(descriptor)
                path = Path(filename)
                try:
                    with (
                        closing(
                            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
                        ) as original,
                        closing(sqlite3.connect(path)) as exported,
                    ):
                        original.backup(exported)
                        exported.execute(
                            "UPDATE app_runtime SET secret_key=?",
                            (secrets.token_hex(32),),
                        )
                        exported.execute("DELETE FROM login_links")
                        exported.commit()
                    return path.open("rb"), path
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise

        try:
            handle, temporary = await asyncio.to_thread(open_copy)
        except backup_db.BackupBusy as exc:
            raise HTTPException(
                409, "Копия создаётся. Скачивание будет доступно после завершения."
            ) from exc
        except (backup_db.BackupError, OSError) as exc:
            raise HTTPException(404, "Копия не найдена или недоступна.") from exc

        def cleanup():
            handle.close()
            if temporary:
                temporary.unlink(missing_ok=True)

        def chunks():
            try:
                while block := handle.read(1024 * 1024):
                    yield block
            finally:
                cleanup()

        return StreamingResponse(
            chunks(),
            media_type="application/vnd.sqlite3",
            background=BackgroundTask(cleanup),
            headers={
                "Content-Disposition": f'attachment; filename="{name if raw_allowed else "auth-reset-" + name}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-ST9-Authentication-Reset": "0" if raw_allowed else "1",
            },
        )

    return router
