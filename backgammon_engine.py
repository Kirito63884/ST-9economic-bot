"""Rules for long narde and short backgammon. No HTTP, wallets or client RNG.

Points are zero based. White travels 23 -> 0 in short backgammon; black
travels 0 -> 23. In long narde both travel down modulo 24, starting at
23 and 11. A move uses exactly one die, including intermediate landings.
"""

from dataclasses import dataclass
from functools import lru_cache
import copy
import json
import secrets


class BackgammonError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def roll_dice():
    return [secrets.randbelow(6) + 1, secrets.randbelow(6) + 1]


def checked_dice(values):
    if (
        not isinstance(values, list)
        or len(values) != 2
        or any(type(v) is not int or not 1 <= v <= 6 for v in values)
    ):
        raise BackgammonError("Некорректные кости", 409)
    return values


@dataclass(frozen=True)
class Position:
    board: tuple
    bar: tuple = (0, 0)
    off: tuple = (0, 0)

    @classmethod
    def initial(cls, variant):
        board = [0] * 24
        if variant == "long":
            board[23], board[11] = 15, -15
        elif variant == "short":
            for p, n in (
                (23, 2),
                (12, 5),
                (7, 3),
                (5, 5),
                (0, -2),
                (11, -5),
                (16, -3),
                (18, -5),
            ):
                board[p] = n
        else:
            raise BackgammonError("Выберите длинные или короткие нарды")
        return cls(tuple(board))


def progress(point, side, variant):
    if variant == "long":
        return ((23 if side == 0 else 11) - point) % 24
    return 23 - point if side == 0 else point


def point_at(index, side, variant):
    if variant == "long":
        return ((23 if side == 0 else 11) - index) % 24
    return 23 - index if side == 0 else index


def owns(value, side):
    return value > 0 if side == 0 else value < 0


def moved(pos, side, move):
    source, target, _ = move
    sign = 1 if side == 0 else -1
    board, bar, off = list(pos.board), list(pos.bar), list(pos.off)
    if source == -1:
        bar[side] -= 1
    else:
        board[source] -= sign
    if target == 24:
        off[side] += 1
    else:
        if board[target] == -sign:
            board[target] = 0
            bar[1 - side] += 1
        board[target] += sign
    return Position(tuple(board), tuple(bar), tuple(off))


def illegal_prime(pos, side):
    """A six-prime needs an opponent ahead on their route, or already off.

    A route ends at bearing off; it does not wrap past the opponent's head.
    Check every length-six window, including windows inside a longer prime.
    """
    opponent = 1 - side
    if pos.off[opponent]:
        return False
    route = [pos.board[point_at(i, opponent, "long")] for i in range(24)]
    for start in range(19):
        if all(owns(v, side) for v in route[start : start + 6]):
            if not any(owns(v, opponent) for v in route[start + 6 :]):
                return True
    return False


def single_moves(pos, side, die, variant, head_used, head_limit):
    sign = 1 if side == 0 else -1
    sources = (
        [-1]
        if pos.bar[side]
        else [i for i, count in enumerate(pos.board) if owns(count, side)]
    )
    furthest = min((progress(i, side, variant) for i in sources if i >= 0), default=-1)
    home = not pos.bar[side] and furthest >= 18
    for source in sources:
        index = -1 if source == -1 else progress(source, side, variant)
        if variant == "long" and index == 0 and head_used >= head_limit:
            continue
        destination = index + die
        if destination >= 24:
            if not home or (destination > 24 and index != furthest):
                continue
            target = 24
        else:
            target = point_at(destination, side, variant)
            blockers = -sign * pos.board[target]
            if blockers >= (1 if variant == "long" else 2):
                continue
        move = (source, target, die)
        result = moved(pos, side, move)
        if variant == "long" and illegal_prime(result, side):
            continue
        yield move, result, head_used + (variant == "long" and index == 0)


def legal_moves(pos, side, dice, variant, head_used=0, head_limit=1):
    return list(
        _legal_moves(pos, side, tuple(sorted(dice)), variant, head_used, head_limit)
    )


@lru_cache(maxsize=8192)
def _legal_moves(pos, side, dice, variant, head_used, head_limit):
    """Only prefixes of turns using the maximum possible number of dice.

    Caching suffix lengths avoids enumerating tens of thousands of equivalent
    double sequences. The larger-die rule applies when only one die is usable.
    """

    @lru_cache(maxsize=None)
    def best(position, remaining, heads):
        if position.off[side] == 15:
            return 0
        maximum = 0
        for die in set(remaining):
            rest = list(remaining)
            rest.remove(die)
            for _, next_pos, next_heads in single_moves(
                position, side, die, variant, heads, head_limit
            ):
                maximum = max(maximum, 1 + best(next_pos, tuple(rest), next_heads))
        return maximum

    remaining = tuple(sorted(dice))
    maximum = best(pos, remaining, head_used)
    choices = []
    for die in set(remaining):
        rest = list(remaining)
        rest.remove(die)
        for move, next_pos, heads in single_moves(
            pos, side, die, variant, head_used, head_limit
        ):
            if 1 + best(next_pos, tuple(rest), heads) == maximum:
                choices.append(move)
    if maximum == 1 and choices:
        higher = max(m[2] for m in choices)
        choices = [m for m in choices if m[2] == higher]
    return tuple(sorted(choices))


class Round:
    """Replayable round. Opening dice and subsequent rolls are server generated."""

    def __init__(self, variant, opening):
        self.variant = variant
        self.opening = list(checked_dice(opening))
        if opening[0] == opening[1]:
            raise BackgammonError("При равных костях жеребьёвка повторяется", 409)
        self.pos = Position.initial(variant)
        self.turn = 0 if opening[0] > opening[1] else 1
        self.dice = list(opening) if variant == "short" else []
        self.last_roll = list(opening) if variant == "short" else []
        self.turns = [0, 0]
        self.head_used = 0
        self.winner = None
        self.history = []
        self.last_move = None
        self.last_pass = None

    def head_limit(self):
        return (
            2
            if (
                self.turns[self.turn] == 0
                and len(self.last_roll) == 2
                and self.last_roll[0] == self.last_roll[1]
                and self.last_roll[0] in (3, 4, 6)
            )
            else 1
        )

    def legal(self):
        if self.winner is not None:
            return []
        return legal_moves(
            self.pos,
            self.turn,
            self.dice,
            self.variant,
            self.head_used,
            self.head_limit(),
        )

    def advance_if_done(self):
        if self.pos.off[self.turn] == 15:
            self.winner = self.turn
        elif not self.legal():
            self.last_pass = {"side": self.turn, "unused": list(self.dice)}
            self.turns[self.turn] += 1
            self.turn = 1 - self.turn
            self.head_used = 0
            self.dice = []

    def roll(self, values):
        if self.dice or self.winner is not None:
            raise BackgammonError("Кости уже брошены", 409)
        self.last_roll = list(checked_dice(values))
        self.dice = list(values) * (2 if values[0] == values[1] else 1)
        self.history.append({"roll": list(values)})
        self.advance_if_done()

    def move(self, move):
        if (
            not isinstance(move, (list, tuple))
            or len(move) != 3
            or any(type(v) is not int for v in move)
            or tuple(move) not in self.legal()
        ):
            raise BackgammonError(
                "Ход запрещён: используйте подсвеченные пункты и доступную кость", 409
            )
        self.pos = moved(self.pos, self.turn, move)
        if self.variant == "long" and progress(move[0], self.turn, self.variant) == 0:
            self.head_used += 1
        self.dice.remove(move[2])
        self.last_move = {"side": self.turn, "move": list(move)}
        self.history.append({"move": list(move)})
        self.advance_if_done()

    def quality(self):
        if self.winner is None:
            return None
        loser = 1 - self.winner
        if self.pos.off[loser]:
            return "Оин — обычная победа"
        if self.variant == "short" and (
            self.pos.bar[loser]
            or any(
                owns(v, loser) and progress(i, self.winner, self.variant) >= 18
                for i, v in enumerate(self.pos.board)
            )
        ):
            return "Кокс — бэкгэммон"
        return "Марс — гэммон" if self.variant == "short" else "Марс"

    def to_dict(self):
        data = copy.deepcopy(self.__dict__)
        data["pos"] = {
            "board": list(self.pos.board),
            "bar": list(self.pos.bar),
            "off": list(self.pos.off),
        }
        return data

    @classmethod
    def from_dict(cls, data):
        # The exact snapshot is the cache key. Callers receive an independent
        # object, so playing a move cannot mutate a previously verified snapshot.
        return copy.deepcopy(_verified_round(json.dumps(data, sort_keys=True)))

    @classmethod
    def _replay(cls, data):
        try:
            obj = cls(data["variant"], data["opening"])
            for action in data["history"]:
                if set(action) == {"roll"}:
                    obj.roll(action["roll"])
                elif set(action) == {"move"}:
                    obj.move(action["move"])
                else:
                    raise ValueError("Неизвестное событие")
            if obj.to_dict() != data:
                raise ValueError("Позиция не совпадает с историей")
            return obj
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise BackgammonError("Повреждена сохранённая партия в нарды", 409) from exc


@lru_cache(maxsize=64)
def _verified_round(snapshot):
    return Round._replay(json.loads(snapshot))
