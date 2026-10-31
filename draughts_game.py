"""Russian draughts, 8×8: mandatory captures, flying kings and delayed removal.

Captures are persisted one landing at a time. Captured pieces remain blockers
until the entire turn ends; promotion during a capture takes effect immediately.
No maximum-capture rule. This module performs no wallet or network operations.
"""

from __future__ import annotations

import copy
import time

FILES = "abcdefgh"
DIAGONALS = ((-1, -1), (-1, 1), (1, -1), (1, 1))
DEFAULTS = {"entry_cents": 0, "private": False}


class DraughtsError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def coords(square):
    if (
        not isinstance(square, str)
        or len(square) != 2
        or square[0] not in FILES
        or square[1] not in "12345678"
    ):
        raise DraughtsError("Некорректная клетка")
    x, y = FILES.index(square[0]), int(square[1]) - 1
    if (x + y) % 2:
        raise DraughtsError("Играть можно только на тёмных клетках")
    return x, y


def square(x, y):
    return FILES[x] + str(y + 1) if 0 <= x < 8 and 0 <= y < 8 else None


def owner(piece):
    return "white" if piece.lower() == "w" else "black"


def initial_board():
    return {
        square(x, y): "w" if y < 3 else "b"
        for y in (*range(3), *range(5, 8))
        for x in range(8)
        if (x + y) % 2 == 0
    }


def _raw_steps(board, side, pending=None):
    """Generate only the next legal landing, avoiding exponential capture trees."""
    pending = pending or {}
    taken = set(pending.get("captured", []))
    forced = pending.get("at")
    captures, quiet = [], []
    for start, piece in board.items():
        if start in taken or owner(piece) != side or (forced and start != forced):
            continue
        x, y = coords(start)
        king = piece.isupper()
        for dx, dy in DIAGONALS:
            distance = 1
            target = square(x + dx, y + dy)
            while target and target not in board:
                if not forced and (king or dy == (1 if side == "white" else -1)):
                    quiet.append({"from": start, "to": target, "capture": None})
                if not king:
                    break
                distance += 1
                target = square(x + dx * distance, y + dy * distance)
            if (
                not target
                or target not in board
                or target in taken
                or owner(board[target]) == side
            ):
                continue
            # A man can jump only an adjacent enemy. A king scans to the first
            # occupied square, then may land anywhere free behind that enemy.
            distance += 1
            landing = square(x + dx * distance, y + dy * distance)
            while landing and landing not in board:
                captures.append({"from": start, "to": landing, "capture": target})
                if not king:
                    break
                distance += 1
                landing = square(x + dx * distance, y + dy * distance)
    return captures if captures or forced else quiet


def steps(board, side, pending=None):
    candidates = _raw_steps(board, side, pending)
    if not candidates or not candidates[0]["capture"]:
        return candidates
    # A king must choose a landing allowing continuation when another landing
    # behind the SAME captured piece permits it. This is not a maximum-capture
    # rule between different enemies/pieces. One-step lookahead stays bounded.
    continue_groups = set()
    continuing = set()
    for i, move in enumerate(candidates):
        after = board.copy()
        piece = after.pop(move["from"])
        if move["to"][1] == ("8" if side == "white" else "1"):
            piece = piece.upper()
        after[move["to"]] = piece
        capture = {
            "at": move["to"],
            "captured": (pending or {}).get("captured", []) + [move["capture"]],
        }
        if _raw_steps(after, side, capture):
            continuing.add(i)
            continue_groups.add((move["from"], move["capture"]))
    return [
        move
        for i, move in enumerate(candidates)
        if i in continuing or (move["from"], move["capture"]) not in continue_groups
    ]


def position_key(board, side):
    return side + ":" + ",".join(k + board[k] for k in sorted(board))


def settings(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise DraughtsError("Некорректные настройки шашек")
    rules = {**DEFAULTS, **data}
    if (
        type(rules["entry_cents"]) is not int
        or not 0 <= rules["entry_cents"] <= 100_000_000
    ):
        raise DraughtsError("Взнос должен быть от 0 до 1 000 000 БК")
    if type(rules["private"]) is not bool:
        raise DraughtsError("Некорректная настройка доступа")
    return rules


def endgame_mode(board):
    """Return the stronger side and its move limit for lone-king endings."""
    by_side = {
        side: [(sq, p) for sq, p in board.items() if owner(p) == side]
        for side in ("white", "black")
    }
    for weak, strong in (("white", "black"), ("black", "white")):
        weak_pieces, strong_pieces = by_side[weak], by_side[strong]
        kings = sum(p.isupper() for _, p in strong_pieces)
        if len(weak_pieces) != 1 or not weak_pieces[0][1].isupper() or not kings:
            continue
        signature = ":".join(str(v) for v in (weak, len(strong_pieces)))
        if len(strong_pieces) <= 2:
            return {"key": "small:" + signature, "strong": strong, "limit": 5}
        if len(strong_pieces) == 3:
            weak_sq = weak_pieces[0][0]
            if coords(weak_sq)[0] == coords(weak_sq)[1] and not any(
                coords(sq)[0] == coords(sq)[1] for sq, _ in strong_pieces
            ):
                return {"key": "diagonal:" + signature, "strong": strong, "limit": 5}
        if kings == len(strong_pieces) and kings >= 3:
            return {"key": "kings:" + signature, "strong": strong, "limit": 15}
    return None


class DraughtsTable:
    def __init__(self, tid, creator, name, rules=None):
        self.id, self.creator = tid, creator
        self.rules = settings({} if rules is None else rules)
        self.created_at = self.updated_at = self.last_player_activity = time.time()
        self.players, self.events = [], []
        self.phase, self.revision, self.match_number = "waiting", 0, 0
        self.match = self.actor = self.draw_offer = None
        self.position = initial_board()
        self.turn = "white"
        self.pending = None
        self.history = []
        self.positions = {position_key(self.position, self.turn): 1}
        self.king_quiet_plies = self.stable_material_plies = 0
        self.endgame = None
        self.add_player(creator, name)

    def to_dict(self):
        return copy.deepcopy(self.__dict__)

    @classmethod
    def from_dict(cls, data):
        obj = cls.__new__(cls)
        obj.__dict__.update(copy.deepcopy(data))
        obj.rules = settings(obj.rules)
        if obj.phase not in ("waiting", "playing", "finished") or any(
            p not in "wWbB" for p in obj.position.values()
        ):
            raise DraughtsError("Сохранённая позиция шашек повреждена", 409)
        for sq in obj.position:
            coords(sq)
        if obj.pending and (
            obj.pending["at"] not in obj.position
            or len(obj.pending["captured"]) != len(set(obj.pending["captured"]))
            or any(sq not in obj.position for sq in obj.pending["captured"])
        ):
            raise DraughtsError("Сохранённая серия взятий повреждена", 409)
        from table_lifecycle import initialise

        initialise(obj)
        return obj

    def player(self, uid):
        return next((p for p in self.players if p["id"] == uid), None)

    def active_players(self):
        return [p for p in self.players if not p["left"]]

    def touch(self, text=None):
        self.revision += 1
        self.updated_at = time.time()
        if text:
            self.events.append({"text": text, "time": self.updated_at})
            self.events = self.events[-40:]

    def add_player(self, uid, name):
        if self.player(uid):
            return
        if self.phase != "waiting" or len(self.players) >= 2:
            raise DraughtsError("За столом нет свободного места", 409)
        self.players.append({"id": uid, "name": name, "ready": False, "left": False})
        for player in self.players:
            player["ready"] = False
        self.touch(f"{name} присоединился к столу")

    def change_settings(self, data):
        if self.phase != "waiting":
            raise DraughtsError("Условия меняются только до начала партии", 409)
        self.rules = settings(data)
        for player in self.players:
            player["ready"] = False
        self.touch("Условия изменены. Подтвердите готовность заново")

    def start(self):
        if (
            self.phase != "waiting"
            or len(self.active_players()) != 2
            or not all(p["ready"] for p in self.players)
        ):
            raise DraughtsError("Нужны два игрока и готовность каждого", 409)
        self.match_number += 1
        self.match = {
            "number": self.match_number,
            "participants": [{"id": p["id"], "name": p["name"]} for p in self.players],
            "entry_cents": self.rules["entry_cents"],
            "bank_cents": self.rules["entry_cents"] * 2,
            "result": None,
            "settled": False,
        }
        self.position, self.turn = initial_board(), "white"
        self.actor = self.players[0]["id"]
        self.pending = self.draw_offer = None
        self.history = []
        self.positions = {position_key(self.position, self.turn): 1}
        self.king_quiet_plies = self.stable_material_plies = 0
        self.endgame = None
        self.phase = "playing"
        self.touch("Партия началась. Белые ходят первыми")

    def finish(self, winners, reason):
        self.phase, self.actor, self.draw_offer = "finished", None, None
        self.match["result"] = {
            "winners": list(winners),
            "reason": reason,
            "payouts": {},
        }
        self.touch(reason)

    def complete_turn(self, info):
        for sq in info["captured"]:
            self.position.pop(sq)
        self.pending = None
        self.history.append(
            {
                "side": self.turn,
                "path": info["path"],
                "captured": info["captured"],
                "promoted": info["promoted"],
            }
        )
        self.king_quiet_plies = (
            self.king_quiet_plies + 1
            if info["was_king"] and not info["captured"]
            else 0
        )
        material_changed = bool(info["captured"] or info["promoted"])
        self.stable_material_plies = (
            0 if material_changed else self.stable_material_plies + 1
        )
        mode = endgame_mode(self.position)
        if (
            mode
            and self.endgame
            and mode["key"] == self.endgame["key"]
            and not info["captured"]
            and (
                not info["promoted"] or mode["key"].startswith(("small:", "diagonal:"))
            )
        ):
            mode["moves"] = self.endgame["moves"] + (self.turn == mode["strong"])
        elif mode:
            mode["moves"] = 0
        self.endgame = mode
        mover = self.actor
        self.turn = "black" if self.turn == "white" else "white"
        self.actor = next(
            p["id"] for p in self.match["participants"] if p["id"] != mover
        )
        if not steps(self.position, self.turn):
            self.finish([mover], "У соперника не осталось шашек или допустимых ходов")
            return
        if not info["was_king"] or info["captured"]:
            self.positions = {}
        key = position_key(self.position, self.turn)
        self.positions[key] = self.positions.get(key, 0) + 1
        both_kings = all(
            any(owner(p) == s and p.isupper() for p in self.position.values())
            for s in ("white", "black")
        )
        reason = None
        if self.positions[key] >= 3:
            reason = "Ничья: трёхкратное повторение позиции"
        elif mode and mode["moves"] >= mode["limit"]:
            reason = (
                f"Ничья: лимит {mode['limit']} ходов в окончании против одинокой дамки"
            )
        elif self.king_quiet_plies >= 30:
            reason = "Ничья: 15 ходов дамками без взятий и хода простой"
        elif (
            both_kings
            and len(self.position) in (4, 5)
            and self.stable_material_plies >= 60
        ):
            reason = "Ничья: 30 ходов без изменения материала в 4–5-фигурном окончании"
        elif (
            both_kings
            and len(self.position) in (6, 7)
            and self.stable_material_plies >= 120
        ):
            reason = "Ничья: 60 ходов без изменения материала в 6–7-фигурном окончании"
        if reason:
            self.finish([], reason)
        else:
            self.touch("Белые ходят" if self.turn == "white" else "Чёрные ходят")

    def action(self, uid, kind, move=None):
        if self.phase != "playing" or uid not in {
            p["id"] for p in self.match["participants"]
        }:
            raise DraughtsError("Партия не идёт или вы не её участник", 409)
        if kind == "resign":
            self.finish(
                [p["id"] for p in self.match["participants"] if p["id"] != uid],
                "Соперник сдался",
            )
            return
        if kind == "accept_draw":
            if self.draw_offer is None or self.draw_offer == uid:
                raise DraughtsError("Нет предложения ничьей от соперника", 409)
            self.finish([], "Ничья по соглашению")
            return
        if uid != self.actor:
            raise DraughtsError("Сейчас ход соперника", 409)
        if kind == "offer_draw":
            if self.pending or self.draw_offer is not None:
                raise DraughtsError(
                    "Сначала закончите серию взятий или дождитесь ответа", 409
                )
            self.draw_offer = uid
            self.touch("Предложена ничья")
            return
        if kind != "move" or not isinstance(move, dict) or set(move) != {"from", "to"}:
            raise DraughtsError("Укажите начальную и конечную клетку")
        coords(move["from"])
        coords(move["to"])
        legal = next(
            (
                m
                for m in steps(self.position, self.turn, self.pending)
                if m["from"] == move["from"] and m["to"] == move["to"]
            ),
            None,
        )
        if legal is None:
            raise DraughtsError(
                "Недопустимый ход. При наличии взятия бить обязательно", 409
            )
        start, end = legal["from"], legal["to"]
        piece = self.position.pop(start)
        info = self.pending or {
            "path": [start],
            "captured": [],
            "was_king": piece.isupper(),
            "promoted": False,
        }
        if piece.islower() and end[1] == ("8" if self.turn == "white" else "1"):
            piece = piece.upper()
            info["promoted"] = True
        self.position[end] = piece
        info["path"].append(end)
        self.draw_offer = None
        if legal["capture"]:
            info["captured"].append(legal["capture"])
            info["at"] = end
            self.pending = info
            if steps(self.position, self.turn, self.pending):
                self.touch(
                    "Продолжите взятие той же шашкой"
                    + (" · теперь дамкой" if info["promoted"] else "")
                )
                return
        self.complete_turn(info)

    def rematch(self):
        if (
            self.phase != "finished"
            or not self.match["settled"]
            or len(self.active_players()) != 2
        ):
            raise DraughtsError(
                "Для новой партии нужны два игрока и рассчитанный результат", 409
            )
        self.players.reverse()
        for p in self.players:
            p["ready"] = False
        self.phase, self.actor, self.match = "waiting", None, None
        self.position, self.turn = initial_board(), "white"
        self.pending = self.draw_offer = None
        self.history = []
        self.touch("Новая партия: игроки поменялись цветами. Подтвердите готовность")

    def public_state(self, uid):
        participants = self.match["participants"] if self.match else self.players
        legal = (
            steps(self.position, self.turn, self.pending)
            if self.phase == "playing" and uid == self.actor
            else []
        )
        taken = set((self.pending or {}).get("captured", []))
        return {
            "id": self.id,
            "creator": self.creator,
            "phase": self.phase,
            "revision": self.revision,
            "rules": self.rules.copy(),
            "players": copy.deepcopy(self.players),
            "match": copy.deepcopy(self.match),
            "position": dict(sorted(self.position.items())),
            "actor": self.actor,
            "turn": self.turn,
            "your_side": "white" if uid == participants[0]["id"] else "black",
            "legal_moves": sorted(legal, key=lambda m: (m["from"], m["to"])),
            "must_capture": bool(legal and legal[0]["capture"]),
            "pending": copy.deepcopy(self.pending),
            "draw_offer": self.draw_offer,
            "reason": self.match["result"]["reason"]
            if self.match and self.match["result"]
            else None,
            "history": copy.deepcopy(self.history),
            "counts": {
                s: {
                    "men": sum(
                        owner(p) == s and p.islower() and sq not in taken
                        for sq, p in self.position.items()
                    ),
                    "kings": sum(
                        owner(p) == s and p.isupper() and sq not in taken
                        for sq, p in self.position.items()
                    ),
                }
                for s in ("white", "black")
            },
            "events": copy.deepcopy(self.events),
        }
