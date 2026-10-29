"""Human activity, safe eight-hour expiry and owner-only idle-table closure."""

import time
import pytest
import main
from billiards import BilliardsTable
from billiards_routes import BilliardsService
from durak import DurakTable
from durak_routes import DurakService
from database import SessionLocal, GameHistory, AppRuntime
from table_lifecycle import (
    IDLE_SECONDS,
    expire_match,
    expire_poker,
)
from conftest import wallet


def persist():
    with SessionLocal.begin() as db:
        db.get(AppRuntime, 1).payload = main.serialize_runtime()


@pytest.mark.parametrize("game", ["poker", "durak", "billiards"])
def test_exact_eight_hour_boundary_and_poll_does_not_keep_table_alive(
    game, make_user, login_as
):
    make_user(101)
    client = login_as(101)
    if game == "poker":
        tid = client.post("/api/poker/create-table", data={}).json()["table_id"]
        table = main.poker_tables[tid]
        url = f"/api/poker/table/{tid}"
    else:
        tid = client.post(f"/api/{game}/tables", json={"rules": {}}).json()["table_id"]
        table = getattr(main, game + "_service").table(tid)
        url = f"/api/{game}/table/{tid}/state"
    stamp = time.time() - IDLE_SECONDS + 30
    table.last_player_activity = stamp
    persist()
    assert client.get(url).status_code == 200
    if game == "poker":
        assert main.poker_tables[tid].last_player_activity == stamp
        with SessionLocal.begin() as db:
            assert not expire_poker(
                main.poker_tables,
                main.table_requests,
                tid,
                db,
                main.sync_table_balances,
                stamp + IDLE_SECONDS - 0.001,
            )
            assert expire_poker(
                main.poker_tables,
                main.table_requests,
                tid,
                db,
                main.sync_table_balances,
                stamp + IDLE_SECONDS,
            )
        assert tid not in main.poker_tables
    else:
        service = getattr(main, game + "_service")
        assert service.table(tid).last_player_activity == stamp
        with SessionLocal.begin() as db:
            assert not expire_match(
                service, tid, db, game, stamp + IDLE_SECONDS - 0.001
            )
            assert expire_match(service, tid, db, game, stamp + IDLE_SECONDS)
        assert tid not in service.tables
    assert wallet(101) == 1000


@pytest.mark.parametrize("game", ["durak", "billiards"])
def test_active_expiry_refunds_deposits_once(game, make_user):
    make_user(101, balance=100)
    make_user(102, balance=100)
    cls, service = (
        (DurakTable, DurakService())
        if game == "durak"
        else (BilliardsTable, BilliardsService())
    )
    table = cls("old", 101, "A", {"entry_cents": 1000})
    table.add_player(102, "B")
    for player in table.players:
        player["ready"] = True
    service.tables["old"] = table
    with SessionLocal.begin() as db:
        service.start(table, db)
    assert wallet(101) == 90 and wallet(102) == 90
    table.last_player_activity = time.time() - IDLE_SECONDS
    table.touch("automatic update")
    stamp = table.last_player_activity
    with SessionLocal.begin() as db:
        assert expire_match(service, "old", db, game)
        assert not expire_match(service, "old", db, game)
    assert (
        stamp == table.last_player_activity
        and wallet(101) == 100
        and wallet(102) == 100
    )
    with SessionLocal() as db:
        history = db.query(GameHistory).filter_by(game_type=game).all()
        assert len(history) == 2 and all(
            row.result == "push" and row.amount == 0 for row in history
        )


def test_expired_poker_returns_invested_pot_and_clears_requests(make_user):
    make_user(101, balance=100)
    make_user(102, balance=100)
    table = main.PokerTable("old", 101, "A", small_blind=1, big_blind=2)
    table.players[0]["balance"] = table.players[0]["synced_balance"] = 100
    table.add_player(102, "B", "B", 100)
    assert table.start_hand()
    with SessionLocal.begin() as db:
        main.sync_table_balances(db, table)
    assert wallet(101) + wallet(102) == 197
    table.last_player_activity = time.time() - IDLE_SECONDS
    tables = {"old": table}
    requests = {"old": [{"user_id": 101}]}
    with SessionLocal.begin() as db:
        assert expire_poker(tables, requests, "old", db, main.sync_table_balances)
        assert not expire_poker(tables, requests, "old", db, main.sync_table_balances)
    assert not tables and not requests and wallet(101) == 100 and wallet(102) == 100


def test_expiry_respects_finished_winner_instead_of_refunding(make_user):
    make_user(101, balance=100)
    make_user(102, balance=100)
    table = BilliardsTable("old", 101, "A", {"entry_cents": 1000})
    table.add_player(102, "B")
    for player in table.players:
        player["ready"] = True
    service = BilliardsService()
    service.tables["old"] = table
    with SessionLocal.begin() as db:
        service.start(table, db)
    table.finish([102], "B won")
    table.last_player_activity = time.time() - IDLE_SECONDS
    with SessionLocal.begin() as db:
        assert expire_match(service, "old", db, "billiards")
    assert wallet(101) == 90 and wallet(102) == 110


def test_refund_failure_rolls_back_all_users_and_keeps_durable_table(make_user):
    make_user(101, balance=90)
    table = BilliardsTable("old", 101, "A", {"entry_cents": 1000})
    table.add_player(102, "Missing")
    for player in table.players:
        player["ready"] = True
    table.start()
    table.last_player_activity = time.time() - IDLE_SECONDS
    service = BilliardsService()
    service.tables["old"] = table
    with pytest.raises(ValueError, match="профиль"):
        with SessionLocal.begin() as db:
            expire_match(service, "old", db, "billiards")
    assert "old" in service.tables and not table.match["settled"] and wallet(101) == 90
    with SessionLocal() as db:
        assert db.query(GameHistory).count() == 0


def test_successful_action_updates_activity_but_failed_csrf_and_timers_do_not(
    make_user, login_as
):
    make_user(101)
    client = login_as(101)
    tid = client.post(
        "/api/billiards/tables", json={"rules": {"mode": "practice"}}
    ).json()["table_id"]
    path = f"/api/billiards/table/{tid}"
    game = main.billiards_service.table(tid)
    game.last_player_activity = 10
    persist()
    s = client.get(path + "/state").json()
    assert main.billiards_service.table(tid).last_player_activity == 10
    assert (
        client.post(
            path + "/ready",
            json={"revision": s["revision"], "ready": True},
            headers={"X-ST9-CSRF": "wrong"},
        ).status_code
        == 403
    )
    assert main.billiards_service.table(tid).last_player_activity == 10
    s = client.post(
        path + "/ready", json={"revision": s["revision"], "ready": True}
    ).json()
    assert main.billiards_service.table(tid).last_player_activity > 10
    old = main.billiards_service.table(tid).last_player_activity
    assert (
        client.post(path + "/ready", json={"revision": -1, "ready": False}).status_code
        == 409
    )
    assert main.billiards_service.table(tid).last_player_activity == old


@pytest.mark.parametrize("game", ["poker", "durak", "billiards"])
def test_creator_can_close_idle_other_player_cannot_and_stale_version_rejected(
    game, make_user, login_as
):
    make_user(101)
    make_user(102)
    client = login_as(101)
    tid = (
        client.post("/api/poker/create-table", data={})
        if game == "poker"
        else client.post(f"/api/{game}/tables", json={"rules": {}})
    ).json()["table_id"]
    url = (
        f"/api/poker/table/{tid}"
        if game == "poker"
        else f"/api/{game}/table/{tid}/state"
    )
    state = client.get(url).json()
    assert state["can_close"]
    path = f"/api/game-tables/{game}/{tid}/close"
    client = login_as(102)
    assert (
        client.post(path, json={"version": state["close_version"]}).status_code == 403
    )
    client = login_as(101)
    assert client.post(path, json={"version": "stale"}).status_code == 409
    assert (
        client.post(
            path,
            json={"version": state["close_version"]},
            headers={"X-ST9-CSRF": "wrong"},
        ).status_code
        == 403
    )
    assert (
        client.post(path, json={"version": state["close_version"]}).status_code == 200
    )
    assert wallet(101) == 1000 and wallet(102) == 1000


def test_close_active_game_denied_and_departure_is_normal_forfeit(make_user, login_as):
    make_user(101)
    client = login_as(101)
    tid = client.post(
        "/api/billiards/tables", json={"rules": {"mode": "practice"}}
    ).json()["table_id"]
    path = f"/api/billiards/table/{tid}"
    s = client.get(path + "/state").json()
    s = client.post(
        path + "/ready", json={"revision": s["revision"], "ready": True}
    ).json()
    s = client.post(path + "/start", json={"revision": s["revision"]}).json()
    assert s["can_close_table"] and not s["can_close"]
    assert (
        client.post(
            f"/api/game-tables/billiards/{tid}/close",
            json={"version": s["close_version"]},
        ).status_code
        == 409
    )
    assert (
        client.post(path + "/leave", json={"revision": s["revision"]}).status_code
        == 200
    )
    assert tid not in main.billiards_service.tables


def test_legacy_last_activity_is_captured_once_and_survives_restore():
    game = BilliardsTable("old", 101, "A")
    values = game.to_dict()
    values.pop("last_player_activity")
    values["updated_at"] = 123
    game = BilliardsTable.from_dict(values)
    assert game.last_player_activity == 123
    game.touch("automatic timeout")
    assert BilliardsTable.from_dict(game.to_dict()).last_player_activity == 123


def test_expired_completed_poker_keeps_winnings_and_history(make_user):
    make_user(101, balance=100)
    make_user(102, balance=100)
    table = main.PokerTable("paid", 101, "A", small_blind=1, big_blind=2)
    table.players[0]["balance"] = table.players[0]["synced_balance"] = 100
    table.add_player(102, "B", "B", 100)
    table.start_hand()
    with SessionLocal.begin() as db:
        main.sync_table_balances(db, table)
    table.players[0]["folded"] = True
    table.end_hand()
    with SessionLocal.begin() as db:
        main.sync_table_balances(db, table)
    balances = (wallet(101), wallet(102))
    table.last_player_activity = 1
    with SessionLocal.begin() as db:
        assert expire_poker({"paid": table}, {}, "paid", db, main.sync_table_balances)
    assert (wallet(101), wallet(102)) == balances and sum(balances) == 200
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="poker").count() == 2


def test_corrupt_bank_keeps_table_instead_of_losing_or_creating_money(make_user):
    make_user(101, balance=100)
    make_user(102, balance=100)
    game = BilliardsTable("corrupt", 101, "A", {"entry_cents": 1000})
    game.add_player(102, "B")
    for player in game.players:
        player["ready"] = True
    service = BilliardsService()
    service.tables[game.id] = game
    with SessionLocal.begin() as db:
        service.start(game, db)
    game.last_player_activity = 1
    game.match["bank_cents"] += 1
    with pytest.raises(ValueError, match="Банк"):
        with SessionLocal.begin() as db:
            service.tick_table(game.id, db)
    assert game.id in service.tables and wallet(101) == 90 and wallet(102) == 90


def test_automatic_ticks_and_restart_pause_preserve_activity(make_user):
    make_user(101)
    game = BilliardsTable("practice", 101, "A", {"mode": "practice"})
    game.players[0]["ready"] = True
    game.start()
    game.last_player_activity = time.time() - 100
    old = game.last_player_activity
    game.timeout(game.deadline + 1)
    assert game.last_player_activity == old
    main.billiards_service.tables[game.id] = game
    payload = main.serialize_runtime()
    main.restore_runtime(payload)
    assert main.billiards_service.table(game.id).last_player_activity == old
