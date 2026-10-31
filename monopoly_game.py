"""Server-owned classic Monopoly for 2–10 players.

Site wallets are deliberately absent here. Internal money, tasks, auctions,
negotiations and shuffled decks are stored in the existing game runtime.
"""

from __future__ import annotations

import copy
import secrets
import time
from functools import wraps

from monopoly_board import BOARD, DECKS, GROUPS, JAIL_CARDS, PROPERTIES

DEFAULTS = {"max_players": 10, "entry_cents": 0, "private": False}
COLORS = [
    "#ffcd75",
    "#84b8ff",
    "#e68eb8",
    "#86d8b1",
    "#c5a2ef",
    "#efaa7d",
    "#e4de88",
    "#82d6df",
    "#c3d49a",
    "#c9b5a4",
]


class MonopolyError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def integer(value, minimum=0, maximum=1_000_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise MonopolyError("Некорректное целое число")
    return value


def settings(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise MonopolyError("Некорректные настройки Монополии")
    rules = {**DEFAULTS, **data}
    integer(rules["max_players"], 2, 10)
    integer(rules["entry_cents"], 0, 100_000_000)
    if type(rules["private"]) is not bool:
        raise MonopolyError("Некорректный доступ")
    return rules


def atomic(method):
    @wraps(method)
    def execute(self, *args, **kwargs):
        before = copy.deepcopy(self.__dict__)
        try:
            result = method(self, *args, **kwargs)
            self.validate()
            return result
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(before)
            raise

    return execute


class MonopolyTable:
    def __init__(self, tid, creator, name, rules=None):
        self.id, self.creator = tid, creator
        self.rules = settings({} if rules is None else rules)
        self.players = []
        self.created_at = self.updated_at = self.last_player_activity = time.time()
        self.phase = "waiting"
        self.revision = self.match_number = 0
        self.match = None
        self.events = []
        self._reset_board()
        self.add_player(creator, name)

    def _reset_board(self):
        self.estate = {
            str(i): {"owner": None, "mortgaged": False, "level": 0} for i in PROPERTIES
        }
        self.bank = {"houses": 32, "hotels": 12}
        self.decks = {key: list(range(len(cards))) for key, cards in DECKS.items()}
        self.actor = None
        self.turn_number = 0
        self.turn_action = "roll"
        self.doubles = 0
        self.dice = []
        self.opening_rolls = []
        self.pending = None
        self.tasks = []
        self.offers = []
        self.last_card = None
        self.last_rent_dice = []

    def player(self, uid):
        return next((p for p in self.players if p["id"] == uid), None)

    def active_players(self):
        return [p for p in self.players if not p["left"]]

    def living(self):
        return [p for p in self.players if not p["left"] and not p["bankrupt"]]

    def alive(self, uid):
        p = self.player(uid)
        return p is not None and not p["left"] and not p["bankrupt"]

    def touch(self, text=None):
        self.revision += 1
        self.updated_at = time.time()
        if text:
            self.events.append({"time": self.updated_at, "text": text})
            self.events = self.events[-100:]

    def log(self, text):
        self.events.append({"time": time.time(), "text": text})
        self.events = self.events[-100:]

    def add_player(self, uid, name):
        if self.player(uid):
            return
        if self.phase != "waiting" or len(self.players) >= self.rules["max_players"]:
            raise MonopolyError("За столом нет места", 409)
        used = {p["token"] for p in self.players}
        token = next(i for i in range(10) if i not in used)
        self.players.append(
            {
                "id": uid,
                "name": name,
                "token": token,
                "ready": False,
                "left": False,
                "cash": 1500,
                "pos": 0,
                "jail": False,
                "jail_attempts": 0,
                "bankrupt": False,
                "cards": [],
            }
        )
        for p in self.players:
            p["ready"] = False
        self.touch(name + " присоединился к столу")

    def change_settings(self, data):
        if self.phase != "waiting":
            raise MonopolyError("Условия меняются до начала игры", 409)
        rules = settings(data)
        if rules["max_players"] < len(self.players):
            raise MonopolyError("Нельзя убрать занятые места", 409)
        self.rules = rules
        for p in self.players:
            p["ready"] = False
        self.touch("Условия изменены: подтвердите готовность заново")

    @staticmethod
    def roll_dice():
        return [secrets.randbelow(6) + 1, secrets.randbelow(6) + 1]

    @atomic
    def start(self):
        seats = self.active_players()
        if (
            self.phase != "waiting"
            or not 2 <= len(seats) <= self.rules["max_players"]
            or not all(p["ready"] for p in seats)
        ):
            raise MonopolyError("Нужны 2–10 игроков и готовность каждого", 409)
        self._reset_board()
        self.players = seats
        for p in seats:
            p.update(
                cash=1500, pos=0, jail=False, jail_attempts=0, bankrupt=False, cards=[]
            )
        contenders = [p["id"] for p in seats]
        for _ in range(100):
            rolled = {uid: self.roll_dice() for uid in contenders}
            self.opening_rolls.append(rolled)
            high = max(map(sum, rolled.values()))
            contenders = [uid for uid, dice in rolled.items() if sum(dice) == high]
            if len(contenders) == 1:
                break
        else:
            raise MonopolyError(
                "Не удалось определить первый ход. Попробуйте снова", 409
            )
        first = next(i for i, p in enumerate(seats) if p["id"] == contenders[0])
        self.players = seats[first:] + seats[:first]
        for cards in self.decks.values():
            secrets.SystemRandom().shuffle(cards)
        self.match_number += 1
        self.match = {
            "number": self.match_number,
            "participants": [{"id": p["id"], "name": p["name"]} for p in self.players],
            "entry_cents": self.rules["entry_cents"],
            "bank_cents": self.rules["entry_cents"] * len(seats),
            "result": None,
            "settled": False,
        }
        self.phase = "playing"
        self.actor = self.players[0]["id"]
        self.turn_number = 1
        self.touch(
            "Партия началась. Первый ход: "
            + self.players[0]["name"]
            + ". У каждого 1500 игровых денег"
        )

    def owned(self, uid):
        return [i for i in PROPERTIES if self.estate[str(i)]["owner"] == uid]

    def group(self, index):
        return GROUPS.get(PROPERTIES[index].get("group"), [index])

    def complete_group(self, uid, index):
        cell = PROPERTIES[index]
        return cell["kind"] == "street" and all(
            self.estate[str(i)]["owner"] == uid for i in self.group(index)
        )

    def building_kind(self, index):
        return "hotels" if self.estate[str(index)]["level"] == 4 else "houses"

    def buildable(self, uid, index):
        if index not in PROPERTIES or PROPERTIES[index]["kind"] != "street":
            return False
        state = self.estate[str(index)]
        group = [self.estate[str(i)] for i in self.group(index)]
        return (
            self.complete_group(uid, index)
            and state["level"] < 5
            and not any(s["mortgaged"] for s in group)
            and state["level"] == min(s["level"] for s in group)
        )

    def available_builders(self, kind):
        return [
            p["id"]
            for p in self.living()
            if any(
                self.buildable(p["id"], i) and self.building_kind(i) == kind
                for i in self.owned(p["id"])
            )
        ]

    def rent(self, index, dice, rail_multiplier=1, utility_multiplier=None):
        cell, state = PROPERTIES[index], self.estate[str(index)]
        uid = state["owner"]
        if uid is None or state["mortgaged"]:
            return 0
        if cell["kind"] == "street":
            return cell["rents"][state["level"]] * (
                2 if not state["level"] and self.complete_group(uid, index) else 1
            )
        count = sum(PROPERTIES[i]["kind"] == cell["kind"] for i in self.owned(uid))
        if cell["kind"] == "rail":
            return 25 * 2 ** (count - 1) * rail_multiplier
        return dice * (
            utility_multiplier
            if utility_multiplier is not None
            else 10
            if count == 2
            else 4
        )

    def liquidation_value(self, uid):
        value = self.player(uid)["cash"]
        for i in self.owned(uid):
            cell, state = PROPERTIES[i], self.estate[str(i)]
            value += cell.get("build_cost", 0) * state["level"] // 2
            if not state["mortgaged"]:
                value += cell["mortgage"]
        return value

    def _prepend(self, *tasks):
        self.tasks = list(tasks) + self.tasks

    def _pay_now(self, uid, creditor, amount, reason):
        p = self.player(uid)
        p["cash"] -= amount
        if creditor is not None:
            self.player(creditor)["cash"] += amount
        self.log(f"{p['name']}: выплата {amount} · {reason}")

    def _pay_task(self, task):
        uid = task["uid"]
        if not self.alive(uid):
            return
        creditor = task.get("creditor")
        if creditor is not None and not self.alive(creditor):
            creditor = None
        amount = task["amount"]
        if self.player(uid)["cash"] >= amount:
            self._pay_now(uid, creditor, amount, task["reason"])
            if task.get("after"):
                self._prepend(task["after"])
        else:
            self.pending = {
                "type": "debt",
                "uid": uid,
                "creditor": creditor,
                "amount": amount,
                "reason": task["reason"],
                "after": task.get("after"),
            }
            self.log(
                f"{self.player(uid)['name']}: нужно погасить долг {amount} · {task['reason']}"
            )

    def _resolve_debt(self):
        debt = self.pending
        if not debt or debt["type"] != "debt":
            return
        if self.player(debt["uid"])["cash"] < debt["amount"]:
            return
        self.pending = None
        self._pay_now(debt["uid"], debt["creditor"], debt["amount"], debt["reason"])
        if debt.get("after"):
            self._prepend(debt["after"])

    def _move(self, task):
        uid = task["uid"]
        if not self.alive(uid):
            return
        p = self.player(uid)
        old = p["pos"]
        if "steps" in task:
            target = (old + task["steps"]) % 40
            salary = task["steps"] > 0 and old + task["steps"] >= 40
        else:
            target = task["to"]
            salary = target < old
        if salary:
            p["cash"] += 200
            self.log(p["name"] + ": пересечение Старта, +200")
        p["pos"] = target
        self.log(p["name"] + ": " + BOARD[target]["name"])
        self._land(
            uid, task.get("rail_multiplier", 1), task.get("special_utility", False)
        )

    def _land(self, uid, rail_multiplier=1, special_utility=False):
        p = self.player(uid)
        index = p["pos"]
        cell = BOARD[index]
        kind = cell["kind"]
        if index in PROPERTIES:
            owner_id = self.estate[str(index)]["owner"]
            if owner_id is None:
                self.pending = {"type": "buy", "uid": uid, "property": index}
            elif owner_id != uid and not self.estate[str(index)]["mortgaged"]:
                dice = sum(self.dice)
                multiplier = None
                if special_utility and kind == "utility":
                    self.last_rent_dice = self.roll_dice()
                    dice = sum(self.last_rent_dice)
                    multiplier = 10
                amount = self.rent(index, dice, rail_multiplier, multiplier)
                self._prepend(
                    {
                        "kind": "pay",
                        "uid": uid,
                        "creditor": owner_id,
                        "amount": amount,
                        "reason": "Рента: " + cell["name"],
                    }
                )
        elif kind in DECKS:
            self._prepend({"kind": "draw", "uid": uid, "deck": kind})
        elif kind == "tax":
            self._prepend(
                {
                    "kind": "pay",
                    "uid": uid,
                    "amount": cell["tax"],
                    "reason": cell["name"],
                }
            )
        elif kind == "go_jail":
            self._jail(uid)

    def _jail(self, uid):
        p = self.player(uid)
        p.update(pos=10, jail=True, jail_attempts=0)
        self.doubles = 0
        self.turn_action = "end"
        self.log(p["name"] + ": отправляется в тюрьму без премии за Старт")

    def _draw(self, uid, deck):
        if not self.alive(uid):
            return
        card_id = self.decks[deck].pop(0)
        card = DECKS[deck][card_id]
        p = self.player(uid)
        self.last_card = {"deck": deck, "text": card["text"], "by": uid}
        self.log(("Шанс" if deck == "chance" else "Казна") + ": " + card["text"])
        effect = card["effect"]
        if effect == "jail_card":
            p["cards"].append(deck)
            return
        self.decks[deck].append(card_id)
        if effect == "cash":
            if card["amount"] >= 0:
                p["cash"] += card["amount"]
            else:
                self._prepend(
                    {
                        "kind": "pay",
                        "uid": uid,
                        "amount": -card["amount"],
                        "reason": card["text"],
                    }
                )
        elif effect == "go":
            p["cash"] += 200
            p["pos"] = 0
        elif effect == "jail":
            self._jail(uid)
        elif effect == "advance":
            self._prepend({"kind": "move", "uid": uid, "to": card["to"]})
        elif effect == "back":
            self._prepend({"kind": "move", "uid": uid, "steps": -card["steps"]})
        elif effect in ("rail", "utility"):
            targets = [i for i, c in PROPERTIES.items() if c["kind"] == effect]
            target = min(targets, key=lambda i: (i - p["pos"]) % 40 or 40)
            self._prepend(
                {
                    "kind": "move",
                    "uid": uid,
                    "to": target,
                    "rail_multiplier": 2 if effect == "rail" else 1,
                    "special_utility": effect == "utility",
                }
            )
        elif effect == "repairs":
            houses = sum(
                s["level"]
                for s in self.estate.values()
                if s["owner"] == uid and s["level"] < 5
            )
            hotels = sum(
                s["owner"] == uid and s["level"] == 5 for s in self.estate.values()
            )
            self._prepend(
                {
                    "kind": "pay",
                    "uid": uid,
                    "amount": houses * card["house"] + hotels * card["hotel"],
                    "reason": card["text"],
                }
            )
        elif effect == "each":
            order = self._clockwise(uid)
            tasks = []
            for other in order:
                if other == uid or not self.alive(other):
                    continue
                tasks.append(
                    {
                        "kind": "pay",
                        "uid": other if card["amount"] > 0 else uid,
                        "creditor": uid if card["amount"] > 0 else other,
                        "amount": abs(card["amount"]),
                        "reason": card["text"],
                    }
                )
            self._prepend(*tasks)

    def _clockwise(self, uid):
        index = next((i for i, p in enumerate(self.players) if p["id"] == uid), -1)
        return [
            self.players[(index + i) % len(self.players)]["id"]
            for i in range(1, len(self.players) + 1)
        ]

    def _next_turn(self):
        self.actor = next(uid for uid in self._clockwise(self.actor) if self.alive(uid))
        self.turn_action = "roll"
        self.doubles = 0
        self.turn_number += 1
        self.log("Ход: " + self.player(self.actor)["name"])

    def _drain(self):
        for _ in range(200):
            if self.phase != "playing":
                return
            if self.pending:
                if self.pending["type"] == "debt":
                    self._resolve_debt()
                if self.pending:
                    return
            if not self.tasks:
                if not self.alive(self.actor):
                    self._next_turn()
                return
            task = self.tasks.pop(0)
            kind = task["kind"]
            if kind == "pay":
                self._pay_task(task)
            elif kind == "move":
                self._move(task)
            elif kind == "draw":
                self._draw(task["uid"], task["deck"])
            elif kind == "release":
                if self.alive(task["uid"]):
                    self.player(task["uid"]).update(jail=False, jail_attempts=0)
            elif kind == "release_move":
                if self.alive(task["uid"]):
                    self.player(task["uid"]).update(jail=False, jail_attempts=0)
                    self._prepend(
                        {"kind": "move", "uid": task["uid"], "steps": task["steps"]}
                    )
            elif kind == "auction":
                self._start_auction("property", task["property"])
            elif kind == "transfer":
                if (
                    self.alive(task["uid"])
                    and self.estate[str(task["property"])]["owner"] == task["uid"]
                ):
                    self.pending = {
                        "type": "transfer",
                        "uid": task["uid"],
                        "property": task["property"],
                    }
            elif kind == "redeem_transfer":
                if (
                    self.alive(task["uid"])
                    and self.estate[str(task["property"])]["owner"] == task["uid"]
                ):
                    self.estate[str(task["property"])]["mortgaged"] = False
            else:
                raise MonopolyError("Неизвестная задача партии", 409)
        raise MonopolyError("Не удалось завершить цепочку карточек", 409)

    def _roll(self, uid):
        if uid != self.actor or self.turn_action != "roll" or self.pending:
            raise MonopolyError("Сейчас нельзя бросать кубики", 409)
        p = self.player(uid)
        self.dice = self.roll_dice()
        double = self.dice[0] == self.dice[1]
        self.log(f"{p['name']}: кубики {self.dice[0]} + {self.dice[1]}")
        if p["jail"]:
            self.turn_action = "end"
            if double:
                p.update(jail=False, jail_attempts=0)
                self._prepend({"kind": "move", "uid": uid, "steps": sum(self.dice)})
            else:
                p["jail_attempts"] += 1
                if p["jail_attempts"] >= 3:
                    self._prepend(
                        {
                            "kind": "pay",
                            "uid": uid,
                            "amount": 50,
                            "reason": "Третий бросок в тюрьме",
                            "after": {
                                "kind": "release_move",
                                "uid": uid,
                                "steps": sum(self.dice),
                            },
                        }
                    )
        else:
            self.doubles = self.doubles + 1 if double else 0
            self.turn_action = "roll" if double else "end"
            if self.doubles >= 3:
                self._jail(uid)
            else:
                self._prepend({"kind": "move", "uid": uid, "steps": sum(self.dice)})

    def _start_auction(self, kind, index=None, first=None):
        if self.pending:
            raise MonopolyError("Сначала завершите текущее действие", 409)
        eligible = (
            [p["id"] for p in self.living()]
            if kind == "property"
            else self.available_builders(kind)
        )
        if not eligible:
            return
        self.pending = {
            "type": "auction",
            "kind": kind,
            "property": index,
            "eligible": eligible,
            "passed": [],
            "bid": 0,
            "winner": None,
            "target": None,
            "bidder": first
            if first in eligible
            else self.actor
            if self.actor in eligible
            else eligible[0],
        }
        self.log(
            "Аукцион: "
            + (
                PROPERTIES[index]["name"]
                if kind == "property"
                else "дом"
                if kind == "houses"
                else "отель"
            )
        )

    def _auction_next(self, last):
        auction = self.pending
        candidates = [
            uid
            for uid in self._clockwise(last)
            if uid in auction["eligible"]
            and uid not in auction["passed"]
            and uid != auction["winner"]
            and self.alive(uid)
        ]
        if candidates:
            auction["bidder"] = candidates[0]
            return
        self.pending = None
        uid = auction["winner"]
        if uid is not None:
            p = self.player(uid)
            if not self.alive(uid) or p["cash"] < auction["bid"]:
                raise MonopolyError("Ставка аукциона больше доступных денег", 409)
            p["cash"] -= auction["bid"]
            if auction["kind"] == "property":
                self.estate[str(auction["property"])]["owner"] = uid
            else:
                self._add_building(auction["target"])
            self.log(p["name"] + ": выиграл аукцион за " + str(auction["bid"]))
        else:
            self.log("Аукцион завершён без покупки")

    def _bid(self, uid, data):
        auction = self.pending
        if not auction or auction["type"] != "auction" or auction["bidder"] != uid:
            raise MonopolyError("Сейчас торгуется другой игрок", 409)
        amount = integer(data.get("amount"), auction["bid"] + 1)
        if self.player(uid)["cash"] < amount:
            raise MonopolyError("Недостаточно игровых денег для ставки", 409)
        target = None
        if auction["kind"] != "property":
            target = self._property_id(data.get("property"))
            if (
                not self.buildable(uid, target)
                or self.building_kind(target) != auction["kind"]
            ):
                raise MonopolyError("Выберите подходящий участок для строения", 409)
        auction.update(bid=amount, winner=uid, target=target)
        self._auction_next(uid)

    def _property_id(self, index):
        integer(index, 0, 39)
        if index not in PROPERTIES:
            raise MonopolyError("На этой клетке нет продаваемого объекта")
        return index

    def _require_owned(self, uid, index):
        self._property_id(index)
        if self.estate[str(index)]["owner"] != uid:
            raise MonopolyError("Этот объект вам не принадлежит", 403)
        pending = self.pending
        if (
            pending
            and pending["type"] == "auction"
            and pending["kind"] != "property"
            and pending["winner"] == uid
            and index in self.group(pending["target"])
        ):
            raise MonopolyError("Этот район зарезервирован вашей ставкой аукциона", 409)

    def _may_spend(self):
        if self.pending and self.pending["type"] in ("debt", "auction", "transfer"):
            raise MonopolyError(
                "Сначала завершите долг, аукцион или передачу залога", 409
            )

    def _add_building(self, index):
        state = self.estate[str(index)]
        kind = self.building_kind(index)
        if not self.buildable(state["owner"], index) or self.bank[kind] <= 0:
            raise MonopolyError("Сейчас это строение недоступно", 409)
        self.bank[kind] -= 1
        if kind == "hotels":
            self.bank["houses"] += 4
        state["level"] += 1

    def _build(self, uid, index):
        self._require_owned(uid, index)
        self._may_spend()
        if self.pending:
            raise MonopolyError("Сначала завершите покупку", 409)
        if not self.buildable(uid, index):
            raise MonopolyError(
                "Нужен полный незаложенный район и равномерная застройка", 409
            )
        kind = self.building_kind(index)
        if self.bank[kind] <= 0:
            raise MonopolyError("В банке закончились строения этого типа", 409)
        if self.bank[kind] < len(self.available_builders(kind)):
            self._start_auction(kind, first=uid)
            return
        cost = PROPERTIES[index]["build_cost"]
        if self.player(uid)["cash"] < cost:
            raise MonopolyError("Недостаточно игровых денег", 409)
        self.player(uid)["cash"] -= cost
        self._add_building(index)
        self.log(
            self.player(uid)["name"] + ": строительство на " + PROPERTIES[index]["name"]
        )

    def _sell(self, uid, index, whole=False):
        self._require_owned(uid, index)
        state = self.estate[str(index)]
        if not state["level"]:
            raise MonopolyError("На участке нет строений", 409)
        indexes = self.group(index) if whole else [index]
        if not whole:
            if state["level"] != max(
                self.estate[str(i)]["level"] for i in self.group(index)
            ):
                raise MonopolyError("Продавайте строения равномерно", 409)
            if state["level"] == 5 and self.bank["houses"] < 4:
                raise MonopolyError(
                    "Для размена отеля нужны 4 дома. Можно продать все строения района",
                    409,
                )
            if state["level"] == 5:
                self.bank["hotels"] += 1
                self.bank["houses"] -= 4
            else:
                self.bank["houses"] += 1
            state["level"] -= 1
            self.player(uid)["cash"] += PROPERTIES[index]["build_cost"] // 2
        else:
            for i in indexes:
                other = self.estate[str(i)]
                if other["owner"] != uid:
                    raise MonopolyError("Район не принадлежит вам полностью", 409)
                self.player(uid)["cash"] += (
                    PROPERTIES[i]["build_cost"] * other["level"] // 2
                )
                self.bank["hotels"] += int(other["level"] == 5)
                self.bank["houses"] += other["level"] if other["level"] < 5 else 0
                other["level"] = 0
        self.log(self.player(uid)["name"] + ": строения проданы банку за половину цены")

    def _mortgage(self, uid, index, redeem=False):
        self._require_owned(uid, index)
        state = self.estate[str(index)]
        cell = PROPERTIES[index]
        if redeem:
            self._may_spend()
            if not state["mortgaged"]:
                raise MonopolyError("Этот объект не заложен", 409)
            cost = cell["mortgage"] + cell["interest"]
            if self.player(uid)["cash"] < cost:
                raise MonopolyError("Недостаточно денег для выкупа", 409)
            self.player(uid)["cash"] -= cost
            state["mortgaged"] = False
        else:
            if state["mortgaged"] or any(
                self.estate[str(i)]["level"] for i in self.group(index)
            ):
                raise MonopolyError(
                    "Сначала продайте строения района; повторный залог запрещён", 409
                )
            state["mortgaged"] = True
            self.player(uid)["cash"] += cell["mortgage"]
        self.log(
            self.player(uid)["name"]
            + (": выкуп залога · " if redeem else ": залог · ")
            + cell["name"]
        )

    def _leg(self, leg, uid):
        if not isinstance(leg, dict) or set(leg) != {"cash", "properties", "cards"}:
            raise MonopolyError("Некорректная сторона сделки")
        cash = integer(leg["cash"])
        props = leg["properties"]
        cards = leg["cards"]
        if (
            not isinstance(props, list)
            or len(props) > 28
            or any(type(i) is not int for i in props)
            or len(set(props)) != len(props)
        ):
            raise MonopolyError("Некорректный список объектов")
        if (
            not isinstance(cards, list)
            or len(cards) > 2
            or any(not isinstance(c, str) or c not in DECKS for c in cards)
            or len(set(cards)) != len(cards)
        ):
            raise MonopolyError("Некорректные карточки выхода из тюрьмы")
        for i in props:
            self._require_owned(uid, i)
            if any(self.estate[str(j)]["level"] for j in self.group(i)):
                raise MonopolyError("Перед сделкой продайте все строения района", 409)
            locked = self.pending
            if locked and locked["type"] == "transfer" and locked["property"] == i:
                raise MonopolyError("Сначала оплатите передачу залога", 409)
            if (
                locked
                and locked["type"] == "debt"
                and (locked.get("after") or {}).get("property") == i
            ):
                raise MonopolyError("Сначала оплатите передачу залога", 409)
        if any(c not in self.player(uid)["cards"] for c in cards):
            raise MonopolyError("У игрока нет указанных карточек", 409)
        return {"cash": cash, "properties": props[:], "cards": cards[:]}

    def _redeem_list(self, value, incoming):
        if (
            not isinstance(value, list)
            or len(value) > 28
            or any(type(i) is not int for i in value)
            or len(set(value)) != len(value)
        ):
            raise MonopolyError("Некорректный выбор выкупа")
        if any(
            i not in incoming or not self.estate[str(i)]["mortgaged"] for i in value
        ):
            raise MonopolyError("Выкупать можно получаемые заложенные объекты", 409)
        return value[:]

    def _trade_fees(self, leg, redeem):
        return sum(
            PROPERTIES[i]["interest"]
            + (PROPERTIES[i]["mortgage"] if i in redeem else 0)
            for i in leg["properties"]
            if self.estate[str(i)]["mortgaged"]
        )

    def _offer(self, uid, data):
        if self.pending and self.pending["type"] in ("auction", "transfer"):
            raise MonopolyError("Сначала завершите аукцион или передачу залога", 409)
        other = integer(data.get("to"), 1, 2**63 - 1)
        if other == uid or not self.alive(other):
            raise MonopolyError("Нужен другой действующий игрок", 409)
        give = self._leg(data.get("give"), uid)
        take = self._leg(data.get("take"), other)
        if not any(
            (
                give["cash"],
                take["cash"],
                give["properties"],
                take["properties"],
                give["cards"],
                take["cards"],
            )
        ):
            raise MonopolyError("Пустая сделка")
        redeem = self._redeem_list(data.get("redeem", []), take["properties"])
        states = {
            str(i): copy.deepcopy(self.estate[str(i)])
            for i in give["properties"] + take["properties"]
        }
        self.offers = [
            o for o in self.offers if o["from"] != uid and o["expires"] > time.time()
        ]
        self.offers.append(
            {
                "id": secrets.token_hex(8),
                "from": uid,
                "to": other,
                "give": give,
                "take": take,
                "redeem": redeem,
                "states": states,
                "expires": time.time() + 300,
            }
        )
        self.log(
            self.player(uid)["name"]
            + ": предложена сделка "
            + self.player(other)["name"]
        )

    def _accept_trade(self, uid, data):
        offer = next(
            (
                o
                for o in self.offers
                if o["id"] == data.get("offer_id")
                and o["to"] == uid
                and o["expires"] > time.time()
            ),
            None,
        )
        if offer is None or not self.alive(offer["from"]):
            raise MonopolyError("Предложение уже недоступно", 409)
        if self.pending and self.pending["type"] in ("auction", "transfer"):
            raise MonopolyError("Сначала завершите аукцион или передачу залога", 409)
        for key, state in offer["states"].items():
            if self.estate[key] != state:
                raise MonopolyError(
                    "Условия объектов изменились. Нужно новое предложение", 409
                )
        sender = offer["from"]
        give = self._leg(offer["give"], sender)
        take = self._leg(offer["take"], uid)
        redeem_other = self._redeem_list(data.get("redeem", []), give["properties"])
        redeem_sender = self._redeem_list(offer["redeem"], take["properties"])
        fees_sender = self._trade_fees(take, redeem_sender)
        fees_other = self._trade_fees(give, redeem_other)
        delta_sender = take["cash"] - give["cash"] - fees_sender
        delta_other = give["cash"] - take["cash"] - fees_other
        if (
            self.player(sender)["cash"] + delta_sender < 0
            or self.player(uid)["cash"] + delta_other < 0
        ):
            raise MonopolyError("Недостаточно денег для сделки и процентов банку", 409)
        if self.pending and self.pending["type"] == "debt":
            debtor = self.pending["uid"]
            delta = (
                delta_sender
                if debtor == sender
                else delta_other
                if debtor == uid
                else 0
            )
            if delta < 0:
                raise MonopolyError(
                    "Должник не может выводить игровые деньги сделкой", 409
                )
        self.player(sender)["cash"] += delta_sender
        self.player(uid)["cash"] += delta_other
        for source, dest, leg, redeem in (
            (sender, uid, give, redeem_other),
            (uid, sender, take, redeem_sender),
        ):
            for i in leg["properties"]:
                self.estate[str(i)]["owner"] = dest
                if i in redeem:
                    self.estate[str(i)]["mortgaged"] = False
            for deck in leg["cards"]:
                self.player(source)["cards"].remove(deck)
                self.player(dest)["cards"].append(deck)
        self.offers.remove(offer)
        self.log(
            self.player(sender)["name"]
            + " и "
            + self.player(uid)["name"]
            + ": сделка выполнена"
        )

    def _finish(self, winner):
        self.phase = "finished"
        self.match["result"] = {
            "winners": [winner],
            "reason": "Остался один небанкрот",
            "payouts": {},
        }
        self.actor = None
        self.pending = None
        self.tasks = []
        self.offers = []
        self.log("Победитель: " + self.player(winner)["name"])

    def _bankrupt(self, uid, creditor=None):
        p = self.player(uid)
        if not self.alive(uid):
            return
        if not self.alive(creditor):
            creditor = None
        indexes = self.owned(uid)
        refund = 0
        for i in indexes:
            state = self.estate[str(i)]
            level = state["level"]
            refund += PROPERTIES[i].get("build_cost", 0) * level // 2
            self.bank["hotels"] += int(level == 5)
            self.bank["houses"] += level if level < 5 else 0
            state["level"] = 0
        if creditor is not None:
            self.player(creditor)["cash"] += p["cash"] + refund
        p["cash"] = 0
        p["bankrupt"] = True
        additions = []
        for i in indexes:
            state = self.estate[str(i)]
            state["owner"] = creditor
            if creditor is None:
                state["mortgaged"] = False
                additions.append({"kind": "auction", "property": i})
            elif state["mortgaged"]:
                additions.append({"kind": "transfer", "uid": creditor, "property": i})
        for deck in p["cards"]:
            if creditor is None:
                self.decks[deck].append(JAIL_CARDS[deck])
            else:
                self.player(creditor)["cards"].append(deck)
        p["cards"] = []
        self.offers = [o for o in self.offers if uid not in (o["from"], o["to"])]
        self.tasks = [t for t in self.tasks if t.get("uid") != uid]
        if self.pending and self.pending["type"] == "debt":
            if self.pending["uid"] == uid:
                self.pending = None
            elif self.pending["creditor"] == uid:
                self.pending["creditor"] = None
        if self.pending and self.pending.get("uid") == uid:
            self.pending = None
        if self.pending and self.pending["type"] == "auction":
            auction = self.pending
            auction["eligible"] = [i for i in auction["eligible"] if self.alive(i)]
            if auction["winner"] == uid:
                auction.update(bid=0, winner=None, target=None, passed=[])
            auction["passed"] = [i for i in auction["passed"] if i != uid]
            if auction["bidder"] == uid or len(auction["eligible"]) == 1:
                self._auction_next(uid)
        self.tasks += additions
        self.log(
            p["name"]
            + ": банкротство"
            + (
                " · имущество передано " + self.player(creditor)["name"]
                if creditor
                else " · имущество возвращено банку"
            )
        )
        alive = self.living()
        if len(alive) == 1:
            self._finish(alive[0]["id"])

    @atomic
    def leave(self, uid):
        p = self.player(uid)
        if p is None or p["left"]:
            return
        if self.phase == "waiting":
            self.players.remove(p)
            for other in self.players:
                other["ready"] = False
        else:
            if self.phase == "playing" and not p["bankrupt"]:
                creditor = (
                    self.pending.get("creditor")
                    if self.pending
                    and self.pending["type"] == "debt"
                    and self.pending["uid"] == uid
                    else None
                )
                self._bankrupt(uid, creditor)
            p["left"] = True
            self._drain()
        if self.creator == uid and self.active_players():
            choices = self.living() or self.active_players()
            self.creator = choices[0]["id"]
        self.touch(p["name"] + ": выход со стола")

    @atomic
    def action(self, uid, kind, data=None):
        if self.phase != "playing" or not self.alive(uid):
            raise MonopolyError("Вы не действующий участник партии", 403)
        if data is None:
            data = {}
        allowed = {
            "roll": set(),
            "end_turn": set(),
            "buy": set(),
            "decline": set(),
            "pass_bid": set(),
            "bid": {"amount", "property"},
            "build": {"property"},
            "sell": {"property"},
            "sell_group": {"property"},
            "mortgage": {"property"},
            "unmortgage": {"property"},
            "pay_bail": set(),
            "jail_card": {"deck"},
            "bankrupt": set(),
            "transfer": {"redeem"},
            "trade_offer": {"to", "give", "take", "redeem"},
            "trade_accept": {"offer_id", "redeem"},
            "trade_cancel": {"offer_id"},
        }
        if (
            not isinstance(kind, str)
            or kind not in allowed
            or not isinstance(data, dict)
            or set(data) - allowed[kind]
        ):
            raise MonopolyError("Некорректные поля действия")
        self.offers = [o for o in self.offers if o["expires"] > time.time()]
        if kind == "roll":
            self._roll(uid)
        elif kind == "end_turn":
            if uid != self.actor or self.turn_action != "end" or self.pending:
                raise MonopolyError("Сначала завершите действия хода", 409)
            self._next_turn()
        elif kind in ("buy", "decline"):
            offer = self.pending
            if not offer or offer["type"] != "buy" or offer["uid"] != uid:
                raise MonopolyError("Сейчас нечего покупать", 409)
            index = offer["property"]
            if kind == "buy":
                cost = PROPERTIES[index]["price"]
                if self.player(uid)["cash"] < cost:
                    raise MonopolyError(
                        "Недостаточно денег. Можно заложить имущество или запустить аукцион",
                        409,
                    )
                self.player(uid)["cash"] -= cost
                self.estate[str(index)]["owner"] = uid
                self.pending = None
                self.log(
                    self.player(uid)["name"] + ": покупка " + PROPERTIES[index]["name"]
                )
            else:
                self.pending = None
                self._start_auction("property", index, uid)
        elif kind == "bid":
            self._bid(uid, data)
        elif kind == "pass_bid":
            auction = self.pending
            if not auction or auction["type"] != "auction" or auction["bidder"] != uid:
                raise MonopolyError("Сейчас торгуется другой игрок", 409)
            auction["passed"].append(uid)
            self._auction_next(uid)
        elif kind in ("build", "sell", "sell_group", "mortgage", "unmortgage"):
            index = self._property_id(data.get("property"))
            if kind == "build":
                self._build(uid, index)
            elif kind in ("sell", "sell_group"):
                self._sell(uid, index, kind == "sell_group")
            else:
                self._mortgage(uid, index, kind == "unmortgage")
        elif kind == "pay_bail":
            p = self.player(uid)
            if (
                uid != self.actor
                or self.turn_action != "roll"
                or self.pending
                or not p["jail"]
            ):
                raise MonopolyError("Сейчас нельзя оплатить выход", 409)
            self._prepend(
                {
                    "kind": "pay",
                    "uid": uid,
                    "amount": 50,
                    "reason": "Выход из тюрьмы",
                    "after": {"kind": "release", "uid": uid},
                }
            )
        elif kind == "jail_card":
            p = self.player(uid)
            deck = data.get("deck")
            if (
                uid != self.actor
                or self.turn_action != "roll"
                or self.pending
                or not p["jail"]
                or deck not in p["cards"]
            ):
                raise MonopolyError("Карточка сейчас недоступна", 409)
            p["cards"].remove(deck)
            self.decks[deck].append(JAIL_CARDS[deck])
            p.update(jail=False, jail_attempts=0)
        elif kind == "transfer":
            pending = self.pending
            if (
                not pending
                or pending["type"] != "transfer"
                or pending["uid"] != uid
                or type(data.get("redeem")) is not bool
            ):
                raise MonopolyError("Сейчас нет передачи залога", 409)
            index = pending["property"]
            cell = PROPERTIES[index]
            self.pending = None
            after = (
                {"kind": "redeem_transfer", "uid": uid, "property": index}
                if data["redeem"]
                else None
            )
            self._prepend(
                {
                    "kind": "pay",
                    "uid": uid,
                    "amount": cell["interest"]
                    + (cell["mortgage"] if data["redeem"] else 0),
                    "reason": "Передача залога: " + cell["name"],
                    "after": after,
                }
            )
        elif kind == "bankrupt":
            debt = self.pending
            if not debt or debt["type"] != "debt" or debt["uid"] != uid:
                raise MonopolyError("Сейчас нет вашего долга", 409)
            if self.liquidation_value(uid) >= debt["amount"]:
                raise MonopolyError(
                    "Долг можно погасить продажей строений и залогом имущества", 409
                )
            self._bankrupt(uid, debt["creditor"])
        elif kind == "trade_offer":
            self._offer(uid, data)
        elif kind == "trade_accept":
            self._accept_trade(uid, data)
        elif kind == "trade_cancel":
            offer = next(
                (
                    o
                    for o in self.offers
                    if o["id"] == data.get("offer_id") and uid in (o["from"], o["to"])
                ),
                None,
            )
            if offer is None:
                raise MonopolyError("Предложение уже недоступно", 409)
            self.offers.remove(offer)
        self._drain()
        self.touch()

    @atomic
    def rematch(self):
        seats = self.active_players()
        if self.phase != "finished" or not self.match["settled"] or len(seats) < 2:
            raise MonopolyError(
                "Для новой партии нужны два игрока и рассчитанный результат", 409
            )
        self._reset_board()
        self.players = seats
        for p in seats:
            p.update(
                ready=False,
                bankrupt=False,
                cash=1500,
                pos=0,
                jail=False,
                jail_attempts=0,
                cards=[],
            )
        self.match = None
        self.phase = "waiting"
        self.touch("Новая партия. Подтвердите готовность")

    def validate(self):
        ids = [p["id"] for p in self.players]
        if len(ids) != len(set(ids)) or len(ids) > 10:
            raise MonopolyError("Повреждён состав партии", 409)
        if self.phase not in ("waiting", "playing", "finished"):
            raise MonopolyError("Повреждена фаза партии", 409)
        for p in self.players:
            if (
                type(p["cash"]) is not int
                or p["cash"] < 0
                or type(p["pos"]) is not int
                or not 0 <= p["pos"] < 40
            ):
                raise MonopolyError("Повреждены деньги или позиция игрока", 409)
        if self.phase != "waiting":
            match = self.match
            participants = [p["id"] for p in match["participants"]] if match else []
            if (
                not match
                or not 2 <= len(participants) <= 10
                or len(set(participants)) != len(participants)
                or set(participants) != set(ids)
                or type(match["entry_cents"]) is not int
                or match["entry_cents"] != self.rules["entry_cents"]
                or match["bank_cents"] != match["entry_cents"] * len(participants)
            ):
                raise MonopolyError("Повреждены участники или банк партии", 409)
            result = match["result"]
            if self.phase == "playing" and (result or match["settled"]):
                raise MonopolyError("Активная партия уже рассчитана", 409)
            if self.phase == "finished" and (
                not result
                or len(result["winners"]) != 1
                or result["winners"][0] not in participants
                or self.player(result["winners"][0])["bankrupt"]
            ):
                raise MonopolyError("Неверный победитель партии", 409)
        if set(self.estate) != set(map(str, PROPERTIES)):
            raise MonopolyError("Повреждены объекты поля", 409)
        houses = hotels = 0
        for key, state in self.estate.items():
            level = state["level"]
            if (
                type(level) is not int
                or not 0 <= level <= 5
                or state["owner"] is not None
                and state["owner"] not in ids
            ):
                raise MonopolyError("Повреждена собственность", 409)
            if level and (
                state["mortgaged"] or PROPERTIES[int(key)]["kind"] != "street"
            ):
                raise MonopolyError("Неверная застройка", 409)
            if state["owner"] is None and (level or state["mortgaged"]):
                raise MonopolyError("Неверная собственность банка", 409)
            houses += level if level < 5 else 0
            hotels += int(level == 5)
        for indexes in GROUPS.values():
            group = [self.estate[str(i)] for i in indexes]
            levels = [a["level"] for a in group]
            if max(levels) and (
                max(levels) - min(levels) > 1
                or len({a["owner"] for a in group}) != 1
                or any(a["mortgaged"] for a in group)
            ):
                raise MonopolyError("Нарушена равномерная застройка района", 409)
        if (
            self.bank["houses"] + houses != 32
            or self.bank["hotels"] + hotels != 12
            or self.bank["houses"] < 0
            or self.bank["hotels"] < 0
        ):
            raise MonopolyError("Не сходится запас домов и отелей", 409)
        for deck in DECKS:
            cards = self.decks[deck] + [
                JAIL_CARDS[deck]
                for p in self.players
                for held in p["cards"]
                if held == deck
            ]
            if sorted(cards) != list(range(16)):
                raise MonopolyError("Не сходятся карточки колоды", 409)
        if self.phase == "playing" and (
            self.actor not in ids or len(self.living()) < 2
        ):
            raise MonopolyError("Повреждена очередь хода", 409)

    def to_dict(self):
        return copy.deepcopy(self.__dict__)

    @classmethod
    def from_dict(cls, data):
        obj = cls.__new__(cls)
        obj.__dict__.update(copy.deepcopy(data))
        obj.rules = settings(obj.rules)
        obj.validate()
        from table_lifecycle import initialise

        initialise(obj)
        return obj

    def public_state(self, uid):
        allowed = []
        if self.phase == "playing" and self.alive(uid):
            allowed = [
                "mortgage",
                "sell",
                "sell_group",
                "trade_offer",
                "trade_accept",
                "trade_cancel",
            ]
            if self.pending:
                kind = self.pending["type"]
                if kind == "buy" and self.pending["uid"] == uid:
                    allowed += ["buy", "decline"]
                elif kind == "auction" and self.pending["bidder"] == uid:
                    allowed += ["bid", "pass_bid"]
                elif (
                    kind == "debt"
                    and self.pending["uid"] == uid
                    and self.liquidation_value(uid) < self.pending["amount"]
                ):
                    allowed += ["bankrupt"]
                elif kind == "transfer" and self.pending["uid"] == uid:
                    allowed += ["transfer"]
            else:
                allowed += ["build", "unmortgage"]
                if self.actor == uid:
                    allowed.append("roll" if self.turn_action == "roll" else "end_turn")
                    if self.turn_action == "roll" and self.player(uid)["jail"]:
                        allowed += ["pay_bail", "jail_card"]
        return {
            "id": self.id,
            "your_id": uid,
            "creator": self.creator,
            "phase": self.phase,
            "revision": self.revision,
            "match_number": self.match_number,
            "rules": self.rules.copy(),
            "match": copy.deepcopy(self.match),
            "players": copy.deepcopy(self.players),
            "actor": self.actor,
            "turn_action": self.turn_action,
            "turn_number": self.turn_number,
            "dice": self.dice[:],
            "doubles": self.doubles,
            "opening_rolls": copy.deepcopy(self.opening_rolls),
            "board": copy.deepcopy(BOARD),
            "estate": copy.deepcopy(self.estate),
            "bank": self.bank.copy(),
            "pending": copy.deepcopy(self.pending),
            "last_card": copy.deepcopy(self.last_card),
            "last_rent_dice": self.last_rent_dice[:],
            "offers": [
                copy.deepcopy(o)
                for o in self.offers
                if uid in (o["from"], o["to"]) and o["expires"] > time.time()
            ],
            "allowed": allowed,
            "buildable": [i for i in self.owned(uid) if self.buildable(uid, i)],
            "liquidation": self.liquidation_value(uid) if self.player(uid) else 0,
            "events": copy.deepcopy(self.events),
        }
