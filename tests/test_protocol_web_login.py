import threading
from contextlib import nullcontext

import httpx
import pytest

import protocol_login_admin as web
from conftest import session_cookie
from database import SessionLocal, User

PATH = "/api/admin/protocol-login"


@pytest.fixture
def exchange(monkeypatch):
    calls = []

    def login(code, before_save):
        before_save()
        calls.append(code)
        return {"players": []}

    monkeypatch.setattr(web, "login_slot", nullcontext)
    monkeypatch.setattr(web, "login_with_code", login)
    monkeypatch.setattr(web, "refresh_after_login", lambda doc: True)
    return calls


def test_access_csrf_and_no_secret_in_response(client, make_user, login_as, exchange):
    assert client.post(PATH, json={"code": "123456"}).status_code == 401
    for uid, flags in ((101, {}), (102, {"is_admin": True, "is_senior_admin": True})):
        make_user(uid, **flags)
        login_as(uid)
        assert client.post(PATH, json={"code": "123456"}).status_code == 403
        assert 'id="protocolLoginForm"' not in client.get("/admin").text
    make_user(103, is_admin=True, is_chief_admin=True)
    login_as(103)
    assert 'id="protocolLoginForm"' in client.get("/admin").text
    assert (
        client.post(
            PATH, json={"code": "123456"}, headers={"X-ST9-CSRF": "wrong"}
        ).status_code
        == 403
    )
    result = client.post(PATH, json={"code": "123456"})
    assert result.status_code == 200 and result.json()["ok"]
    assert "no-store" in result.headers["cache-control"]
    assert "123456" not in result.text and exchange == ["123456"]


def test_invalid_codes_and_extra_fields_never_reach_protocol(
    make_user, login_as, exchange
):
    client = login_as(make_user(is_admin=True, is_chief_admin=True))
    for value in (123456, True, "１２３４５６", "12345", "1234567", "12 456", None):
        assert client.post(PATH, json={"code": value}).status_code == 400
    assert (
        client.post(
            PATH, json={"code": "123456", "url": "https://evil.invalid"}
        ).status_code
        == 400
    )
    assert not exchange


def test_revoke_access_during_network_before_save(
    make_user, login_as, monkeypatch, exchange
):
    uid = make_user(is_admin=True, is_chief_admin=True)
    client = login_as(uid)

    def revoke(code, before_save):
        with SessionLocal.begin() as db:
            db.get(User, uid).is_chief_admin = False
        before_save()
        pytest.fail("Revoked admin must not replace the cookie")

    monkeypatch.setattr(web, "login_with_code", revoke)
    assert client.post(PATH, json={"code": "123456"}).status_code == 403


def test_sanitized_error_and_success_despite_stats_failure(
    make_user, login_as, monkeypatch, exchange
):
    client = login_as(make_user(is_admin=True, is_chief_admin=True))

    def failed(*args, **kwargs):
        raise httpx.ConnectError("secret-code=123456 session=private")

    monkeypatch.setattr(web, "login_with_code", failed)
    response = client.post(PATH, json={"code": "123456"})
    assert (
        response.status_code == 503
        and "123456" not in response.text
        and "private" not in response.text
    )
    monkeypatch.setattr(
        web, "login_with_code", lambda code, before_save: {"players": []}
    )
    monkeypatch.setattr(web, "refresh_after_login", failed)
    response = client.post(PATH, json={"code": "123456"})
    assert response.status_code == 200 and response.json()["ok"]
    assert "123456" not in response.text


def test_cross_worker_lock_and_retry_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "cookie_path", lambda: tmp_path / "session.cookies")
    with web.login_slot():
        with pytest.raises(web.HTTPException) as error:
            with web.login_slot():
                pytest.fail("Concurrent login allowed")
        assert error.value.status_code == 409
    with pytest.raises(web.HTTPException) as error:
        with web.login_slot():
            pass
    assert error.value.status_code == 429
    assert (tmp_path / "session.login.lock").stat().st_mode & 0o777 == 0o600


def test_slow_protocol_does_not_hold_wallet_transaction(
    make_user, login_as, monkeypatch, exchange
):
    from fastapi.testclient import TestClient
    import main

    client = login_as(make_user(is_admin=True, is_chief_admin=True))
    entered, finish = threading.Event(), threading.Event()

    def slow(code, before_save):
        entered.set()
        assert finish.wait(5)
        before_save()
        return {"players": []}

    monkeypatch.setattr(web, "login_with_code", slow)
    result = []
    thread = threading.Thread(
        target=lambda: result.append(client.post(PATH, json={"code": "123456"}))
    )
    thread.start()
    try:
        assert entered.wait(3)
        other = TestClient(main.app)
        other.cookies.set("session", session_cookie(101))
        assert other.get("/api/chess/tables").status_code == 200
    finally:
        finish.set()
        thread.join(timeout=5)
    assert result[0].status_code == 200
