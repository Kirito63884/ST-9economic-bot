"""Classic draw rules, hidden state, complete games and transactional payouts."""

import json
import random
import time

import pytest
import main
from domino import DominoError, DominoTable, TILES, pips, values
from domino_routes import DominoService
from database import AppRuntime, GameHistory, SessionLocal, User
from table_lifecycle import IDLE_SECONDS
from conftest import wallet


def game(players=2, seed=1, entry=0):
    table = DominoTable(
        "test", 101, "Alice", {"max_players": players, "entry_cents": entry}
    )
    for index in range(1, players):
        table.add_player(101 + index, "Player " + str(index))
    for player in table.players:
        player["ready"] = True
    deck = list(TILES)
    random.Random(seed).shuffle(deck)
    table.start(deck)
    return table


def save():
    with SessionLocal.begin() as db:
        db.get(AppRuntime, 1).payload = main.serialize_runtime()


def assert_conservation(table):
    all_tiles = (
        table.stock
        + [b["id"] for b in table.chain]
        + [tile for p in table.players for tile in p["hand"]]
    )
    assert len(all_tiles) == 28 and set(all_tiles) == set(TILES)
    assert all(a["right"] == b["left"] for a, b in zip(table.chain, table.chain[1:]))
    assert all(
        sorted((b["left"], b["right"])) == list(values(b["id"])) for b in table.chain
    )


@pytest.mark.parametrize("players,count,stock", [(2, 7, 14), (3, 5, 13), (4, 5, 8)])
def test_deal_opening_and_private_view(players, count, stock):
    table = game(players)
    assert (
        all(len(p["hand"]) == count for p in table.players)
        and len(table.stock) == stock
    )
    doubles = [
        tile
        for p in table.players
        for tile in p["hand"]
        if values(tile)[0] == values(tile)[1]
    ]
    assert table.opening_tile == max(doubles, key=lambda t: values(t)[0])
    assert table.legal(table.actor) == {table.opening_tile: ["right"]}
    state = table.public_state(101)
    assert state["hand"] == table.player(101)["hand"]
    assert "stock" not in state and all("hand" not in p for p in state["players"])
    assert state["match"]["result"] is None
    assert_conservation(table)


def test_no_doubles_dealt_opens_highest_sum():
    nondoubles = [tile for tile in TILES if values(tile)[0] != values(tile)[1]]
    deck = nondoubles + [tile for tile in TILES if tile not in nondoubles]
    table = DominoTable("x", 101, "A", {"entry_cents": 0})
    table.add_player(102, "B")
    for player in table.players:
        player["ready"] = True
    table.start(deck)
    assert table.opening_tile == max(
        [t for p in table.players for t in p["hand"]],
        key=lambda t: (sum(values(t)), max(values(t))),
    )


def test_orientation_both_ends_and_reject_illegal_without_mutation():
    table = game()
    table.chain = [{"id": "2-4", "left": 2, "right": 4, "by": 102}]
    table.actor = 101
    table.player(101)["hand"] = ["1-2", "3-4", "5-5"]
    assert table.legal(101) == {"1-2": ["left"], "3-4": ["right"]}
    before = table.to_dict()
    for args in [
        ("play", "5-5", "right"),
        ("play", "1-2", "right"),
        ("play", "6-6", "left"),
        ("play", [], {}),
        ("pass", None, None),
        ("draw", None, None),
    ]:
        with pytest.raises(DominoError):
            table.action(101, *args)
        assert table.to_dict() == before
    table.action(101, "play", "1-2", "left")
    assert table.chain[0]["left"] == 1 and table.chain[0]["right"] == 2
    table.actor = 101
    table.action(101, "play", "3-4", "right")
    assert table.chain[-1]["left"] == 4 and table.chain[-1]["right"] == 3


def test_draw_until_first_playable_then_same_player_chooses_end():
    table = game()
    table.actor = 101
    table.chain = [{"id": "2-2", "left": 2, "right": 2, "by": 102}]
    table.player(101)["hand"] = ["1-1"]
    table.stock = ["2-6", "0-0", "3-3"]
    table.action(101, "draw")
    assert table.actor == 101 and table.player(101)["hand"] == [
        "1-1",
        "3-3",
        "0-0",
        "2-6",
    ]
    assert table.legal(101) == {"2-6": ["left", "right"]} and not table.stock
    assert table.public_state(102)["hand"] == table.player(102)["hand"]
    assert "2-6" not in table.events[-1]["text"]


def test_pass_only_without_move_and_without_stock():
    table = game()
    table.actor = 101
    table.chain = [{"id": "2-2", "left": 2, "right": 2, "by": 102}]
    table.player(101)["hand"] = ["1-1"]
    table.player(102)["hand"] = ["1-2"]
    table.stock = ["3-3"]
    with pytest.raises(DominoError):
        table.action(101, "pass")
    table.stock = []
    table.action(101, "pass")
    assert table.actor == 102 and table.phase == "playing"


@pytest.mark.parametrize(
    "hands,winners", [(["0-0", "1-1"], [101]), (["1-1", "0-2"], [101, 102])]
)
def test_fish_uses_smallest_remaining_pip_sum(hands, winners):
    table = game()
    table.chain = [{"id": "6-6", "left": 6, "right": 6, "by": 101}]
    table.stock = []
    for player, tile in zip(table.players, hands):
        player["hand"] = [tile]
    assert table.blocked() and table.match["result"]["winners"] == winners
    assert "Рыба" in table.match["result"]["reason"]
    assert [row["pips"] for row in table.match["result"]["hands"]] == [
        pips([tile]) for tile in hands
    ]


def test_final_bone_wins_immediately_and_cannot_replay():
    table = game()
    table.chain = [{"id": "1-2", "left": 1, "right": 2, "by": 102}]
    table.actor = 101
    table.player(101)["hand"] = ["2-3"]
    table.action(101, "play", "2-3", "right")
    assert table.phase == "finished" and table.match["result"]["winners"] == [101]
    with pytest.raises(DominoError):
        table.action(101, "play", "2-3", "right")


@pytest.mark.parametrize("players", [2, 3, 4])
def test_complete_seeded_games_keep_every_tile_and_restore(players):
    for seed in range(20):
        table = game(players, seed)
        for move in range(300):
            assert_conservation(table)
            if table.phase == "finished":
                break
            legal = table.legal(table.actor)
            if legal:
                tile = list(legal)[seed % len(legal)]
                side = legal[tile][-1 if move % 2 else 0]
                table.action(table.actor, "play", tile, side)
            elif table.stock:
                table.action(table.actor, "draw")
            else:
                table.action(table.actor, "pass")
            if move % 5 == 0:
                restored = DominoTable.from_dict(
                    json.loads(json.dumps(table.to_dict()))
                )
                assert restored.to_dict() == table.to_dict()
                table = restored
        else:
            pytest.fail("Game did not finish")
        assert table.match["result"]["winners"]
        assert_conservation(table)


def test_timeout_automatic_moves_three_consecutive_timeouts_forfeit():
    table = game()
    uid = table.actor
    for _ in range(3):
        table.actor = uid
        table.deadline = 1
        table.tick()
    assert table.player(uid)["left"] and table.phase == "finished"
    assert table.match["result"]["winners"] == [
        p["id"] for p in table.players if p["id"] != uid
    ]
    table = game()
    uid = table.actor
    table.player(uid)["timeouts"] = 2
    table.action(uid, "play", table.opening_tile, "right")
    assert table.player(uid)["timeouts"] == 0


def test_leave_advances_in_seat_order_and_transfers_creator():
    table = game(4)
    table.actor = 102
    table.leave(102)
    assert table.actor == 103 and table.player(102)["left"]
    table.leave(101)
    assert table.creator == 103


@pytest.mark.parametrize(
    "rules",
    [
        [],
        False,
        {"max_players": True},
        {"max_players": 5},
        {"entry_cents": -1},
        {"entry_cents": float("inf")},
        {"private": 1},
        {"turn_seconds": 0},
        {"variant": "kozol"},
    ],
)
def test_invalid_settings_rejected(rules):
    with pytest.raises(DominoError):
        DominoTable("x", 101, "A", rules)


def test_expired_and_wrong_player_actions_rejected_before_mutation():
    table = game()
    before = table.to_dict()
    with pytest.raises(DominoError):
        table.action(
            table.next_player(table.actor), "play", table.opening_tile, "right"
        )
    assert table.to_dict() == before
    table.deadline = 1
    before = table.to_dict()
    with pytest.raises(DominoError, match="истекло"):
        table.action(table.actor, "play", table.opening_tile, "right")
    assert table.to_dict() == before


def test_wallet_split_odd_cent_no_duplicates_and_history(make_user):
    for uid in (101, 102, 103):
        make_user(uid, balance=100)
    table = game(3, entry=1)
    table.match["result"] = {
        "winners": [101, 102],
        "reason": "Рыба",
        "payouts": {},
        "hands": [],
    }
    table.phase = "finished"
    service = DominoService()
    with SessionLocal.begin() as db:
        for uid in (101, 102, 103):
            db.get(User, uid).balance = 99.99
        service.settle(table, db)
        service.settle(table, db)
    assert [wallet(uid) for uid in (101, 102, 103)] == [100.01, 100, 99.99]
    assert table.match["result"]["payouts"] == {"101": 2, "102": 1}
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="domino").count() == 3


def test_http_flow_privacy_csrf_stale_repeat_and_wallet(make_user, login_as):
    make_user(101, balance=100)
    make_user(102, balance=100)
    client = login_as(101)
    assert client.get("/games/domino/lobby").status_code == 200
    tid = client.post(
        "/api/domino/tables", json={"rules": {"entry_cents": 1000, "private": True}}
    ).json()["table_id"]
    path = f"/api/domino/table/{tid}"
    client = login_as(102)
    assert all(
        t["id"] != tid for t in client.get("/api/domino/tables").json()["tables"]
    )
    assert client.get(path + "/state").status_code == 403
    assert client.post(path + "/join").status_code == 200
    s = client.get(path + "/state").json()
    s = client.post(
        path + "/ready", json={"revision": s["revision"], "ready": True}
    ).json()
    client = login_as(101)
    s = client.get(path + "/state").json()
    s = client.post(
        path + "/ready", json={"revision": s["revision"], "ready": True}
    ).json()
    assert wallet(101) == 100 and wallet(102) == 100
    s = client.post(path + "/start", json={"revision": s["revision"]}).json()
    assert wallet(101) == 90 and wallet(102) == 90
    assert "stock" not in s and all("hand" not in player for player in s["players"])
    response = client.get(path + "/state")
    assert response.headers["cache-control"] == "private, no-store"
    table = main.domino_service.table(tid)
    table.chain = [{"id": "1-2", "left": 1, "right": 2, "by": 102}]
    table.actor = 101
    table.player(101)["hand"] = ["2-3"]
    save()
    s = client.get(path + "/state").json()
    payload = {
        "revision": s["revision"],
        "kind": "play",
        "tile": "2-3",
        "side": "right",
    }
    assert (
        client.post(
            path + "/action", json=payload, headers={"X-ST9-CSRF": "wrong"}
        ).status_code
        == 403
    )
    assert (
        client.post(path + "/action", json=payload | {"stock": []}).status_code == 400
    )
    done = client.post(path + "/action", json=payload)
    assert done.status_code == 200, done.text
    assert done.json()["match"]["result"]["winners"] == [101]
    assert wallet(101) == 110 and wallet(102) == 90
    assert client.post(path + "/action", json=payload).status_code == 409
    client.get(path + "/state")
    assert wallet(101) == 110


def test_refund_on_eight_hours_and_owner_close_integration(make_user, login_as):
    make_user(101, balance=100)
    make_user(102, balance=100)
    table = DominoTable("old", 101, "A", {"entry_cents": 1000})
    table.add_player(102, "B")
    for player in table.players:
        player["ready"] = True
    main.domino_service.tables[table.id] = table
    with SessionLocal.begin() as db:
        main.domino_service.start(table, db)
    table.last_player_activity = time.time() - IDLE_SECONDS
    save()
    with SessionLocal.begin() as db:
        main.domino_service.tick_table("old", db)
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    assert (
        "old" not in main.domino_service.tables
        and wallet(101) == 100
        and wallet(102) == 100
    )
    client = login_as(101)
    tid = client.post("/api/domino/tables", json={"rules": {}}).json()["table_id"]
    s = client.get(f"/api/domino/table/{tid}/state").json()
    assert s["can_close"]
    assert (
        client.post(
            f"/api/game-tables/domino/{tid}/close", json={"version": s["close_version"]}
        ).status_code
        == 200
    )


def test_activity_persistence_archive_detach_and_game_filter(make_user, login_as):
    make_user(101)
    make_user(102)
    client = login_as(101)
    tid = client.post("/api/domino/tables", json={"rules": {}}).json()["table_id"]
    path = f"/api/domino/table/{tid}"
    table = main.domino_service.table(tid)
    table.last_player_activity = 10
    save()
    s = client.get(path + "/state").json()
    assert main.domino_service.table(tid).last_player_activity == 10
    client.post(path + "/ready", json={"revision": s["revision"], "ready": True})
    assert main.domino_service.table(tid).last_player_activity > 10
    before = main.serialize_runtime()
    main.restore_runtime(before)
    assert tid in main.domino_service.tables
    with SessionLocal.begin() as db:
        main.detach_user_from_games(db, db.get(User, 101))
    assert tid not in main.domino_service.tables
    from audit_logs import GAMES, TYPES

    assert "domino" in GAMES and TYPES["domino"] == "Домино"


@pytest.mark.parametrize("rules", [None, False, []])
def test_api_rejects_non_object_rules(make_user, login_as, rules):
    make_user(101)
    response = login_as(101).post("/api/domino/tables", json={"rules": rules})
    assert response.status_code == 400
    assert not main.domino_service.tables


def test_anonymous_denial_and_invalid_body(client):
    assert client.get("/api/domino/tables").status_code == 401
    assert client.post("/api/domino/tables", json={"rules": {}}).status_code == 401
    assert client.get("/games/domino/lobby", follow_redirects=False).status_code == 307


def test_table_scripts_parse_and_private_home_activity_hides_invite(
    make_user, login_as
):
    import subprocess
    from datetime import datetime, timezone
    from test_frontend_scripts import Scripts
    from home_activity import activity

    make_user(
        101,
        display_name="O'Brien <script>alert(1)</script>",
        last_seen=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    make_user(102, last_seen=datetime.now(timezone.utc).replace(tzinfo=None))
    client = login_as(101)
    tid = client.post("/api/domino/tables", json={"rules": {"private": True}}).json()[
        "table_id"
    ]
    response = client.get("/games/domino/table/" + tid)
    assert response.status_code == 200
    scripts = Scripts()
    scripts.feed(response.text)
    for script in scripts.blocks:
        result = subprocess.run(
            ["node", "--check"], input=script, text=True, capture_output=True
        )
        assert result.returncode == 0, result.stderr
    table = main.domino_service.table(tid)
    table.add_player(102, "B")
    for player in table.players:
        player["ready"] = True
    table.start()
    with SessionLocal() as db:
        result = activity(
            db,
            {},
            {},
            {},
            main.durak_service,
            main.billiards_service,
            main.domino_service,
        )
    row = next(group for group in result["groups"] if group["key"] == "domino-private")
    assert row["url"] == "/games/domino/lobby" and tid not in json.dumps(result)
    assert "hand" not in row and "stock" not in row
