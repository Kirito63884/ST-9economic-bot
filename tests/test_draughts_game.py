"""Russian-specific rules, including positions where other variants differ."""

import copy

import pytest

from draughts_game import (
    DraughtsError,
    DraughtsTable,
    initial_board,
    steps,
    position_key,
    endgame_mode,
)


def game(board=None, side="white"):
    table = DraughtsTable("rules", 101, "Белые")
    table.add_player(102, "Чёрные")
    for p in table.players:
        p["ready"] = True
    table.start()
    if board is not None:
        table.position = board.copy()
        table.turn = side
        table.actor = 101 if side == "white" else 102
        table.positions = {position_key(board, side): 1}
        mode = endgame_mode(board)
        table.endgame = mode | {"moves": 0} if mode else None
    return table


def move(table, start, end):
    table.action(table.actor, "move", {"from": start, "to": end})


def test_initial_board_and_quiet_forward_moves():
    board = initial_board()
    assert len(board) == 24
    assert sum(p == "w" for p in board.values()) == 12
    assert board["a1"] == "w" and board["h8"] == "b"
    legal = steps(board, "white")
    assert len(legal) == 7
    assert all(
        m["from"][1] == "3" and m["to"][1] == "4" and not m["capture"] for m in legal
    )
    table = game()
    before = table.to_dict()
    with pytest.raises(DraughtsError):
        move(table, "a3", "b2")
    assert table.to_dict() == before
    move(table, "a3", "b4")
    assert table.actor == 102 and table.position["b4"] == "w"


def test_backward_capture_is_mandatory_for_every_piece():
    table = game({"c3": "w", "b2": "b", "g3": "w", "h8": "b"})
    assert steps(table.position, "white") == [
        {"from": "c3", "to": "a1", "capture": "b2"}
    ]
    with pytest.raises(DraughtsError):
        move(table, "g3", "f4")
    move(table, "c3", "a1")
    assert "b2" not in table.position and table.position["a1"] == "w"


def test_chain_is_forced_persisted_and_captured_pieces_block_until_completion():
    table = game({"a1": "W", "c3": "b", "f6": "b", "h2": "b", "a7": "w"})
    move(table, "a1", "d4")
    assert table.actor == 101 and table.pending["captured"] == ["c3"]
    assert table.position["c3"] == "b" and len(table.history) == 0
    restored = DraughtsTable.from_dict(table.to_dict())
    assert restored.to_dict() == table.to_dict()
    for invalid in [("a7", "b8"), ("d4", "b2")]:
        before = restored.to_dict()
        with pytest.raises(DraughtsError):
            move(restored, *invalid)
        assert restored.to_dict() == before
    move(restored, "d4", "g7")
    assert restored.pending is None and restored.actor == 102
    assert "c3" not in restored.position and "f6" not in restored.position
    assert restored.history[-1]["captured"] == ["c3", "f6"]


def test_king_must_land_where_same_capture_can_continue():
    board = {"a1": "W", "c3": "b", "f4": "b"}
    legal = steps(board, "white")
    # Behind c3, only e5 can then jump f4; d4/f6/g7/h8 cannot stop early.
    assert legal == [{"from": "a1", "to": "e5", "capture": "c3"}]


def test_no_maximum_capture_rule_or_king_priority():
    board = {"c3": "w", "b4": "b", "d4": "B", "f6": "b", "h8": "b"}
    assert {m["to"] for m in steps(board, "white")} == {"a5", "e5"}
    table = game(board)
    move(table, "c3", "a5")
    assert table.actor == 102 and table.history[-1]["captured"] == ["b4"]
    assert "d4" in table.position and "f6" in table.position


def test_immediate_promotion_then_flying_capture_in_same_turn():
    table = game({"b6": "w", "c7": "b", "f6": "b", "h2": "b"})
    move(table, "b6", "d8")
    assert table.position["d8"] == "W" and table.pending["promoted"]
    assert table.actor == 101
    assert {m["to"] for m in steps(table.position, "white", table.pending)} == {
        "g5",
        "h4",
    }
    move(table, "d8", "h4")
    assert table.position["h4"] == "W" and table.actor == 102
    assert table.history[-1]["path"] == ["b6", "d8", "h4"]


def test_flying_king_and_blockers_cannot_jump_two_enemies_or_own_piece():
    assert len(steps({"a1": "W"}, "white")) == 7
    assert steps({"a1": "W", "b2": "b", "c3": "b"}, "white") == []
    assert all(m["from"] != "a1" for m in steps({"a1": "W", "b2": "w"}, "white"))
    pending = {"at": "e5", "captured": ["d4"]}
    assert not steps({"e5": "W", "d4": "b", "b2": "b"}, "white", pending)


def test_capture_can_revisit_its_start_square():
    table = game({"c3": "W", "d4": "b", "f4": "b", "f2": "b", "d2": "b", "a7": "b"})
    for start, end in [("c3", "e5"), ("e5", "g3"), ("g3", "e1"), ("e1", "c3")]:
        move(table, start, end)
    assert table.history[-1]["path"] == ["c3", "e5", "g3", "e1", "c3"]
    assert len(table.history[-1]["captured"]) == 4 and table.position["c3"] == "W"


@pytest.mark.parametrize(
    "board,start,end",
    [({"a1": "w", "b2": "b"}, "a1", "c3"), ({"a5": "w", "a1": "b"}, "a5", "b6")],
)
def test_win_by_capture_or_blocking_all_legal_moves(board, start, end):
    table = game(board)
    move(table, start, end)
    assert table.phase == "finished" and table.match["result"]["winners"] == [101]


def test_promotion_on_quiet_move():
    table = game({"a7": "w", "h6": "b"})
    move(table, "a7", "b8")
    assert table.position["b8"] == "W" and table.history[-1]["promoted"]


def test_threefold_same_turn_and_draw_offer_lifecycle():
    table = game({"a1": "W", "h6": "B", "a7": "w", "h2": "b"})
    # Artificially prime the exact upcoming position twice; counters are durable.
    expected = {"b2": "W", "h6": "B", "a7": "w", "h2": "b"}
    table.positions[position_key(expected, "black")] = 2
    move(table, "a1", "b2")
    assert (
        table.phase == "finished" and "трёхкратное" in table.match["result"]["reason"]
    )
    table = game()
    with pytest.raises(DraughtsError):
        table.action(102, "offer_draw")
    table.action(101, "offer_draw")
    with pytest.raises(DraughtsError):
        table.action(101, "accept_draw")
    table.action(102, "accept_draw")
    assert table.match["result"]["winners"] == []


@pytest.mark.parametrize("count,plies", [(4, 60), (5, 60), (6, 120), (7, 120)])
def test_material_draw_limits_count_total_pieces_not_per_side(count, plies):
    board = {
        "a1": "W",
        "h8": "B",
        "a3": "w",
        "h6": "b",
        "c3": "w",
        "f6": "b",
        "e3": "w",
    }
    board = dict(list(board.items())[:count])
    table = game(board)
    table.stable_material_plies = plies - 1
    move(table, "a3", "b4")
    assert table.phase == "finished" and "фигурном" in table.match["result"]["reason"]


def test_fifteen_king_moves_and_short_endgame_limits():
    table = game(
        {
            "a1": "W",
            "c1": "w",
            "e1": "w",
            "g1": "w",
            "b8": "B",
            "d8": "b",
            "f8": "b",
            "h8": "b",
        }
    )
    table.king_quiet_plies = 29
    move(table, "a1", "b2")
    assert (
        table.phase == "finished"
        and "15 ходов дамками" in table.match["result"]["reason"]
    )
    table = game({"a1": "W", "c5": "W", "h6": "B"})
    table.endgame["moves"] = 4
    move(table, "c5", "b4")
    assert table.phase == "finished" and "лимит 5" in table.match["result"]["reason"]


def test_surrender_during_capture_and_rematch_swaps_colours():
    table = game({"a1": "W", "c3": "b", "f6": "b", "h2": "b"})
    move(table, "a1", "d4")
    table.action(101, "resign")
    table.match["settled"] = True
    table.rematch()
    assert (
        table.players[0]["id"] == 102
        and table.public_state(101)["your_side"] == "black"
    )
    assert table.pending is None and table.position == initial_board()
    assert all(not p["ready"] for p in table.players)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"from": "a1", "to": "b2", "winner": 101},
        {"from": True, "to": "b2"},
        {"from": "a2", "to": "b3"},
    ],
)
def test_bad_moves_cannot_mutate_state(payload):
    table = game()
    before = copy.deepcopy(table.to_dict())
    with pytest.raises(DraughtsError):
        table.action(101, "move", payload)
    assert table.to_dict() == before


def test_promotion_does_not_restart_an_already_active_five_move_limit():
    table = game({"c5": "W", "a7": "w", "h6": "B"})
    assert table.endgame["limit"] == 5
    table.endgame["moves"] = 4
    move(table, "a7", "b8")
    assert table.phase == "finished" and "лимит 5" in table.match["result"]["reason"]
