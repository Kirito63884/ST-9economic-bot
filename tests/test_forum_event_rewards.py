import asyncio
import httpx
import main
from datetime import timedelta

import pytest
from itsdangerous.timed import TimestampSigner

from automation_control import AutomationSetting
from database import SessionLocal, User, GameHistory
import forum_events as events

EID = "a" * 32
URL = f"/api/admin/forum-events/{EID}/rewards"


@pytest.fixture(autouse=True)
def seed():
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("forum%event%:%")
        ).delete()
        events.create(
            db,
            EID,
            "101",
            dict(
                title="Match",
                starts_at=(events.now() + timedelta(days=2)).isoformat(),
                capacity=20,
                teams="",
                map="",
                server="",
                rules="",
                image_url="",
                status="open",
            ),
        )


def setup(make_user, login_as):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102, balance=10)
    make_user(103, balance=20)
    client = login_as(101)
    revision = client.get(URL).json()["event_revision"]
    return client, {
        "event_revision": revision,
        "recipients": [
            {"user_id": 102, "amount": "1.25"},
            {"user_id": 103, "amount": "2,01"},
        ],
    }


def test_exact_atomic_payout_history_and_duplicate_protection(make_user, login_as):
    client, data = setup(make_user, login_as)
    preview = client.post(URL + "/preview", json=data).json()
    assert preview["total_cents"] == 326
    apply = {**data, "confirmation": preview["confirmation"]}
    assert client.post(URL + "/apply", json=apply).status_code == 200
    assert client.post(URL + "/apply", json=apply).status_code == 409
    with SessionLocal() as db:
        assert db.get(User, 102).balance == 11.25
        assert db.get(User, 103).balance == 22.01
        rows = (
            db.query(GameHistory).filter(GameHistory.result == "admin_adjustment").all()
        )
        assert len(rows) == 2 and all(
            "Награда за событие" in row.details for row in rows
        )
    assert client.post(URL + "/preview", json=data).status_code == 409


def test_invalid_amounts_limits_and_duplicate_people_do_not_partially_pay(
    make_user, login_as
):
    client, data = setup(make_user, login_as)
    for value in ("NaN", "Infinity", "-1", "0", "0.001", "1e2", "1000001", True, 1):
        bad = {
            **data,
            "recipients": [data["recipients"][0], {"user_id": 103, "amount": value}],
        }
        assert client.post(URL + "/preview", json=bad).status_code == 400
    duplicate = {**data, "recipients": [data["recipients"][0]] * 2}
    assert client.post(URL + "/preview", json=duplicate).status_code == 400
    with SessionLocal.begin() as db:
        db.get(User, 103).balance = 1_000_000_000
    assert client.post(URL + "/preview", json=data).status_code == 400
    with SessionLocal() as db:
        assert db.get(User, 102).balance == 10
        assert (
            not db.query(GameHistory)
            .filter(GameHistory.result == "admin_adjustment")
            .all()
        )


def test_proof_binds_author_amount_and_expiry(make_user, login_as, monkeypatch):
    client, data = setup(make_user, login_as)
    make_user(104, is_admin=True, is_senior_admin=True)
    token = client.post(URL + "/preview", json=data).json()["confirmation"]
    body = {**data, "confirmation": token}
    assert login_as(104).post(URL + "/apply", json=body).status_code == 409
    client = login_as(101)
    assert (
        client.post(
            URL + "/apply",
            json={**body, "recipients": [{"user_id": 102, "amount": "99"}]},
        ).status_code
        == 409
    )
    stamp = TimestampSigner.get_timestamp
    monkeypatch.setattr(
        TimestampSigner, "get_timestamp", lambda self: stamp(self) + 301
    )
    assert client.post(URL + "/apply", json=body).status_code == 409


def test_recheck_event_recipient_and_wallet_at_confirmation(make_user, login_as):
    client, data = setup(make_user, login_as)
    token = client.post(URL + "/preview", json=data).json()["confirmation"]
    body = {**data, "confirmation": token}
    with SessionLocal.begin() as db:
        db.get(User, 103).balance = 1_000_000_000
    assert client.post(URL + "/apply", json=body).status_code == 400
    with SessionLocal.begin() as db:
        assert db.get(User, 102).balance == 10
        db.get(User, 103).balance = 20
        event = events.get(db, EID)
        event["config"]["status"] = "cancelled"
        events.save(db, event)
    assert client.post(URL + "/apply", json=body).status_code == 400


def test_simultaneous_requests_pay_once(make_user, login_as):
    client, data = setup(make_user, login_as)
    token = client.post(URL + "/preview", json=data).json()["confirmation"]
    body = {**data, "confirmation": token}

    async def requests():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            cookies=client.cookies,
        ) as async_client:
            replies = await asyncio.gather(
                *(async_client.post(URL + "/apply", json=body) for _ in range(2))
            )
            return [response.status_code for response in replies]

    responses = asyncio.run(requests())
    assert sorted(responses) == [200, 409]
    with SessionLocal() as db:
        assert db.get(User, 102).balance == 11.25
        assert (
            db.query(GameHistory)
            .filter(GameHistory.result == "admin_adjustment")
            .count()
            == 2
        )


def test_junior_and_guest_forbidden_csrf_required(make_user, login_as, client):
    assert client.get(URL).status_code == 403
    make_user(101, is_admin=True)
    client = login_as(101)
    assert client.get(URL).status_code == 403
    assert client.post(URL + "/apply", json={}).status_code == 403
    make_user(102, is_admin=True, is_senior_admin=True)
    client = login_as(102)
    assert (
        client.post(
            URL + "/preview", json={}, headers={"X-ST9-CSRF": "wrong"}
        ).status_code
        == 403
    )
