import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main
import pytest
from admin_presence import player_statuses
from award_design import AwardDesign, normalized_image
from award_role_names import (
    AwardRoleName,
    award_name,
    named_catalog,
    role_ids,
    sync_names,
)
from conftest import session_cookie
from database import SessionLocal, User, engine
from medals import CATALOG, MANUAL_CATALOG, image_path, progress
from medals_bot import MedalBot
from PIL import Image
from sqlalchemy import event
from squad_play_history import SquadPlaySession, SquadPresence, utcnow


@pytest.fixture(autouse=True)
def clean_design():
    main.request_guard.reset()
    with SessionLocal.begin() as db:
        db.query(AwardDesign).delete()
        db.query(AwardRoleName).delete()
        db.query(SquadPresence).delete()
        db.query(SquadPlaySession).delete()


def picture(color="red", fmt="PNG", size=(50, 80)):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=fmt)
    return output.getvalue()


def save(
    client,
    code="gold_star",
    name="Новое название",
    revision=None,
    image=None,
    reset=False,
):
    fields = {
        "settings": (
            None,
            json.dumps({"name": name, "revision": revision, "reset_image": reset}),
        )
    }
    if image is not None:
        fields["image"] = ("photo.png", image, "image/png")
    return client.post(
        "/api/admin/award-design/" + code,
        files=fields,
        headers={"X-ST9-Action": "award-design"},
    )


def chief(client, make_user):
    uid = make_user(is_admin=True, is_chief_admin=True)
    client.cookies.set("session", session_cookie(uid))
    return uid


@pytest.mark.parametrize(
    "flags", [{}, {"is_admin": True}, {"is_admin": True, "is_senior_admin": True}]
)
def test_edit_is_chief_only(client, make_user, flags):
    uid = make_user(**flags)
    client.cookies.set("session", session_cookie(uid))
    assert client.get("/api/admin/award-design").status_code == 403
    assert save(client, image=picture()).status_code == 403
    with SessionLocal() as db:
        assert db.query(AwardDesign).count() == 0


@pytest.mark.parametrize("code", ["gold_star", "best_crew", "valor", "iron_cross"])
def test_name_photo_shared_by_catalog_profile_and_progress(client, make_user, code):
    uid = chief(client, make_user)
    response = save(client, code=code, name="Обновлённая награда", image=picture())
    assert response.status_code == 200, response.text
    entry = next(a for a in response.json()["awards"] if a["code"] == code)
    assert entry["custom_name"] == "Обновлённая награда"
    assert entry["image"].startswith("/award-images/" + code + "/")
    image = client.get(entry["image"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/webp"
    assert "immutable" in image.headers["cache-control"]
    with SessionLocal() as db:
        assert award_name(db, code) == "Обновлённая награда"
        assert (
            named_catalog(db, {**CATALOG, **MANUAL_CATALOG})[code][0]
            == "Обновлённая награда"
        )
        assert image_path(code, db) == entry["image"]
        row = db.get(AwardDesign, code)
        assert sha256(image.content).hexdigest() == row.digest
        assert row.updated_by == str(uid)
        from medals import MedalAward, visible_awards

        db.query(MedalAward).filter_by(discord_id=str(uid), medal=code).delete()
        db.add(
            MedalAward(
                discord_id=str(uid),
                medal=code,
                reason="test",
                evidence="{}",
                earned_at=utcnow(),
            )
        )
        db.flush()
        visible = visible_awards(db, str(uid))
        assert any(a["image"] == entry["image"] for a in visible)
        data = progress(db, db.get(User, uid))
        assert any(a["image"] == entry["image"] for a in data["cards"])
    catalog = client.get("/medals")
    assert catalog.status_code == 200
    assert entry["image"] in catalog.text and "Обновлённая награда" in catalog.text


def test_discord_sync_cannot_erase_custom_title_and_reset_works(client, make_user):
    chief(client, make_user)
    first = save(client, image=picture()).json()["awards"]
    row = next(a for a in first if a["code"] == "gold_star")
    with SessionLocal.begin() as db:
        sync_names(db, [{"id": role_ids()["gold_star"], "name": "Новая роль Discord"}])
        assert award_name(db, "gold_star") == "Новое название"
    reset = save(client, name=None, revision=row["revision"], reset=True)
    assert reset.status_code == 200
    current = next(a for a in reset.json()["awards"] if a["code"] == "gold_star")
    assert current["name"] == "Новая роль Discord"
    assert current["image"] == "/medal-assets/gold_star.png"


def test_replacement_changes_url_and_stale_editor_cannot_overwrite(client, make_user):
    chief(client, make_user)
    a = next(
        a
        for a in save(client, image=picture()).json()["awards"]
        if a["code"] == "gold_star"
    )
    assert save(client, name="lost update").status_code == 409
    b = next(
        a
        for a in save(client, image=picture("blue"), revision=a["revision"]).json()[
            "awards"
        ]
        if a["code"] == "gold_star"
    )
    assert a["image"] != b["image"]
    assert client.get(a["image"]).status_code == 404
    assert client.get(b["image"]).status_code == 200


@pytest.mark.parametrize(
    "raw",
    [
        b'<svg onload="alert(1)"></svg>',
        b"not an image",
        picture()[:40],
        picture(size=(4097, 1)),
    ],
)
def test_bad_images_are_rejected_without_changing_name(client, make_user, raw):
    chief(client, make_user)
    assert save(client, image=raw).status_code == 422
    with SessionLocal() as db:
        assert db.query(AwardDesign).count() == 0


def test_csrf_unknown_award_and_upload_size(client, make_user):
    chief(client, make_user)
    assert client.post("/api/admin/award-design/gold_star").status_code == 403
    assert save(client, code="unknown").status_code == 404
    assert save(client, image=b"a" * 900001).status_code == 413
    assert save(client, name=" ").status_code == 422
    assert save(client, name="x" * 101).status_code == 422
    assert save(client, name="bad\nname").status_code == 422
    assert (
        client.post(
            "/api/admin/award-design/gold_star",
            headers={"Origin": "https://other.example", "X-ST9-Action": "award-design"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize("repeat", [False, True])
def test_bot_reads_new_picture_and_name_at_send_time(client, make_user, repeat):
    chief(client, make_user)
    first = next(
        a
        for a in save(client, image=picture()).json()["awards"]
        if a["code"] == "gold_star"
    )
    record = {
        "id": 999,
        "discord_id": "123",
        "medal": "gold_star",
        "reason": "Причина",
        "attempts": 1,
        "name": "OLD CLAIMED NAME",
        "reward": 0,
        "repeat": repeat,
    }
    received = {}

    async def capture(route, **kwargs):
        received["payload"] = json.loads(kwargs["form"][0]["value"])
        received["bytes"] = kwargs["files"][0].fp.read()
        return {"id": "555"}

    # Editing after a queued record was claimed must still affect the message.
    assert (
        save(
            client,
            name="Текущее имя",
            image=picture("blue"),
            revision=first["revision"],
        ).status_code
        == 200
    )
    channel = SimpleNamespace(guild=SimpleNamespace(id=77))
    bot = SimpleNamespace(
        get_channel=lambda _: channel,
        http=SimpleNamespace(add_role=AsyncMock(), request=capture),
    )
    handler = MedalBot(bot)
    handler.guild_id = "77"
    assert asyncio.run(handler.deliver_announcement(record)) == "555"
    assert received["payload"]["embeds"][0]["title"] == "🏅 Текущее имя"
    with SessionLocal() as db:
        row = db.get(AwardDesign, "gold_star")
        assert received["bytes"] == row.image
        assert row.digest[:16] in received["payload"]["attachments"][0]["filename"]


def test_bulk_indicators_for_both_site_and_game(client, make_user, monkeypatch):
    uid = make_user(is_admin=True, steam_id="76561198000000000")
    make_user(uid=102, steam_id="76561198000000001")
    make_user(uid=103)
    make_user(
        uid=104, deleted_at=datetime.now(timezone.utc).astimezone().replace(tzinfo=None)
    )
    now = utcnow()
    with SessionLocal.begin() as db:
        db.get(User, uid).last_seen = (
            datetime.now(timezone.utc).astimezone().replace(tzinfo=None)
        )
        db.get(User, 102).last_seen = datetime.now(timezone.utc).astimezone().replace(
            tzinfo=None
        ) - timedelta(minutes=6)
        db.add(
            SquadPresence(
                steam_id="76561198000000000",
                checked_at=now,
                payload=json.dumps(
                    [{"server_id": "1", "server_name": "Protocol 1", "online": True}]
                ),
            )
        )
        # Old positive evidence is not shown as live.
        db.add(
            SquadPresence(
                steam_id="76561198000000001",
                checked_at=now - timedelta(minutes=3),
                payload=json.dumps(
                    [{"server_id": "1", "server_name": "Old", "online": True}]
                ),
            )
        )
    monkeypatch.setattr("squad_routes.load_snapshot", lambda *a: [])
    client.cookies.set("session", session_cookie(uid))
    response = client.get("/api/admin/player-presence")
    assert response.status_code == 200
    rows = {r["id"]: r for r in response.json()["players"]}
    assert set(rows) == {uid, 102, 103}
    assert rows[uid]["site_online"] and rows[uid]["squad_online"]
    assert rows[uid]["server_name"] == "Protocol 1"
    assert rows[102]["squad_online"] is None and not rows[102]["site_online"]


def test_bulk_presence_has_bounded_queries(make_user):
    for i in range(20):
        make_user(uid=200 + i, steam_id=str(76561198000000000 + i))
    queries = []

    def capture(*args):
        queries.append(args[2])

    with SessionLocal() as db:
        users = db.query(User).all()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            rows = player_statuses(db, users, servers=[])
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    assert len(rows) == 20
    assert len(queries) == 2


def test_presence_is_private_and_heartbeat_updates_current_user(client, make_user):
    assert client.get("/api/admin/player-presence").status_code == 401
    uid = make_user()
    client.cookies.set("session", session_cookie(uid))
    assert client.get("/api/admin/player-presence").status_code == 403
    assert client.post("/api/site-presence").status_code == 403
    assert (
        client.post(
            "/api/site-presence", headers={"X-ST9-Action": "site-presence"}
        ).status_code
        == 200
    )
    with SessionLocal() as db:
        assert datetime.now(timezone.utc).astimezone().replace(tzinfo=None) - db.get(
            User, uid
        ).last_seen < timedelta(seconds=10)


def test_large_photo_has_only_a_scoped_body_limit(client, make_user, monkeypatch):
    import os

    chief(client, make_user)
    monkeypatch.setattr(main.request_guard, "enabled", True)
    raw = BytesIO()
    Image.frombytes("RGB", (450, 450), os.urandom(450 * 450 * 3)).save(
        raw, format="PNG"
    )
    assert 262144 < len(raw.getvalue()) < 900000
    assert save(client, image=raw.getvalue()).status_code == 200
    assert client.post("/api/site-presence", content=b"x" * 300000).status_code == 413
    assert save(client, image=b"x" * 1000001).status_code == 413


def test_image_processing_does_not_block_wallet_requests(
    client, make_user, monkeypatch
):
    uid = chief(client, make_user)
    entered, released = Event(), Event()

    def slow_image(raw):
        entered.set()
        assert released.wait(5)
        return normalized_image(raw)

    monkeypatch.setattr("award_design.normalized_image", slow_image)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(save, client, image=picture())
        try:
            assert entered.wait(3)
            response = pool.submit(client.get, f"/api/medals/progress/{uid}").result(
                timeout=3
            )
            assert response.status_code == 200
        finally:
            released.set()
        assert future.result(timeout=5).status_code == 200
