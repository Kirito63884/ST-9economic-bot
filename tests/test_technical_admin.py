"""Owner-only delegation, current privileges and authentication boundaries."""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import main
from conftest import session_cookie, wallet
from database import GameHistory, SessionLocal, User

OWNER = "100000000000000000"


def accounts(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    make_user(103, is_admin=True, is_senior_admin=True, is_chief_admin=True)
    make_user(104)
    with SessionLocal.begin() as db:
        db.get(User, 101).discord_id = OWNER


def flags(uid):
    with SessionLocal() as db:
        u = db.get(User, uid)
        return u.is_tech_admin, u.is_admin, u.is_senior_admin, u.is_chief_admin


def grant(client, uid=102):
    client.cookies.set("session", session_cookie(101))
    response = client.post(f"/admin/toggle-admin/{uid}", data={"action": "make_tech"})
    assert response.status_code == 200, response.text
    assert response.json()["is_tech_admin"]
    return response


def test_owner_grants_revokes_and_audits_without_auto_assignment(make_user, login_as):
    accounts(make_user)
    assert flags(102) == (False, False, False, False)
    client = login_as(101)
    grant(client)
    assert flags(102) == (True, True, True, True)
    assert (
        client.post(
            "/admin/toggle-admin/102", data={"action": "remove_tech"}
        ).status_code
        == 200
    )
    assert flags(102) == (False, True, True, True)
    # The owner can also select a different level or explicitly revoke all access.
    grant(client)
    assert (
        client.post("/admin/toggle-admin/102", data={"action": "remove"}).status_code
        == 200
    )
    assert flags(102) == (False, False, False, False)
    with SessionLocal() as db:
        logs = db.query(GameHistory).filter_by(user_id=102, result="role_change").all()
        assert len(logs) == 4
        assert all("техническ" in row.details for row in logs)


@pytest.mark.parametrize("actor", [102, 103, 104])
@pytest.mark.parametrize(
    "action",
    ["make_tech", "remove_tech", "make_chief", "make_senior", "make_junior", "remove"],
)
def test_no_delegated_or_ordinary_admin_can_reassign_or_demote_tech(
    make_user, login_as, actor, action
):
    accounts(make_user)
    client = login_as(101)
    grant(client)
    client.cookies.set("session", session_cookie(actor))
    response = client.post("/admin/toggle-admin/102", data={"action": action})
    assert response.status_code in (400, 403)
    assert flags(102) == (True, True, True, True)
    if action == "make_tech":
        assert client.post(
            "/admin/toggle-admin/104", data={"action": action}
        ).status_code in (400, 403)
        assert flags(104) == (False, False, False, False)


def test_csrf_anonymous_deletion_and_identity_protection(
    make_user, login_as, monkeypatch
):
    accounts(make_user)
    client = login_as(101)
    assert (
        client.post(
            "/admin/toggle-admin/102",
            data={"action": "make_tech"},
            headers={"X-ST9-CSRF": "invalid"},
        ).status_code
        == 403
    )
    assert not flags(102)[0]
    grant(client)
    grant(client, 104)
    for actor in (102, 103, 104):
        client.cookies.set("session", session_cookie(actor))
        assert client.post("/admin/delete/102").status_code in (400, 403)
        assert client.post("/admin/delete/101").status_code == 403
    with SessionLocal() as db:
        assert db.get(User, 102) is not None
    with SessionLocal.begin() as db:
        db.get(User, 102).discord_id = "200000000000000102"
    monkeypatch.setenv("SITE_URL", "https://balance.example")
    for actor, target in [
        (102, OWNER),
        (103, "200000000000000102"),
        (104, "200000000000000102"),
    ]:
        client.cookies.set("session", session_cookie(actor))
        assert (
            client.post(
                "/api/admin/login-links", json={"discord_id": target}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/admin/login-links/revoke", json={"discord_id": target}
            ).status_code
            == 403
        )
    client.cookies.clear()
    assert (
        client.post("/admin/toggle-admin/102", data={"action": "remove"}).status_code
        == 403
    )


def test_tech_has_owner_operational_access_and_revocation_is_immediate(
    make_user, login_as
):
    accounts(make_user)
    client = login_as(101)
    grant(client)
    client.cookies.set("session", session_cookie(102))
    with SessionLocal() as db:
        owner, tech = db.get(User, 101), db.get(User, 102)
        assert main.has_owner_access(tech) and main.is_super_admin(tech)
        assert main.is_senior_or_super(tech) and main.can_edit_user(tech, owner)
        assert not main.is_owner_admin(tech)
    for path in (
        "/admin",
        "/api/statistics/freshness",
        "/api/admin/groups",
        "/api/admin/game-tables",
        "/api/admin/automation/settings",
        "/api/admin/award-design",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
    assert (
        client.post("/admin/balance/101", data={"balance": "1234.56"}).status_code
        == 200
    )
    assert wallet(101) == 1234.56
    assert (
        client.post(
            "/admin/toggle-admin/103", data={"action": "make_senior"}
        ).status_code
        == 200
    )
    # Even a pending bulk preview loses authority when the owner revokes access.
    data = {"user_ids": [103], "amount": 5}
    preview = client.post("/admin/mass-adjust", json=data | {"preview": True})
    assert preview.status_code == 200
    cookie = session_cookie(102)
    client.cookies.set("session", session_cookie(101))
    assert (
        client.post("/admin/toggle-admin/102", data={"action": "remove"}).status_code
        == 200
    )
    client.cookies.set("session", cookie)
    assert client.get("/admin").status_code == 403
    assert (
        client.post(
            "/admin/mass-adjust",
            json=data,
            headers={"X-ST9-Confirmation": preview.json()["confirmation"]},
        ).status_code
        == 403
    )
    assert wallet(103) == 1000


def test_import_and_profile_do_not_grant_roles_and_discord_sync_cannot_revoke(
    make_user, login_as
):
    accounts(make_user)
    client = login_as(101)
    grant(client)
    client.cookies.set("session", session_cookie(103))
    data = {
        "users": [
            {
                "discord_id": "104",
                "balance": 1000,
                "is_tech_admin": True,
                "is_admin": True,
            }
        ]
    }
    preview = client.post("/admin/import", json=data | {"preview": True})
    assert preview.status_code == 200
    assert (
        client.post(
            "/admin/import",
            json=data,
            headers={"X-ST9-Confirmation": preview.json()["confirmation"]},
        ).status_code
        == 200
    )
    assert flags(104) == (False, False, False, False)
    with SessionLocal.begin() as db:
        user = db.get(User, 102)
        main.apply_discord_membership(
            db, user, {"allowed": False, "reason": "no_role", "roles": []}
        )
        assert user.deleted_at is None and user.is_tech_admin
        db.get(User, 104).is_tech_admin = False
    client.cookies.set("session", session_cookie(104))
    assert (
        client.post("/profile/update", data={"is_tech_admin": "true"}).status_code
        == 200
    )
    assert not flags(104)[0]


def test_role_controls_are_visible_only_to_real_owner(make_user, login_as):
    accounts(make_user)
    client = login_as(101)
    grant(client)
    html = client.get("/admin").text
    assert 'data-action="make_tech"' in html and 'data-action="remove_tech"' in html
    assert 'data-role="tech"' in html and "Технический админ" in html
    for uid in (102, 103):
        client.cookies.set("session", session_cookie(uid))
        html = client.get("/admin").text
        assert (
            'data-action="make_tech"' not in html
            and 'data-action="remove_tech"' not in html
        )


def test_old_database_migration_adds_false_role_without_touching_wallets(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, discord_id TEXT UNIQUE NOT NULL, username TEXT NOT NULL, balance FLOAT, is_admin BOOLEAN, is_senior_admin BOOLEAN, is_chief_admin BOOLEAN)"
        )
        db.executemany(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(1, OWNER, "Owner", 123.45, 1, 1, 1), (2, "2", "Player", -12.34, 0, 0, 0)],
        )
    code = "from database import run_migrations; run_migrations(); run_migrations()"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "DATABASE_URL": "sqlite:///" + str(path),
            "PYTHON_DOTENV_DISABLED": "1",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT id,balance,is_admin,is_senior_admin,is_chief_admin,is_tech_admin,auth_version FROM users ORDER BY id"
        ).fetchall() == [(1, 123.45, 1, 1, 1, 0, 0), (2, -12.34, 0, 0, 0, 0, 0)]


def test_promotion_invalidates_old_sessions_and_pending_delegated_links(
    make_user, login_as
):
    from datetime import timedelta
    from login_links import utcnow
    from database import LoginLink

    accounts(make_user)
    old_cookie = session_cookie(102)
    with SessionLocal.begin() as db:
        db.add(
            LoginLink(
                token_hash="test-old-delegated-link",
                discord_id="102",
                user_id=102,
                source="admin",
                issued_by="103",
                created_at=utcnow(),
                expires_at=utcnow() + timedelta(minutes=10),
            )
        )
    client = login_as(101)
    grant(client)
    with SessionLocal() as db:
        assert db.get(LoginLink, "test-old-delegated-link").revoked_at is not None
    client.cookies.set("session", old_cookie)
    assert client.get("/api/admin/award-design").status_code == 403
    client.cookies.set("session", old_cookie)
    assert (
        client.post("/admin/toggle-admin/103", data={"action": "remove"}).status_code
        == 403
    )
    client.cookies.set("session", session_cookie(102))
    assert client.get("/api/admin/award-design").status_code == 200


def test_delegated_backup_contains_wallets_without_owner_auth_secrets(
    make_user, login_as, monkeypatch, tmp_path
):
    from contextlib import nullcontext
    import backup_db

    accounts(make_user)
    client = login_as(101)
    grant(client)
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE app_runtime (id INTEGER PRIMARY KEY, secret_key TEXT)")
        db.execute("INSERT INTO app_runtime VALUES(1,'protected-owner-session-secret')")
        db.execute("CREATE TABLE login_links (token_hash TEXT)")
        db.execute("INSERT INTO login_links VALUES('private-login-hash')")
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, balance FLOAT)")
        db.execute("INSERT INTO users VALUES(101,123.45)")
    original = source.read_bytes()
    monkeypatch.setattr(backup_db, "backup_lock", nullcontext)
    monkeypatch.setattr(backup_db, "_manifest", lambda name: {})
    monkeypatch.setattr(backup_db, "_file_path", lambda name: source)
    client.cookies.set("session", session_cookie(102))
    response = client.get("/api/admin/backups/download/fixture.sqlite3")
    assert (
        response.status_code == 200
        and response.headers["X-ST9-Authentication-Reset"] == "1"
    )
    exported = tmp_path / "export.sqlite3"
    exported.write_bytes(response.content)
    with sqlite3.connect(exported) as db:
        assert db.execute("SELECT balance FROM users").fetchone() == (123.45,)
        assert (
            db.execute("SELECT secret_key FROM app_runtime").fetchone()[0]
            != "protected-owner-session-secret"
        )
        assert db.execute("SELECT COUNT(*) FROM login_links").fetchone() == (0,)
    assert source.read_bytes() == original
    client.cookies.set("session", session_cookie(101))
    response = client.get("/api/admin/backups/download/fixture.sqlite3")
    assert (
        response.content == original
        and response.headers["X-ST9-Authentication-Reset"] == "0"
    )


def test_discord_cargo_cannot_archive_tech_but_ordinary_member_still_archives(
    make_user, login_as
):
    from roster_membership import RosterMembership, RosterSyncHealth, sync_snapshot

    accounts(make_user)
    client = login_as(101)
    grant(client)
    roles = [
        {"id": "90001", "name": "Участник"},
        {"id": "90002", "name": "Груз 200"},
    ]
    members = [
        {"user": {"id": str(uid), "username": f"Member{uid}"}, "roles": ["90002"]}
        for uid in (102, 104)
    ]
    with SessionLocal.begin() as db:
        db.query(RosterMembership).delete()
        sync_snapshot(db, roles, members, required_role_id="90001")
        assert db.get(User, 102).deleted_at is None
        assert db.get(User, 102).is_tech_admin
        assert db.get(User, 104).deleted_at is not None
        db.query(RosterMembership).delete()
        db.query(RosterSyncHealth).delete()
