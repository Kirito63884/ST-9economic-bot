"""Shared award presentation for the website and Discord announcements."""

import json
import unicodedata
import uuid
import warnings
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO

from database import Base, SessionLocal, User
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Column, DateTime, LargeBinary, String
from sqlalchemy.orm import deferred
from starlette.concurrency import run_in_threadpool

MAX_UPLOAD = 900_000


class AwardDesign(Base):
    __tablename__ = "award_designs"
    code = Column(String(60), primary_key=True)
    name = Column(String(100), nullable=True)
    image = deferred(Column(LargeBinary, nullable=True))
    digest = Column(String(64), nullable=True)
    revision = Column(String(32), nullable=False)
    updated_at = Column(DateTime, nullable=False)
    updated_by = Column(String(20), nullable=False)


def normalized_image(raw):
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not raw or len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "Изображение слишком большое. Выберите другой файл.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as source:
                if source.format not in ("PNG", "JPEG", "WEBP"):
                    raise ValueError()
                if max(source.size) > 4096 or source.width * source.height > 8_000_000:
                    raise ValueError()
                image = ImageOps.exif_transpose(source).convert("RGBA")
                image.thumbnail((1600, 1600))
                output = BytesIO()
                image.save(output, format="WEBP", quality=90, method=4)
                result = output.getvalue()
                if len(result) > 2_000_000:
                    raise ValueError()
                return result
    except (
        ValueError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise HTTPException(
            422, "Нужна корректная картинка PNG, JPEG или WebP размером до 4096×4096."
        ) from exc


def custom_image_url(db, code):
    row = db.get(AwardDesign, code)
    return f"/award-images/{code}/{row.digest}.webp" if row and row.digest else None


def attachment_for(db, code):
    """Always read current content; no process-local or bot cache."""
    row = db.get(AwardDesign, code)
    if row and row.digest and row.image:
        return BytesIO(row.image), f"{code}-{row.digest[:16]}.webp", "image/webp"
    from pathlib import Path

    from medals import image_path

    url = image_path(code)
    if not url:
        return None
    path = Path(__file__).resolve().parent / "medal-assets" / Path(url).name
    if not path.is_file():
        raise RuntimeError("asset_missing")
    return (
        path,
        path.name,
        {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[path.suffix],
    )


def require_editor(db, request):
    import main

    actor = (
        db.get(User, request.session.get("user_id"))
        if request.session.get("user_id")
        else None
    )
    if not actor:
        raise HTTPException(401)
    if actor.deleted_at or not actor.is_admin or not main.is_super_admin(actor):
        raise HTTPException(403, "Редактировать награды могут главные администраторы.")
    return actor


def design_data(db):
    from award_role_names import named_catalog
    from distinctions_catalog import ROLE_IDS
    from medals import CATALOG, MANUAL_CATALOG, image_path

    result = []
    for code, item in named_catalog(db, {**CATALOG, **MANUAL_CATALOG}).items():
        row = db.get(AwardDesign, code)
        result.append(
            {
                "code": code,
                "name": item[0],
                "custom_name": row.name if row else None,
                "image": image_path(code, db),
                "custom_image": bool(row and row.digest),
                "symbol": item[3],
                "category": "distinction" if code in ROLE_IDS else "medal",
                "revision": row.revision if row else None,
            }
        )
    return {"awards": result}


def make_router():
    router = APIRouter()

    @router.get("/api/admin/award-design")
    def read_designs(request: Request):
        with SessionLocal() as db:
            require_editor(db, request)
            return JSONResponse(
                design_data(db), headers={"Cache-Control": "private, no-store"}
            )

    @router.post("/api/admin/award-design/{code}")
    async def save_design(code: str, request: Request):
        from medals import CATALOG, MANUAL_CATALOG
        from runtime import metadata_transaction

        with SessionLocal() as db:
            require_editor(db, request)
        if code not in {**CATALOG, **MANUAL_CATALOG}:
            raise HTTPException(404)
        if request.headers.get("X-ST9-Action") != "award-design":
            raise HTTPException(403)
        async with request.form(max_files=1, max_fields=1, max_part_size=4096) as form:
            try:
                data = json.loads(form.get("settings", ""))
                if not isinstance(data, dict) or set(data) != {
                    "name",
                    "reset_image",
                    "revision",
                }:
                    raise ValueError()
                name = data["name"]
                if name is not None:
                    if (
                        not isinstance(name, str)
                        or not name.strip()
                        or len(name.strip()) > 100
                    ):
                        raise ValueError()
                    name = name.strip()
                    if any(unicodedata.category(c) == "Cc" for c in name):
                        raise ValueError()
                if type(data["reset_image"]) is not bool or (
                    data["revision"] is not None
                    and not isinstance(data["revision"], str)
                ):
                    raise ValueError()
            except (TypeError, ValueError):
                raise HTTPException(
                    422, "Проверьте название награды: от 1 до 100 символов."
                )
            upload = form.get("image")
            raw = None
            if upload is not None:
                if not hasattr(upload, "read") or data["reset_image"]:
                    raise HTTPException(422, "Выберите фото или сброс изображения.")
                raw = await upload.read(MAX_UPLOAD + 1)
                raw = await run_in_threadpool(normalized_image, raw)
        async with metadata_transaction() as db:
            actor = require_editor(db, request)
            row = db.get(AwardDesign, code)
            if (row.revision if row else None) != data["revision"]:
                raise HTTPException(
                    409,
                    "Награда уже изменена. Обновите список и повторите редактирование.",
                )
            if not row:
                row = AwardDesign(code=code)
                db.add(row)
            row.name = name
            if data["reset_image"]:
                row.image = row.digest = None
            elif raw is not None:
                row.image, row.digest = raw, sha256(raw).hexdigest()
            row.revision = uuid.uuid4().hex
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            row.updated_by = actor.discord_id
            db.flush()
            from admin_hub import log_action

            log_action(
                db,
                actor,
                "award_design_updated",
                "Обновлено оформление награды: " + code,
            )
            result = design_data(db)
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"})

    @router.get("/award-images/{code}/{digest}.webp")
    def award_image(code: str, digest: str):
        with SessionLocal() as db:
            row = db.get(AwardDesign, code)
            if not row or row.digest != digest or not row.image:
                raise HTTPException(404)
            return Response(
                row.image,
                media_type="image/webp",
                headers={
                    "Cache-Control": "public, max-age=31536000, immutable",
                    "ETag": '"' + digest + '"',
                    "X-Content-Type-Options": "nosniff",
                },
            )

    return router
