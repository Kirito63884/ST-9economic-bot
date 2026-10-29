"""Durak rules and serializable tables. Wallet writes belong to the API transaction."""

import copy
import secrets
import time
from fractions import Fraction

SUITS = {"C": "♣", "D": "♦", "H": "♥", "S": "♠"}
VALUES = {11: "J", 12: "Q", 13: "K", 14: "A"}
DEFAULTS = {
    "deck_size": 36,
    "hand_size": 6,
    "max_players": 6,
    "variant": "throw",
    "throwers": "all",
    "attack_limit": 6,
    "unlimited_attack": False,
    "cheating": False,
    "first_five": False,
    "exchange_trump": False,
    "throw_after_take": True,
    "turn_seconds": 60,
    "private": False,
    "entry_cents": 1000,
}


class DurakError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def settings(values):
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise DurakError("Некорректные настройки стола")
    result = {**DEFAULTS, **values}
    for key in (
        "deck_size",
        "hand_size",
        "max_players",
        "attack_limit",
        "turn_seconds",
        "entry_cents",
    ):
        if type(result[key]) is not int:
            raise DurakError("Числовые настройки должны быть целыми")
    for key in (
        "first_five",
        "exchange_trump",
        "throw_after_take",
        "private",
        "unlimited_attack",
        "cheating",
    ):
        if type(result[key]) is not bool:
            raise DurakError("Некорректные переключатели правил")
    if result["deck_size"] not in (24, 36, 52) or not 4 <= result["hand_size"] <= 8:
        raise DurakError("Колода: 24/36/52 карты; на руках: от 4 до 8")
    if not 2 <= result["max_players"] <= 6:
        raise DurakError("Игроков: от 2 до 6")
    if result["hand_size"] * result["max_players"] > result["deck_size"]:
        raise DurakError("В колоде не хватит карт для выбранного числа игроков")
    if result["variant"] not in ("throw", "transfer") or result["throwers"] not in (
        "all",
        "neighbors",
    ):
        raise DurakError("Неизвестный вариант дурака")
    if not 1 <= result["attack_limit"] <= 8 or result["turn_seconds"] not in (
        30,
        60,
        90,
        120,
    ):
        raise DurakError("Лимит нападения: 1–8 карт; таймер: 30/60/90/120 секунд")
    if not 0 <= result["entry_cents"] <= 100_000_000:
        raise DurakError("Взнос: от 0 до 1 000 000 БК")
    return result


def card(cid):
    suit, rank = cid[0], int(cid[1:])
    return {
        "id": cid,
        "suit": SUITS[suit],
        "rank": rank,
        "value": VALUES.get(rank, str(rank)),
        "red": suit in "DH",
    }


def beats(defense, attack, trump):
    return (defense[0] == attack[0] and int(defense[1:]) > int(attack[1:])) or (
        defense[0] == trump and attack[0] != trump
    )


def ranked_payouts(bank, groups):
    """Descending weights; tied finishers share their positions. Exact cents, no fee."""
    count = sum(map(len, groups))
    if not count:
        return {}
    weights = {}
    place = 0
    for group in groups:
        weight = Fraction(
            sum(range(count - place - len(group) + 1, count - place + 1)), len(group)
        )
        for uid in group:
            weights[uid] = weight
        place += len(group)
    total = sum(weights.values())
    amounts = {uid: Fraction(bank) * weight / total for uid, weight in weights.items()}
    result = {uid: int(amount) for uid, amount in amounts.items()}
    # Allocate indivisible cents by largest remainder, then finishing position and ID.
    order = {uid: i for i, uid in enumerate(weights)}
    for uid in sorted(
        amounts, key=lambda uid: (-(amounts[uid] - result[uid]), order[uid], uid)
    )[: bank - sum(result.values())]:
        result[uid] += 1
    return result


class DurakTable:
    def __init__(self, tid, creator, name, rules=None):
        self.id = tid
        self.creator = creator
        self.rules = settings(rules or {})
        self.players = []
        self.phase = "waiting"
        self.created_at = self.updated_at = time.time()
        self.last_player_activity = self.created_at
        self.revision = 0
        self.match_number = 0
        self.match = None
        self.deck = []
        self.discard = []
        self.board = []
        self.trump = None
        self.trump_card = None
        self.actor = None
        self.attacker = self.defender = self.draw_anchor = None
        self.deadline = None
        self.out_groups = []
        self.throw_queue = []
        self.events = []
        self.last_play = None
        self.add_player(creator, name)

    def to_dict(self):
        return copy.deepcopy(self.__dict__)

    @classmethod
    def from_dict(cls, data):
        table = cls.__new__(cls)
        table.__dict__.update(copy.deepcopy(data))
        # Old saved tables inherit the new options without changing their rules.
        table.rules = settings(table.rules)
        from table_lifecycle import initialise

        initialise(table)
        table.__dict__.setdefault("last_play", None)
        return table

    def player(self, uid):
        return next((p for p in self.players if p["id"] == uid), None)

    def touch(self, message=None):
        self.revision += 1
        self.updated_at = time.time()
        if message:
            self.events.append({"text": message, "time": self.updated_at})
            self.events = self.events[-40:]

    def set_turn(self, uid, phase):
        self.actor, self.phase = uid, phase
        self.deadline = time.time() + self.rules["turn_seconds"]

    def add_player(self, uid, name):
        if self.player(uid):
            return
        if self.phase != "waiting":
            raise DurakError("Присоединиться можно только до начала партии", 409)
        if len(self.players) >= self.rules["max_players"]:
            raise DurakError("За столом нет свободных мест", 409)
        self.players.append(
            {
                "id": uid,
                "name": name,
                "hand": [],
                "ready": False,
                "out": False,
                "timeouts": 0,
            }
        )
        # Joining changes bank and the payout schedule; everyone must consent again.
        for p in self.players:
            p["ready"] = False
        self.touch(f"{name} присоединился к столу")

    def active_ids(self):
        return [p["id"] for p in self.players if not p["out"]]

    def clockwise(self, anchor, ids=None, include_anchor=True):
        seats = [p["id"] for p in self.players]
        index = seats.index(anchor)
        sequence = seats[index:] + seats[:index]
        if not include_anchor:
            sequence = sequence[1:] + sequence[:1]
        allowed = set(self.active_ids() if ids is None else ids)
        return [uid for uid in sequence if uid in allowed]

    def next_active(self, uid):
        return self.clockwise(uid, include_anchor=False)[0]

    def change_settings(self, values):
        if self.phase not in ("waiting", "finished"):
            raise DurakError("Правила нельзя менять во время партии", 409)
        new = settings(values)
        if new["max_players"] < sum(not p.get("left") for p in self.players):
            raise DurakError("За столом уже больше игроков, чем выбранный лимит")
        if self.phase == "finished":
            self.rematch()
        self.rules = new
        for p in self.players:
            p["ready"] = False
        self.touch("Правила изменены. Подтвердите готовность заново")

    def start(self, deck=None):
        if (
            self.phase != "waiting"
            or len(self.players) < 2
            or not all(p["ready"] for p in self.players)
        ):
            raise DurakError(
                "Для старта нужны минимум двое и готовность всех игроков", 409
            )
        low = {24: 9, 36: 6, 52: 2}[self.rules["deck_size"]]
        expected = [f"{s}{r}" for s in SUITS for r in range(low, 15)]
        self.deck = list(deck) if deck is not None else expected[:]
        if len(self.deck) != len(expected) or set(self.deck) != set(expected):
            raise DurakError("Некорректная колода")
        if deck is None:
            secrets.SystemRandom().shuffle(self.deck)
        self.trump_card = self.deck[-1]
        self.trump = self.trump_card[0]
        self.discard, self.board, self.out_groups = [], [], []
        self.last_play = None
        self.bout_number, self.exchange_done = 1, False
        self.match_number += 1
        self.match = {
            "number": self.match_number,
            "participants": [{"id": p["id"], "name": p["name"]} for p in self.players],
            "entry_cents": self.rules["entry_cents"],
            "bank_cents": self.rules["entry_cents"] * len(self.players),
            "result": None,
            "settled": False,
        }
        for p in self.players:
            p.update(hand=[], out=False, timeouts=0)
        for _ in range(self.rules["hand_size"]):
            for p in self.players:
                p["hand"].append(self.deck.pop(0))
        trump_holders = [
            (int(c[1:]), i, p["id"])
            for i, p in enumerate(self.players)
            for c in p["hand"]
            if c[0] == self.trump
        ]
        if trump_holders:
            first = min(trump_holders)[2]
        else:
            first = min(
                (min(int(c[1:]) for c in p["hand"]), i, p["id"])
                for i, p in enumerate(self.players)
            )[2]
        self.begin_bout(first)
        self.touch(f"Партия №{self.match_number} началась")

    def begin_bout(self, attacker):
        self.attacker = self.draw_anchor = attacker
        self.defender = self.next_active(attacker)
        self.capacity = len(self.player(self.defender)["hand"])
        self.limit = self.attack_capacity(self.defender)
        self.board, self.throw_queue = [], []
        self.taking = False
        self.set_turn(attacker, "attack")

    def attack_capacity(self, defender):
        if self.rules["unlimited_attack"]:
            return self.rules["deck_size"]
        return min(
            len(self.player(defender)["hand"]),
            self.rules["attack_limit"],
            5 if self.rules["first_five"] and self.bout_number == 1 else 8,
        )

    def eligible_throwers(self):
        ids = self.active_ids()
        if self.rules["throwers"] == "neighbors":
            ordered = self.clockwise(self.defender)
            ids = [ordered[1], ordered[-1]]
        return self.clockwise(
            self.attacker, [uid for uid in ids if uid != self.defender]
        )

    def table_ranks(self):
        return {
            int(c[1:])
            for pair in self.board
            for c in (pair["attack"], pair["defense"])
            if c
        }

    def throw_cards(self, uid):
        if (
            len(self.board) >= self.limit
            or self.taking
            and not self.rules["throw_after_take"]
        ):
            return []
        ranks = self.table_ranks()
        return [
            c
            for c in self.player(uid)["hand"]
            if self.rules["cheating"] or int(c[1:]) in ranks
        ]

    def next_thrower(self):
        # Having no legal cards does not imply consent to clearing the board.
        # Every eligible player passes explicitly, including an empty hand.
        while self.throw_queue and (
            self.throw_queue[0] not in self.active_ids()
            or self.throw_queue[0] == self.defender
        ):
            self.throw_queue.pop(0)
        if not self.throw_queue:
            self.end_bout()
        else:
            self.set_turn(self.throw_queue[0], "throw")

    def begin_throw(self):
        self.throw_queue = self.eligible_throwers()
        self.next_thrower()

    def can_transfer(self, uid, cid):
        if (
            self.rules["variant"] != "transfer"
            or self.phase != "defend"
            or uid != self.defender
        ):
            return False
        if any(pair["defense"] for pair in self.board) or not self.board:
            return False
        ranks = {int(pair["attack"][1:]) for pair in self.board}
        nxt = self.next_active(uid)
        cap = self.attack_capacity(nxt)
        return len(ranks) == 1 and int(cid[1:]) in ranks and len(self.board) + 1 <= cap

    def exchange_card(self, uid):
        if (
            not self.rules["exchange_trump"]
            or self.phase != "attack"
            or self.board
            or not self.deck
            or self.exchange_done
        ):
            return None
        p = self.player(uid)
        low = {24: 9, 36: 6, 52: 2}[self.rules["deck_size"]]
        cid = f"{self.trump}{low}"
        return cid if p and not p["out"] and cid in p["hand"] else None

    def legal(self, uid):
        result = {
            "attack_cards": [],
            "defenses": {},
            "transfer_cards": [],
            "can_take": False,
            "can_pass": False,
            "exchange_card": None,
            "can_catch": False,
        }
        p = self.player(uid)
        last = self.last_play
        result["can_catch"] = bool(
            p
            and not p.get("left")
            and self.rules["cheating"]
            and self.phase not in ("waiting", "finished")
            and last
            and last["player"] != uid
            and uid not in last["checked_by"]
        )
        if not p or self.phase in ("waiting", "finished") or p["out"]:
            return result
        result["exchange_card"] = self.exchange_card(uid)
        if uid != self.actor:
            return result
        if self.phase == "attack":
            result["attack_cards"] = p["hand"][:]
        elif self.phase == "throw":
            result["attack_cards"] = self.throw_cards(uid)
            result["can_pass"] = True
        elif self.phase == "defend":
            result["can_take"] = True
            result["defenses"] = {
                c: [
                    i
                    for i, pair in enumerate(self.board)
                    if not pair["defense"]
                    and (self.rules["cheating"] or beats(c, pair["attack"], self.trump))
                ]
                for c in p["hand"]
            }
            result["transfer_cards"] = [
                c for c in p["hand"] if self.can_transfer(uid, c)
            ]
        return result

    def action(self, uid, kind, cid=None, target=None, automatic=False):
        if not isinstance(kind, str) or (cid is not None and not isinstance(cid, str)):
            raise DurakError("Некорректное действие или карта")
        p = self.player(uid)
        if not p:
            raise DurakError("Вы не участник этого стола", 403)
        legal = self.legal(uid)
        if kind == "catch":
            return self.catch(uid, cid)
        checkpoint = None
        if (
            self.rules["cheating"]
            and kind in ("attack", "defend", "transfer")
            and cid in p["hand"]
        ):
            checkpoint = {
                "player": uid,
                "card": cid,
                "kind": kind,
                "target": target,
                "honest": self.phase == "attack"
                or kind == "transfer"
                or (kind == "attack" and int(cid[1:]) in self.table_ranks())
                or (
                    kind == "defend"
                    and type(target) is int
                    and 0 <= target < len(self.board)
                    and beats(cid, self.board[target]["attack"], self.trump)
                ),
                "hand_index": p["hand"].index(cid),
                "checked_by": [],
                "board_before": copy.deepcopy(self.board),
                "turn": {
                    key: copy.deepcopy(getattr(self, key))
                    for key in (
                        "phase",
                        "actor",
                        "attacker",
                        "defender",
                        "draw_anchor",
                        "capacity",
                        "limit",
                        "taking",
                        "throw_queue",
                    )
                },
            }
        if kind == "exchange":
            if not legal["exchange_card"]:
                raise DurakError("Обмен козыря сейчас недоступен", 409)
            low = legal["exchange_card"]
            p["hand"].remove(low)
            p["hand"].append(self.trump_card)
            self.deck[-1] = self.trump_card = low
            self.exchange_done = True
        elif kind == "attack":
            if cid not in legal["attack_cards"]:
                raise DurakError("Эту карту нельзя подкинуть сейчас", 409)
            p["hand"].remove(cid)
            self.board.append({"attack": cid, "defense": None})
            if self.taking:
                self.next_thrower()
            else:
                self.set_turn(self.defender, "defend")
        elif kind == "defend":
            if type(target) is not int or target not in legal["defenses"].get(cid, []):
                raise DurakError("Этой картой нельзя отбить выбранную карту", 409)
            p["hand"].remove(cid)
            self.board[target]["defense"] = cid
            if all(pair["defense"] for pair in self.board):
                self.begin_throw()
            else:
                self.set_turn(uid, "defend")
        elif kind == "transfer":
            if cid not in legal["transfer_cards"]:
                raise DurakError("Перевод этой картой недоступен", 409)
            p["hand"].remove(cid)
            self.board.append({"attack": cid, "defense": None})
            self.attacker, self.defender = uid, self.next_active(uid)
            self.capacity = len(self.player(self.defender)["hand"])
            self.limit = self.attack_capacity(self.defender)
            self.set_turn(self.defender, "defend")
        elif kind == "take":
            if not legal["can_take"]:
                raise DurakError("Взять карты сейчас нельзя", 409)
            self.taking = True
            self.begin_throw()
        elif kind == "pass":
            if not legal["can_pass"]:
                raise DurakError("Завершить подкидывание сейчас нельзя", 409)
            self.throw_queue.pop(0)
            self.next_thrower()
        else:
            raise DurakError("Неизвестное действие")
        if checkpoint:
            self.last_play = checkpoint
        if not automatic:
            p["timeouts"] = 0
        names = {
            "attack": "подкинул карту",
            "defend": "отбил карту",
            "transfer": "перевёл нападение",
            "take": "берёт карты",
            "pass": "закончил подкидывать",
            "exchange": "обменял козырь",
        }
        self.touch(
            ("По таймеру: " if automatic else "") + p["name"] + " — " + names[kind]
        )

    def catch(self, uid, cid):
        if not self.legal(uid)["can_catch"] or cid != self.last_play["card"]:
            raise DurakError(
                "Эту карту уже нельзя проверить. Обновите состояние стола", 409
            )
        move = self.last_play
        if move["honest"]:
            move["checked_by"].append(uid)
            self.touch(
                self.player(uid)["name"]
                + " проверил последнюю карту — сыграно по правилам"
            )
            return
        cheater = self.player(move["player"])
        cheater["hand"].insert(
            min(move["hand_index"], len(cheater["hand"])), move["card"]
        )
        self.board = copy.deepcopy(move["board_before"])
        for key, value in move["turn"].items():
            setattr(self, key, copy.deepcopy(value))
        self.last_play = None
        self.set_turn(move["player"], move["turn"]["phase"])
        self.touch(
            self.player(uid)["name"]
            + " поймал на шулерстве "
            + cheater["name"]
            + ": карта возвращена, ход нужно повторить"
        )

    def end_bout(self):
        self.last_play = None
        defender = self.defender
        took = self.taking
        cards = [
            c for pair in self.board for c in (pair["attack"], pair["defense"]) if c
        ]
        if took:
            self.player(defender)["hand"].extend(cards)
        else:
            self.discard.extend(cards)
        draw_order = self.clockwise(self.draw_anchor)
        draw_order = [uid for uid in draw_order if uid != defender] + [defender]
        for uid in draw_order:
            hand = self.player(uid)["hand"]
            while len(hand) < self.rules["hand_size"] and self.deck:
                hand.append(self.deck.pop(0))
        self.board = []
        # Exit is assessed at bout end; simultaneous exits share finishing places.
        group = (
            [p["id"] for p in self.players if not p["out"] and not p["hand"]]
            if not self.deck
            else []
        )
        if group:
            self.out_groups.append(group)
            for uid in group:
                self.player(uid)["out"] = True
        active = self.active_ids()
        if not active:
            self.finish(None, "Все оставшиеся игроки вышли одновременно — ничья")
        elif len(active) == 1:
            self.finish(active[0], "Последний игрок с картами")
        else:
            next_attacker = self.clockwise(defender, active, include_anchor=not took)[0]
            self.bout_number += 1
            self.begin_bout(next_attacker)

    def finish(self, loser, reason):
        if self.phase == "finished":
            return
        groups = copy.deepcopy(self.out_groups)
        remaining = [p["id"] for p in self.players if p["id"] != loser and not p["out"]]
        if remaining:
            groups.append(remaining)
        refunds = {
            p["id"]: self.match["entry_cents"] for p in self.match["participants"]
        }
        payouts = (
            refunds
            if loser is None
            else ranked_payouts(self.match["bank_cents"], groups)
        )
        self.match["result"] = {
            "loser": loser,
            "groups": groups,
            "draw": loser is None,
            "reason": reason,
            "payouts": {str(uid): amount for uid, amount in payouts.items()},
        }
        self.phase, self.actor, self.deadline = "finished", None, None
        self.last_play = None
        self.touch("Партия завершена: " + reason)

    def leave(self, uid):
        p = self.player(uid)
        if not p:
            return
        if self.phase not in ("waiting", "finished") and not p["out"]:
            self.finish(uid, p["name"] + " сдался и покинул игру")
        elif self.phase not in ("waiting", "finished"):
            # Already finished players remain as immutable seats until settlement.
            p["left"] = True
            if self.creator == uid:
                self.creator = next(
                    (other["id"] for other in self.players if not other.get("left")),
                    uid,
                )
            self.touch(p["name"] + " покинул стол после выхода")
            return
        self.players.remove(p)
        for other in self.players:
            other["ready"] = False
        if self.creator == uid and self.players:
            self.creator = next(
                (other["id"] for other in self.players if not other.get("left")),
                self.players[0]["id"],
            )
        self.touch(p["name"] + " покинул стол")

    def rematch(self):
        if self.phase != "finished" or not self.match["settled"]:
            raise DurakError("Предыдущая партия ещё не рассчитана", 409)
        self.players = [p for p in self.players if not p.get("left")]
        if self.players and not self.player(self.creator):
            self.creator = self.players[0]["id"]
        for p in self.players:
            p.update(hand=[], ready=False, out=False, timeouts=0)
        self.phase, self.match = "waiting", None
        self.deck, self.discard, self.board = [], [], []
        self.actor = self.deadline = self.trump = self.trump_card = None
        self.out_groups = []
        self.last_play = None
        self.touch("Новая партия: подтвердите готовность")

    def tick(self, now=None):
        now = time.time() if now is None else now
        if self.phase in ("waiting", "finished") or now < self.deadline:
            return False
        p = self.player(self.actor)
        p["timeouts"] += 1
        if p["timeouts"] >= 3:
            self.finish(p["id"], p["name"] + ": три пропуска хода подряд")
        elif self.phase == "defend":
            self.action(p["id"], "take", automatic=True)
        elif self.phase == "throw":
            self.action(p["id"], "pass", automatic=True)
        else:
            cid = min(p["hand"], key=lambda c: (c[0] == self.trump, int(c[1:]), c))
            self.action(p["id"], "attack", cid, automatic=True)
        return True

    def public_state(self, viewer):
        place, position = {}, 1
        for group in self.out_groups:
            place.update({uid: position for uid in group})
            position += len(group)
        return {
            "id": self.id,
            "creator": self.creator,
            "rules": copy.deepcopy(self.rules),
            "revision": self.revision,
            "phase": self.phase,
            "match_number": self.match_number,
            "actor": self.actor,
            "attacker": self.attacker,
            "defender": self.defender,
            "deadline": self.deadline,
            "server_time": time.time(),
            "deck_count": len(self.deck),
            "discard_count": len(self.discard),
            "trump": SUITS.get(self.trump),
            "trump_card": card(self.trump_card) if self.trump_card else None,
            "taking": getattr(self, "taking", False),
            "attack_limit": getattr(self, "limit", 0),
            "hand": [card(c) for c in (self.player(viewer) or {}).get("hand", [])],
            "board": [
                {
                    "attack": card(pair["attack"]),
                    "defense": card(pair["defense"]) if pair["defense"] else None,
                }
                for pair in self.board
            ],
            "players": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "count": len(p["hand"]),
                    "ready": p["ready"],
                    "out": p["out"],
                    "place_group": place.get(p["id"]),
                    "left": p.get("left", False),
                    "timeouts": p["timeouts"],
                }
                for p in self.players
            ],
            "legal": self.legal(viewer),
            "events": copy.deepcopy(self.events[-20:]),
            # Never reveal whether the move was honest or its private checkpoint.
            "last_play": {
                key: copy.deepcopy(self.last_play[key])
                for key in ("player", "card", "kind", "target")
            }
            if self.last_play
            else None,
            "bank_cents": self.match["bank_cents"]
            if self.match
            else self.rules["entry_cents"] * len(self.players),
            "result": copy.deepcopy(self.match["result"]) if self.match else None,
            "participants": copy.deepcopy(self.match["participants"])
            if self.match
            else [],
        }
