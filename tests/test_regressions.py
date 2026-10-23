import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

import main
from database import AppRuntime, GameHistory, SessionLocal, User
from runtime import transaction
from conftest import wallet, saved_state, save_state, session_cookie


@pytest.mark.parametrize(
    "endpoint",
    ["blackjack/hit", "blackjack/stand", "blackjack/finalize", "crash/cashout"],
)
def test_anonymous_game_actions_return_401(client, endpoint):
    assert client.post("/api/" + endpoint).status_code == 401


@pytest.mark.parametrize("bet", ["nan", "inf", "-1", "0", "1e300", "1e308"])
def test_invalid_bets_never_change_balance(make_user, login_as, bet):
    uid = make_user()
    c = login_as(uid)
    assert c.post(
        "/api/roulette/spin", data={"bet_amount": bet, "bet_type": "red"}
    ).status_code in (400, 422)
    assert wallet(uid) == 1000


def test_roulette_zero_payout_and_net_history(make_user, login_as, monkeypatch):
    uid = make_user()
    monkeypatch.setattr(main.random, "randint", lambda a, b: 0)
    response = login_as(uid).post(
        "/api/roulette/spin", data={"bet_amount": 10, "bet_type": "green"}
    )
    assert response.status_code == 200
    assert response.json()["win_amount"] == 360
    assert wallet(uid) == 1350
    with SessionLocal() as db:
        assert db.query(GameHistory).one().amount == 350


def test_concurrent_bonus_is_credited_once(make_user):
    uid = make_user()

    async def requests():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            cookies={"session": session_cookie(uid)},
        ) as client:
            return await asyncio.gather(
                *(client.post("/api/daily-bonus") for _ in range(8))
            )

    responses = asyncio.run(requests())
    assert all(r.status_code == 200 for r in responses)
    assert sum(r.json()["ok"] for r in responses) == 1
    assert wallet(uid) == 1100


def test_parallel_bets_do_not_overspend(make_user, monkeypatch):
    uid = make_user(balance=10)
    monkeypatch.setattr(main.random, "randint", lambda a, b: 0)

    async def requests():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            cookies={"session": session_cookie(uid)},
        ) as c:
            return await asyncio.gather(
                *(
                    c.post(
                        "/api/roulette/spin", data={"bet_amount": 10, "bet_type": "red"}
                    )
                    for _ in range(4)
                )
            )

    responses = asyncio.run(requests())
    assert sum(r.status_code == 200 for r in responses) == 1
    assert wallet(uid) == 0


def test_daily_preview_uses_next_streak(make_user, login_as):
    uid = make_user(
        daily_streak=6, last_daily_bonus=datetime.now() - timedelta(hours=25)
    )
    c = login_as(uid)
    assert c.get("/api/daily-bonus/status").json()["next_bonus"] == 110
    assert c.post("/api/daily-bonus").json()["amount"] == 110


def test_blackjack_natural_payout_idempotent_and_durable(
    make_user, login_as, monkeypatch
):
    uid = make_user()
    deck = [
        {"value": "8", "suit": "♠"},
        {"value": "9", "suit": "♠"},
        {"value": "K", "suit": "♥"},
        {"value": "A", "suit": "♥"},
    ]
    monkeypatch.setattr(main, "create_deck", lambda: deck.copy())
    monkeypatch.setattr(main.random, "shuffle", lambda deck: None)
    c = login_as(uid)
    assert c.post("/api/blackjack/start", data={"bet": 100}).status_code == 200
    assert wallet(uid) == 900
    state = saved_state()
    assert state["blackjack"][str(uid)]["status"] == "showdown"
    main.restore_runtime(json.dumps(state))
    main.active_games[uid]["resolved_at"] -= 5
    save_state()
    main.active_games.clear()  # Simulates a fresh worker/restart.
    assert c.post("/api/blackjack/finalize").json()["balance"] == 1150
    assert c.post("/api/blackjack/finalize").json()["balance"] == 1150
    with SessionLocal() as db:
        assert db.query(GameHistory).one().amount == 150


def test_blackjack_natural_beats_drawn_21(make_user):
    uid = make_user(balance=900)
    game = {
        "bet": 100,
        "player": [{"value": "A", "suit": "♠"}, {"value": "K", "suit": "♠"}],
        "dealer": [
            {"value": "7", "suit": "♥"},
            {"value": "7", "suit": "♣"},
            {"value": "7", "suit": "♦"},
        ],
        "deck": [],
    }
    with SessionLocal() as db:
        asyncio.run(main._resolve_stand(db.get(User, uid), game, db))
    assert game["outcome"] == "won"


def test_crash_delayed_poll_preserves_auto_cashout(make_user, login_as, monkeypatch):
    uid = make_user()
    monkeypatch.setattr(main, "generate_crash_point", lambda: 3)
    c = login_as(uid)
    assert c.post("/api/crash/start", data={"bet": 100, "target": 2}).status_code == 200
    main.restore_runtime(json.dumps(saved_state()))
    main.crash_games[uid].started_at -= 100
    save_state()
    main.crash_games.clear()
    state = c.get("/api/crash/state").json()
    assert state["status"] == "cashed_out"
    assert wallet(uid) == 1090
    assert c.post("/api/crash/cashout").json()["balance"] == 1090
    with SessionLocal() as db:
        assert db.query(GameHistory).one().amount == 90


def test_crash_equal_target_loses_and_long_downtime_does_not_overflow():
    game = main.CrashGame(1, 100, 2)
    game.crash_point = 2
    game.started_at -= 10**9
    assert game.check_crash()
    assert game.status == "crashed"


@pytest.mark.parametrize("point", [1.01, 1.31, 2.37, 5, 10, 25])
def test_crash_reaches_exact_point_despite_exp_rounding(point):
    game = main.CrashGame(1, 100)
    game.crash_point = point
    game.started_at -= 1000
    assert game.get_current_multiplier() == point
    assert game.check_crash()


def make_table(balances=(1000, 1000, 1000)):
    table = main.PokerTable("test", 101, "Alice")
    table.players[0]["balance"] = balances[0]
    table.players[0]["synced_balance"] = balances[0]
    for i, balance in enumerate(balances[1:], start=102):
        table.add_player(i, f"user{i}", balance=balance)
    return table


def test_three_player_blinds_and_each_player_gets_a_check():
    table = make_table()
    assert table.start_hand()
    dealer = table.dealer_position
    sb, bb = (dealer + 1) % 3, (dealer + 2) % 3
    assert table.players[sb]["bet"] == 10
    assert table.players[bb]["bet"] == 20
    # Complete preflop calls, then every player must act on the flop.
    for _ in range(3):
        idx = table.current_turn
        player = table.players[idx]
        amount = table.current_bet - player["bet"]
        table._commit_bet(player, amount)
        table.pot += amount
        table.next_turn(idx)
    assert table.phase == "flop"
    for i in range(3):
        table.next_turn(table.current_turn)
        assert table.phase == ("turn" if i == 2 else "flop")


def test_heads_up_all_in_blinds_immediately_reach_showdown():
    table = make_table((10, 20))
    assert table.start_hand()
    assert table.phase == "showdown"
    assert sum(p["balance"] for p in table.players) == pytest.approx(30)


def test_folded_departure_does_not_remove_committed_chips():
    table = make_table()
    table.start_hand()
    idx = table.current_turn
    table.players[idx]["folded"] = True
    uid = table.players[idx]["user_id"]
    invested = sum(p["invested"] for p in table.players)
    table.remove_player(uid)
    assert len(table.players) == 3
    assert sum(p["invested"] for p in table.players) == invested
    assert table.players[idx]["leave_pending"]


def test_side_pots_and_uncalled_overbet_conserve_money():
    table = make_table((0, 0, 0))
    table.phase = "river"
    table.community = [
        {"value": v, "suit": s}
        for v, s in [("2", "♠"), ("3", "♥"), ("7", "♣"), ("8", "♦"), ("9", "♠")]
    ]
    for p, investment, cards in zip(
        table.players, (100, 200, 300), (("A", "A"), ("K", "K"), ("Q", "Q"))
    ):
        p["invested"] = investment
        p["all_in"] = True
        p["cards"] = [
            {"value": cards[0], "suit": "♥"},
            {"value": cards[1], "suit": "♦"},
        ]
        p["participated"] = True
    table.pot = 600
    table.showdown()
    assert [p["balance"] for p in table.players] == [300, 200, 100]
    table.showdown()
    assert sum(p["balance"] for p in table.players) == 600


def test_short_all_in_does_not_reopen_raise():
    table = make_table()
    table.start_hand()
    actor = table.current_turn
    table.players[(actor + 1) % 3]["acted"] = True
    table.players[(actor + 1) % 3]["raise_allowed"] = False
    table.reopen_action(actor, 5)
    assert not table.players[(actor + 1) % 3]["raise_allowed"]
    table.reopen_action(actor, 20)
    assert table.players[(actor + 1) % 3]["raise_allowed"]


def test_poker_bankroll_keeps_external_bonus_and_single_table(make_user, login_as):
    uid = make_user()
    c = login_as(uid)
    table_id = c.post("/api/poker/create-table").json()["table_id"]
    assert c.post("/api/poker/create-table").status_code == 409
    assert c.post("/api/daily-bonus").json()["balance"] == 1100
    assert c.get("/api/poker/table/" + table_id).json()["players"][0]["balance"] == 1100


def test_invalid_poker_raise_is_rolled_back(make_user, login_as):
    first = make_user(101)
    second = make_user(102)
    c = login_as(first)
    tid = c.post("/api/poker/create-table").json()["table_id"]
    login_as(second).post("/api/poker/join-table/" + tid)
    login_as(first).post("/api/poker/table/" + tid + "/start")
    state = c.get("/api/poker/table/" + tid).json()
    actor = state["players"][state["current_turn"]]["user_id"]
    before = saved_state()
    response = login_as(actor).post(
        "/api/poker/table/" + tid + "/action",
        data={"action": "raise", "raise_amount": "nan"},
    )
    assert response.status_code in (400, 422)
    assert saved_state() == before


def test_folded_cards_are_not_disclosed(make_user, login_as):
    uid = make_user()
    table = make_table()
    table.folded_cards = [{"value": "A", "suit": "♠"}]
    main.poker_tables["test"] = table
    save_state()
    assert login_as(uid).get("/api/poker/table/test").json()["folded_cards"] == []


def test_transaction_rollback_restores_wallet_and_games(make_user):
    uid = make_user()

    async def fail():
        async with transaction(main.serialize_runtime, main.restore_runtime) as db:
            db.get(User, uid).balance = 12
            main.active_games[uid] = {"status": "playing"}
            db.flush()
            raise ValueError("simulated failure")

    with pytest.raises(ValueError):
        asyncio.run(fail())
    assert wallet(uid) == 1000
    assert not main.active_games


def test_role_loss_archives_and_return_restores(make_user, monkeypatch):
    uid = make_user(bio="keep me", balance=1234)
    monkeypatch.setattr(main, "REQUIRED_GUILD_ID", "guild")
    monkeypatch.setattr(main, "DISCORD_BOT_TOKEN", "test")

    async def denied(discord_id):
        return {"allowed": False, "reason": "no_role"}

    monkeypatch.setattr(main, "check_discord_membership", denied)
    asyncio.run(main.run_role_check_once())
    with SessionLocal() as db:
        assert db.get(User, uid).deleted_at is not None

    async def allowed(discord_id):
        return {"allowed": True}

    monkeypatch.setattr(main, "check_discord_membership", allowed)
    asyncio.run(main.run_role_check_once())
    with SessionLocal() as db:
        user = db.get(User, uid)
        assert user.deleted_at is None
        assert user.balance == 1234 and user.bio == "keep me"


def test_role_api_failure_never_archives(make_user, monkeypatch):
    uid = make_user()
    monkeypatch.setattr(main, "REQUIRED_GUILD_ID", "guild")
    monkeypatch.setattr(main, "DISCORD_BOT_TOKEN", "test")

    async def failed(discord_id):
        raise main.HTTPException(503)

    monkeypatch.setattr(main, "check_discord_membership", failed)
    asyncio.run(main.run_role_check_once())
    with SessionLocal() as db:
        assert db.get(User, uid).deleted_at is None


def test_seven_day_expiry_removes_history_without_reusing_identity(make_user):
    uid = make_user(deleted_at=datetime.now() - timedelta(days=8))
    with SessionLocal() as db:
        db.add(GameHistory(user_id=uid, game_type="daily", result="bonus", amount=100))
        db.commit()

    async def purge():
        async with transaction(main.serialize_runtime, main.restore_runtime) as db:
            main.purge_expired_users(db)

    asyncio.run(purge())
    with SessionLocal() as db:
        assert db.get(User, uid) is None
        assert db.query(GameHistory).count() == 0
        assert db.get(AppRuntime, 1).next_user_id > uid


def test_archived_session_and_profile_cannot_access_site(make_user, login_as):
    uid = make_user(deleted_at=datetime.now())
    c = login_as(uid)
    assert c.post("/api/daily-bonus").status_code == 401
    assert c.get(f"/api/user/{uid}/public-profile").status_code == 404


def test_membership_fail_closed_on_discord_403(monkeypatch):
    monkeypatch.setattr(main, "REQUIRED_GUILD_ID", "guild")
    monkeypatch.setattr(main, "DISCORD_BOT_TOKEN", "test")
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    monkeypatch.setattr(
        main, "get_discord_client", lambda **kw: httpx.AsyncClient(transport=transport)
    )
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.check_discord_membership("123"))
    assert error.value.status_code == 503


def test_oauth_state_is_encoded_and_required(client, monkeypatch):
    monkeypatch.setattr(main, "DISCORD_CLIENT_ID", "client")
    monkeypatch.setattr(main, "DISCORD_CLIENT_SECRET", "secret")
    monkeypatch.setattr(
        main, "DISCORD_REDIRECT_URI", "https://example.com/auth/callback?test=1&two=2"
    )
    response = client.get("/login", follow_redirects=False)
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["redirect_uri"] == ["https://example.com/auth/callback?test=1&two=2"]
    assert query["state"]
    assert client.get("/auth/callback?code=abc&state=invalid").status_code == 400


def test_first_login_does_not_become_admin(client, monkeypatch):
    monkeypatch.setattr(main, "DISCORD_CLIENT_ID", "client")
    monkeypatch.setattr(main, "DISCORD_CLIENT_SECRET", "secret")

    def discord(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "test-token"})
        return httpx.Response(
            200, json={"id": "999999999999999999", "username": "new-user"}
        )

    transport = httpx.MockTransport(discord)
    monkeypatch.setattr(
        main, "get_discord_client", lambda **kw: httpx.AsyncClient(transport=transport)
    )
    location = client.get("/login", follow_redirects=False).headers["location"]
    state = parse_qs(urlsplit(location).query)["state"][0]
    assert (
        client.get(
            "/auth/callback",
            params={"state": state, "code": "abc"},
            follow_redirects=False,
        ).status_code
        == 307
    )
    with SessionLocal() as db:
        assert not db.query(User).one().is_admin


def test_cross_site_post_is_rejected(make_user, login_as):
    uid = make_user()
    assert (
        login_as(uid)
        .post("/api/daily-bonus", headers={"Origin": "https://attacker.example"})
        .status_code
        == 403
    )
    assert wallet(uid) == 1000


@pytest.mark.parametrize("body", [[], None, {"updates": {}}, {"updates": [[]]}])
def test_malformed_admin_payload_is_client_error(make_user, login_as, body):
    uid = make_user(is_admin=True, is_senior_admin=True)
    response = login_as(uid).post(
        "/admin/batch-update",
        content=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_junior_cannot_bypass_required_reason(make_user, login_as):
    admin = make_user(101, is_admin=True)
    target = make_user(102)
    c = login_as(admin)
    assert c.post(f"/admin/balance/{target}", data={"balance": 1}).status_code == 400
    assert (
        c.post(
            "/admin/mass-adjust", json={"user_ids": [target], "amount": 10}
        ).status_code
        == 403
    )
    assert wallet(target) == 1000


def test_duplicate_ids_do_not_double_credit(make_user, login_as):
    admin = make_user(101, is_admin=True, is_senior_admin=True)
    uid = make_user(102)
    c = login_as(admin)
    assert (
        confirmed_bulk(
            c, "/admin/mass-adjust", {"user_ids": [uid, uid], "amount": 10}
        ).status_code
        == 200
    )
    assert wallet(uid) == 1010


def test_steam_binding_database_uniqueness(make_user):
    first = make_user(101, steam_id="76561198000000000")
    second = make_user(102)
    with SessionLocal() as db:
        db.get(User, second).steam_id = db.get(User, first).steam_id
        with pytest.raises(IntegrityError):
            db.commit()


def test_templates_render_with_suspicious_names(make_user, login_as):
    uid = make_user(
        is_admin=True,
        is_senior_admin=True,
        display_name="O'Brien <script>alert(1)</script>",
    )
    c = login_as(uid)
    for url in [
        "/",
        "/profile",
        "/admin",
        f"/admin/logs/{uid}",
        "/games",
        "/games/poker/lobby",
        "/games/blackjack",
        "/games/roulette",
        "/games/crash",
    ]:
        response = c.get(url)
        assert response.status_code == 200, url
        assert response.text.count("<!DOCTYPE html>") == 1, url
        assert "<script>alert(1)</script>" not in response.text


def test_fresh_worker_restores_blackjack(make_user, login_as, monkeypatch):
    uid = make_user()
    monkeypatch.setattr(main.random, "shuffle", lambda deck: None)
    c = login_as(uid)
    assert c.post("/api/blackjack/start", data={"bet": 10}).status_code == 200
    state = c.get("/api/blackjack/state").json()
    code = """import json,sys; import main
from fastapi.testclient import TestClient
client=TestClient(main.app, headers={'X-ST9-CSRF':'test-csrf-token'}); client.cookies.set('session',sys.argv[1])
print(json.dumps(client.get('/api/blackjack/state').json()))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, session_cookie(uid)],
        cwd=Path(main.__file__).parent,
        capture_output=True,
        text=True,
        check=True,
    )
    restored = json.loads(result.stdout)
    assert restored["player"] == state["player"]
    assert restored["dealer"] == state["dealer"]
    assert restored["balance"] == state["balance"]


def test_two_workers_only_credit_one_bonus(make_user):
    uid = make_user()
    code = """import sys,json; import main
from fastapi.testclient import TestClient
client=TestClient(main.app, headers={'X-ST9-CSRF':'test-csrf-token'}); client.cookies.set('session',sys.argv[1])
print(json.dumps(client.post('/api/daily-bonus').json()))
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, session_cookie(uid)],
            cwd=Path(main.__file__).parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert sum(result["ok"] for result in results) == 1
    assert wallet(uid) == 1100


def test_existing_sqlite_schema_is_migrated_without_losing_balance():
    import sqlite3, uuid
    from conftest import test_data

    path = test_data / (uuid.uuid4().hex + ".db")
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, discord_id TEXT UNIQUE NOT NULL, "
            "username TEXT NOT NULL, balance FLOAT, is_admin BOOLEAN)"
        )
        db.execute("INSERT INTO users VALUES (7,'777','legacy',250,0)")
    env = os.environ.copy()
    env["DATABASE_URL"] = "sqlite:///" + path.as_posix()
    code = """import main; from database import SessionLocal,User
with SessionLocal() as db:
    user=db.get(User,7)
    assert user.balance==250 and user.username=='legacy'
    assert user.profile_public and not user.is_senior_admin and user.deleted_at is None
print('migration OK')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(main.__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "migration OK" in result.stdout


def test_downtime_pauses_crash_and_poker_clocks():
    import time

    game = main.CrashGame(101, 100, 2)
    table = make_table()
    table.start_hand()
    now = time.time()
    game.started_at = now - 120
    table.last_action_time = now - 120
    main.crash_games[101] = game
    main.poker_tables["test"] = table
    state = json.loads(main.serialize_runtime())
    state["last_tick"] = now - 120
    main.restore_runtime(json.dumps(state))
    assert time.time() - main.crash_games[101].started_at < 1
    assert time.time() - main.poker_tables["test"].last_action_time < 1


def test_private_table_state_is_not_visible_to_unapproved_players(make_user, login_as):
    uid = make_user(101)
    other = make_user(102)
    tid = (
        login_as(uid)
        .post("/api/poker/create-table", data={"is_private": True})
        .json()["table_id"]
    )
    assert login_as(other).get("/api/poker/table/" + tid).status_code == 403


def test_subcent_bet_is_rejected(make_user, login_as):
    uid = make_user()
    assert (
        login_as(uid).post("/api/blackjack/start", data={"bet": 0.001}).status_code
        == 400
    )
    assert wallet(uid) == 1000


def test_import_restores_steam_and_privacy(make_user, login_as):
    admin = make_user(101, is_admin=True, is_senior_admin=True)
    uid = make_user(102)
    response = confirmed_bulk(
        login_as(admin),
        "/admin/import",
        {
            "users": [
                {
                    "discord_id": str(uid),
                    "balance": 123,
                    "steam_id": "76561198000000000",
                    "profile_public": False,
                }
            ]
        },
    )
    assert response.status_code == 200
    with SessionLocal() as db:
        user = db.get(User, uid)
        assert (
            user.balance == 123
            and user.steam_id == "76561198000000000"
            and not user.profile_public
        )


def confirmed_bulk(client, path, body):
    preview = client.post(path, json={**body, "preview": True})
    assert preview.status_code == 200, preview.text
    return client.post(
        path, json=body, headers={"X-ST9-Confirmation": preview.json()["confirmation"]}
    )
