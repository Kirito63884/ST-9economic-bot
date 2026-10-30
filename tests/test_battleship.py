"""Fleet geometry, private turn state, equal stakes and once-only settlement."""

import copy
from datetime import datetime, timezone
import json
import time

import pytest

import main
from battleship import (
    BattleshipError,
    BattleshipTable,
    FLEET,
    random_fleet,
    validate_fleet,
)
from battleship_routes import BattleshipService, MAX_CENTS
from conftest import session_cookie, wallet
from database import AppRuntime, GameHistory, SessionLocal, User
from table_lifecycle import IDLE_SECONDS

LAYOUT = [
    [0, 1, 2, 3],
    [5, 6, 7],
    [20, 21, 22],
    [24, 25],
    [40, 41],
    [43, 44],
    [60],
    [63],
    [66],
    [69],
]


def game(entry=0):
    table = BattleshipTable("test", 101, "Первый", {"entry_cents": entry})
    table.add_player(102, "Второй")
    for player in table.players:
        table.set_fleet(player["id"], copy.deepcopy(LAYOUT))
        player["ready"] = True
    table.start()
    table.actor = 101
    return table


def save():
    with SessionLocal.begin() as db:
        db.get(AppRuntime, 1).payload = main.serialize_runtime()


def request(client, path, endpoint, **payload):
    state = client.get(path + "/state")
    assert state.status_code == 200, state.text
    response = client.post(
        path + "/" + endpoint, json={"revision": state.json()["revision"], **payload}
    )
    assert response.status_code == 200, response.text
    return response.json()


def room(make_user, login_as, entry=1000, balances=(100, 100), private=False):
    make_user(101, balance=balances[0])
    make_user(102, balance=balances[1])
    client = login_as(101)
    response = client.post(
        "/api/battleship/tables",
        json={"rules": {"entry_cents": entry, "private": private}},
    )
    assert response.status_code == 200, response.text
    tid = response.json()["table_id"]
    path = "/api/battleship/table/" + tid
    client.cookies.set("session", session_cookie(102))
    assert client.post(path + "/join").status_code == 200
    return client, path, tid


def prepare(client, path):
    for uid in (101, 102):
        client.cookies.set("session", session_cookie(uid))
        request(client, path, "fleet", fleet=copy.deepcopy(LAYOUT))
        request(client, path, "ready", ready=True)
    client.cookies.set("session", session_cookie(101))


def test_random_fleets_are_complete_straight_and_separated():
    for _ in range(100):
        fleet = random_fleet()
        assert sorted(map(len, fleet)) == sorted(FLEET)
        assert len(set(cell for ship in fleet for cell in ship)) == 20
        for index, ship in enumerate(fleet):
            assert all(0 <= cell < 100 for cell in ship)
            assert (
                len({cell // 10 for cell in ship}) == 1
                or len({cell % 10 for cell in ship}) == 1
            )
            for other in fleet[index + 1 :]:
                assert all(
                    max(abs(a // 10 - b // 10), abs(a % 10 - b % 10)) > 1
                    for a in ship
                    for b in other
                )


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "extra",
        "duplicate",
        "wrap",
        "bend",
        "gap",
        "touch",
        "diagonal",
        "outside",
        "bool",
        "string",
        "wrong-counts",
    ],
)
def test_invalid_layouts_rejected_without_changing_saved_fleet(kind):
    table = BattleshipTable("t", 101, "A")
    table.set_fleet(101, copy.deepcopy(LAYOUT))
    invalid = copy.deepcopy(LAYOUT)
    if kind == "missing":
        invalid.pop()
    elif kind == "extra":
        invalid.append([90])
    elif kind == "duplicate":
        invalid[0] = [0, 1, 2, 2]
    elif kind == "wrap":
        invalid[0] = [8, 9, 10, 11]
    elif kind == "bend":
        invalid[1] = [5, 6, 16]
    elif kind == "gap":
        invalid[1] = [5, 6, 8]
    elif kind == "touch":
        invalid[-1] = [67]
    elif kind == "diagonal":
        invalid[-1] = [55]
    elif kind == "outside":
        invalid[-1] = [100]
    elif kind == "bool":
        invalid[-1] = [True]
    elif kind == "string":
        invalid[-1] = ["69"]
    else:
        invalid[0] = [0]
    before = table.to_dict()
    with pytest.raises(BattleshipError):
        table.set_fleet(101, invalid)
    assert table.to_dict() == before


@pytest.mark.parametrize(
    "rules",
    [
        None,
        [],
        False,
        {"players": 3},
        {"entry_cents": True},
        {"entry_cents": -1},
        {"entry_cents": 1.5},
        {"entry_cents": float("inf")},
        {"private": 1},
        {"turn_seconds": True},
        {"turn_seconds": 1},
    ],
)
def test_settings_strict_types(rules):
    # Explicit null API settings are tested separately; omitted engine rules are defaults.
    if rules is None:
        assert BattleshipTable("t", 101, "A").rules["turn_seconds"] == 60
        return
    with pytest.raises(BattleshipError):
        BattleshipTable("t", 101, "A", rules)


def test_exactly_two_fleets_ready_and_first_player_is_server_random(monkeypatch):
    table = BattleshipTable("t", 101, "A")
    table.set_fleet(101, copy.deepcopy(LAYOUT))
    table.player(101)["ready"] = True
    with pytest.raises(BattleshipError):
        table.start()
    table.add_player(102, "B")
    assert not table.player(101)["ready"]
    with pytest.raises(BattleshipError):
        table.add_player(103, "C")
    for player in table.players:
        player["ready"] = True
    before = table.to_dict()
    with pytest.raises(BattleshipError):
        table.start()
    assert table.to_dict() == before
    table.set_fleet(102, copy.deepcopy(LAYOUT))
    table.player(102)["ready"] = True
    monkeypatch.setattr("battleship.secrets.choice", lambda players: players[-1])
    table.start()
    assert table.actor == 102 and table.match["first_player"] == 102
    assert table.match["bank_cents"] == 2000
    with pytest.raises(BattleshipError):
        table.set_fleet(101, copy.deepcopy(LAYOUT))


def test_hit_sunk_retains_turn_miss_passes_and_known_water_cannot_be_shot():
    table = game()
    for cell in (0, 1, 2):
        table.fire(101, cell)
        assert table.actor == 101 and table.last_shot["result"] == "hit"
    table.fire(101, 3)
    assert table.actor == 101 and table.last_shot["result"] == "sunk"
    enemy = table.public_state(101)["boards"][1]
    assert "sunk_ships" not in enemy
    assert {mark["cell"] for mark in enemy["marks"] if mark["result"] == "sunk"} == {
        0,
        1,
        2,
        3,
    }
    assert any(
        mark["cell"] == 4 and mark["result"] == "water" for mark in enemy["marks"]
    )
    for cell in (0, 4, 10, 14):
        before = table.to_dict()
        with pytest.raises(BattleshipError):
            table.fire(101, cell)
        assert table.to_dict() == before
    table.fire(101, 90)
    assert table.actor == 102 and table.last_shot["result"] == "miss"


def test_privacy_during_placement_battle_and_after_finish():
    table = game()
    for viewer in (101, 102):
        state = table.public_state(viewer)
        own = next(b for b in state["boards"] if b["id"] == viewer)
        opponent = next(b for b in state["boards"] if b["id"] != viewer)
        assert own["fleet"] == validate_fleet(copy.deepcopy(LAYOUT))
        assert "fleet" not in opponent and "sunk_ships" not in opponent
        assert all(
            "fleet" not in player and "incoming" not in player
            for player in state["players"]
        )
        assert state["match"]["result"] is None
        enemy_player = next(
            player for player in state["players"] if player["id"] != viewer
        )
        assert "ships_left" not in enemy_player and "decks_left" not in enemy_player
    table.fire(101, 0)
    assert "sunk_ships" not in table.public_state(101)["boards"][1]
    for cell in [cell for ship in LAYOUT for cell in ship if cell != 0]:
        table.fire(101, cell)
    assert table.phase == "finished" and table.match["result"]["winner"] == 101
    assert all(
        ("fleet" in b) == (b["id"] == 101) for b in table.public_state(101)["boards"]
    )
    before = table.to_dict()
    with pytest.raises(BattleshipError):
        table.fire(101, 90)
    assert table.to_dict() == before


@pytest.mark.parametrize("cell", [True, False, -1, 100, 2.0, "2", None, [], {}])
def test_invalid_shots_do_not_change_game(cell):
    table = game()
    before = table.to_dict()
    with pytest.raises(BattleshipError):
        table.fire(101, cell)
    assert table.to_dict() == before


def test_late_wrong_turn_timeout_pass_reset_and_forfeit():
    table = game()
    before = table.to_dict()
    with pytest.raises(BattleshipError):
        table.fire(102, 0)
    assert table.to_dict() == before
    table.deadline = 1
    before = table.to_dict()
    with pytest.raises(BattleshipError):
        table.fire(101, 0)
    assert table.to_dict() == before
    table.tick()
    assert table.actor == 102 and table.player(101)["timeouts"] == 1
    assert all(not p["incoming"] for p in table.players)
    table.deadline = 1
    table.tick()
    table.fire(101, 0)
    assert table.player(101)["timeouts"] == 0
    for _ in range(3):
        table.actor, table.deadline = 101, 1
        table.tick()
    assert table.phase == "finished" and table.match["result"]["winner"] == 102


def test_serialization_rematch_settings_and_creator_transfer():
    table = game()
    restored = BattleshipTable.from_dict(json.loads(json.dumps(table.to_dict())))
    restored.fire(101, 0)
    assert not table.player(102)["incoming"]
    restored.leave(101)
    assert restored.creator == 102 and restored.match["result"]["winner"] == 102
    with pytest.raises(BattleshipError):
        restored.rematch()
    restored.match["settled"] = True
    restored.rematch()
    assert restored.phase == "waiting" and len(restored.players) == 1
    assert all(
        not p["fleet"] and not p["incoming"] and not p["ready"]
        for p in restored.players
    )
    restored.add_player(103, "C")
    restored.player(102)["ready"] = True
    restored.change_settings({"entry_cents": 1})
    assert all(not p["ready"] for p in restored.players)


def test_equal_stake_limit_only_participants_rechecks_and_freezes_after_debit(
    make_user, login_as
):
    client, path, tid = room(make_user, login_as, entry=10000, balances=(100, 50))
    make_user(103, balance=0)
    make_user(104, balance=-200)
    state = client.get(path + "/state").json()
    assert state["stake_limit_cents"] == 5000 and not state["stake_valid"]
    request(client, path, "fleet", fleet=copy.deepcopy(LAYOUT))
    bad = client.post(
        path + "/ready",
        json={
            "revision": client.get(path + "/state").json()["revision"],
            "ready": True,
        },
    )
    assert bad.status_code == 409
    client.cookies.set("session", session_cookie(101))
    request(client, path, "settings", rules={"entry_cents": 5000})
    prepare(client, path)
    assert wallet(101) == 100 and wallet(102) == 50
    state = request(client, path, "start")
    assert wallet(101) == 50 and wallet(102) == 0
    assert state["match"]["bank_cents"] == 10000
    assert state["stake_limit_cents"] == 5000 and state["stake_valid"]
    # A stake is fixed once escrowed; the zero balance after debit does not cancel it.
    request(client, path, "leave")
    assert wallet(101) == 50 and wallet(102) == 100
    table = main.battleship_service.table(tid)
    with SessionLocal.begin() as db:
        main.battleship_service.settle(table, db)
        main.battleship_service.settle(table, db)
    assert wallet(102) == 100
    with SessionLocal() as db:
        rows = db.query(GameHistory).filter_by(game_type="battleship").all()
        assert len(rows) == 2 and sorted(row.amount for row in rows) == [-50, 50]


def test_balance_changes_after_ready_block_start_without_partial_charge(
    make_user, login_as
):
    client, path, _ = room(make_user, login_as, entry=1000)
    prepare(client, path)
    with SessionLocal.begin() as db:
        db.get(User, 102).balance = 5
    state = client.get(path + "/state").json()
    response = client.post(path + "/start", json={"revision": state["revision"]})
    assert response.status_code == 409
    assert wallet(101) == 100 and wallet(102) == 5
    assert main.battleship_service.table(path.rsplit("/", 1)[1]).phase == "waiting"


def test_negative_and_zero_balances_and_precise_cent_limit(make_user, login_as):
    make_user(101, balance=-1)
    client = login_as(101)
    assert client.get("/api/battleship/limits").json() == {
        "max_entry_cents": 0,
        "can_play": False,
    }
    assert (
        client.post(
            "/api/battleship/tables", json={"rules": {"entry_cents": 0}}
        ).status_code
        == 409
    )
    with SessionLocal.begin() as db:
        db.get(User, 101).balance = 1.01
    assert (
        client.post(
            "/api/battleship/tables", json={"rules": {"entry_cents": 102}}
        ).status_code
        == 409
    )
    with SessionLocal.begin() as db:
        db.get(User, 101).balance = 0
    assert (
        client.post(
            "/api/battleship/tables", json={"rules": {"entry_cents": 0}}
        ).status_code
        == 200
    )


@pytest.mark.parametrize("rules", [None, False, []])
def test_api_rejects_non_object_rules_without_creating_a_table(
    make_user, login_as, rules
):
    make_user(101)
    response = login_as(101).post("/api/battleship/tables", json={"rules": rules})
    assert response.status_code == 400
    assert not main.battleship_service.tables and wallet(101) == 1000


def test_api_secrets_auth_csrf_fields_replay_and_final_once_only_payout(
    make_user, login_as
):
    client, path, tid = room(make_user, login_as, entry=1, private=True)
    make_user(103, is_admin=True, is_chief_admin=True)
    client.cookies.set("session", session_cookie(103))
    assert (
        client.get(path + "/state").status_code == 403
    )  # Even chief cannot see fleets.
    client.cookies.clear()
    assert client.get("/api/battleship/tables").status_code == 401
    client.cookies.set("session", session_cookie(102))
    prepare(client, path)
    before_start = client.get(path + "/state").json()["revision"]
    state = request(client, path, "start")
    assert (
        client.post(path + "/start", json={"revision": before_start}).status_code == 409
    )
    assert wallet(101) == wallet(102) == 99.99
    actor = state["actor"]
    other = 102 if actor == 101 else 101
    client.cookies.set("session", session_cookie(actor))
    state = client.get(path + "/state").json()
    assert "fleet" not in next(b for b in state["boards"] if b["id"] == other)
    assert client.get(path + "/state").headers["cache-control"] == "private, no-store"
    payload = {"revision": state["revision"], "cell": 0}
    assert (
        client.post(
            path + "/fire", json=payload, headers={"X-ST9-CSRF": "wrong"}
        ).status_code
        == 403
    )
    assert (
        client.post(path + "/fire", json=payload | {"winner": actor}).status_code == 400
    )
    assert client.post(path + "/fire", json=payload).status_code == 200
    assert client.post(path + "/fire", json=payload).status_code == 409
    for cell in [cell for ship in LAYOUT for cell in ship if cell != 0]:
        request(client, path, "fire", cell=cell)
    state = client.get(path + "/state").json()
    assert state["match"]["settled"] and state["match"]["result"]["winner"] == actor
    assert wallet(actor) == 100.01 and wallet(other) == 99.99
    client.get(path + "/state")
    assert wallet(actor) == 100.01
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="battleship").count() == 2
    assert all(("fleet" in b) == (b["id"] == actor) for b in state["boards"])
    assert main.battleship_service.table(tid).phase == "finished"


def test_private_room_listing_and_fleet_edit_does_not_reveal_coordinates(
    make_user, login_as
):
    make_user(101)
    make_user(102)
    client = login_as(101)
    tid = client.post(
        "/api/battleship/tables", json={"rules": {"private": True}}
    ).json()["table_id"]
    path = "/api/battleship/table/" + tid
    request(client, path, "fleet", fleet=copy.deepcopy(LAYOUT))
    client.cookies.set("session", session_cookie(102))
    assert not client.get("/api/battleship/tables").json()["tables"]
    assert "fleet" not in json.dumps(client.get(path + "/preview").json())
    assert client.post(path + "/join").status_code == 200
    state = client.get(path + "/state").json()
    assert next(b for b in state["boards"] if b["id"] == 101) == {
        "id": 101,
        "marks": [],
    }
    assert (
        client.post(
            path + "/fleet",
            json={"revision": state["revision"], "fleet": LAYOUT, "random": True},
        ).status_code
        == 400
    )
    assert (
        client.post(
            path + "/fleet", json={"revision": state["revision"], "random": 1}
        ).status_code
        == 400
    )


def test_idle_refund_pause_restore_membership_activity_and_owner_close(
    make_user, login_as
):
    client, path, tid = room(make_user, login_as)
    prepare(client, path)
    state = request(client, path, "start")
    table = main.battleship_service.table(tid)
    assert (
        client.post(
            f"/api/game-tables/battleship/{tid}/close",
            json={"version": state["close_version"]},
        ).status_code
        == 409
    )
    original = table.last_player_activity
    client.get(path + "/state")
    assert main.battleship_service.table(tid).last_player_activity == original
    # Successful firing is human activity; polling is not.
    actor = state["actor"]
    client.cookies.set("session", session_cookie(actor))
    request(client, path, "fire", cell=0)
    assert main.battleship_service.table(tid).last_player_activity >= original
    table = main.battleship_service.table(tid)
    table.last_player_activity = time.time() - IDLE_SECONDS
    old = table.last_player_activity
    service = BattleshipService()
    service.restore({tid: table.to_dict()}, pause=100)
    assert service.table(tid).last_player_activity == old
    assert service.table(tid).deadline == table.deadline + 100
    save()
    response = client.post(path + "/fire", json={"revision": table.revision, "cell": 1})
    assert response.status_code == 409  # Expired tables cannot be revived by a POST.
    with SessionLocal.begin() as db:
        main.battleship_service.tick_table(tid, db)
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    assert tid not in main.battleship_service.tables
    assert wallet(101) == wallet(102) == 100
    with SessionLocal() as db:
        assert (
            db.query(GameHistory)
            .filter_by(game_type="battleship", result="push")
            .count()
            == 2
        )
    client.cookies.set("session", session_cookie(101))
    tid = client.post("/api/battleship/tables", json={"rules": {}}).json()["table_id"]
    state = client.get(f"/api/battleship/table/{tid}/state").json()
    assert (
        client.post(
            f"/api/game-tables/battleship/{tid}/close",
            json={"version": state["close_version"]},
        ).status_code
        == 200
    )


def test_corrupt_bank_or_impossible_payout_keeps_unsettled_game(make_user):
    make_user(101)
    make_user(102)
    table, service = game(entry=1), BattleshipService()
    table.leave(102)
    table.match["bank_cents"] = 3
    with SessionLocal.begin() as db:
        with pytest.raises(BattleshipError):
            service.settle(table, db)
    assert not table.match["settled"] and wallet(101) == 1000
    table.match["bank_cents"] = 2
    with SessionLocal.begin() as db:
        db.get(User, 101).balance = MAX_CENTS / 100
    with SessionLocal.begin() as db:
        with pytest.raises(BattleshipError):
            service.settle(table, db)
    assert not table.match["settled"]


def test_failed_final_payout_rolls_back_shot_and_can_be_retried_after_wallet_fix(
    make_user, login_as
):
    client, path, tid = room(make_user, login_as)
    prepare(client, path)
    request(client, path, "start")
    table = main.battleship_service.table(tid)
    table.actor = 101
    last = LAYOUT[-1][0]
    table.player(102)["incoming"] = {
        str(cell): {"by": 101, "hit": True}
        for ship in LAYOUT
        for cell in ship
        if cell != last
    }
    save()
    with SessionLocal.begin() as db:
        db.get(User, 101).balance = MAX_CENTS / 100
    state = client.get(path + "/state").json()
    payload = {"revision": state["revision"], "cell": last}
    response = client.post(path + "/fire", json=payload)
    assert response.status_code == 409
    table = main.battleship_service.table(tid)
    assert table.phase == "playing" and not table.match["result"]
    assert str(last) not in table.player(102)["incoming"]
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="battleship").count() == 0
    assert wallet(102) == 90
    with SessionLocal.begin() as db:
        db.get(User, 101).balance = 90
    response = client.post(path + "/fire", json=payload)
    assert response.status_code == 200, response.text
    assert wallet(101) == 110 and wallet(102) == 90


def test_free_battle_records_result_without_changing_wallets(make_user, login_as):
    client, path, _ = room(make_user, login_as, entry=0, balances=(0, 100))
    prepare(client, path)
    state = request(client, path, "start")
    assert state["match"]["bank_cents"] == 0
    request(client, path, "leave")
    assert wallet(101) == 0 and wallet(102) == 100
    with SessionLocal() as db:
        rows = db.query(GameHistory).filter_by(game_type="battleship").all()
        assert len(rows) == 2 and all(row.amount == 0 for row in rows)
        assert {row.result for row in rows} == {"won", "lost"}


def test_archive_detach_settles_and_public_activity_hides_invite(make_user, login_as):
    client, path, tid = room(make_user, login_as, private=True)
    prepare(client, path)
    request(client, path, "start")
    with SessionLocal.begin() as db:
        for uid in (101, 102):
            db.get(User, uid).last_seen = (
                datetime.now(timezone.utc).astimezone().replace(tzinfo=None)
            )
    from home_activity import activity

    with SessionLocal() as db:
        data = activity(
            db,
            {},
            {},
            {},
            main.durak_service,
            main.billiards_service,
            main.domino_service,
            main.battleship_service,
        )
    group = next(
        group for group in data["groups"] if group["key"] == "battleship-private"
    )
    assert group["url"] == "/games/battleship/lobby" and tid not in json.dumps(data)
    with SessionLocal.begin() as db:
        main.detach_user_from_games(db, db.get(User, 101))
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    assert wallet(101) == 90 and wallet(102) == 110
    from audit_logs import GAMES, TYPES

    assert "battleship" in GAMES and TYPES["battleship"] == "Морской бой"


def test_rendered_table_and_lobby_scripts_parse(make_user, login_as):
    import subprocess
    from test_frontend_scripts import Scripts

    make_user(101, display_name="Игрок <script>alert(1)</script>")
    client = login_as(101)
    tid = client.post("/api/battleship/tables", json={"rules": {}}).json()["table_id"]
    for route in ("/games/battleship/lobby", "/games/battleship/table/" + tid):
        response = client.get(route)
        assert response.status_code == 200
        parser = Scripts()
        parser.feed(response.text)
        for script in parser.blocks:
            result = subprocess.run(
                ["node", "--check"], input=script, text=True, capture_output=True
            )
            assert result.returncode == 0, result.stderr
