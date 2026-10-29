"""Classic double-six draw dominoes: one round, individual play, 2–4 seats.

28 bones; seven each with two players, five each with three/four. Draw until
playable or stock empty; no passing a playable hand. A blocked round is won by
lowest remaining pip total, with tied winners sharing the pot. No team rules.
"""

from __future__ import annotations
import copy
import secrets
import time

TILES = tuple(f"{a}-{b}" for a in range(7) for b in range(a, 7))
DEFAULTS = {"max_players": 2, "entry_cents": 1000, "turn_seconds": 90, "private": False}


class DominoError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def values(tile):
    return tuple(map(int, tile.split("-")))


def pips(hand):
    return sum(sum(values(tile)) for tile in hand)


def settings(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise DominoError("Некорректные настройки домино")
    rules = {**DEFAULTS, **data}
    for name in ("max_players", "entry_cents", "turn_seconds"):
        if type(rules[name]) is not int:
            raise DominoError("Числовые настройки должны быть целыми")
    if not 2 <= rules["max_players"] <= 4:
        raise DominoError("За столом должно быть от 2 до 4 игроков")
    if not 0 <= rules["entry_cents"] <= 100_000_000:
        raise DominoError("Ставка: от 0 до 1 000 000 БК")
    if rules["turn_seconds"] not in (45, 60, 90, 120):
        raise DominoError("Время хода: 45, 60, 90 или 120 секунд")
    if type(rules["private"]) is not bool:
        raise DominoError("Некорректная настройка закрытого стола")
    return rules


class DominoTable:
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
        self.chain = []
        self.stock = []
        self.actor = None
        self.deadline = None
        self.opening_tile = None
        self.events = []
        self.last_move = None
        self.add_player(creator, name)

    def to_dict(self):
        return copy.deepcopy(self.__dict__)

    @classmethod
    def from_dict(cls, data):
        table = cls.__new__(cls)
        table.__dict__.update(copy.deepcopy(data))
        table.rules = settings(table.rules)
        from table_lifecycle import initialise

        initialise(table)
        return table

    def player(self, uid):
        return next((p for p in self.players if p["id"] == uid), None)

    def active_players(self):
        return [p for p in self.players if not p["left"]]

    def touch(self, message=None):
        self.revision += 1
        self.updated_at = time.time()
        if message:
            self.events.append({"text": message, "time": self.updated_at})
            self.events = self.events[-50:]

    def add_player(self, uid, name):
        if self.player(uid):
            return
        if self.phase != "waiting":
            raise DominoError("Присоединиться можно до начала партии", 409)
        if len(self.players) >= self.rules["max_players"]:
            raise DominoError("Свободных мест нет", 409)
        self.players.append(
            {
                "id": uid,
                "name": name,
                "ready": False,
                "left": False,
                "hand": [],
                "timeouts": 0,
                "moves": 0,
            }
        )
        for player in self.players:
            player["ready"] = False
        self.touch(f"{name} присоединился к столу")

    def change_settings(self, data):
        if self.phase not in ("waiting", "finished"):
            raise DominoError("Нельзя менять правила во время партии", 409)
        rules = settings(data)
        if len(self.active_players()) > rules["max_players"]:
            raise DominoError("Участников больше, чем выбранных мест")
        if self.phase == "finished":
            self.rematch()
        self.rules = rules
        for player in self.players:
            player["ready"] = False
        self.touch("Правила изменены. Подтвердите готовность заново")

    def start(self, deck=None):
        active = self.active_players()
        if (
            self.phase != "waiting"
            or len(active) != self.rules["max_players"]
            or not all(p["ready"] for p in active)
        ):
            raise DominoError("Нужны все участники и готовность каждого", 409)
        bones = list(TILES) if deck is None else list(deck)
        if len(bones) != 28 or set(bones) != set(TILES):
            raise DominoError("Некорректный набор костей")
        if deck is None:
            secrets.SystemRandom().shuffle(bones)
        count = 7 if len(active) == 2 else 5
        for player in active:
            player.update(hand=bones[:count], moves=0, timeouts=0)
            del bones[:count]
        self.stock = bones
        self.chain = []
        self.last_move = None
        doubles = [
            (values(tile)[0], p["id"], tile)
            for p in active
            for tile in p["hand"]
            if values(tile)[0] == values(tile)[1]
        ]
        if doubles:
            _, self.actor, self.opening_tile = max(doubles)
        else:
            _, _, self.actor, self.opening_tile = max(
                (sum(values(tile)), max(values(tile)), p["id"], tile)
                for p in active
                for tile in p["hand"]
            )
        self.match_number += 1
        entry = self.rules["entry_cents"]
        self.match = {
            "number": self.match_number,
            "participants": [{"id": p["id"], "name": p["name"]} for p in active],
            "entry_cents": entry,
            "bank_cents": entry * len(active),
            "result": None,
            "settled": False,
        }
        self.phase = "playing"
        self.deadline = time.time() + self.rules["turn_seconds"]
        self.touch(
            f"Партия №{self.match_number}. Первый ход: {self.player(self.actor)['name']}, {self.opening_tile}"
        )

    def legal(self, uid):
        if self.phase != "playing" or uid != self.actor:
            return {}
        return self.moves_for(uid)

    def moves_for(self, uid):
        player = self.player(uid)
        if not player or player["left"]:
            return {}
        if not self.chain:
            return (
                {self.opening_tile: ["right"]}
                if self.opening_tile in player["hand"]
                else {}
            )
        left, right = self.chain[0]["left"], self.chain[-1]["right"]
        return {
            tile: [
                side
                for side, end in (("left", left), ("right", right))
                if end in values(tile)
            ]
            for tile in player["hand"]
            if left in values(tile) or right in values(tile)
        }

    def next_player(self, uid):
        ids = [p["id"] for p in self.players]
        if not ids:
            return None
        start = ids.index(uid) if uid in ids else -1
        for step in range(1, len(ids) + 1):
            player = self.player(ids[(start + step) % len(ids)])
            if not player["left"]:
                return player["id"]
        return None

    def finish(self, winners, reason):
        self.phase = "finished"
        self.actor = None
        self.deadline = None
        self.match["result"] = {
            "winners": list(winners),
            "reason": reason,
            "payouts": {},
            "hands": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "pips": pips(p["hand"]),
                    "tiles": p["hand"][:],
                    "left": p["left"],
                }
                for p in self.players
            ],
        }
        self.touch(reason)

    def blocked(self):
        if self.phase != "playing" or self.stock or not self.chain:
            return False
        active = self.active_players()
        if any(self.moves_for(p["id"]) for p in active):
            return False
        best = min(pips(p["hand"]) for p in active)
        winners = [p["id"] for p in active if pips(p["hand"]) == best]
        self.finish(
            winners,
            f"Рыба. Наименьший остаток: {best} очк."
            + (" Банк делится." if len(winners) > 1 else ""),
        )
        return True

    def action(self, uid, kind, tile=None, side=None, automatic=False):
        if self.phase != "playing" or uid != self.actor:
            raise DominoError("Сейчас ход другого игрока", 409)
        if not automatic and self.deadline and time.time() >= self.deadline:
            raise DominoError("Время хода истекло. Обновите стол", 409)
        player = self.player(uid)
        legal = self.legal(uid)
        if kind == "play":
            if (
                not isinstance(tile, str)
                or tile not in legal
                or side not in legal[tile]
            ):
                raise DominoError("Эту кость нельзя поставить на выбранный конец", 409)
            a, b = values(tile)
            if self.chain:
                end = (
                    self.chain[0]["left"] if side == "left" else self.chain[-1]["right"]
                )
                if side == "left":
                    a, b = (b, a) if a == end else (a, b)
                else:
                    a, b = (a, b) if a == end else (b, a)
            player["hand"].remove(tile)
            bone = {"id": tile, "left": a, "right": b, "by": uid}
            if side == "left":
                self.chain.insert(0, bone)
            else:
                self.chain.append(bone)
            player["moves"] += 1
            self.last_move = {
                "by": uid,
                "tile": tile,
                "side": side,
                "revision": self.revision + 1,
            }
            self.touch(
                f"{player['name']} поставил {tile} "
                + ("слева" if side == "left" else "справа")
            )
            if not player["hand"]:
                self.finish([uid], f"{player['name']} выложил все кости")
            else:
                self.actor = self.next_player(uid)
                self.blocked()
        elif kind == "draw":
            if legal or not self.stock:
                raise DominoError("Базар доступен только без подходящей кости", 409)
            drawn = 0
            while self.stock and not self.moves_for(uid):
                player["hand"].append(self.stock.pop())
                drawn += 1
            self.touch(f"{player['name']} взял с базара: {drawn}")
            self.blocked()
        elif kind == "pass":
            if legal or self.stock:
                raise DominoError(
                    "Нельзя пропускать: есть ход или кости на базаре", 409
                )
            self.touch(f"{player['name']} пропускает ход")
            self.actor = self.next_player(uid)
            self.blocked()
        else:
            raise DominoError("Неизвестное действие")
        if not automatic:
            player["timeouts"] = 0
        if self.phase == "playing":
            self.deadline = time.time() + self.rules["turn_seconds"]

    def tick(self, now=None):
        now = time.time() if now is None else now
        if self.phase != "playing" or not self.deadline or now < self.deadline:
            return
        player = self.player(self.actor)
        player["timeouts"] += 1
        if player["timeouts"] >= 3:
            self.leave(player["id"])
            return
        uid = player["id"]
        legal = self.legal(uid)
        if not legal and self.stock:
            self.action(uid, "draw", automatic=True)
            if self.phase != "playing":
                return
            legal = self.legal(uid)
        if legal:
            tile = min(legal, key=lambda t: (sum(values(t)), t))
            self.action(uid, "play", tile, legal[tile][0], automatic=True)
        else:
            self.action(uid, "pass", automatic=True)

    def leave(self, uid):
        player = self.player(uid)
        if not player or player["left"]:
            return
        player["left"] = True
        player["ready"] = False
        if self.phase == "waiting":
            self.players.remove(player)
        elif self.phase == "playing":
            active = self.active_players()
            if len(active) == 1:
                self.finish([active[0]["id"]], f"{player['name']} сдался")
            elif not active:
                self.finish([], "Участники покинули стол")
            else:
                if self.actor == uid:
                    self.actor = self.next_player(uid)
                self.deadline = time.time() + self.rules["turn_seconds"]
                self.blocked()
        if self.creator == uid and self.active_players():
            self.creator = self.active_players()[0]["id"]
        self.touch(f"{player['name']} покинул стол")

    def rematch(self):
        if self.phase != "finished":
            raise DominoError("Текущая партия ещё не закончена", 409)
        self.players = self.active_players()
        self.phase = "waiting"
        self.match = None
        self.actor = None
        self.deadline = None
        self.chain = []
        self.stock = []
        self.opening_tile = None
        self.last_move = None
        for player in self.players:
            player.update(hand=[], ready=False, moves=0, timeouts=0)
        self.touch("Новая партия. Подтвердите готовность")

    def public_state(self, uid):
        own = self.player(uid)
        legal = self.legal(uid)
        return {
            "id": self.id,
            "creator": self.creator,
            "rules": self.rules.copy(),
            "phase": self.phase,
            "revision": self.revision,
            "match_number": self.match_number,
            "match": copy.deepcopy(self.match),
            "actor": self.actor,
            "deadline": self.deadline,
            "server_time": time.time(),
            "your_id": uid,
            "chain": copy.deepcopy(self.chain),
            "stock_count": len(self.stock),
            "players": [
                {k: v for k, v in p.items() if k != "hand"} | {"count": len(p["hand"])}
                for p in self.players
            ],
            "hand": own["hand"][:] if own else [],
            "legal": legal,
            "can_draw": bool(
                self.phase == "playing"
                and self.actor == uid
                and not legal
                and self.stock
            ),
            "can_pass": bool(
                self.phase == "playing"
                and self.actor == uid
                and not legal
                and not self.stock
            ),
            "events": copy.deepcopy(self.events[-30:]),
            "last_move": copy.deepcopy(self.last_move),
        }
