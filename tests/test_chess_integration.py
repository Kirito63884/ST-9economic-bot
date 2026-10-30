"""HTTP settlement, failure rollback, and coexistence with the naval game."""

import json

import main
from conftest import saved_state, session_cookie, wallet
from database import GameHistory, SessionLocal, User
from chess_routes import MAX_CENTS


def action(client, path, suffix, **body):
    state = client.get(path + "/state")
    assert state.status_code == 200, state.text
    return client.post(
        path + "/" + suffix, json={"revision": state.json()["revision"], **body}
    )


def table(make_user, login_as, entry=1000):
    make_user(101, balance=100)
    make_user(102, balance=50)
    client = login_as(101)
    made = client.post(
        "/api/chess/tables", json={"rules": {"entry_cents": entry, "private": True}}
    )
    assert made.status_code == 200
    path = "/api/chess/table/" + made.json()["table_id"]
    client.cookies.set("session", session_cookie(102))
    assert client.post(path + "/join").status_code == 200
    assert action(client, path, "ready", ready=True).status_code == 200
    client.cookies.set("session", session_cookie(101))
    assert action(client, path, "ready", ready=True).status_code == 200
    return client, path


def test_http_wallet_guards_replay_csrf_and_single_draw_refund(make_user, login_as):
    client, path = table(make_user, login_as)
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 5
    assert action(client, path, "start").status_code == 409
    assert wallet(101) == 100 and wallet(102) == 5
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 50
    before = client.get(path + "/state").json()["revision"]
    assert action(client, path, "start").status_code == 200
    assert wallet(101) == 90 and wallet(102) == 40
    assert client.post(path + "/start", json={"revision": before}).status_code == 409
    payload = {
        "revision": client.get(path + "/state").json()["revision"],
        "kind": "move",
        "move": "e2e4",
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
    client.cookies.set("session", session_cookie(102))
    assert action(client, path, "action", kind="offer_draw").status_code == 200
    client.cookies.set("session", session_cookie(101))
    accepted = action(client, path, "action", kind="accept_draw")
    assert accepted.status_code == 200 and accepted.json()["match"]["settled"]
    assert wallet(101) == 100 and wallet(102) == 50
    assert action(client, path, "action", kind="accept_draw").status_code == 409
    for _ in range(2):
        assert client.get(path + "/state").status_code == 200
    with SessionLocal() as db:
        rows = db.query(GameHistory).filter_by(game_type="chess").all()
        assert len(rows) == 2 and all(
            row.result == "push" and row.amount == 0 for row in rows
        )


def test_failed_winning_move_rolls_back_position_and_finances(make_user, login_as):
    client, path = table(make_user, login_as)
    assert action(client, path, "start").status_code == 200
    for uid, move in [(101, "f2f3"), (102, "e7e5"), (101, "g2g4")]:
        client.cookies.set("session", session_cookie(uid))
        assert action(client, path, "action", kind="move", move=move).status_code == 200
    client.cookies.set("session", session_cookie(102))
    before = client.get(path + "/state").json()
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = MAX_CENTS / 100
    assert action(client, path, "action", kind="move", move="d8h4").status_code == 409
    failed = client.get(path + "/state").json()
    assert failed["fen"] == before["fen"] and failed["revision"] == before["revision"]
    assert failed["phase"] == "playing" and not failed["match"]["result"]
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="chess").count() == 0
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 40
    assert action(client, path, "action", kind="move", move="d8h4").status_code == 200
    assert wallet(101) == 90 and wallet(102) == 60
    assert action(client, path, "action", kind="move", move="d8h4").status_code == 409


def test_chess_and_battleship_share_runtime_without_losing_either(make_user, login_as):
    client, path = table(make_user, login_as, entry=0)
    made = client.post("/api/battleship/tables", json={"rules": {"entry_cents": 0}})
    assert made.status_code == 200
    naval = "/api/battleship/table/" + made.json()["table_id"]
    assert action(client, path, "start").status_code == 200
    assert action(client, path, "action", kind="move", move="e2e4").status_code == 200
    snapshot = saved_state()
    assert len(snapshot["chess"]) == len(snapshot["battleship"]) == 1
    main.restore_runtime(json.dumps(snapshot))
    assert client.get(naval + "/state").status_code == 200
    assert client.get(path + "/state").json()["fen"].startswith("rnbqkbnr/pppppppp")
    client.cookies.set("session", session_cookie(102))
    assert client.get(naval + "/state").status_code == 403
    assert client.get("/api/chess/tables").json()["tables"][0]["mine"]
    make_user(103, is_admin=True, is_senior_admin=True, is_chief_admin=True)
    client.cookies.set("session", session_cookie(103))
    tables = client.get("/api/admin/game-tables")
    assert tables.status_code == 200
    assert {row["game"] for row in tables.json()["tables"]} >= {"battleship", "chess"}
    html = client.get("/games").text
    assert "/games/chess/lobby" in html and "/games/battleship/lobby" in html
