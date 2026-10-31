"""Atomic real-wallet/runtime boundaries for a multi-seat property game."""

import json

import main
from conftest import saved_state, session_cookie, wallet
from database import AppRuntime, GameHistory, SessionLocal, User
from monopoly_routes import MAX_CENTS
from table_lifecycle import IDLE_SECONDS


def request(client, path, suffix, **values):
    response = client.get(path + "/state")
    assert response.status_code == 200, response.text
    return client.post(
        path + "/" + suffix, json={"revision": response.json()["revision"], **values}
    )


def table(make_user, login_as, n=3, entry=1000):
    for uid in range(101, 101 + n):
        make_user(uid, balance=100)
    client = login_as(101)
    made = client.post(
        "/api/monopoly/tables",
        json={"rules": {"entry_cents": entry, "max_players": n, "private": True}},
    )
    assert made.status_code == 200, made.text
    path = "/api/monopoly/table/" + made.json()["table_id"]
    for uid in range(102, 101 + n):
        client.cookies.set("session", session_cookie(uid))
        assert client.post(path + "/join").status_code == 200
    for uid in range(101, 101 + n):
        client.cookies.set("session", session_cookie(uid))
        assert request(client, path, "ready", ready=True).status_code == 200
    client.cookies.set("session", session_cookie(101))
    return client, path


def test_stakes_min_balance_recheck_replay_winner_all_and_rematch(make_user, login_as):
    c, p = table(make_user, login_as)
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 9
    assert request(c, p, "start").status_code == 409
    assert wallet(101) == 100 and wallet(102) == 9
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 100
    revision = c.get(p + "/state").json()["revision"]
    assert request(c, p, "start").status_code == 200
    assert [wallet(uid) for uid in (101, 102, 103)] == [90, 90, 90]
    assert c.post(p + "/start", json={"revision": revision}).status_code == 409
    assert (
        request(c, p, "action", kind="roll", data={"dice": [6, 6]}).status_code == 400
    )
    for uid in (102, 103):
        c.cookies.set("session", session_cookie(uid))
        assert request(c, p, "leave").status_code == 200
    c.cookies.set("session", session_cookie(101))
    state = c.get(p + "/state").json()
    assert state["phase"] == "finished" and state["match"]["settled"]
    assert wallet(101) == 120 and wallet(102) == wallet(103) == 90
    for _ in range(3):
        assert c.get(p + "/state").status_code == 200
    with SessionLocal() as db:
        rows = db.query(GameHistory).filter_by(game_type="monopoly").all()
        assert len(rows) == 3 and sum(row.amount for row in rows) == 0
    assert (
        request(c, p, "rematch").status_code == 409
    )  # Only one seated player remains.


def test_wallet_limit_failure_restores_entire_last_resignation(make_user, login_as):
    c, p = table(make_user, login_as, n=2)
    assert request(c, p, "start").status_code == 200
    c.cookies.set("session", session_cookie(102))
    before = c.get(p + "/state").json()
    with SessionLocal.begin() as db:
        db.get(User, 101).balance = MAX_CENTS / 100
    assert request(c, p, "leave").status_code == 409
    after = c.get(p + "/state").json()
    assert after["revision"] == before["revision"] and after["phase"] == "playing"
    assert not next(x for x in after["players"] if x["id"] == 102)["left"]
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="monopoly").count() == 0
    with SessionLocal.begin() as db:
        db.get(User, 101).balance = 90
    assert request(c, p, "leave").status_code == 200
    assert wallet(101) == 110


def test_restore_auction_private_listing_admin_and_other_games(make_user, login_as):
    c, p = table(make_user, login_as)
    assert request(c, p, "start").status_code == 200
    tid = p.rsplit("/", 1)[1]
    t = main.monopoly_service.tables[tid]
    t.pending = {"type": "buy", "uid": t.actor, "property": 1}
    t.action(t.actor, "decline")
    with SessionLocal.begin() as db:
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    snapshot = saved_state()
    main.restore_runtime(json.dumps(snapshot))
    assert c.get(p + "/state").json()["pending"]["type"] == "auction"
    assert c.post("/api/chess/tables", json={"rules": {}}).status_code == 200
    assert c.post("/api/battleship/tables", json={"rules": {}}).status_code == 200
    snapshot = saved_state()
    assert all(len(snapshot[key]) == 1 for key in ("monopoly", "chess", "battleship"))
    make_user(104, is_admin=True, is_chief_admin=True, is_senior_admin=True)
    c.cookies.set("session", session_cookie(104))
    assert c.get(p + "/state").status_code == 403
    assert c.get("/api/monopoly/tables").json()["tables"]
    assert {r["game"] for r in c.get("/api/admin/game-tables").json()["tables"]} >= {
        "monopoly",
        "chess",
        "battleship",
    }
    make_user(105)
    c.cookies.set("session", session_cookie(105))
    assert not c.get("/api/monopoly/tables").json()["tables"]
    assert tid not in str(c.get("/api/home/activity").json())
    c.cookies.set("session", session_cookie(101))
    state = c.get(p + "/state").json()
    close = c.post(
        f"/api/game-tables/monopoly/{tid}/close",
        json={"version": state["close_version"]},
    )
    assert close.status_code == 409
    revision = state["revision"]
    assert (
        c.post(
            p + "/action",
            json={"revision": revision, "kind": "roll", "data": {}},
            headers={"X-ST9-CSRF": "wrong"},
        ).status_code
        == 403
    )


def test_idle_polling_does_not_extend_and_refunds_once(make_user, login_as):
    c, p = table(make_user, login_as, n=2)
    assert request(c, p, "start").status_code == 200
    tid = p.rsplit("/", 1)[1]
    t = main.monopoly_service.tables[tid]
    t.last_player_activity -= IDLE_SECONDS + 1
    with SessionLocal.begin() as db:
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    assert c.get(p + "/state").status_code == 200
    with SessionLocal.begin() as db:
        main.monopoly_service.tick_table(tid, db)
        main.monopoly_service.tick_table(tid, db)
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    assert tid not in main.monopoly_service.tables
    assert wallet(101) == wallet(102) == 100
    with SessionLocal() as db:
        assert (
            db.query(GameHistory).filter_by(game_type="monopoly", result="push").count()
            == 2
        )


def test_ten_players_pot_and_negative_balances(make_user, login_as):
    c, p = table(make_user, login_as, n=10, entry=1)
    assert request(c, p, "start").status_code == 200
    for uid in range(102, 111):
        c.cookies.set("session", session_cookie(uid))
        assert request(c, p, "leave").status_code == 200
    assert wallet(101) == 100.09
    make_user(200, balance=-1)
    c.cookies.set("session", session_cookie(200))
    assert c.post("/api/monopoly/tables", json={"rules": {}}).status_code == 409


def test_winner_can_leave_finished_table_without_repaying(make_user, login_as):
    c, p = table(make_user, login_as, n=2)
    assert request(c, p, "start").status_code == 200
    c.cookies.set("session", session_cookie(102))
    assert request(c, p, "leave").status_code == 200
    c.cookies.set("session", session_cookie(101))
    assert request(c, p, "leave").status_code == 200
    assert wallet(101) == 110 and wallet(102) == 90
    assert p.rsplit("/", 1)[1] not in main.monopoly_service.tables


def test_ready_settings_reset_and_prospective_ten_seat_payout_limit(
    make_user, login_as
):
    c, p = table(make_user, login_as, n=10, entry=10)
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = (MAX_CENTS - 50) / 100
    assert (
        request(c, p, "start").status_code == 409
    )  # A ten-seat win would add 90 cents.
    assert (
        request(
            c,
            p,
            "settings",
            rules={"max_players": 10, "entry_cents": 0, "private": False},
        ).status_code
        == 200
    )
    state = c.get(p + "/state").json()
    assert not any(player["ready"] for player in state["players"])
    assert request(c, p, "start").status_code == 409


def test_natural_finish_rematch_has_new_consent_and_new_single_stake(
    make_user, login_as
):
    c, p = table(make_user, login_as, n=2)
    assert request(c, p, "start").status_code == 200
    tid = p.rsplit("/", 1)[1]
    t = main.monopoly_service.tables[tid]
    t.player(102)["cash"] = 0
    t._prepend({"kind": "pay", "uid": 102, "amount": 999, "reason": "Налог"})
    t._drain()
    with SessionLocal.begin() as db:
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    c.cookies.set("session", session_cookie(102))
    assert request(c, p, "action", kind="bankrupt", data={}).status_code == 200
    assert wallet(101) == 110 and wallet(102) == 90
    c.cookies.set("session", session_cookie(101))
    assert request(c, p, "rematch").status_code == 200
    assert request(c, p, "start").status_code == 409
    for uid in (102, 101):
        c.cookies.set("session", session_cookie(uid))
        assert request(c, p, "ready", ready=True).status_code == 200
    assert request(c, p, "start").status_code == 200
    assert wallet(101) == 100 and wallet(102) == 80
    c.cookies.set("session", session_cookie(102))
    assert request(c, p, "leave").status_code == 200
    assert wallet(101) == 120 and wallet(102) == 80
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="monopoly").count() == 4


def test_monopoly_and_draughts_coexist_after_merge(make_user, login_as):
    """Both pending and already installed games must survive shared runtime writes."""
    c, p = table(make_user, login_as, n=2, entry=1000)
    made = c.post("/api/draughts/tables", json={"rules": {"entry_cents": 500}})
    assert made.status_code == 200, made.text
    d = "/api/draughts/table/" + made.json()["table_id"]
    c.cookies.set("session", session_cookie(102))
    assert c.post(d + "/join").status_code == 200
    assert request(c, d, "ready", ready=True).status_code == 200
    c.cookies.set("session", session_cookie(101))
    assert request(c, d, "ready", ready=True).status_code == 200
    assert request(c, d, "start").status_code == 200
    assert request(c, p, "start").status_code == 200
    assert wallet(101) == wallet(102) == 85
    before = saved_state()
    assert before["draughts"] and before["monopoly"]
    main.restore_runtime(json.dumps(before))
    assert c.get(d + "/state").status_code == c.get(p + "/state").status_code == 200
    c.cookies.set("session", session_cookie(102))
    assert request(c, p, "leave").status_code == 200
    assert wallet(101) == 105 and wallet(102) == 85
    c.cookies.set("session", session_cookie(101))
    assert c.get(d + "/state").json()["phase"] == "playing"
    assert request(c, d, "action", kind="resign").status_code == 200
    assert wallet(101) == 105 and wallet(102) == 95
    with SessionLocal() as db:
        rows = db.query(GameHistory).all()
        assert len(rows) == 4
        assert {r.game_type for r in rows} == {"draughts", "monopoly"}
        assert sum(r.amount for r in rows) == 0
