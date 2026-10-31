"""Real request guards, fixed-stake settlement, restart and timeout behavior."""

import asyncio
import json
import random

import httpx
import pytest

import main
from backgammon_routes import MAX_CENTS
from conftest import saved_state, session_cookie, wallet
from database import AppRuntime, GameHistory, SessionLocal, User
from table_lifecycle import IDLE_SECONDS


def action(client, path, suffix, **body):
    response = client.get(path + "/state")
    assert response.status_code == 200, response.text
    return client.post(
        path + "/" + suffix, json={"revision": response.json()["revision"], **body}
    )


def table(make_user, login_as, entry=1000, variant="long"):
    make_user(101, balance=100)
    make_user(102, balance=50)
    client = login_as(101)
    made = client.post(
        "/api/backgammon/tables",
        json={
            "rules": {
                "entry_cents": entry,
                "private": True,
                "variant": variant,
            }
        },
    )
    assert made.status_code == 200, made.text
    path = "/api/backgammon/table/" + made.json()["table_id"]
    client.cookies.set("session", session_cookie(102))
    assert client.post(path + "/join").status_code == 200
    assert action(client, path, "ready", ready=True).status_code == 200
    client.cookies.set("session", session_cookie(101))
    assert action(client, path, "ready", ready=True).status_code == 200
    return client, path


def test_limits_negative_balance_and_atomic_start(make_user, login_as):
    client, path = table(make_user, login_as)
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 5
    assert action(client, path, "start").status_code == 409
    assert wallet(101) == 100 and wallet(102) == 5
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 50
    revision = client.get(path + "/state").json()["revision"]
    payload = {"revision": revision}

    async def simultaneous():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            cookies={"session": session_cookie(101)},
        ) as async_client:
            return await asyncio.gather(
                *(async_client.post(path + "/start", json=payload) for _ in range(2))
            )

    assert sorted(r.status_code for r in asyncio.run(simultaneous())) == [200, 409]
    assert wallet(101) == 90 and wallet(102) == 40
    make_user(103, balance=-1)
    client.cookies.set("session", session_cookie(103))
    assert client.post("/api/backgammon/tables", json={"rules": {}}).status_code == 409


def test_actor_revision_csrf_and_client_dice_rejected(make_user, login_as, monkeypatch):
    monkeypatch.setattr("backgammon_game.roll_dice", lambda: [6, 1])
    client, path = table(make_user, login_as, variant="short")
    start = action(client, path, "start").json()
    move = start["round"]["legal"][0]
    payload = {"revision": start["revision"], "kind": "move", "move": move}
    assert (
        client.post(
            path + "/action", json=payload, headers={"X-ST9-CSRF": "wrong"}
        ).status_code
        == 403
    )
    assert (
        client.post(path + "/action", json=payload | {"dice": [6, 6]}).status_code
        == 400
    )
    client.cookies.set("session", session_cookie(102))
    assert client.post(path + "/action", json=payload).status_code == 409
    assert client.get(path + "/state").json()["round"]["legal"] == []
    client.cookies.set("session", session_cookie(101))
    assert client.post(path + "/action", json=payload).status_code == 200
    assert client.post(path + "/action", json=payload).status_code == 409
    make_user(103)
    client.cookies.set("session", session_cookie(103))
    assert client.get(path + "/state").status_code == 403
    assert client.get("/api/backgammon/tables").json()["tables"] == []
    assert client.post(path + "/action", json=payload).status_code == 403


def test_resignation_pays_once_rematch_requires_new_consent(make_user, login_as):
    client, path = table(make_user, login_as, entry=123)
    assert action(client, path, "start").status_code == 200
    result = action(client, path, "action", kind="resign")
    assert result.status_code == 200 and result.json()["match"]["settled"]
    assert wallet(101) == 98.77 and wallet(102) == 51.23
    assert action(client, path, "action", kind="resign").status_code == 409
    assert len(saved_state()["backgammon"]) == 1
    for _ in range(2):
        assert client.get(path + "/state").status_code == 200
    with SessionLocal() as db:
        records = db.query(GameHistory).filter_by(game_type="backgammon").all()
        assert len(records) == 2
        assert sorted(round(row.amount * 100) for row in records) == [-123, 123]
    rematch = action(client, path, "rematch").json()
    assert rematch["phase"] == "waiting" and not any(
        p["ready"] for p in rematch["players"]
    )
    assert action(client, path, "start").status_code == 409
    assert wallet(101) == 98.77 and wallet(102) == 51.23


def test_failed_payout_rolls_back_table_wallet_and_journal(make_user, login_as):
    client, path = table(make_user, login_as)
    assert action(client, path, "start").status_code == 200
    before = client.get(path + "/state").json()
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = MAX_CENTS / 100
    assert action(client, path, "action", kind="resign").status_code == 409
    failed = client.get(path + "/state").json()
    assert (
        failed["revision"] == before["revision"] and failed["round"] == before["round"]
    )
    assert failed["phase"] == "playing" and failed["match"]["result"] is None
    assert wallet(101) == 90
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="backgammon").count() == 0
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 40
    assert action(client, path, "action", kind="resign").status_code == 200
    assert wallet(102) == 60


def test_restart_timeout_refund_and_polling_does_not_extend(make_user, login_as):
    client, path = table(make_user, login_as)
    assert action(client, path, "start").status_code == 200
    tid = path.rsplit("/", 1)[1]
    snapshot = saved_state()
    snapshot["backgammon"][tid]["last_player_activity"] = 1
    with SessionLocal.begin() as db:
        db.get(AppRuntime, 1).payload = json.dumps(snapshot)
    main.restore_runtime(json.dumps(snapshot))
    assert client.get(path + "/state").status_code == 200
    assert main.backgammon_service.table(tid).last_player_activity == 1

    async def expire():
        from runtime import transaction

        async with transaction(main.serialize_runtime, main.restore_runtime) as db:
            from table_lifecycle import expire_match

            assert expire_match(
                main.backgammon_service, tid, db, "backgammon", now=IDLE_SECONDS + 2
            )
            assert not expire_match(
                main.backgammon_service, tid, db, "backgammon", now=IDLE_SECONDS + 3
            )

    asyncio.run(expire())
    assert wallet(101) == 100 and wallet(102) == 50
    assert client.get(path + "/state").status_code == 404
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="backgammon").count() == 2


def test_leaving_waiting_transfers_creator_and_finished_leave_preserves_result(
    make_user, login_as
):
    client, path = table(make_user, login_as)
    assert action(client, path, "leave").status_code == 200
    client.cookies.set("session", session_cookie(102))
    state = client.get(path + "/state").json()
    assert state["creator"] == 102 and len(state["players"]) == 1
    assert not state["players"][0]["ready"]
    client.cookies.set("session", session_cookie(101))
    assert client.post(path + "/join").status_code == 200
    assert action(client, path, "ready", ready=True).status_code == 200
    client.cookies.set("session", session_cookie(102))
    assert action(client, path, "ready", ready=True).status_code == 200
    assert action(client, path, "start").status_code == 200
    assert action(client, path, "leave").status_code == 200
    assert wallet(101) == 110 and wallet(102) == 40
    client.cookies.set("session", session_cookie(101))
    assert client.get(path + "/state").json()["match"]["result"]["winners"] == [101]
    assert action(client, path, "leave").status_code == 200
    assert not main.backgammon_service.tables


def test_coexistence_private_activity_admin_close_and_templates(make_user, login_as):
    client, path = table(make_user, login_as, entry=0)
    chess = client.post("/api/chess/tables", json={"rules": {}})
    assert chess.status_code == 200
    assert action(client, path, "start").status_code == 200
    activity = client.get("/api/home/activity").json()
    group = next(g for g in activity["groups"] if g["key"] == "backgammon-private")
    assert group["url"] == "/games/backgammon/lobby"
    assert path.rsplit("/", 1)[1] not in str(activity)
    snapshot = saved_state()
    main.restore_runtime(json.dumps(snapshot))
    assert len(main.chess_service.tables) == len(main.backgammon_service.tables) == 1
    for route in (
        "/games/backgammon/lobby",
        "/games/backgammon/table/" + path.rsplit("/", 1)[1],
    ):
        assert client.get(route).status_code == 200
    state = client.get(path + "/state").json()
    close = "/api/game-tables/backgammon/" + state["id"] + "/close"
    assert (
        client.post(close, json={"version": state["close_version"]}).status_code == 409
    )
    assert action(client, path, "action", kind="resign").status_code == 200
    state = client.get(path + "/state").json()
    assert (
        client.post(close, json={"version": state["close_version"]}).status_code == 200
    )
    assert len(saved_state()["chess"]) == 1
    assert "/games/backgammon/lobby" in client.get("/games").text


@pytest.mark.parametrize("variant", ["long", "short"])
def test_real_complete_game_pays_winner_once(make_user, login_as, monkeypatch, variant):
    rng = random.Random(674)
    monkeypatch.setattr("backgammon_game.roll_dice", lambda: [6, 1])
    monkeypatch.setattr("backgammon_routes.secrets.token_urlsafe", lambda n: "complete")
    client, path = table(make_user, login_as, entry=250, variant=variant)
    assert action(client, path, "start").status_code == 200

    # Exercise production table/service rules with the real session, commit once
    # through runtime; per-request guards are covered separately above.
    async def complete():
        from runtime import transaction

        async with transaction(main.serialize_runtime, main.restore_runtime) as db:
            t = main.backgammon_service.table("complete")
            for _ in range(2200):
                if t.phase == "finished":
                    break
                if not t.round.dice:
                    monkeypatch.setattr(
                        "backgammon_game.roll_dice",
                        lambda: [rng.randint(1, 6), rng.randint(1, 6)],
                    )
                    t.action(t.actor, "roll")
                else:
                    t.action(t.actor, "move", rng.choice(t.round.legal()))
            assert t.phase == "finished"
            main.backgammon_service.settle(t, db)
            main.backgammon_service.settle(t, db)

    asyncio.run(complete())
    state = client.get(path + "/state").json()
    winner = state["match"]["result"]["winners"][0]
    assert state["round"]["pos"]["off"][0 if winner == 101 else 1] == 15
    assert wallet(winner) == (102.5 if winner == 101 else 52.5)
    assert wallet(101) + wallet(102) == 150
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="backgammon").count() == 2


def test_failed_last_bear_off_rolls_back_checker_and_turn(
    make_user, login_as, monkeypatch
):
    rng = random.Random(106)
    monkeypatch.setattr("backgammon_game.roll_dice", lambda: [6, 1])
    client, path = table(make_user, login_as, variant="short")
    assert action(client, path, "start").status_code == 200
    tid = path.rsplit("/", 1)[1]
    t = main.backgammon_service.table(tid)
    for _ in range(2400):
        if not t.round.dice:
            t.round.roll([rng.randint(1, 6), rng.randint(1, 6)])
            t.actor = t.match["participants"][t.round.turn]["id"]
            continue
        move = rng.choice(t.round.legal())
        if move[1] == 24 and t.round.pos.off[t.round.turn] == 14:
            break
        t.action(t.actor, "move", move)
    else:
        pytest.fail("Synthetic game did not reach its last bear-off")
    winner = t.actor
    with SessionLocal.begin() as db:
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
        db.get(User, winner).balance = MAX_CENTS / 100
    client.cookies.set("session", session_cookie(winner))
    before = client.get(path + "/state").json()
    payload = {"revision": before["revision"], "kind": "move", "move": move}
    assert client.post(path + "/action", json=payload).status_code == 409
    failed = client.get(path + "/state").json()
    assert failed["round"] == before["round"] and failed["actor"] == winner
    assert failed["revision"] == before["revision"] and failed["phase"] == "playing"
    with SessionLocal.begin() as db:
        db.get(User, winner).balance = 90 if winner == 101 else 40
    assert client.post(path + "/action", json=payload).status_code == 200
    assert client.post(path + "/action", json=payload).status_code == 409
    assert wallet(winner) == (110 if winner == 101 else 60)
