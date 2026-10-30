"""HTTP, runtime, wallet rollback and coexistence with the installed games."""

import time

import main
from conftest import session_cookie, wallet
from database import AppRuntime, GameHistory, SessionLocal, User
from draughts_routes import MAX_CENTS
from draughts_game import initial_board


def action(client, path, suffix="action", **payload):
    state = client.get(path + "/state")
    assert state.status_code == 200, state.text
    return client.post(
        path + "/" + suffix, json={"revision": state.json()["revision"], **payload}
    )


def room(make_user, login_as, entry=1000):
    make_user(101, balance=100)
    make_user(102, balance=50)
    client = login_as(101)
    made = client.post(
        "/api/draughts/tables", json={"rules": {"entry_cents": entry, "private": True}}
    )
    assert made.status_code == 200, made.text
    tid = made.json()["table_id"]
    path = "/api/draughts/table/" + tid
    client.cookies.set("session", session_cookie(102))
    assert client.post(path + "/join").status_code == 200
    assert action(client, path, "ready", ready=True).status_code == 200
    client.cookies.set("session", session_cookie(101))
    assert action(client, path, "ready", ready=True).status_code == 200
    return client, path, tid


def save():
    with SessionLocal.begin() as db:
        db.get(AppRuntime, 1).payload = main.serialize_runtime()


def test_start_checks_current_balances_csrf_replays_and_draw_refund(
    make_user, login_as
):
    client, path, _ = room(make_user, login_as)
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 5
    assert action(client, path, "start").status_code == 409
    assert (wallet(101), wallet(102)) == (100, 5)
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 50
    assert action(client, path, "start").status_code == 200
    assert (wallet(101), wallet(102)) == (90, 40)
    assert action(client, path, "start").status_code == 409
    state = client.get(path + "/state").json()
    payload = {
        "revision": state["revision"],
        "kind": "move",
        "move": {"from": "a3", "to": "b4"},
    }
    assert (
        client.post(
            path + "/action", json=payload, headers={"X-ST9-CSRF": "wrong"}
        ).status_code
        == 403
    )
    assert (
        client.post(path + "/action", json=payload | {"winner": 101}).status_code == 400
    )
    assert client.post(path + "/action", json=payload).status_code == 200
    assert client.post(path + "/action", json=payload).status_code == 409
    assert (
        action(client, path, kind="move", move={"from": "c3", "to": "d4"}).status_code
        == 409
    )
    client.cookies.set("session", session_cookie(102))
    assert action(client, path, kind="offer_draw").status_code == 200
    client.cookies.set("session", session_cookie(101))
    assert action(client, path, kind="accept_draw").json()["match"]["settled"]
    assert (wallet(101), wallet(102)) == (100, 50)
    assert action(client, path, kind="accept_draw").status_code == 409
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="draughts").count() == 2


def test_winning_jump_and_wallet_roll_back_together_when_payout_impossible(
    make_user, login_as
):
    client, path, tid = room(make_user, login_as)
    assert action(client, path, "start").status_code == 200
    table = main.draughts_service.table(tid)
    table.position = {"a1": "w", "b2": "b"}
    save()
    before = client.get(path + "/state").json()
    with SessionLocal.begin() as db:
        db.get(User, 101).balance = MAX_CENTS / 100
    response = action(client, path, kind="move", move={"from": "a1", "to": "c3"})
    assert response.status_code == 409
    state = client.get(path + "/state").json()
    assert (
        state["position"] == before["position"]
        and state["revision"] == before["revision"]
    )
    with SessionLocal.begin() as db:
        db.get(User, 101).balance = 90
    result = action(client, path, kind="move", move={"from": "a1", "to": "c3"})
    assert result.status_code == 200 and result.json()["match"]["settled"]
    assert (wallet(101), wallet(102)) == (110, 40)
    client.get(path + "/state")
    assert wallet(101) == 110
    assert action(client, path, "rematch").status_code == 200
    assert client.get(path + "/state").json()["your_side"] == "black"
    assert main.draughts_service.table(tid).position == initial_board()


def test_mid_capture_restore_privacy_admin_management_and_idle_refund(
    make_user, login_as
):
    client, path, tid = room(make_user, login_as)
    assert action(client, path, "start").status_code == 200
    table = main.draughts_service.table(tid)
    table.position = {"b6": "w", "c7": "b", "f6": "b", "h2": "b"}
    save()
    result = action(client, path, kind="move", move={"from": "b6", "to": "d8"}).json()
    assert result["pending"]["at"] == "d8" and result["position"]["d8"] == "W"
    payload = main.serialize_runtime()
    main.restore_runtime(payload)
    assert main.draughts_service.table(tid).pending["captured"] == ["c7"]
    assert client.get(path + "/state").headers["cache-control"] == "private, no-store"
    make_user(103, is_admin=True, is_chief_admin=True)
    client.cookies.set("session", session_cookie(103))
    tables = client.get("/api/admin/game-tables").json()["tables"]
    assert any(
        t["game"] == "draughts" and t["id"] == tid and not t["can_delete"]
        for t in tables
    )
    assert client.get(path + "/state").status_code == 403
    make_user(104)
    client.cookies.set("session", session_cookie(104))
    assert not client.get("/api/draughts/tables").json()["tables"]
    assert client.get(path + "/state").status_code == 403
    main.draughts_service.table(tid).last_player_activity = time.time() - 8 * 3600 - 1
    save()
    client.get("/")
    assert (
        main.draughts_service.table(tid).last_player_activity < time.time() - 8 * 3600
    )
    with SessionLocal.begin() as db:
        main.draughts_service.tick_table(tid, db)
        main.draughts_service.tick_table(tid, db)
    assert tid not in main.draughts_service.tables
    assert (wallet(101), wallet(102)) == (100, 50)


def test_surrender_archive_and_coexistence_preserve_other_game_snapshots(
    make_user, login_as
):
    from chess_game import ChessTable
    from battleship import BattleshipTable

    client, path, tid = room(make_user, login_as)
    assert action(client, path, "start").status_code == 200
    main.chess_service.tables["existing-chess"] = ChessTable(
        "existing-chess", 101, "Chess"
    )
    main.battleship_service.tables["existing-naval"] = BattleshipTable(
        "existing-naval", 102, "Naval"
    )
    save()
    before = {
        "chess": main.chess_service.table("existing-chess").to_dict(),
        "naval": main.battleship_service.table("existing-naval").to_dict(),
    }
    assert action(client, path, kind="resign").status_code == 200
    assert (wallet(101), wallet(102)) == (90, 60)
    assert main.chess_service.table("existing-chess").to_dict() == before["chess"]
    assert main.battleship_service.table("existing-naval").to_dict() == before["naval"]
    state = client.get(path + "/state").json()
    assert (
        client.post(
            "/api/game-tables/draughts/" + tid + "/close",
            json={"version": state["close_version"]},
        ).status_code
        == 200
    )
    assert client.get(path + "/state").status_code == 404


def test_waiting_owner_can_leave_without_destroying_guest_or_touching_wallet(
    make_user, login_as
):
    client, path, tid = room(make_user, login_as)
    assert action(client, path, "leave").status_code == 200
    client.cookies.set("session", session_cookie(102))
    state = client.get(path + "/state").json()
    assert state["creator"] == 102 and len(state["players"]) == 1
    assert not state["players"][0]["ready"]
    assert (wallet(101), wallet(102)) == (100, 50)
