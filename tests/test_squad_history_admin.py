"""Historical online evidence, access control and atomic reviewed wallet grants."""

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest

import main
import squad_history_admin as history
from bulk_operations import BulkConfirmation
from conftest import session_cookie, wallet
from database import GameHistory, SessionLocal, User
from squad_play_history import SquadPlaySession

MOMENT = datetime(2026, 10, 20, 17, 30)
STEAM = "76561198000000000"
OTHER = "76561198000000001"
PATH = "/api/admin/squad/history"


@pytest.fixture(autouse=True)
def reset_history(monkeypatch):
    monkeypatch.setattr(history, "utcnow", lambda: datetime(2026, 10, 29, 12))
    with SessionLocal.begin() as db:
        db.query(SquadPlaySession).delete()
        db.query(BulkConfirmation).delete()


def add_session(steam=STEAM, server="100", **overrides):
    values = dict(
        steam_id=steam,
        server_id=server,
        server_name="Сервер " + server,
        started_at=MOMENT - timedelta(hours=1),
        last_seen_at=MOMENT + timedelta(hours=1),
        ended_at=MOMENT + timedelta(hours=1),
        sample_count=20,
    )
    values.update(overrides)
    with SessionLocal.begin() as db:
        db.add(SquadPlaySession(**values))


@pytest.fixture
def admin_client(make_user, login_as):
    uid = make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102, steam_id=STEAM)
    make_user(103, steam_id=OTHER)
    add_session()
    add_session(OTHER)
    return login_as(uid)


def payload(**changes):
    return dict(
        at=MOMENT.isoformat() + "Z",
        server_ids=["100"],
        user_ids=[102, 103],
        amount="12.34",
        reason="Участие в сборе",
        **changes,
    )


def prepare(client, data=None):
    result = client.post(
        history.GRANT_PATH, json={**(data or payload()), "preview": True}
    )
    assert result.status_code == 200, result.text
    return result.json()


def confirm(client, plan, data=None):
    return client.post(
        history.GRANT_PATH,
        json=data or payload(),
        headers={"X-ST9-Confirmation": plan["confirmation"]},
    )


def test_lookup_deduplicates_sessions_and_uses_moscow_time(admin_client, make_user):
    add_session(server="200", server_name="Другой <img src=x onerror=alert(1)>")
    add_session(started_at=MOMENT - timedelta(minutes=30))
    make_user(104, steam_id="76561198000000002", deleted_at=MOMENT)
    add_session("76561198000000002")
    add_session("76561198000000003")  # No linked account.
    response = admin_client.get(PATH, params={"at": "2026-10-20T20:30:00+03:00"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["at"] == "2026-10-20T17:30:00Z"
    assert result["members_count"] == 2 and len(result["servers"]) == 2
    assert len(next(s for s in result["servers"] if s["id"] == "100")["members"]) == 2
    assert "no-store" in response.headers["cache-control"]
    with SessionLocal() as db:
        assert db.query(GameHistory).count() == 0
    assert wallet(102) == 1000


def test_open_session_does_not_prove_presence_after_last_seen(admin_client):
    with SessionLocal.begin() as db:
        row = db.query(SquadPlaySession).filter_by(steam_id=STEAM).one()
        row.ended_at = None
        row.last_seen_at = MOMENT - timedelta(minutes=1)
    result = admin_client.get(PATH, params={"at": MOMENT.isoformat() + "Z"}).json()
    assert result["members_count"] == 1
    assert result["servers"][0]["members"][0]["user_id"] == 103


def test_bounds_and_gaps_are_not_filled(admin_client):
    for minutes, count in [(-61, 0), (-60, 2), (0, 2), (60, 2), (61, 0)]:
        at = MOMENT + timedelta(minutes=minutes)
        result = admin_client.get(PATH, params={"at": at.isoformat() + "Z"}).json()
        assert result["members_count"] == count
    # An imported end after last observation does not extend observed coverage.
    with SessionLocal.begin() as db:
        for row in db.query(SquadPlaySession):
            row.ended_at = MOMENT + timedelta(days=2)
    result = admin_client.get(
        PATH, params={"at": (MOMENT + timedelta(hours=2)).isoformat() + "Z"}
    ).json()
    assert result["members_count"] == 0


@pytest.mark.parametrize(
    "value", ["2026-10-20", "2026-10-20T17:30", "bad", "2026-10-30T12:00:00+03:00"]
)
def test_invalid_or_future_time_rejected(admin_client, value):
    assert admin_client.get(PATH, params={"at": value}).status_code == 400


@pytest.mark.parametrize(
    "role", ["anonymous", "player", "junior", "senior_flag_only", "archived"]
)
def test_history_and_grants_require_active_senior_admin(
    make_user, login_as, client, role
):
    if role != "anonymous":
        options = {
            "is_admin": role in ("junior", "archived"),
            "is_senior_admin": role in ("senior_flag_only", "archived"),
        }
        if role == "archived":
            options["deleted_at"] = MOMENT
        client = login_as(make_user(**options))
    assert client.get(PATH, params={"at": MOMENT.isoformat() + "Z"}).status_code == 403
    assert (
        client.post(history.GRANT_PATH, json={**payload(), "preview": True}).status_code
        == 403
    )


@pytest.mark.parametrize("role", ["senior", "chief", "owner"])
def test_privileged_roles_have_access(make_user, login_as, monkeypatch, role):
    uid = make_user(
        is_admin=True, is_senior_admin=role == "senior", is_chief_admin=role == "chief"
    )
    if role == "owner":
        monkeypatch.setattr(main, "ADMIN_DISCORD_ID", str(uid))
    response = login_as(uid).get(PATH, params={"at": MOMENT.isoformat() + "Z"})
    assert response.status_code == 200 and response.json()["servers"] == []


def test_preview_then_confirm_pays_once_and_records_evidence(admin_client):
    add_session(server="200")
    data = {**payload(), "server_ids": ["100", "200"], "user_ids": [102, 103, 102]}
    plan = prepare(admin_client, data)
    assert plan["changed"] == 2 and plan["total_delta"] == 24.68
    assert "20.10.2026 20:30:00 МСК" in plan["context"]
    assert wallet(102) == 1000
    with SessionLocal() as db:
        assert db.query(GameHistory).count() == 0
    result = confirm(admin_client, plan, data)
    assert result.status_code == 200, result.text
    assert wallet(102) == 1012.34 and wallet(103) == 1012.34
    assert confirm(admin_client, plan, data).status_code == 409
    with SessionLocal() as db:
        logs = db.query(GameHistory).all()
        assert len(logs) == 2
        assert all(
            "20.10.2026 20:30:00 МСК" in log.details
            and "Участие в сборе" in log.details
            for log in logs
        )
        assert all("Сервер 100" in log.details for log in logs)
        assert "Сервер 200" in next(log.details for log in logs if log.user_id == 102)
        assert "Сервер 200" not in next(
            log.details for log in logs if log.user_id == 103
        )


def test_cannot_pay_without_preview_or_change_payload(admin_client):
    assert admin_client.post(history.GRANT_PATH, json=payload()).status_code == 409
    plan = prepare(admin_client)
    for change in [
        {"amount": "100"},
        {"reason": "Другая причина"},
        {"at": "2026-10-20T17:31:00Z"},
    ]:
        assert confirm(admin_client, plan, {**payload(), **change}).status_code == 409
    assert wallet(102) == 1000


@pytest.mark.parametrize(
    "change", ["steam", "deleted", "history", "balance", "demotion"]
)
def test_changed_identity_evidence_balance_or_rights_blocks_payment(
    admin_client, change
):
    plan = prepare(admin_client)
    with SessionLocal.begin() as db:
        if change == "steam":
            db.get(User, 102).steam_id = "76561198000000009"
        elif change == "deleted":
            db.get(User, 102).deleted_at = MOMENT
        elif change == "history":
            db.query(SquadPlaySession).filter_by(
                steam_id=STEAM
            ).one().started_at += timedelta(minutes=1)
        elif change == "balance":
            db.get(User, 102).balance = 999
        else:
            db.get(User, 101).is_senior_admin = False
    response = confirm(admin_client, plan)
    assert response.status_code == (403 if change == "demotion" else 409), response.text
    assert wallet(103) == 1000
    with SessionLocal() as db:
        assert db.query(GameHistory).count() == 0


def test_owner_is_visible_but_balance_protected(admin_client, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_DISCORD_ID", "102")
    result = admin_client.get(PATH, params={"at": MOMENT.isoformat() + "Z"}).json()
    assert (
        next(m for m in result["servers"][0]["members"] if m["user_id"] == 102)[
            "can_select"
        ]
        is False
    )
    response = admin_client.post(
        history.GRANT_PATH, json={**payload(), "preview": True}
    )
    assert response.status_code == 403 and wallet(103) == 1000


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"user_ids": [101]}, 409),
        ({"server_ids": ["999"]}, 409),
        ({"server_ids": []}, 400),
        ({"user_ids": []}, 400),
        ({"reason": "  "}, 400),
        ({"amount": "-1"}, 400),
        ({"amount": "NaN"}, 400),
        ({"amount": "1.001"}, 400),
    ],
)
def test_invalid_selection_or_payment_is_rejected(admin_client, changes, status):
    response = admin_client.post(
        history.GRANT_PATH, json={**payload(), **changes, "preview": True}
    )
    assert response.status_code == status, response.text
    assert wallet(102) == 1000


def test_balance_limit_fails_whole_batch(admin_client):
    with SessionLocal.begin() as db:
        db.get(User, 103).balance = main.MAX_BALANCE - 1
    response = admin_client.post(
        history.GRANT_PATH, json={**payload(), "preview": True}
    )
    assert response.status_code == 409
    assert wallet(102) == 1000
    with SessionLocal() as db:
        assert db.query(GameHistory).count() == 0


def test_csrf_and_confirmation_actor_and_expiry(admin_client, make_user):
    plan = prepare(admin_client)
    admin_client.headers.pop("X-ST9-CSRF")
    assert confirm(admin_client, plan).status_code == 403
    admin_client.headers["X-ST9-CSRF"] = "test-csrf-token"
    make_user(104, is_admin=True, is_chief_admin=True)
    admin_client.cookies.set("session", session_cookie(104))
    assert confirm(admin_client, plan).status_code == 409
    admin_client.cookies.set("session", session_cookie(101))
    with SessionLocal.begin() as db:
        db.query(BulkConfirmation).update({"expires_at": datetime(2000, 1, 1)})
    assert confirm(admin_client, plan).status_code == 409


def test_concurrent_confirmation_only_pays_once(admin_client):
    plan = prepare(admin_client)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            cookies={"session": session_cookie(101)},
        ) as client:
            return await asyncio.gather(
                *[
                    client.post(
                        history.GRANT_PATH,
                        json=payload(),
                        headers={"X-ST9-Confirmation": plan["confirmation"]},
                    )
                    for _ in range(2)
                ]
            )

    results = asyncio.run(run())
    assert sorted(r.status_code for r in results) == [200, 409]
    assert wallet(102) == 1012.34
    with SessionLocal() as db:
        assert db.query(GameHistory).count() == 2


def test_independent_server_selection_is_preserved_in_evidence_and_logs(admin_client):
    add_session(server="200")
    add_session(OTHER, server="200")
    data = {
        **payload(),
        "server_ids": ["100", "200"],
        "server_selections": {"100": [102], "200": [103]},
    }
    plan = prepare(admin_client, data)
    assert plan["changed"] == 2
    result = confirm(admin_client, plan, data)
    assert result.status_code == 200, result.text
    evidence = result.json()["evidence"]
    assert {s["server_id"]: [m["user_id"] for m in s["members"]] for s in evidence} == {
        "100": [102],
        "200": [103],
    }
    with SessionLocal() as db:
        logs = {log.user_id: log.details for log in db.query(GameHistory)}
        assert "Сервер 100" in logs[102] and "Сервер 200" not in logs[102]
        assert "Сервер 200" in logs[103] and "Сервер 100" not in logs[103]


def test_same_user_selected_on_two_servers_gets_one_payment(admin_client):
    add_session(server="200")
    data = {
        **payload(),
        "server_ids": ["100", "200"],
        "user_ids": [102],
        "server_selections": {"100": [102], "200": [102]},
    }
    plan = prepare(admin_client, data)
    assert plan["changed"] == 1 and plan["total_delta"] == 12.34
    assert confirm(admin_client, plan, data).status_code == 200
    assert wallet(102) == 1012.34 and wallet(103) == 1000
    with SessionLocal() as db:
        assert db.query(GameHistory).count() == 1


def test_server_selection_cannot_be_changed_after_preview(admin_client):
    add_session(server="200")
    add_session(OTHER, server="200")
    data = {
        **payload(),
        "server_ids": ["100", "200"],
        "server_selections": {"100": [102], "200": [103]},
    }
    plan = prepare(admin_client, data)
    changed = {**data, "server_selections": {"100": [103], "200": [102]}}
    assert confirm(admin_client, plan, changed).status_code == 409
    assert wallet(102) == 1000 and wallet(103) == 1000


@pytest.mark.parametrize(
    "choices,status",
    [
        (None, 400),
        ([], 400),
        ({}, 400),
        ({"200": [102, 103]}, 400),
        ({"100": []}, 400),
        ({"100": "102"}, 400),
        ({"100": [True]}, 400),
        ({"100": [102]}, 400),
        ({"100": [101, 103]}, 409),
    ],
)
def test_invalid_per_server_selection_is_rejected(admin_client, choices, status):
    data = {**payload(), "server_selections": choices}
    if choices == {"100": [101, 103]}:
        data["user_ids"] = [101, 103]
    response = admin_client.post(history.GRANT_PATH, json={**data, "preview": True})
    assert response.status_code == status, response.text
    assert wallet(102) == 1000
