import copy
import json
import random

import pytest

from backgammon_engine import (
    BackgammonError,
    Position,
    Round,
    illegal_prime,
    legal_moves,
    moved,
    point_at,
)


def position(white, black=None, bar=(0, 0)):
    board = [0] * 24
    for point, count in white.items():
        board[point] = count
    for point, count in (black or {}).items():
        assert not board[point]
        board[point] = -count
    return Position(
        tuple(board),
        tuple(bar),
        (15 - sum(white.values()) - bar[0], 15 - sum((black or {}).values()) - bar[1]),
    )


def test_opening_direction_and_initial_counts():
    short = Round("short", [2, 5])
    assert short.turn == 1 and short.dice == [2, 5]
    assert short.pos.board[0] == -2 and short.pos.board[23] == 2
    long = Round("long", [6, 1])
    assert long.turn == 0 and not long.dice
    assert long.pos.board[23] == 15 and long.pos.board[11] == -15
    long.roll([1, 2])
    assert set(long.legal()) == {(23, 22, 1), (23, 21, 2)}
    with pytest.raises(BackgammonError):
        Round("short", [3, 3])


def test_maximum_dice_and_larger_die_rule():
    pos = position({6: 1}, {0: 2, 4: 2})
    assert legal_moves(pos, 0, [1, 2], "short") == [(6, 5, 1)]
    pos = position({0: 1})
    assert legal_moves(pos, 0, [1, 6], "short") == [(0, 24, 6)]
    # A superficially legal move may waste the other die: reject it.
    pos = position({2: 1}, {0: 2})
    assert legal_moves(pos, 0, [1, 2], "short") == [(2, 1, 1)]


def test_bar_priority_hit_and_blocked_entry():
    pos = position({5: 13}, {23: 2, 22: 1, 18: 12}, bar=(2, 0))
    moves = legal_moves(pos, 0, [1, 2], "short")
    assert moves == [(-1, 22, 2)]
    after = moved(pos, 0, moves[0])
    assert after.bar == (1, 1) and after.board[22] == 1
    assert legal_moves(after, 0, [1], "short") == []
    pos = position({5: 13}, {18: 15}, bar=(2, 0))
    assert all(m[0] == -1 for m in legal_moves(pos, 0, [1, 2], "short"))
    pos = position({18: 13}, {0: 1, 5: 14}, bar=(0, 2))
    assert (-1, 0, 1) in legal_moves(pos, 1, [1, 2], "short")


def test_bear_off_requires_home_exact_or_farthest_and_bar_empty():
    pos = position({5: 1, 2: 1})
    choices = legal_moves(pos, 0, [6], "short")
    assert (5, 24, 6) in choices and (2, 24, 6) not in choices
    assert (2, 24, 3) in legal_moves(pos, 0, [3], "short")
    assert not any(
        m[1] == 24 for m in legal_moves(position({6: 1, 0: 1}), 0, [1], "short")
    )
    assert all(
        m[0] == -1 for m in legal_moves(position({0: 1}, bar=(1, 0)), 0, [1], "short")
    )
    # Black's home and oversize bearing off are mirrored correctly.
    assert legal_moves(position({}, {23: 1}), 1, [6], "short") == [(23, 24, 6)]
    assert legal_moves(position({}, {12: 1}), 1, [6], "long") == [(12, 24, 6)]


@pytest.mark.parametrize(
    "die,uses,heads", [(1, 4, 1), (2, 4, 1), (3, 4, 2), (4, 4, 2), (5, 4, 1), (6, 2, 2)]
)
def test_long_first_double_head_exceptions(die, uses, heads):
    game = Round("long", [6, 1])
    game.roll([die, die])
    while game.turn == 0:
        choices = game.legal()
        assert choices
        game.move(choices[0])
    assert len([e for e in game.history if "move" in e]) == uses
    assert game.pos.board[23] == 15 - heads
    # The exception is each player's first personal turn, including second player.
    game.roll([6, 6])
    while game.turn == 1:
        game.move(game.legal()[0])
    assert game.pos.board[11] == -13


def test_long_no_hitting_and_one_head_after_first_turn():
    pos = Position.initial("long")
    assert not legal_moves(pos, 0, [6], "long", 1, 1)
    pos = position({12: 1, 23: 14}, {11: 15})
    assert (12, 11, 1) not in legal_moves(pos, 0, [1], "long")


def test_long_six_prime_requires_opponent_ahead():
    def make(opponent_index, off=0):
        own = {point_at(i, 1, "long"): 1 for i in range(5, 11)}
        own[22] = 9
        return position(own, {point_at(opponent_index, 1, "long"): 15 - off})

    assert illegal_prime(make(2), 0)
    assert not illegal_prime(make(12), 0)
    assert not illegal_prime(make(2, 1), 0)
    # A route stops at bearing off: don't join its end to the head.
    own = {point_at(i, 1, "long"): 1 for i in (22, 23, 0, 1, 2, 3)}
    own[23] = own.get(23, 0) + 9
    assert not illegal_prime(position(own, {point_at(10, 1, "long"): 15}), 0)


def test_blocked_roll_passes_without_reroll_or_client_skip():
    game = Round("short", [6, 1])
    game.pos = position({5: 14}, {23: 2, 22: 2}, bar=(1, 0))
    game.dice = []
    game.roll([1, 2])
    assert game.turn == 1 and game.dice == []
    assert game.last_pass == {"side": 0, "unused": [1, 2]}
    with pytest.raises(BackgammonError):
        game.move([-1, 23, 1])


def test_replay_round_trip_and_tampered_state_rejected():
    game = Round("short", [6, 1])
    game.move(game.legal()[0])
    saved = json.loads(json.dumps(game.to_dict()))
    assert Round.from_dict(saved).to_dict() == saved
    corrupted = copy.deepcopy(saved)
    corrupted["pos"]["off"][0] = 15
    with pytest.raises(BackgammonError):
        Round.from_dict(corrupted)
    corrupted = copy.deepcopy(saved)
    corrupted["dice"] = [6, 6, 6, 6]
    with pytest.raises(BackgammonError):
        Round.from_dict(corrupted)
    before = game.to_dict()
    with pytest.raises(BackgammonError):
        game.move([True, 1, 6])
    assert game.to_dict() == before
    with pytest.raises(BackgammonError):
        game.roll([6, 6])


@pytest.mark.parametrize("variant", ["long", "short"])
def test_complete_seeded_games_conserve_checkers_and_replay(variant):
    rng = random.Random(471 if variant == "long" else 513)
    game = Round(variant, [6, 1])
    for _ in range(2400):
        if game.winner is not None:
            break
        if not game.dice:
            game.roll([rng.randint(1, 6), rng.randint(1, 6)])
        else:
            game.move(rng.choice(game.legal()))
        for side, sign in ((0, 1), (1, -1)):
            assert (
                sum(abs(v) for v in game.pos.board if v * sign > 0)
                + game.pos.bar[side]
                + game.pos.off[side]
                == 15
            )
        if variant == "long":
            assert game.pos.bar == (0, 0)
    assert game.winner is not None
    assert game.pos.off[game.winner] == 15
    assert Round.from_dict(game.to_dict()).to_dict() == game.to_dict()


@pytest.mark.parametrize(
    "variant,loser_off,bar,expected",
    [
        ("long", 1, 0, "Оин"),
        ("long", 0, 0, "Марс"),
        ("short", 1, 0, "Оин"),
        ("short", 0, 1, "Кокс"),
        ("short", 0, 0, "Марс"),
    ],
)
def test_win_classification(variant, loser_off, bar, expected):
    game = Round(variant, [6, 1])
    game.pos = position({}, {12: 15 - loser_off - bar}, bar=(0, bar))
    game.winner = 0
    assert game.quality().startswith(expected)


def test_verified_snapshot_cache_returns_independent_objects():
    game = Round("short", [6, 1])
    saved = game.to_dict()
    first = Round.from_dict(saved)
    first.move(first.legal()[0])
    second = Round.from_dict(saved)
    assert second.to_dict() == saved and second.to_dict() != first.to_dict()
