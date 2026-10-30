"""Server-authoritative two-player chess tables."""

from __future__ import annotations

import copy
import time

import chess

DEFAULTS = {"entry_cents": 0, "private": False}


class ChessError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def claim_draw_reason(board):
    """Return a claim available in the current position, without a proposed move."""
    if board.is_repetition(3):
        return "Ничья: трёхкратное повторение позиции"
    if board.is_fifty_moves():
        return "Ничья: правило 50 ходов"
    return None


def automatic_result(board, mover):
    """Return the result of an automatic ending, if the move ended the game."""
    if board.is_checkmate():
        return [mover], "Мат"
    if board.is_stalemate():
        return [], "Пат"
    if board.is_insufficient_material():
        return [], "Ничья: недостаточно материала для мата"
    if board.is_fivefold_repetition():
        return [], "Ничья: пятикратное повторение позиции"
    if board.is_seventyfive_moves():
        return [], "Ничья: правило 75 ходов"
    return None


def settings(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise ChessError("Некорректные настройки шахмат")
    rules = {**DEFAULTS, **data}
    if (
        type(rules["entry_cents"]) is not int
        or not 0 <= rules["entry_cents"] <= 100_000_000
    ):
        raise ChessError("Взнос должен быть от 0 до 1 000 000 БК")
    if type(rules["private"]) is not bool:
        raise ChessError("Некорректная настройка закрытого стола")
    return rules


class ChessTable:
    def __init__(self, tid, creator, name, rules=None):
        self.id = tid
        self.creator = creator
        self.rules = settings({} if rules is None else rules)
        self.created_at = self.updated_at = self.last_player_activity = time.time()
        self.players = []
        self.phase = "waiting"
        self.revision = 0
        self.match_number = 0
        self.match = None
        self.fen = chess.STARTING_FEN
        self.moves = []
        self.actor = None
        self.draw_offer = None
        self.events = []
        self.add_player(creator, name)

    def to_dict(self):
        return copy.deepcopy(self.__dict__)

    @classmethod
    def from_dict(cls, data):
        obj = cls.__new__(cls)
        obj.__dict__.update(copy.deepcopy(data))
        obj.rules = settings(obj.rules)
        from table_lifecycle import initialise

        initialise(obj)
        return obj

    @property
    def board(self):
        board = chess.Board(chess.STARTING_FEN)
        for move in self.moves:
            board.push_uci(move)
        if board.fen() != self.fen:
            raise ChessError("Сохранённая позиция повреждена", 409)
        return board

    def player(self, uid):
        return next((p for p in self.players if p["id"] == uid), None)

    def active_players(self):
        return [p for p in self.players if not p["left"]]

    def touch(self, text=None):
        self.revision += 1
        self.updated_at = time.time()
        if text:
            self.events.append({"text": text, "time": self.updated_at})
            self.events = self.events[-30:]

    def add_player(self, uid, name):
        if self.player(uid):
            return
        if self.phase != "waiting" or len(self.players) >= 2:
            raise ChessError("За столом нет свободного места", 409)
        self.players.append({"id": uid, "name": name, "ready": False, "left": False})
        for player in self.players:
            player["ready"] = False
        self.touch(f"{name} присоединился к столу")

    def change_settings(self, data):
        if self.phase != "waiting":
            raise ChessError("Нельзя менять настройки во время партии", 409)
        self.rules = settings(data)
        for player in self.players:
            player["ready"] = False
        self.touch("Настройки изменены. Подтвердите готовность заново")

    def start(self):
        players = self.active_players()
        if (
            self.phase != "waiting"
            or len(players) != 2
            or not all(p["ready"] for p in players)
        ):
            raise ChessError("Нужны два игрока и готовность каждого", 409)
        self.match_number += 1
        self.match = {
            "number": self.match_number,
            "participants": [{"id": p["id"], "name": p["name"]} for p in players],
            "entry_cents": self.rules["entry_cents"],
            "bank_cents": self.rules["entry_cents"] * 2,
            "result": None,
            "settled": False,
        }
        self.moves, self.fen, self.actor, self.draw_offer = (
            [],
            chess.STARTING_FEN,
            players[0]["id"],
            None,
        )
        self.phase = "playing"
        self.touch(f"Партия №{self.match_number}. Белые: {players[0]['name']}")

    def finish(self, winners, reason):
        self.phase, self.actor, self.draw_offer = "finished", None, None
        self.match["result"] = {
            "winners": list(winners),
            "reason": reason,
            "payouts": {},
        }
        self.touch(reason)

    def action(self, uid, kind, move=None):
        if self.phase != "playing":
            raise ChessError("Партия не идёт", 409)
        board = self.board
        if kind == "resign":
            if uid not in {p["id"] for p in self.match["participants"]}:
                raise ChessError("Сдаться может только участник партии", 403)
            winner = next(p["id"] for p in self.match["participants"] if p["id"] != uid)
            self.finish([winner], "Соперник сдался")
            return
        if kind == "offer_draw":
            if uid != self.actor:
                raise ChessError("Сейчас ход другого игрока", 409)
            if self.draw_offer is not None:
                raise ChessError("Предложение ничьей уже ожидает ответа", 409)
            self.draw_offer = uid
            self.touch("Предложена ничья")
            return
        if kind == "accept_draw":
            if self.draw_offer is None or uid == self.draw_offer:
                raise ChessError("Нет предложения ничьей от соперника", 409)
            self.finish([], "Ничья по соглашению")
            return
        if kind == "claim_draw":
            reason = claim_draw_reason(board)
            if uid != self.actor or reason is None:
                raise ChessError("Правила не дают права заявить ничью", 409)
            self.finish([], reason)
            return
        if (
            kind != "move"
            or uid != self.actor
            or not isinstance(move, str)
            or len(move) not in (4, 5)
        ):
            raise ChessError("Некорректный ход или сейчас ход другого игрока", 409)
        try:
            parsed = chess.Move.from_uci(move)
        except ValueError:
            raise ChessError("Некорректная запись хода")
        if parsed not in board.legal_moves:
            raise ChessError("Этот ход запрещён правилами", 409)
        board.push(parsed)
        self.moves.append(parsed.uci())
        self.fen = board.fen()
        self.actor = next(p["id"] for p in self.match["participants"] if p["id"] != uid)
        self.draw_offer = None
        result = automatic_result(board, uid)
        if result:
            self.finish(*result)
        else:
            self.touch("Шах" if board.is_check() else None)

    def public_state(self, uid):
        board = self.board if self.phase == "playing" else chess.Board(self.fen)
        participants = self.match["participants"] if self.match else self.players
        side = (
            chess.WHITE
            if participants and uid == participants[0]["id"]
            else chess.BLACK
        )
        legal = (
            [m.uci() for m in board.legal_moves]
            if self.phase == "playing" and uid == self.actor
            else []
        )
        return {
            "id": self.id,
            "phase": self.phase,
            "revision": self.revision,
            "rules": self.rules,
            "players": [
                {k: p[k] for k in ("id", "name", "ready", "left")} for p in self.players
            ],
            "match": self.match,
            "fen": board.fen(),
            "turn": "white" if board.turn else "black",
            "actor": self.actor,
            "your_side": "white" if side else "black",
            "legal_moves": legal,
            "check": board.is_check(),
            "draw_offer": self.draw_offer,
            "can_claim_draw": self.phase == "playing"
            and uid == self.actor
            and claim_draw_reason(board) is not None,
            "reason": self.match["result"]["reason"]
            if self.match and self.match.get("result")
            else None,
            "events": getattr(self, "events", []),
        }
