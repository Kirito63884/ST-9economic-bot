"""Captures, suicide, ko cycles, area scoring and replayed consensus."""

import copy
import json
import random

import pytest

from go_engine import GoError, GoPosition, area_score, fingerprint, group_at
from go_game import GoTable


def seeded(stones, color=1, size=9):
    p = GoPosition(size)
    for point, value in stones.items():
        p.board[point] = value
    p.to_move = color
    p.seen = {fingerprint(p.board)}
    return p


def ko_stones(row, col, swap=False, size=9):
    black = [(row - 1, col), (row, col - 1), (row + 1, col)]
    white = [(row, col), (row - 1, col + 1), (row + 1, col + 1), (row, col + 2)]
    result = {r * size + c: 1 if not swap else 2 for r, c in black}
    result.update({r * size + c: 2 if not swap else 1 for r, c in white})
    return result


def table(monkeypatch, size=9):
    monkeypatch.setattr("go_game.secrets.randbelow", lambda _: 0)
    t = GoTable("go", 1, "Чёрные", {"size": size})
    t.add_player(2, "Белые")
    for p in t.players:
        p["ready"] = True
    t.start()
    return t


@pytest.mark.parametrize("size", [9, 13, 19])
def test_empty_board_sizes_and_starting_turn(size):
    p = GoPosition(size)
    assert len(p.board) == size * size and len(p.legal_points()) == size * size
    assert p.to_move == 1 and area_score(p.board)["neutral"] == size * size
    assert area_score(p.board)["white_half"] == 15


def test_diagonals_do_not_connect_and_liberties_are_unique():
    p = seeded({0: 1, 1: 1, 10: 1, 20: 1})
    group, liberties = group_at(p.board, 0, 9)
    assert group == {0, 1, 10} and len(liberties) == 4
    assert 20 not in group and 0 not in liberties


def test_capture_single_and_multiple_adjacent_groups_before_suicide():
    # The placed black stone initially has no empty neighbour, but captures two whites.
    p = seeded({10: 2, 12: 2, 19: 1, 1: 1, 9: 1, 3: 1, 13: 1, 21: 1})
    assert p.play(11) == [10, 12]
    assert p.board[11] == 1 and p.board[10] == p.board[12] == 0
    assert p.captures == {"black": 2, "white": 0}
    assert group_at(p.board, 11, 9)[1]


def test_capture_entire_connected_group_and_edge():
    p = seeded({0: 2, 1: 2, 9: 1, 10: 1})
    assert p.play(2) == [0, 1]
    assert p.board[:3] == [0, 0, 1]


def test_suicide_single_or_connected_is_atomic():
    for stones, point in [({1: 2, 9: 2}, 0), ({0: 1, 1: 2, 10: 2, 18: 2}, 9)]:
        p = seeded(stones)
        before = copy.deepcopy(p.__dict__)
        with pytest.raises(GoError, match="Самоубий"):
            p.play(point)
        assert p.__dict__ == before


def test_simple_ko_and_pass_does_not_erase_superko():
    p = seeded(ko_stones(2, 2))
    p.play(21)
    before = copy.deepcopy(p.__dict__)
    with pytest.raises(GoError, match="суперко"):
        p.play(20)
    assert p.__dict__ == before
    p.pass_turn()
    p.pass_turn()
    p.resume()
    with pytest.raises(GoError, match="суперко"):
        p.play(20)


def test_superko_rejects_a_long_triple_ko_cycle():
    stones = {}
    stones.update(ko_stones(2, 2))
    stones.update(ko_stones(2, 6, True))
    stones.update(ko_stones(6, 2))
    p = seeded(stones)
    for point in (21, 25, 57, 20, 24):
        p.play(point)
    before = copy.deepcopy(p.__dict__)
    with pytest.raises(GoError, match="суперко"):
        p.play(56)
    assert p.__dict__ == before


def test_snapback_is_legal_when_entire_board_does_not_repeat():
    stones = {11: 1, 19: 1, 29: 1, 20: 2, 12: 2, 30: 2, 22: 1, 13: 2, 31: 2, 23: 2}
    p = seeded(stones)
    assert p.play(21) == [20]
    assert p.play(20) == [21, 22]
    assert p.board[20] == 2


def test_two_eyes_prohibit_an_opponent_suicide():
    stones = {}
    for r in range(5):
        for c in range(7):
            if r in (0, 4) or c in (0, 6):
                stones[r * 9 + c] = 2
            elif (r, c) not in ((2, 2), (2, 4)):
                stones[r * 9 + c] = 1
    p = seeded(stones, color=2)
    assert group_at(p.board, 10, 9)[1] == {20, 22}
    for eye in (20, 22):
        with pytest.raises(GoError, match="Самоубий"):
            p.play(eye)


def test_area_counts_stones_territory_komi_and_shared_neutral():
    p = seeded({0: 2, 31: 1, 39: 1, 41: 1, 49: 1})
    score = area_score(p.board)
    assert score["territory"] == {"black": 1, "white": 0}
    assert score["stones"] == {"black": 4, "white": 1}
    assert score["black_half"] == 10 and score["white_half"] == 17
    assert score["neutral"] == 75
    assert (
        sum(score["stones"].values())
        + sum(score["territory"].values())
        + score["neutral"]
        == 81
    )


def test_dead_group_removal_is_a_scoring_copy_not_extra_prisoner_points():
    p = seeded({0: 2, 40: 2, 31: 1, 49: 1, 39: 1, 32: 1, 50: 1, 42: 1})
    before = p.board[:]
    score = area_score(p.board, [40])
    assert score["stones"]["black"] == 6 and score["territory"]["black"] == 2
    assert score["black_half"] == 16 and score["board"][40] == 0
    assert p.board == before


def test_two_passes_only_enter_scoring_and_both_confirm_same_marks(monkeypatch):
    t = table(monkeypatch)
    for uid, point in ((1, 0), (2, 80), (1, 1), (2, 79)):
        t.action(uid, "play", {"point": point})
    t.action(1, "pass")
    t.action(2, "pass")
    assert t.phase == "scoring" and t.match["result"] is None
    t.action(1, "confirm_score")
    assert t.score_accepts == [1] and t.phase == "scoring"
    t.action(2, "mark_dead", {"point": 0})
    assert t.dead == [0, 1] and not t.score_accepts
    t.action(2, "confirm_score")
    restored = GoTable.from_dict(json.loads(json.dumps(t.to_dict())))
    assert restored.score_accepts == [2] and restored.dead == [0, 1]
    restored.action(1, "confirm_score")
    assert restored.phase == "finished" and restored.match["result"]["score"][
        "dead"
    ] == [0, 1]


def test_disputed_scoring_resumes_original_board_queue_and_history(monkeypatch):
    t = table(monkeypatch)
    for uid, point in ((1, 0), (2, 80)):
        t.action(uid, "play", {"point": point})
    board = t.position.board[:]
    seen = t.position.seen.copy()
    t.action(1, "pass")
    t.action(2, "pass")
    t.action(1, "mark_dead", {"point": 0})
    t.action(1, "confirm_score")
    t.action(2, "resume")
    assert t.phase == "playing" and t.actor == 1 and t.position.passes == 0
    assert (
        t.position.board == board
        and t.position.seen == seen
        and t.dead == t.score_accepts == []
    )
    assert (
        GoTable.from_dict(json.loads(json.dumps(t.to_dict()))).position.board == board
    )


def test_forged_actions_out_of_turn_and_stale_result_are_rejected(monkeypatch):
    t = table(monkeypatch)
    for uid, kind, data in (
        (2, "play", {"point": 0}),
        (1, "play", {"point": True}),
        (1, "play", {"point": 81}),
        (1, "play", {"point": 0, "winner": 1}),
        (1, "confirm_score", {}),
        (3, "resign", {}),
    ):
        before = t.to_dict()
        with pytest.raises(GoError):
            t.action(uid, kind, data)
        assert t.to_dict() == before
    t.action(1, "play", {"point": 0})
    before = t.to_dict()
    with pytest.raises(GoError):
        t.action(2, "play", {"point": 0})
    assert t.to_dict() == before


@pytest.mark.parametrize("size", [9, 13, 19])
def test_seeded_play_and_restore_reproduce_every_position(size):
    rng = random.Random(size)
    p = GoPosition(size)
    for turn in range(100):
        legal = p.legal_points()
        if not legal:
            break
        p.play(rng.choice(legal))
        if turn % 17 == 0:
            p = GoPosition.from_dict(json.loads(json.dumps(p.to_dict())))
    restored = GoPosition.from_dict(json.loads(json.dumps(p.to_dict())))
    assert restored.to_dict() == p.to_dict() and restored.seen == p.seen
    broken = p.to_dict()
    broken["board"][0] = (broken["board"][0] + 1) % 3
    with pytest.raises(GoError):
        GoPosition.from_dict(broken)


def test_corrupt_restore_cannot_change_komi_consent_winner_or_board_types(monkeypatch):
    t = table(monkeypatch)
    t.action(1, "pass")
    t.action(2, "pass")
    t.action(1, "confirm_score")
    t.action(2, "confirm_score")
    for field, value in (("winner", [1]), ("consent", [1]), ("score", 99)):
        data = t.to_dict()
        if field == "winner":
            data["match"]["result"]["winners"] = value
        elif field == "consent":
            data["score_accepts"] = value
        else:
            data["match"]["result"]["score"]["komi_half"] = value
        with pytest.raises(GoError):
            GoTable.from_dict(data)
    data = GoPosition(9).to_dict()
    data["board"][0] = False
    with pytest.raises(GoError):
        GoPosition.from_dict(data)


@pytest.mark.parametrize(
    "rules",
    [
        {"size": 7},
        {"size": True},
        {"size": "19"},
        {"entry_cents": -1},
        {"entry_cents": True},
        {"entry_cents": 1.1},
        {"private": "false"},
        {"komi_half": 0},
    ],
)
def test_invalid_unagreed_rules_are_rejected(rules):
    with pytest.raises(GoError):
        GoTable("invalid", 1, "Player", rules)
