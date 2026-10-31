"""OGS/KGS Chinese-like Go: area, positional superko, no suicide, no handicap.

The pure board engine has no wallets, clocks, HTTP or automatic life/death solver.
Positions use row-major intersections: 0 empty, 1 black, 2 white.
"""

from __future__ import annotations

import hashlib
import math

KOMI_HALF = 15  # 7.5 points, separate from all Bishopcoin amounts.


class GoError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def board_size(board):
    if not isinstance(board, list):
        raise GoError("Повреждена доска Го", 409)
    size = math.isqrt(len(board))
    if (
        size < 2
        or size * size != len(board)
        or any(type(p) is not int or p not in (0, 1, 2) for p in board)
    ):
        raise GoError("Повреждена доска Го", 409)
    return size


def point_id(point, size):
    if type(point) is not int or not 0 <= point < size * size:
        raise GoError("Выберите пересечение линий на доске")
    return point


def neighbors(point, size):
    row, col = divmod(point, size)
    if row:
        yield point - size
    if row + 1 < size:
        yield point + size
    if col:
        yield point - 1
    if col + 1 < size:
        yield point + 1


def group_at(board, point, size):
    """Return the entire orthogonally connected group and its unique liberties."""
    color = board[point]
    if not color:
        return set(), set()
    group, liberties, todo = {point}, set(), [point]
    while todo:
        for other in neighbors(todo.pop(), size):
            if board[other] == 0:
                liberties.add(other)
            elif board[other] == color and other not in group:
                group.add(other)
                todo.append(other)
    return group, liberties


def fingerprint(board):
    return hashlib.sha256(bytes(board)).hexdigest()


def played(board, point, color, size, previous=()):
    """Compute first; failed captures, suicide and superko never mutate the board."""
    point_id(point, size)
    if board[point]:
        raise GoError("Пересечение уже занято", 409)
    candidate = board.copy()
    candidate[point] = color
    captured, inspected = set(), set()
    # Capture opponents BEFORE checking the placed stone's own liberties.
    for other in neighbors(point, size):
        if candidate[other] != 3 - color or other in inspected:
            continue
        group, liberties = group_at(candidate, other, size)
        inspected.update(group)
        if not liberties:
            captured.update(group)
    for other in captured:
        candidate[other] = 0
    if not group_at(candidate, point, size)[1]:
        raise GoError("Самоубийственный ход запрещён", 409)
    key = fingerprint(candidate)
    if key in previous:
        raise GoError("Ко / суперко: нельзя повторить прежнюю позицию доски", 409)
    return candidate, sorted(captured), key


def area_score(board, dead=()):
    """Count stones and single-color-bordered empty regions, not prisoners.

    Shared liberties (dame, including shared liberties in seki) are neutral.
    Single-color eyes in seki count under area scoring; there is no seki tax.
    Dead groups are removed only from this scoring copy, after mutual agreement.
    """
    size = board_size(board)
    removed = set(dead)
    if any(
        type(i) is not int or not 0 <= i < len(board) or not board[i] for i in removed
    ):
        raise GoError("Некорректная отметка мёртвых камней", 409)
    counted = board.copy()
    for point in removed:
        group, _ = group_at(board, point, size)
        if not group <= removed:
            raise GoError("Группа должна отмечаться целиком", 409)
        counted[point] = 0
    stones = {"black": counted.count(1), "white": counted.count(2)}
    territory = {"black": 0, "white": 0}
    ownership = counted.copy()
    visited = set()
    for point, color in enumerate(counted):
        if color or point in visited:
            continue
        region, border, todo = {point}, set(), [point]
        visited.add(point)
        while todo:
            for other in neighbors(todo.pop(), size):
                if counted[other]:
                    border.add(counted[other])
                elif other not in visited:
                    visited.add(other)
                    region.add(other)
                    todo.append(other)
        owner = next(iter(border)) if len(border) == 1 else 0
        if owner:
            territory["black" if owner == 1 else "white"] += len(region)
        for other in region:
            ownership[other] = owner
    black = 2 * (stones["black"] + territory["black"])
    white = 2 * (stones["white"] + territory["white"]) + KOMI_HALF
    return {
        "black_half": black,
        "white_half": white,
        "komi_half": KOMI_HALF,
        "stones": stones,
        "territory": territory,
        "neutral": ownership.count(0),
        "ownership": ownership,
        "dead": sorted(removed),
        "board": counted,
        "winner_color": "black"
        if black > white
        else "white"
        if white > black
        else None,
        "margin_half": abs(black - white),
    }


class GoPosition:
    def __init__(self, size):
        if type(size) is not int or size not in (9, 13, 19):
            raise GoError("Размер доски: 9×9, 13×13 или 19×19")
        self.size = size
        self.board = [0] * (size * size)
        self.to_move = 1
        self.passes = 0
        self.captures = {"black": 0, "white": 0}
        self.history = []
        self.seen = {fingerprint(self.board)}
        self.last_point = None

    def play(self, point):
        if self.passes >= 2:
            raise GoError("Сейчас подсчёт: сначала возобновите игру", 409)
        board, captured, key = played(
            self.board, point, self.to_move, self.size, self.seen
        )
        self.board = board
        self.captures["black" if self.to_move == 1 else "white"] += len(captured)
        self.to_move = 3 - self.to_move
        self.passes = 0
        self.last_point = point
        self.seen.add(key)
        self.history.append(["play", point])
        return captured

    def pass_turn(self):
        if self.passes >= 2:
            raise GoError("Оба игрока уже спасовали", 409)
        # Passes are explicitly exempt from superko and do not clear its history.
        self.to_move = 3 - self.to_move
        self.passes += 1
        self.last_point = None
        self.history.append(["pass"])

    def resume(self):
        if self.passes != 2:
            raise GoError("Подсчёт ещё не начался", 409)
        self.passes = 0
        self.history.append(["resume"])

    def legal_points(self):
        if self.passes >= 2:
            return []
        legal = []
        for point, value in enumerate(self.board):
            if value:
                continue
            try:
                played(self.board, point, self.to_move, self.size, self.seen)
            except GoError:
                continue
            legal.append(point)
        return legal

    def to_dict(self):
        return {
            "size": self.size,
            "history": [move[:] for move in self.history],
            "board": self.board[:],
            "to_move": self.to_move,
            "passes": self.passes,
            "captures": self.captures.copy(),
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) != {
            "size",
            "history",
            "board",
            "to_move",
            "passes",
            "captures",
        }:
            raise GoError("Повреждена сохранённая позиция Го", 409)
        board_size(data["board"])
        if (
            type(data["to_move"]) is not int
            or data["to_move"] not in (1, 2)
            or type(data["passes"]) is not int
            or not 0 <= data["passes"] <= 2
            or not isinstance(data["captures"], dict)
            or set(data["captures"]) != {"black", "white"}
            or any(
                type(value) is not int or value < 0
                for value in data["captures"].values()
            )
        ):
            raise GoError("Повреждены очередь или захваченные камни", 409)
        obj = cls(data["size"])
        if not isinstance(data["history"], list):
            raise GoError("Повреждена история ходов Го", 409)
        for entry in data["history"]:
            if not isinstance(entry, list):
                raise GoError("Повреждена запись хода", 409)
            if len(entry) == 2 and entry[0] == "play":
                obj.play(entry[1])
            elif entry == ["pass"]:
                obj.pass_turn()
            elif entry == ["resume"]:
                obj.resume()
            else:
                raise GoError("Повреждена запись хода", 409)
        if obj.to_dict() != data:
            raise GoError("Сохранённая доска не совпадает с историей ходов", 409)
        return obj
