"""Durable two-player Go tables with mutually confirmed scoring."""

from __future__ import annotations

import copy
import secrets
import time

from go_engine import GoError, GoPosition, KOMI_HALF, area_score, group_at, point_id

DEFAULTS = {"size": 9, "entry_cents": 0, "private": False}
RULESET = "chinese-ogs-psk-v1"


def settings(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise GoError("Некорректные настройки Го")
    rules = {**DEFAULTS, **data}
    if type(rules["size"]) is not int or rules["size"] not in (9, 13, 19):
        raise GoError("Выберите поле 9×9, 13×13 или 19×19")
    if (
        type(rules["entry_cents"]) is not int
        or not 0 <= rules["entry_cents"] <= 100_000_000
    ):
        raise GoError("Взнос должен быть от 0 до 1 000 000 БК")
    if type(rules["private"]) is not bool:
        raise GoError("Некорректный доступ к столу")
    return rules


class GoTable:
    def __init__(self, tid, creator, name, rules=None):
        self.id, self.creator = tid, creator
        self.rules = settings({} if rules is None else rules)
        self.created_at = self.updated_at = self.last_player_activity = time.time()
        self.players = []
        self.phase = "waiting"
        self.revision = self.match_number = 0
        self.match = None
        self.position = GoPosition(self.rules["size"])
        self.actor = None
        self.dead = []
        self.score_accepts = []
        self.score_revision = 0
        self.events = []
        self.ruleset = RULESET
        self.add_player(creator, name)

    def player(self, uid):
        return next((p for p in self.players if p["id"] == uid), None)

    def active_players(self):
        return [p for p in self.players if not p["left"]]

    def touch(self, text=None):
        self.revision += 1
        self.updated_at = time.time()
        if text:
            self.events.append({"text": text, "time": self.updated_at})
            self.events = self.events[-80:]

    def add_player(self, uid, name):
        if self.player(uid):
            return
        if self.phase != "waiting" or len(self.players) >= 2:
            raise GoError("За столом нет свободного места", 409)
        self.players.append({"id": uid, "name": name, "ready": False, "left": False})
        for p in self.players:
            p["ready"] = False
        self.touch(name + " присоединился к столу")

    def change_settings(self, data):
        if self.phase != "waiting":
            raise GoError("Размер поля и взнос меняются только до начала партии", 409)
        self.rules = settings(data)
        self.position = GoPosition(self.rules["size"])
        for p in self.players:
            p["ready"] = False
        self.touch("Условия изменены. Подтвердите готовность заново")

    def start(self):
        seats = self.active_players()
        if (
            self.phase != "waiting"
            or len(seats) != 2
            or not all(p["ready"] for p in seats)
        ):
            raise GoError("Нужны два игрока и готовность обоих", 409)
        ordered = seats[:] if secrets.randbelow(2) == 0 else seats[::-1]
        self.match_number += 1
        self.match = {
            "number": self.match_number,
            "participants": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "color": "black" if i == 0 else "white",
                }
                for i, p in enumerate(ordered)
            ],
            "entry_cents": self.rules["entry_cents"],
            "bank_cents": self.rules["entry_cents"] * 2,
            "result": None,
            "settled": False,
        }
        self.position = GoPosition(self.rules["size"])
        self.dead, self.score_accepts, self.score_revision = [], [], 0
        self.phase, self.actor = "playing", ordered[0]["id"]
        self.touch(
            f"Начало партии {self.rules['size']}×{self.rules['size']}. Чёрные: {ordered[0]['name']}. Коми белым 7,5"
        )

    def color(self, uid):
        return (
            next(
                (
                    i + 1
                    for i, p in enumerate(self.match["participants"])
                    if p["id"] == uid
                ),
                None,
            )
            if self.match
            else None
        )

    def next_actor(self):
        return self.match["participants"][self.position.to_move - 1]["id"]

    def finish(self, winners, reason, score=None):
        if score is None:
            self.dead, self.score_accepts = [], []
        self.phase, self.actor = "finished", None
        self.match["result"] = {
            "winners": list(winners),
            "reason": reason,
            "score": score,
            "payouts": {},
        }
        self.touch(reason)

    def action(self, uid, kind, data=None):
        if self.phase not in ("playing", "scoring"):
            raise GoError("Партия сейчас не идёт", 409)
        p = self.player(uid)
        if not p or p["left"] or not self.color(uid):
            raise GoError("Действовать может только участник партии", 403)
        allowed = {
            "play": {"point"},
            "pass": set(),
            "resign": set(),
            "mark_dead": {"point"},
            "confirm_score": set(),
            "resume": set(),
        }
        if data is None:
            data = {}
        if (
            not isinstance(kind, str)
            or kind not in allowed
            or not isinstance(data, dict)
            or set(data) != allowed[kind]
        ):
            raise GoError("Некорректные поля действия")
        if kind == "resign":
            winner = next(p["id"] for p in self.match["participants"] if p["id"] != uid)
            self.finish([winner], "Соперник сдался")
            return
        if kind in ("play", "pass"):
            if self.phase != "playing" or uid != self.actor:
                raise GoError("Сейчас подсчёт или ход другого игрока", 409)
            if kind == "play":
                point = data["point"]
                captured = self.position.play(point)
                self.touch(
                    f"{p['name']}: {coordinate(point, self.rules['size'])}"
                    + (f", захвачено камней: {len(captured)}" if captured else "")
                )
            else:
                self.position.pass_turn()
                self.touch(p["name"] + ": пас")
            self.actor = self.next_actor()
            if self.position.passes == 2:
                self.phase = "scoring"
                self.dead, self.score_accepts = [], []
                self.score_revision += 1
                self.touch(
                    "Два паса. Согласуйте мёртвые группы и подтвердите итог либо продолжите игру"
                )
            return
        if self.phase != "scoring":
            raise GoError("Сначала нужны два паса подряд", 409)
        if kind == "resume":
            self.position.resume()
            self.phase, self.actor = "playing", self.next_actor()
            self.dead, self.score_accepts = [], []
            self.score_revision += 1
            self.touch(
                p["name"]
                + ": игра возобновлена с исходной доски, история суперко сохранена"
            )
        elif kind == "mark_dead":
            point = point_id(data["point"], self.rules["size"])
            if not self.position.board[point]:
                raise GoError("Выберите камень группы, а не пустой пункт", 409)
            group, _ = group_at(self.position.board, point, self.rules["size"])
            removed = set(self.dead)
            removed = removed - group if group <= removed else removed | group
            self.dead, self.score_accepts = sorted(removed), []
            self.score_revision += 1
            self.touch(
                p["name"] + ": изменены отметки мёртвых групп; подтверждения сброшены"
            )
        elif kind == "confirm_score":
            if uid in self.score_accepts:
                raise GoError("Вы уже подтвердили этот вариант подсчёта", 409)
            score = area_score(self.position.board, self.dead)
            self.score_accepts.append(uid)
            if len(self.score_accepts) == 2:
                color = score["winner_color"]
                winners = (
                    [
                        next(
                            p["id"]
                            for p in self.match["participants"]
                            if p["color"] == color
                        )
                    ]
                    if color
                    else []
                )
                self.finish(
                    winners,
                    f"Согласованный подсчёт: чёрные {score['black_half'] / 2:g}, белые {score['white_half'] / 2:g} (коми 7,5)",
                    score,
                )
            else:
                self.touch(p["name"] + ": итог подтверждён, ожидаем соперника")

    def leave(self, uid):
        player = self.player(uid)
        if not player or player["left"]:
            return
        if self.phase in ("playing", "scoring"):
            self.action(uid, "resign")
        if self.phase == "waiting":
            self.players.remove(player)
            for other in self.players:
                other["ready"] = False
        else:
            player["left"] = True
        if self.creator == uid and self.active_players():
            self.creator = self.active_players()[0]["id"]
        self.touch(player["name"] + ": выход со стола")

    def rematch(self):
        if (
            self.phase != "finished"
            or not self.match["settled"]
            or len(self.active_players()) != 2
        ):
            raise GoError("Нужны два участника и рассчитанная предыдущая партия", 409)
        self.match = None
        self.position = GoPosition(self.rules["size"])
        self.actor, self.phase = None, "waiting"
        self.dead, self.score_accepts = [], []
        for p in self.players:
            p["ready"] = False
        self.touch("Новая партия. Подтвердите готовность обоих")

    def to_dict(self):
        data = copy.deepcopy(
            {key: value for key, value in self.__dict__.items() if key != "position"}
        )
        data["position"] = self.position.to_dict()
        return data

    @classmethod
    def from_dict(cls, data):
        obj = cls.__new__(cls)
        obj.__dict__.update(copy.deepcopy(data))
        obj.rules = settings(obj.rules)
        if obj.ruleset != RULESET:
            raise GoError("Неизвестная версия правил Го", 409)
        obj.position = GoPosition.from_dict(data["position"])
        obj.validate()
        from table_lifecycle import initialise

        initialise(obj)
        return obj

    def validate(self):
        ids = [p["id"] for p in self.players]
        if (
            len(ids) != len(set(ids))
            or len(ids) > 2
            or self.phase not in ("waiting", "playing", "scoring", "finished")
        ):
            raise GoError("Повреждён состав или фаза партии Го", 409)
        if self.position.size != self.rules["size"]:
            raise GoError("Размер доски отличается от условий", 409)
        if self.phase != "waiting":
            participants = self.match["participants"]
            if (
                len(participants) != 2
                or {p["id"] for p in participants} != set(ids)
                or [p["color"] for p in participants] != ["black", "white"]
                or self.match["entry_cents"] != self.rules["entry_cents"]
                or self.match["bank_cents"] != self.rules["entry_cents"] * 2
            ):
                raise GoError("Повреждены участники или банк Го", 409)
        if self.phase in ("playing", "scoring"):
            if (
                any(p["left"] for p in self.players)
                or self.actor != self.next_actor()
                or self.match["result"]
                or self.match["settled"]
                or (self.phase == "scoring") != (self.position.passes == 2)
            ):
                raise GoError("Повреждена очередь или состояние подсчёта", 409)
            if (
                len(set(self.score_accepts)) != len(self.score_accepts)
                or not set(self.score_accepts) <= set(ids)
                or len(self.score_accepts) >= 2
            ):
                raise GoError("Повреждены подтверждения итога", 409)
        if self.phase == "finished":
            result = self.match["result"]
            if (
                not result
                or len(result["winners"]) > 1
                or not set(result["winners"]) <= set(ids)
            ):
                raise GoError("Повреждён результат Го", 409)
            if result.get("score") is not None:
                expected = area_score(self.position.board, self.dead)
                winners = [
                    p["id"]
                    for p in self.match["participants"]
                    if p["color"] == expected["winner_color"]
                ]
                if (
                    result["score"] != expected
                    or result["winners"] != winners
                    or set(self.score_accepts) != set(ids)
                    or self.position.passes != 2
                ):
                    raise GoError(
                        "Сохранённый итог не совпадает с согласованным подсчётом", 409
                    )
        if self.dead or self.phase == "scoring":
            area_score(self.position.board, self.dead)
        if self.phase == "playing" and (self.dead or self.score_accepts):
            raise GoError("Отметки подсчёта остались в активной игре", 409)

    def public_state(self, uid):
        scoring = (
            self.phase == "scoring"
            or self.phase == "finished"
            and self.match["result"].get("score") is not None
        )
        return {
            "id": self.id,
            "creator": self.creator,
            "your_id": uid,
            "phase": self.phase,
            "revision": self.revision,
            "rules": self.rules.copy(),
            "ruleset": RULESET,
            "komi_half": KOMI_HALF,
            "players": copy.deepcopy(self.players),
            "match": copy.deepcopy(self.match),
            "board": self.position.board[:],
            "actor": self.actor,
            "your_color": self.color(uid),
            "turn_color": self.position.to_move,
            "passes": self.position.passes,
            "captures": self.position.captures.copy(),
            "last_point": self.position.last_point,
            "legal_points": self.position.legal_points()
            if self.phase == "playing" and self.actor == uid
            else [],
            "dead": self.dead[:],
            "score_accepts": self.score_accepts[:],
            "score_revision": self.score_revision,
            "score": area_score(self.position.board, self.dead) if scoring else None,
            "moves": copy.deepcopy(self.position.history),
            "events": copy.deepcopy(self.events),
        }


def coordinate(point, size):
    row, col = divmod(point, size)
    return "ABCDEFGHJKLMNOPQRST"[col] + str(size - row)
