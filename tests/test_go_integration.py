"""Durable two-party scoring consent and exact wallet settlement."""

import json

import main
from conftest import saved_state, session_cookie, wallet
from database import AppRuntime, GameHistory, SessionLocal, User
from go_routes import MAX_CENTS
from table_lifecycle import IDLE_SECONDS


def request(c, path, suffix, **values):
    state = c.get(path + "/state")
    assert state.status_code == 200, state.text
    return c.post(
        path + "/" + suffix, json={"revision": state.json()["revision"], **values}
    )


def table(make_user, login_as, monkeypatch, size=9, entry=1000):
    monkeypatch.setattr("go_game.secrets.randbelow", lambda _: 0)
    make_user(101, balance=100)
    make_user(102, balance=50)
    c = login_as(101)
    made = c.post(
        "/api/go/tables",
        json={"rules": {"size": size, "entry_cents": entry, "private": True}},
    )
    assert made.status_code == 200, made.text
    path = "/api/go/table/" + made.json()["table_id"]
    c.cookies.set("session", session_cookie(102))
    assert c.post(path + "/join").status_code == 200
    assert request(c, path, "ready", ready=True).status_code == 200
    c.cookies.set("session", session_cookie(101))
    assert request(c, path, "ready", ready=True).status_code == 200
    return c, path


def act(c, path, uid, kind, data=None):
    c.cookies.set("session", session_cookie(uid))
    return request(c, path, "action", kind=kind, data={} if data is None else data)


def enter_scoring(c, p):
    assert act(c, p, 101, "play", {"point": 0}).status_code == 200
    assert act(c, p, 102, "play", {"point": 80}).status_code == 200
    assert act(c, p, 101, "pass").status_code == 200
    assert act(c, p, 102, "pass").status_code == 200


def test_same_scoring_proposal_needs_two_consents_and_pays_once(
    make_user, login_as, monkeypatch
):
    c, p = table(make_user, login_as, monkeypatch)
    assert request(c, p, "start").status_code == 200
    assert wallet(101) == 90 and wallet(102) == 40
    enter_scoring(c, p)
    before = c.get(p + "/state").json()
    assert before["phase"] == "scoring" and before["match"]["result"] is None
    assert act(c, p, 101, "confirm_score").status_code == 200
    assert wallet(101) == 90 and wallet(102) == 40
    # Changing marks invalidates the first player's consent.
    assert act(c, p, 102, "mark_dead", {"point": 0}).status_code == 200
    assert c.get(p + "/state").json()["score_accepts"] == []
    assert act(c, p, 102, "confirm_score").status_code == 200
    assert c.get(p + "/state").json()["phase"] == "scoring"
    assert act(c, p, 101, "confirm_score").status_code == 200
    final = c.get(p + "/state").json()
    assert final["phase"] == "finished" and final["match"]["settled"]
    assert final["match"]["result"]["winners"] == [102]
    assert wallet(101) == 90 and wallet(102) == 60
    assert act(c, p, 101, "confirm_score").status_code == 409
    for _ in range(3):
        c.get(p + "/state")
    with SessionLocal() as db:
        rows = db.query(GameHistory).filter_by(game_type="go").all()
        assert len(rows) == 2 and sum(row.amount for row in rows) == 0
    c.cookies.set("session", session_cookie(101))
    assert request(c, p, "rematch").status_code == 200
    state = c.get(p + "/state").json()
    assert state["phase"] == "waiting" and not any(x["ready"] for x in state["players"])


def test_payout_failure_rolls_back_second_consent_and_all_money(
    make_user, login_as, monkeypatch
):
    c, p = table(make_user, login_as, monkeypatch)
    assert request(c, p, "start").status_code == 200
    enter_scoring(c, p)
    assert act(c, p, 101, "confirm_score").status_code == 200
    before = c.get(p + "/state").json()
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = MAX_CENTS / 100
    assert act(c, p, 102, "confirm_score").status_code == 409
    after = c.get(p + "/state").json()
    assert (
        after["phase"] == "scoring"
        and after["score_accepts"] == [101]
        and after["revision"] == before["revision"]
    )
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="go").count() == 0
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 40
    assert act(c, p, 102, "confirm_score").status_code == 200
    assert wallet(102) == 60


def test_balance_recheck_max_winnings_replays_and_csrf(
    make_user, login_as, monkeypatch
):
    c, p = table(make_user, login_as, monkeypatch)
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 9
    assert request(c, p, "start").status_code == 409
    assert wallet(101) == 100
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 50
    revision = c.get(p + "/state").json()["revision"]
    assert request(c, p, "start").status_code == 200
    assert c.post(p + "/start", json={"revision": revision}).status_code == 409
    revision = c.get(p + "/state").json()["revision"]
    body = {"revision": revision, "kind": "play", "data": {"point": 0}}
    assert (
        c.post(p + "/action", json=body, headers={"X-ST9-CSRF": "wrong"}).status_code
        == 403
    )
    assert c.post(p + "/action", json=body | {"winner": 101}).status_code == 400
    assert c.post(p + "/action", json=body).status_code == 200
    assert c.post(p + "/action", json=body).status_code == 409
    assert wallet(101) == 90 and wallet(102) == 40


def test_recovery_preserves_superko_scoring_consent_and_other_games(
    make_user, login_as, monkeypatch
):
    c, p = table(make_user, login_as, monkeypatch)
    assert request(c, p, "start").status_code == 200
    enter_scoring(c, p)
    assert act(c, p, 101, "confirm_score").status_code == 200
    assert c.post("/api/chess/tables", json={"rules": {}}).status_code == 200
    assert c.post("/api/battleship/tables", json={"rules": {}}).status_code == 200
    snapshot = saved_state()
    main.restore_runtime(json.dumps(snapshot))
    assert all(len(snapshot[k]) == 1 for k in ("go", "chess", "battleship"))
    state = c.get(p + "/state").json()
    assert state["score_accepts"] == [101]
    assert act(c, p, 102, "resume").status_code == 200
    assert c.get(p + "/state").json()["actor"] == 101
    assert act(c, p, 101, "play", {"point": 1}).status_code == 200


def test_scoring_is_active_cannot_be_closed_and_idle_refunds_once(
    make_user, login_as, monkeypatch
):
    c, p = table(make_user, login_as, monkeypatch)
    assert request(c, p, "start").status_code == 200
    enter_scoring(c, p)
    c.cookies.set("session", session_cookie(101))
    state = c.get(p + "/state").json()
    tid = p.rsplit("/", 1)[1]
    assert not state["can_close"]
    assert (
        c.post(
            f"/api/game-tables/go/{tid}/close", json={"version": state["close_version"]}
        ).status_code
        == 409
    )
    main.go_service.tables[tid].last_player_activity -= IDLE_SECONDS + 1
    with SessionLocal.begin() as db:
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    c.get(p + "/state")
    with SessionLocal.begin() as db:
        main.go_service.tick_table(tid, db)
        main.go_service.tick_table(tid, db)
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    assert (
        wallet(101) == 100 and wallet(102) == 50 and tid not in main.go_service.tables
    )
    with SessionLocal() as db:
        assert (
            db.query(GameHistory).filter_by(game_type="go", result="push").count() == 2
        )


def test_private_membership_size_changes_and_scoring_resignation(
    make_user, login_as, monkeypatch
):
    c, p = table(make_user, login_as, monkeypatch)
    assert (
        request(
            c, p, "settings", rules={"size": 13, "entry_cents": 1000, "private": True}
        ).status_code
        == 200
    )
    state = c.get(p + "/state").json()
    assert len(state["board"]) == 169 and not any(u["ready"] for u in state["players"])
    make_user(103, is_admin=True, is_chief_admin=True)
    c.cookies.set("session", session_cookie(103))
    assert c.get(p + "/state").status_code == 403
    assert any(
        t["game"] == "go" for t in c.get("/api/admin/game-tables").json()["tables"]
    )
    make_user(104)
    c.cookies.set("session", session_cookie(104))
    assert not c.get("/api/go/tables").json()["tables"]
    assert p.rsplit("/", 1)[1] not in str(c.get("/api/home/activity").json())
    for uid in (102, 101):
        c.cookies.set("session", session_cookie(uid))
        assert request(c, p, "ready", ready=True).status_code == 200
    assert request(c, p, "start").status_code == 200
    assert (
        act(c, p, 101, "pass").status_code == 200
        and act(c, p, 102, "pass").status_code == 200
    )
    assert act(c, p, 101, "resign").status_code == 200
    assert wallet(102) == 60


def test_go_and_monopoly_share_runtime_without_losing_stakes(
    make_user, login_as, monkeypatch
):
    # Keep Go's deterministic color choice local: both games import secrets,
    # and a global randbelow stub would make every Monopoly opening roll tie.
    with monkeypatch.context() as go_random:
        c, p = table(make_user, login_as, go_random, entry=1000)
    made = c.post("/api/monopoly/tables", json={"rules": {"entry_cents": 500}})
    assert made.status_code == 200, made.text
    m = "/api/monopoly/table/" + made.json()["table_id"]
    c.cookies.set("session", session_cookie(102))
    assert c.post(m + "/join").status_code == 200
    assert request(c, m, "ready", ready=True).status_code == 200
    c.cookies.set("session", session_cookie(101))
    assert request(c, m, "ready", ready=True).status_code == 200
    assert request(c, m, "start").status_code == 200
    assert request(c, p, "start").status_code == 200
    assert wallet(101) == 85 and wallet(102) == 35
    saved = saved_state()
    assert saved["go"] and saved["monopoly"]
    main.restore_runtime(json.dumps(saved))
    assert c.get(m + "/state").status_code == c.get(p + "/state").status_code == 200
    c.cookies.set("session", session_cookie(102))
    assert request(c, p, "leave").status_code == 200
    assert wallet(101) == 105 and wallet(102) == 35
    c.cookies.set("session", session_cookie(101))
    assert c.get(m + "/state").json()["phase"] == "playing"
    assert request(c, m, "leave").status_code == 200
    assert wallet(101) == 105 and wallet(102) == 45
    with SessionLocal() as db:
        rows = db.query(GameHistory).all()
        assert len(rows) == 4
        assert {r.game_type for r in rows} == {"go", "monopoly"}
        assert sum(r.amount for r in rows) == 0
