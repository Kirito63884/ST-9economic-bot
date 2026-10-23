"""Preview/commit contract, CSRF and read-only source status regressions."""

import base64
import json
from datetime import datetime, timedelta

import pytest
from itsdangerous import TimestampSigner

import main
from bulk_operations import BulkConfirmation
from conftest import wallet
from database import GameHistory, SessionLocal, User
from statistics_freshness import freshness


@pytest.fixture
def admin_client(make_user, login_as):
    admin = make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102, steam_id="76561198000000000")
    return login_as(admin)


CASES = [
    ("/admin/mass-adjust", {"user_ids": [102], "amount": 10}),
    ("/admin/mass-adjust-steam", {"steam_ids": ["76561198000000000"], "amount": 10}),
    ("/admin/batch-update", {"updates": [{"user_id": 102, "balance": 1010}]}),
    (
        "/admin/import",
        {"users": [{"discord_id": "102", "balance": 1010, "bio": "Updated"}]},
    ),
]


@pytest.mark.parametrize("path,payload", CASES)
def test_preview_is_read_only_commit_is_once(admin_client, path, payload):
    preview = admin_client.post(path, json={**payload, "preview": True})
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["total_delta"] == 10
    assert plan["rows"][0]["old"] == 1000 and plan["rows"][0]["new"] == 1010
    assert wallet(102) == 1000
    with SessionLocal() as db:
        assert db.query(GameHistory).count() == 0
        assert db.get(User, 102).bio is None
    headers = {"X-ST9-Confirmation": plan["confirmation"]}
    response = admin_client.post(path, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert wallet(102) == 1010
    assert admin_client.post(path, json=payload, headers=headers).status_code == 409
    assert wallet(102) == 1010
    with SessionLocal() as db:
        assert db.query(GameHistory).count() == 1


def test_confirmation_cannot_be_bypassed_or_changed(admin_client):
    path, payload = CASES[0]
    assert admin_client.post(path, json=payload).status_code == 409
    plan = admin_client.post(path, json={**payload, "preview": True}).json()
    headers = {"X-ST9-Confirmation": plan["confirmation"]}
    assert (
        admin_client.post(
            path, json={**payload, "amount": 100}, headers=headers
        ).status_code
        == 409
    )
    assert wallet(102) == 1000
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 1200
    assert admin_client.post(path, json=payload, headers=headers).status_code == 409
    assert wallet(102) == 1200


def test_confirmation_actor_expiry_and_permissions(admin_client, make_user):
    path, payload = CASES[0]
    plan = admin_client.post(path, json={**payload, "preview": True}).json()
    make_user(103, is_admin=True, is_senior_admin=True)
    from conftest import session_cookie

    admin_client.cookies.set("session", session_cookie(103))
    headers = {"X-ST9-Confirmation": plan["confirmation"]}
    assert admin_client.post(path, json=payload, headers=headers).status_code == 409
    admin_client.cookies.set("session", session_cookie(101))
    with SessionLocal.begin() as db:
        for row in db.query(BulkConfirmation):
            row.expires_at = datetime.now() - timedelta(seconds=1)
    assert admin_client.post(path, json=payload, headers=headers).status_code == 409
    with SessionLocal.begin() as db:
        db.get(User, 101).is_senior_admin = False
    assert admin_client.post(path, json={**payload, "preview": True}).status_code == 403


def test_duplicate_import_rejected_and_duplicate_recipients_pay_once(admin_client):
    path, payload = CASES[0]
    plan = admin_client.post(
        path, json={**payload, "user_ids": [102, 102], "preview": True}
    ).json()
    assert plan["changed"] == 1 and plan["total_delta"] == 10
    assert (
        admin_client.post(
            "/admin/import",
            json={"users": [{"discord_id": "102", "balance": 1}] * 2, "preview": True},
        ).status_code
        == 400
    )


def test_csrf_required_even_without_browser_headers(make_user, login_as):
    client = login_as(make_user())
    client.headers.pop("X-ST9-CSRF", None)
    assert client.post("/api/daily-bonus").status_code == 403
    assert (
        client.post("/api/daily-bonus", headers={"X-ST9-CSRF": "wrong"}).status_code
        == 403
    )
    assert wallet(101) == 1000
    assert (
        client.post(
            "/api/daily-bonus", headers={"X-ST9-CSRF": "test-csrf-token"}
        ).status_code
        == 200
    )


def test_csrf_form_and_cross_origin(admin_client):
    admin_client.headers.pop("X-ST9-CSRF", None)
    response = admin_client.post(
        "/profile/update",
        data={"new_name": "Updated", "csrf_token": "test-csrf-token"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    response = admin_client.post(
        "/api/daily-bonus",
        headers={"X-ST9-CSRF": "test-csrf-token", "Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    response = admin_client.post(
        "/api/daily-bonus",
        headers={
            "X-ST9-CSRF": "test-csrf-token",
            "Referer": "https://evil.example/form",
        },
    )
    assert response.status_code == 403


def test_old_session_gets_token_and_sensitive_responses_are_not_cached(
    make_user, client
):
    uid = make_user()
    cookie = (
        TimestampSigner(main.SECRET_KEY)
        .sign(base64.b64encode(json.dumps({"user_id": uid}).encode()))
        .decode()
    )
    client.cookies.set("session", cookie)
    response = client.get("/profile")
    assert response.status_code == 200
    assert "X-ST9-CSRF" in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    csrf = json.loads(base64.b64decode(response.cookies.get("session").split(".")[0]))[
        "csrf_token"
    ]
    client.cookies.clear()
    client.cookies.update(response.cookies)
    assert (
        client.post("/api/daily-bonus", headers={"X-ST9-CSRF": csrf}).status_code == 200
    )


def test_freshness_does_not_confuse_missing_old_or_future_data():
    assert freshness(None, 1000, 90)["state"] == "missing"
    assert freshness(950, 1000, 90)["state"] == "fresh"
    assert freshness(800, 1000, 90)["state"] == "stale"
    assert freshness(1100, 1000, 90)["state"] == "stale"


def test_freshness_requires_login_and_does_not_call_external_services(
    client, make_user, login_as
):
    assert client.get("/api/statistics/freshness").status_code == 401
    response = login_as(make_user()).get("/api/statistics/freshness")
    assert response.status_code == 200, response.text
    assert any(row["name"].startswith("SQSTAT") for row in response.json()["sources"])
    assert "no-store" in response.headers["cache-control"]


def test_logout_get_is_read_only_and_post_requires_csrf(make_user, login_as):
    client = login_as(make_user())
    response = client.get("/logout")
    assert response.status_code == 200 and "csrf_token" in response.text
    assert client.get("/profile").status_code == 200
    client.headers.pop("X-ST9-CSRF", None)
    assert client.post("/logout", follow_redirects=False).status_code == 403
    response = client.post(
        "/logout", headers={"X-ST9-CSRF": "test-csrf-token"}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/"


def test_admin_custom_layout_includes_csrf_bridge(admin_client):
    response = admin_client.get("/admin")
    assert response.status_code == 200
    assert response.text.count("const token =") == 1
    assert "X-ST9-CSRF" in response.text
    assert "statisticsFreshness" in response.text
