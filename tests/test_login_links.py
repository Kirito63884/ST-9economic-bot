import asyncio
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest

import discord_bot
import main
from conftest import wallet, saved_state
from database import AppRuntime, GameHistory, LoginLink, SessionLocal, User
from fastapi.testclient import TestClient
from login_links import issue_login_link, site_origin, LoginLinkError, utcnow
from runtime import metadata_transaction

TARGET = "200000000000000101"
OWNER = "100000000000000000"


@pytest.fixture(autouse=True)
def link_config(monkeypatch):
    monkeypatch.setenv("SITE_URL", "https://balance.example")

    async def membership(discord_id):
        return {
            "allowed": True,
            "username": "discord-user",
            "guild_nickname": "Ник в Discord",
            "roles": ["role"],
        }

    monkeypatch.setattr(main, "check_discord_membership", membership)


def set_discord_id(uid, discord_id=TARGET):
    with SessionLocal() as db:
        db.get(User, uid).discord_id = discord_id
        db.commit()


def issue(discord_id=TARGET):
    async def run():
        async with metadata_transaction() as db:
            return issue_login_link(db, discord_id, "bot", discord_id)

    return asyncio.run(run())


def csrf_for(client):
    response = client.get("/auth/link")
    assert response.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def enter(client, token, csrf=None):
    csrf = csrf or csrf_for(client)
    return client.post(
        "/auth/link", data={"token": token, "csrf": csrf}, follow_redirects=False
    )


def test_link_login_preserves_profile_wallet_rights_and_games(make_user, login_as):
    uid = make_user(
        balance=4321.25,
        is_admin=True,
        is_senior_admin=True,
        display_name="Свой ник",
        steam_id="76561198000000000",
        profile_public=False,
    )
    set_discord_id(uid)
    original_client = login_as(uid)
    assert original_client.post("/api/poker/create-table").status_code == 200
    game_state = saved_state()["poker"]
    token, url = issue()
    assert urlsplit(url).fragment == token
    c = TestClient(main.app)
    assert enter(c, token).status_code == 303
    assert c.get("/admin").status_code == 200
    assert c.get("/profile").status_code == 200
    assert wallet(uid) == 4321.25
    assert saved_state()["poker"] == game_state
    with SessionLocal() as db:
        user = db.get(User, uid)
        assert db.query(User).count() == 1
        assert user.guild_nickname == "Ник в Discord"
        assert user.display_name == "Свой ник"
        assert user.steam_id == "76561198000000000"
        assert user.profile_public is False
        assert db.query(GameHistory).filter_by(result="login_link_used").count() == 1
        assert token not in db.get(AppRuntime, 1).payload
        link = db.query(LoginLink).one()
        assert link.token_hash != token and len(link.token_hash) == 64
    assert enter(TestClient(main.app), token).status_code == 400


def test_first_bot_login_creates_one_regular_profile(client):
    token, _ = issue()
    assert enter(client, token).status_code == 303
    with SessionLocal() as db:
        user = db.query(User).one()
        assert user.discord_id == TARGET
        assert user.username == "discord-user"
        assert user.guild_nickname == "Ник в Discord"
        assert user.balance == 1000
        assert not user.is_admin and not user.is_senior_admin


def test_link_preview_does_not_consume_or_login(make_user, client):
    set_discord_id(make_user())
    token, _ = issue()
    response = client.get("/auth/link")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert token not in response.text
    assert client.get("/profile", follow_redirects=False).status_code == 307
    with SessionLocal() as db:
        assert db.query(LoginLink).one().used_at is None


def test_expired_link_is_rejected(client):
    token, _ = issue()
    with SessionLocal() as db:
        db.query(LoginLink).one().expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert enter(client, token).status_code == 400
    with SessionLocal() as db:
        assert db.query(User).count() == 0


def test_new_link_revokes_old_and_limits_issuance(client):
    first, _ = issue()
    with pytest.raises(LoginLinkError) as error:
        issue()
    assert error.value.status_code == 429
    with SessionLocal() as db:
        db.query(LoginLink).one().created_at -= timedelta(seconds=31)
        db.commit()
    second, _ = issue()
    assert enter(client, first).status_code == 400
    assert enter(client, second).status_code == 303


def test_login_link_requires_same_browser_confirmation(client):
    token, _ = issue()
    assert (
        client.post("/auth/link", data={"token": token, "csrf": "fake"}).status_code
        == 403
    )
    csrf = csrf_for(client)
    assert (
        client.post(
            "/auth/link",
            data={"token": token, "csrf": csrf},
            headers={"Origin": "https://attacker.example"},
        ).status_code
        == 403
    )
    assert enter(client, token, csrf).status_code == 303


@pytest.mark.parametrize("status", ["lost_role", "discord_failure"])
def test_link_login_cannot_bypass_role_or_api_failure(client, monkeypatch, status):
    token, _ = issue()

    async def failed(discord_id):
        if status == "discord_failure":
            raise main.HTTPException(503, "Discord временно недоступен")
        return {"allowed": False, "message": "У вас нет роли"}

    monkeypatch.setattr(main, "check_discord_membership", failed)
    response = enter(client, token)
    assert response.status_code == (503 if status == "discord_failure" else 403)
    with SessionLocal() as db:
        assert db.query(User).count() == 0
        assert db.query(LoginLink).one().used_at is None


def test_return_within_week_restores_same_account(make_user, client):
    uid = make_user(balance=7654, deleted_at=datetime.now() - timedelta(days=3))
    set_discord_id(uid)
    token, _ = issue()
    assert enter(client, token).status_code == 303
    assert wallet(uid) == 7654
    with SessionLocal() as db:
        assert db.get(User, uid).deleted_at is None


def test_expired_archived_identity_invalidates_link(make_user, client):
    uid = make_user(balance=7654, deleted_at=datetime.now() - timedelta(days=8))
    set_discord_id(uid)
    token, _ = issue()
    assert enter(client, token).status_code == 400
    with SessionLocal() as db:
        assert db.query(LoginLink).one().used_at is None
        assert db.get(User, uid).deleted_at is not None


def test_parallel_redemption_succeeds_exactly_once(client):
    token, _ = issue()
    csrf = csrf_for(client)
    cookie = client.cookies.get("session")

    async def race():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            cookies={"session": cookie},
        ) as c:
            return await asyncio.gather(
                *(
                    c.post("/auth/link", data={"token": token, "csrf": csrf})
                    for _ in range(6)
                )
            )

    responses = asyncio.run(race())
    assert sum(r.status_code == 303 for r in responses) == 1
    assert all(r.status_code in (303, 400) for r in responses)
    with SessionLocal() as db:
        assert db.query(User).count() == 1
        assert db.query(GameHistory).filter_by(result="login_link_used").count() == 1


def test_admin_can_issue_for_new_member_and_cancel(make_user, login_as):
    owner = make_user(is_admin=True, is_senior_admin=True)
    set_discord_id(owner, OWNER)
    c = login_as(owner)
    response = c.post("/api/admin/login-links", json={"discord_id": TARGET})
    assert response.status_code == 200
    assert response.json()["new_profile"] is True
    assert response.headers["cache-control"] == "no-store"
    token = urlsplit(response.json()["url"]).fragment
    assert (
        c.post("/api/admin/login-links/revoke", json={"discord_id": TARGET}).json()[
            "revoked"
        ]
        == 1
    )
    assert enter(TestClient(main.app), token).status_code == 400


@pytest.mark.parametrize(
    "is_admin,is_senior", [(False, False), (True, False), (True, True)]
)
def test_only_owner_can_issue_others_links(make_user, login_as, is_admin, is_senior):
    c = login_as(make_user(is_admin=is_admin, is_senior_admin=is_senior))
    assert (
        c.post("/api/admin/login-links", json={"discord_id": TARGET}).status_code == 403
    )
    assert (
        c.post("/api/admin/login-links/revoke", json={"discord_id": TARGET}).status_code
        == 403
    )
    with SessionLocal() as db:
        assert db.query(LoginLink).count() == 0


def test_admin_cannot_add_member_without_role(make_user, login_as, monkeypatch):
    owner = make_user(is_admin=True)
    set_discord_id(owner, OWNER)

    async def denied(discord_id):
        return {"allowed": False, "message": "Нет роли"}

    monkeypatch.setattr(main, "check_discord_membership", denied)
    assert (
        login_as(owner)
        .post("/api/admin/login-links", json={"discord_id": TARGET})
        .status_code
        == 403
    )
    with SessionLocal() as db:
        assert db.query(LoginLink).count() == 0


def test_role_checker_revokes_pending_links(make_user, monkeypatch):
    uid = make_user()
    set_discord_id(uid)
    issue()
    monkeypatch.setattr(main, "REQUIRED_GUILD_ID", "guild")
    monkeypatch.setattr(main, "DISCORD_BOT_TOKEN", "test")

    async def denied(discord_id):
        return {"allowed": False, "message": "Нет роли"}

    monkeypatch.setattr(main, "check_discord_membership", denied)
    asyncio.run(main.run_role_check_once())
    with SessionLocal() as db:
        assert db.get(User, uid).deleted_at
        assert db.query(LoginLink).one().revoked_at


def test_bot_issues_ephemeral_link_for_own_identity(monkeypatch):
    monkeypatch.setattr(discord_bot, "REQUIRED_GUILD_ID", "123")
    monkeypatch.setattr(discord_bot, "REQUIRED_ROLE_ID", "456")
    send = AsyncMock()
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=int(TARGET)),
        guild_id=123,
        guild=SimpleNamespace(
            fetch_member=AsyncMock(
                return_value=SimpleNamespace(roles=[SimpleNamespace(id=456)])
            )
        ),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=send),
    )
    asyncio.run(discord_bot.login_command.callback(interaction))
    assert interaction.response.defer.await_args.kwargs["ephemeral"]
    assert send.await_args.kwargs["ephemeral"]
    assert "https://balance.example/auth/link#" in send.await_args.args[0]
    with SessionLocal() as db:
        assert db.query(LoginLink).one().discord_id == TARGET


def test_bot_fetches_fresh_roles_before_issuing(monkeypatch):
    monkeypatch.setattr(discord_bot, "REQUIRED_GUILD_ID", "123")
    monkeypatch.setattr(discord_bot, "REQUIRED_ROLE_ID", "456")
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=int(TARGET), roles=[SimpleNamespace(id=456)]),
        guild_id=123,
        guild=SimpleNamespace(
            fetch_member=AsyncMock(return_value=SimpleNamespace(roles=[]))
        ),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    asyncio.run(discord_bot.login_command.callback(interaction))
    with SessionLocal() as db:
        assert db.query(LoginLink).count() == 0


def test_site_url_uses_configured_origin_not_host(monkeypatch):
    monkeypatch.delenv("SITE_URL")
    monkeypatch.setenv("DISCORD_REDIRECT_URI", "https://balance.example/auth/callback")
    assert site_origin() == "https://balance.example"
    monkeypatch.setenv("SITE_URL", "http://evil.example")
    with pytest.raises(LoginLinkError):
        site_origin()


def test_link_survives_a_fresh_website_process(make_user):
    uid = make_user(balance=321)
    set_discord_id(uid)
    token, _ = issue()
    env = os.environ.copy()
    env["LOGIN_TEST_TOKEN"] = token
    script = """
import os, re
import main
from fastapi.testclient import TestClient
async def membership(discord_id):
    return {"allowed": True, "username": "discord-user", "guild_nickname": "Guild nick"}
main.check_discord_membership = membership
c = TestClient(main.app, base_url="https://balance.example")
page = c.get("/auth/link")
csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
r = c.post("/auth/link", data={"csrf": csrf, "token": os.environ["LOGIN_TEST_TOKEN"]}, follow_redirects=False)
assert r.status_code == 303, r.status_code
assert c.get("/profile").status_code == 200
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=main.PROJECT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert wallet(uid) == 321
    with SessionLocal() as db:
        assert db.query(LoginLink).one().used_at is not None


def test_anonymous_cannot_issue_or_cancel_links(client):
    assert (
        client.post("/api/admin/login-links", json={"discord_id": TARGET}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/admin/login-links/revoke", json={"discord_id": TARGET}
        ).status_code
        == 403
    )


def test_unicode_csrf_is_a_client_error(client):
    token, _ = issue()
    csrf_for(client)
    response = client.post("/auth/link", data={"token": token, "csrf": "Я" * 43})
    assert response.status_code == 403


@pytest.mark.parametrize(
    "token", ["", "x" * 44, "<script>alert(1)</script>", "../app.db", "Я" * 43]
)
def test_malformed_tokens_do_not_login_or_leak(client, token):
    response = enter(client, token)
    assert response.status_code == 400
    assert "<script>alert(1)</script>" not in response.text
    with SessionLocal() as db:
        assert db.query(User).count() == 0


@pytest.mark.parametrize("discord_id", [TARGET, 123456, "display-name", None])
def test_admin_creation_handles_malformed_json(make_user, login_as, discord_id):
    owner = make_user(is_admin=True)
    set_discord_id(owner, OWNER)
    c = login_as(owner)
    if discord_id == TARGET:
        response = c.post("/api/admin/login-links", json=[])
    else:
        response = c.post("/api/admin/login-links", json={"discord_id": discord_id})
    assert response.status_code == 400
