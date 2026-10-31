"""Two-player backgammon tables; fixed stakes, durable and replayable rounds."""

import copy
import time

from backgammon_engine import BackgammonError, Round, roll_dice

DEFAULTS = {"entry_cents": 0, "private": False, "variant": "long"}


def settings(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise BackgammonError("Некорректные настройки нард")
    rules = {**DEFAULTS, **data}
    if (
        type(rules["entry_cents"]) is not int
        or not 0 <= rules["entry_cents"] <= 100_000_000
    ):
        raise BackgammonError("Взнос должен быть от 0 до 1 000 000 БК")
    if type(rules["private"]) is not bool or rules["variant"] not in ("long", "short"):
        raise BackgammonError("Выберите вариант нард и доступ к столу")
    return rules


class BackgammonTable:
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
        self.round = None
        self.actor = None
        self.events = []
        self.add_player(creator, name)

    def to_dict(self):
        data = copy.deepcopy(self.__dict__)
        data["round"] = self.round.to_dict() if self.round else None
        return data

    @classmethod
    def from_dict(cls, data):
        obj = cls.__new__(cls)
        obj.__dict__.update(copy.deepcopy(data))
        obj.rules = settings(obj.rules)
        obj.round = Round.from_dict(data["round"]) if data.get("round") else None
        if obj.round and obj.round.variant != obj.rules["variant"]:
            raise BackgammonError("Вариант партии не совпадает с настройками", 409)
        if obj.phase == "playing" and (
            not obj.round
            or obj.round.winner is not None
            or obj.actor != obj.match["participants"][obj.round.turn]["id"]
        ):
            raise BackgammonError("Повреждён сохранённый ход", 409)
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
            self.events = self.events[-30:]

    def add_player(self, uid, name):
        if self.player(uid):
            return
        if self.phase != "waiting" or len(self.players) >= 2:
            raise BackgammonError("За столом нет свободного места", 409)
        self.players.append({"id": uid, "name": name, "ready": False, "left": False})
        for player in self.players:
            player["ready"] = False
        self.touch(f"{name} присоединился к столу")

    def change_settings(self, data):
        if self.phase != "waiting":
            raise BackgammonError("Нельзя менять настройки во время партии", 409)
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
            raise BackgammonError("Нужны два игрока и готовность каждого", 409)
        self.match_number += 1
        self.match = {
            "number": self.match_number,
            "participants": [{"id": p["id"], "name": p["name"]} for p in players],
            "entry_cents": self.rules["entry_cents"],
            "bank_cents": self.rules["entry_cents"] * 2,
            "result": None,
            "settled": False,
        }
        opening = roll_dice()
        while opening[0] == opening[1]:
            opening = roll_dice()
        self.round = Round(self.rules["variant"], opening)
        self.actor = players[self.round.turn]["id"]
        self.phase = "playing"
        self.touch(f"Партия №{self.match_number}. Жеребьёвка {opening[0]}:{opening[1]}")

    def finish(self, winners, reason):
        self.phase, self.actor = "finished", None
        self.match["result"] = {
            "winners": list(winners),
            "reason": reason,
            "payouts": {},
        }
        self.touch(reason)

    def action(self, uid, kind, move=None):
        if self.phase != "playing":
            raise BackgammonError("Партия не идёт", 409)
        ids = [p["id"] for p in self.match["participants"]]
        if uid not in ids:
            raise BackgammonError("Только участник партии может действовать", 403)
        if kind == "resign":
            self.finish([next(p for p in ids if p != uid)], "Соперник сдался")
            return
        if uid != self.actor:
            raise BackgammonError("Сейчас ход соперника", 409)
        if kind == "roll" and move is None:
            self.round.roll(roll_dice())
        elif kind == "move":
            self.round.move(move)
        else:
            raise BackgammonError("Неизвестное действие")
        if self.round.winner is not None:
            self.finish([ids[self.round.winner]], self.round.quality())
        else:
            self.actor = ids[self.round.turn]
            self.touch()

    def public_state(self, uid):
        participants = self.match["participants"] if self.round else self.players
        side = next((i for i, p in enumerate(participants) if p["id"] == uid), None)
        round_state = self.round.to_dict() if self.round else None
        if round_state:
            # Full history is durable server data; return just a small recent log.
            round_state["history"] = round_state["history"][-12:]
            round_state["legal"] = (
                [list(m) for m in self.round.legal()] if uid == self.actor else []
            )
        return {
            "id": self.id,
            "creator": self.creator,
            "phase": self.phase,
            "revision": self.revision,
            "rules": self.rules,
            "players": copy.deepcopy(self.players),
            "match": copy.deepcopy(self.match),
            "actor": self.actor,
            "your_side": side,
            "round": round_state,
            "reason": self.match["result"]["reason"]
            if self.match and self.match.get("result")
            else None,
            "events": copy.deepcopy(self.events),
        }
