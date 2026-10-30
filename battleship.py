"""Two-player Russian sea battle: private fleets and server-owned turn order.

Ten straight ships on a 10x10 board: 4, 3, 3, 2, 2, 2, 1, 1, 1, 1.
Ships cannot touch, including diagonals. A hit retains the turn, a miss passes
it. Three consecutive personal turn timeouts forfeit; timeouts never fire.
"""

from __future__ import annotations

from collections import Counter
import copy
import secrets
import time

SIZE = 10
FLEET = (4, 3, 3, 2, 2, 2, 1, 1, 1, 1)
MAX_CENTS = 100_000_000_000_000
DEFAULTS = {"entry_cents": 1000, "turn_seconds": 60, "private": False}


class BattleshipError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def settings(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise BattleshipError("Некорректные настройки морского боя")
    result = {**DEFAULTS, **data}
    if (
        type(result["entry_cents"]) is not int
        or not 0 <= result["entry_cents"] <= MAX_CENTS
    ):
        raise BattleshipError("Укажите неотрицательный взнос в целых копейках")
    if type(result["turn_seconds"]) is not int or result["turn_seconds"] not in (
        45,
        60,
        90,
        120,
    ):
        raise BattleshipError("Время хода: 45, 60, 90 или 120 секунд")
    if type(result["private"]) is not bool:
        raise BattleshipError("Некорректный признак закрытого стола")
    return result


def neighbours(cells):
    return {
        row * SIZE + col
        for cell in cells
        for row in range(max(0, cell // SIZE - 1), min(SIZE, cell // SIZE + 2))
        for col in range(max(0, cell % SIZE - 1), min(SIZE, cell % SIZE + 2))
    }


def validate_fleet(value):
    if not isinstance(value, list) or len(value) != len(FLEET):
        raise BattleshipError(
            "Нужно расставить все 10 кораблей: 4, 3, 3, 2, 2, 2, 1, 1, 1, 1"
        )
    ships = []
    occupied = set()
    for ship in value:
        if not isinstance(ship, list) or len(ship) not in (1, 2, 3, 4):
            raise BattleshipError("Некорректный размер корабля")
        if any(type(cell) is not int or not 0 <= cell < SIZE * SIZE for cell in ship):
            raise BattleshipError("Корабли должны находиться внутри поля 10×10")
        ordered = sorted(ship)
        if len(set(ordered)) != len(ordered):
            raise BattleshipError("Клетки корабля не должны повторяться")
        horizontal = len({cell // SIZE for cell in ordered}) == 1 and ordered == list(
            range(ordered[0], ordered[0] + len(ordered))
        )
        vertical = len({cell % SIZE for cell in ordered}) == 1 and ordered == list(
            range(ordered[0], ordered[0] + SIZE * len(ordered), SIZE)
        )
        if not (horizontal or vertical):
            raise BattleshipError(
                "Корабль должен занимать непрерывный прямой ряд клеток"
            )
        if occupied & neighbours(ordered):
            raise BattleshipError(
                "Корабли не могут пересекаться или касаться сторонами и углами"
            )
        occupied.update(ordered)
        ships.append(ordered)
    if Counter(map(len, ships)) != Counter(FLEET):
        raise BattleshipError(
            "Нужны: один четырёхпалубный, два трёхпалубных, три двухпалубных и четыре однопалубных"
        )
    return sorted(ships, key=lambda ship: (-len(ship), ship))


def random_fleet():
    """Bounded rejection placement; randomness does not decide the winner."""
    for _ in range(100):
        ships, blocked = [], set()
        for length in FLEET:
            candidates = []
            for row in range(SIZE):
                for col in range(SIZE):
                    if col + length <= SIZE:
                        candidates.append(
                            [row * SIZE + col + step for step in range(length)]
                        )
                    if length > 1 and row + length <= SIZE:
                        candidates.append(
                            [(row + step) * SIZE + col for step in range(length)]
                        )
            choices = [ship for ship in candidates if not blocked.intersection(ship)]
            if not choices:
                break
            ship = secrets.choice(choices)
            ships.append(ship)
            blocked.update(neighbours(ship))
        if len(ships) == len(FLEET):
            return validate_fleet(ships)
    raise BattleshipError("Не удалось подобрать расстановку. Попробуйте ещё раз", 503)


class BattleshipTable:
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
        self.actor = None
        self.deadline = None
        self.last_shot = None
        self.events = []
        self.add_player(creator, name)

    def to_dict(self):
        return copy.deepcopy(self.__dict__)

    @classmethod
    def from_dict(cls, data):
        table = cls.__new__(cls)
        table.__dict__.update(copy.deepcopy(data))
        table.rules = settings(table.rules)
        return table

    def touch(self, message=None):
        self.revision += 1
        self.updated_at = time.time()
        if message:
            self.events.append({"text": message, "time": self.updated_at})
            self.events = self.events[-100:]

    def player(self, uid):
        return next((player for player in self.players if player["id"] == uid), None)

    def active_players(self):
        return [player for player in self.players if not player["left"]]

    def add_player(self, uid, name):
        if self.player(uid):
            return
        if self.phase != "waiting" or len(self.players) >= 2:
            raise BattleshipError(
                "Присоединиться можно до старта, если есть свободное место", 409
            )
        self.players.append(
            {
                "id": uid,
                "name": name,
                "ready": False,
                "left": False,
                "fleet": [],
                "incoming": {},
                "shots_fired": 0,
                "hits": 0,
                "timeouts": 0,
            }
        )
        for player in self.players:
            player["ready"] = False
        self.touch(f"{name} присоединился к столу")

    def set_fleet(self, uid, value):
        player = self.player(uid)
        if self.phase != "waiting" or not player or player["left"]:
            raise BattleshipError("Расстановка меняется только до начала боя", 409)
        fleet = validate_fleet(value)
        player.update(fleet=fleet, ready=False)
        self.touch(f"{player['name']} сохранил расстановку")

    def change_settings(self, data):
        if self.phase != "waiting":
            raise BattleshipError("Настройки меняются только в комнате ожидания", 409)
        self.rules = settings(data)
        for player in self.players:
            player["ready"] = False
        self.touch("Условия стола изменены. Подтвердите готовность заново")

    def start(self):
        players = self.active_players()
        if (
            self.phase != "waiting"
            or len(players) != 2
            or not all(p["ready"] for p in players)
        ):
            raise BattleshipError(
                "Для старта нужны два игрока, расстановка и готовность каждого", 409
            )
        fleets = [validate_fleet(player["fleet"]) for player in players]
        for player, fleet in zip(players, fleets):
            player.update(fleet=fleet, incoming={}, shots_fired=0, hits=0, timeouts=0)
        self.phase = "playing"
        self.match_number += 1
        self.actor = secrets.choice(players)["id"]
        self.deadline = time.time() + self.rules["turn_seconds"]
        self.last_shot = None
        entry = self.rules["entry_cents"]
        self.match = {
            "number": self.match_number,
            "participants": [{"id": p["id"], "name": p["name"]} for p in players],
            "entry_cents": entry,
            "bank_cents": entry * 2,
            "first_player": self.actor,
            "result": None,
            "settled": False,
        }
        self.touch(
            f"Бой №{self.match_number} начался. Первым стреляет {self.player(self.actor)['name']}"
        )

    @staticmethod
    def sunk_ships(player):
        hits = {int(cell) for cell, shot in player["incoming"].items() if shot["hit"]}
        return [ship for ship in player["fleet"] if hits.issuperset(ship)]

    @classmethod
    def known_cells(cls, player):
        sunk = cls.sunk_ships(player)
        return set(map(int, player["incoming"])) | neighbours(
            [cell for ship in sunk for cell in ship]
        )

    def fire(self, uid, cell):
        if self.phase != "playing" or self.actor != uid:
            raise BattleshipError("Сейчас ход другого игрока", 409)
        if self.deadline is None or time.time() >= self.deadline:
            raise BattleshipError("Время хода истекло. Обновите стол", 409)
        if type(cell) is not int or not 0 <= cell < SIZE * SIZE:
            raise BattleshipError("Выберите клетку внутри поля")
        shooter = self.player(uid)
        target = next(player for player in self.active_players() if player["id"] != uid)
        if cell in self.known_cells(target):
            raise BattleshipError("Эта клетка уже известна. Выберите другую", 409)
        hit = any(cell in ship for ship in target["fleet"])
        target["incoming"][str(cell)] = {"by": uid, "hit": hit}
        sunk = next((ship for ship in self.sunk_ships(target) if cell in ship), None)
        shooter["shots_fired"] += 1
        shooter["hits"] += int(hit)
        shooter["timeouts"] = 0
        result = "sunk" if sunk else "hit" if hit else "miss"
        self.last_shot = {
            "by": uid,
            "target": target["id"],
            "cell": cell,
            "result": result,
        }
        coordinate = "АБВГДЕЖЗИК"[cell % SIZE] + str(cell // SIZE + 1)
        self.touch(
            f"{shooter['name']}: {coordinate} — "
            + {"sunk": "потопил", "hit": "попал", "miss": "мимо"}[result]
        )
        if len(self.sunk_ships(target)) == len(FLEET):
            self.finish(uid, f"{shooter['name']} потопил весь флот соперника")
        else:
            if not hit:
                self.actor = target["id"]
            self.deadline = time.time() + self.rules["turn_seconds"]

    def finish(self, winner, reason):
        self.phase = "finished"
        self.actor = self.deadline = None
        self.match["result"] = {"winner": winner, "reason": reason, "payouts": {}}
        self.touch(reason)

    def tick(self, now=None):
        now = time.time() if now is None else now
        if self.phase != "playing" or self.deadline is None or now < self.deadline:
            return
        player = self.player(self.actor)
        player["timeouts"] += 1
        if player["timeouts"] >= 3:
            self.leave(player["id"], reason="три тайм-аута подряд")
            return
        self.actor = next(
            p["id"] for p in self.active_players() if p["id"] != player["id"]
        )
        self.deadline = now + self.rules["turn_seconds"]
        self.touch(
            f"{player['name']} пропускает ход по таймеру ({player['timeouts']}/3)"
        )

    def leave(self, uid, reason="сдача"):
        player = self.player(uid)
        if not player or player["left"]:
            return
        player.update(left=True, ready=False)
        if self.phase == "waiting":
            self.players.remove(player)
            for other in self.players:
                other["ready"] = False
        elif self.phase == "playing":
            winner = next((p["id"] for p in self.active_players()), None)
            if winner is None:
                raise BattleshipError("Нет участника для расчёта боя", 409)
            self.finish(winner, f"{player['name']}: {reason}")
        if self.creator == uid and self.active_players():
            self.creator = self.active_players()[0]["id"]
        self.touch(f"{player['name']} покинул стол")

    def rematch(self):
        if self.phase != "finished" or not self.match or not self.match["settled"]:
            raise BattleshipError("Сначала завершите и рассчитайте текущий бой", 409)
        self.players = self.active_players()
        self.phase = "waiting"
        self.match = None
        self.actor = self.deadline = self.last_shot = None
        for player in self.players:
            player.update(
                fleet=[], incoming={}, ready=False, shots_fired=0, hits=0, timeouts=0
            )
        self.touch("Новый бой. Расставьте корабли и подтвердите готовность")

    def public_state(self, uid):
        reveal = (
            self.phase == "finished"
            and self.match
            and any(p["id"] == uid for p in self.match["participants"])
        )
        boards = []
        for player in self.players:
            sunk = self.sunk_ships(player)
            sunk_cells = {cell for ship in sunk for cell in ship}
            marks = [
                {
                    "cell": int(cell),
                    "by": shot["by"],
                    "result": "sunk"
                    if int(cell) in sunk_cells
                    else "hit"
                    if shot["hit"]
                    else "miss",
                }
                for cell, shot in player["incoming"].items()
            ]
            marked = {mark["cell"] for mark in marks}
            marks += [
                {"cell": cell, "by": None, "result": "water"}
                for cell in sorted(neighbours(sunk_cells) - marked)
            ]
            board = {
                "id": player["id"],
                "marks": marks,
            }
            if player["id"] == uid or reveal:
                board["fleet"] = copy.deepcopy(player["fleet"])
                board["sunk_ships"] = copy.deepcopy(sunk)
            boards.append(board)
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
            "last_shot": copy.deepcopy(self.last_shot),
            "events": copy.deepcopy(self.events),
            "players": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "ready": p["ready"],
                    "left": p["left"],
                    "placed": bool(p["fleet"]),
                    "shots_fired": p["shots_fired"],
                    "hits": p["hits"],
                    "timeouts": p["timeouts"],
                    **(
                        {
                            "ships_left": len(FLEET) - len(self.sunk_ships(p)),
                            "decks_left": sum(map(len, p["fleet"]))
                            - sum(shot["hit"] for shot in p["incoming"].values()),
                        }
                        if p["id"] == uid
                        else {}
                    ),
                }
                for p in self.players
            ],
            "boards": boards,
        }
