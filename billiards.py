"""Serializable multiplayer pool table with deterministic server-side physics."""

from __future__ import annotations

import copy
import math
import time


WIDTH = 1000.0
HEIGHT = 500.0
BALL_RADIUS = 14.0
CUSHION = 22.0
PLAY_LEFT = PLAY_TOP = CUSHION + BALL_RADIUS
PLAY_RIGHT = WIDTH - CUSHION - BALL_RADIUS
PLAY_BOTTOM = HEIGHT - CUSHION - BALL_RADIUS
POCKET_RADIUS = 32.0
POCKET_OPENING = 26.0
POCKETS = (
    (CUSHION, CUSHION),
    (WIDTH / 2, CUSHION),
    (WIDTH - CUSHION, CUSHION),
    (CUSHION, HEIGHT - CUSHION),
    (WIDTH / 2, HEIGHT - CUSHION),
    (WIDTH - CUSHION, HEIGHT - CUSHION),
)
BALL_RESTITUTION = 0.94
CUSHION_RESTITUTION = 0.82
CUSHION_TANGENT_RETENTION = 0.985
ROLLING_DECELERATION = 105.0
STOP_SPEED = 3.0
PHYSICS_HZ = 240
ANIMATION_HZ = 30
DEFAULTS = {
    "mode": "eight",
    "max_players": 2,
    "entry_cents": 1000,
    "turn_seconds": 90,
    "private": False,
}


class BilliardsError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def settings(values):
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise BilliardsError("Некорректные настройки стола")
    result = {**DEFAULTS, **values}
    if result["mode"] not in ("practice", "eight", "rotation"):
        raise BilliardsError("Неизвестный режим бильярда")
    for key in ("max_players", "entry_cents", "turn_seconds"):
        if type(result[key]) is not int:
            raise BilliardsError("Числовые настройки должны быть целыми")
    if type(result["private"]) is not bool:
        raise BilliardsError("Некорректная настройка закрытого стола")
    if result["turn_seconds"] not in (45, 60, 90, 120):
        raise BilliardsError("Время хода: 45, 60, 90 или 120 секунд")
    if result["mode"] == "practice":
        result["max_players"], result["entry_cents"], result["private"] = 1, 0, True
    elif result["mode"] == "eight":
        if result["max_players"] != 2:
            raise BilliardsError("В 8-ball играют ровно два участника")
    elif not 2 <= result["max_players"] <= 6:
        raise BilliardsError("В ротации может играть от 2 до 6 участников")
    if not 0 <= result["entry_cents"] <= 100_000_000:
        raise BilliardsError("Ставка: от 0 до 1 000 000 БК")
    return result


def _rack_ids(mode):
    if mode == "eight":
        # Eight in the middle, unlike groups in the rear corners.
        return (1, 9, 2, 10, 8, 3, 4, 11, 5, 12, 13, 6, 14, 7, 15)
    return tuple(range(1, 16))


def fresh_balls(mode):
    balls = [
        {"id": 0, "x": 245.0, "y": HEIGHT / 2, "vx": 0.0, "vy": 0.0, "pocketed": False}
    ]
    ids = iter(_rack_ids(mode))
    spacing = BALL_RADIUS * 2.04
    start_x = 690.0
    for row in range(5):
        x = start_x + row * spacing * 0.88
        for slot in range(row + 1):
            y = HEIGHT / 2 + (slot - row / 2) * spacing
            balls.append(
                {
                    "id": next(ids),
                    "x": x,
                    "y": y,
                    "vx": 0.0,
                    "vy": 0.0,
                    "pocketed": False,
                }
            )
    return balls


def _frame(balls):
    return [
        [b["id"], round(b["x"], 2), round(b["y"], 2), bool(b["pocketed"])]
        for b in balls
    ]


def _opening(position, centers):
    return any(abs(position - center) <= POCKET_OPENING for center in centers)


def simulate(balls, angle, power):
    """Run a shot with rolling resistance and lossy ball/cushion collisions."""
    cue = next(b for b in balls if b["id"] == 0)
    # A gentler cue speed keeps the break readable. Constant rolling resistance
    # gives the familiar fast opening followed by a long, progressively slower roll.
    # The nonlinear curve leaves useful room for soft positional shots while
    # preserving a strong break at the top of the scale.
    speed = 120.0 + 930.0 * power**1.35
    cue["vx"] = math.cos(angle) * speed
    cue["vy"] = math.sin(angle) * speed
    first_contact = None
    rail_after_contact = False
    object_rails = set()
    pocket_map = {}
    pocketed = []
    frames = [_frame(balls)]
    dt = 1 / PHYSICS_HZ
    frame_steps = PHYSICS_HZ // ANIMATION_HZ
    for step in range(PHYSICS_HZ * 20):
        active = [b for b in balls if not b["pocketed"]]
        for b in active:
            b["x"] += b["vx"] * dt
            b["y"] += b["vy"] * dt
        for b in list(active):
            pocket = next(
                (
                    index
                    for index, (px, py) in enumerate(POCKETS)
                    if (b["x"] - px) ** 2 + (b["y"] - py) ** 2 <= POCKET_RADIUS**2
                ),
                None,
            )
            if pocket is not None:
                b["pocketed"] = True
                b["vx"] = b["vy"] = 0.0
                pocketed.append(b["id"])
                pocket_map[str(b["id"])] = pocket
                if first_contact is not None:
                    rail_after_contact = True
                continue
            hit = False
            if b["x"] < PLAY_LEFT and not _opening(b["y"], (CUSHION, HEIGHT - CUSHION)):
                b["x"], b["vx"], hit = (
                    PLAY_LEFT,
                    abs(b["vx"]) * CUSHION_RESTITUTION,
                    True,
                )
                b["vy"] *= CUSHION_TANGENT_RETENTION
            elif b["x"] > PLAY_RIGHT and not _opening(
                b["y"], (CUSHION, HEIGHT - CUSHION)
            ):
                b["x"], b["vx"], hit = (
                    PLAY_RIGHT,
                    -abs(b["vx"]) * CUSHION_RESTITUTION,
                    True,
                )
                b["vy"] *= CUSHION_TANGENT_RETENTION
            if b["y"] < PLAY_TOP and not _opening(
                b["x"], (CUSHION, WIDTH / 2, WIDTH - CUSHION)
            ):
                b["y"], b["vy"], hit = (
                    PLAY_TOP,
                    abs(b["vy"]) * CUSHION_RESTITUTION,
                    True,
                )
                b["vx"] *= CUSHION_TANGENT_RETENTION
            elif b["y"] > PLAY_BOTTOM and not _opening(
                b["x"], (CUSHION, WIDTH / 2, WIDTH - CUSHION)
            ):
                b["y"], b["vy"], hit = (
                    PLAY_BOTTOM,
                    -abs(b["vy"]) * CUSHION_RESTITUTION,
                    True,
                )
                b["vx"] *= CUSHION_TANGENT_RETENTION
            if hit and first_contact is not None:
                rail_after_contact = True
                if b["id"]:
                    object_rails.add(b["id"])
        active = [b for b in balls if not b["pocketed"]]
        # Two solver passes keep a tightly packed rack stable during the break.
        for _ in range(2):
            for i, a in enumerate(active):
                for b in active[i + 1 :]:
                    dx, dy = b["x"] - a["x"], b["y"] - a["y"]
                    distance2 = dx * dx + dy * dy
                    diameter = BALL_RADIUS * 2
                    if distance2 >= diameter * diameter:
                        continue
                    distance = max(math.sqrt(distance2), 0.001)
                    nx, ny = dx / distance, dy / distance
                    overlap = diameter - distance
                    a["x"] -= nx * overlap / 2
                    a["y"] -= ny * overlap / 2
                    b["x"] += nx * overlap / 2
                    b["y"] += ny * overlap / 2
                    approach = (a["vx"] - b["vx"]) * nx + (a["vy"] - b["vy"]) * ny
                    if approach > 0:
                        impulse = approach * (1.0 + BALL_RESTITUTION) / 2.0
                        a["vx"] -= impulse * nx
                        a["vy"] -= impulse * ny
                        b["vx"] += impulse * nx
                        b["vy"] += impulse * ny
                        if first_contact is None:
                            if a["id"] == 0 and b["id"]:
                                first_contact = b["id"]
                            elif b["id"] == 0 and a["id"]:
                                first_contact = a["id"]
        moving = False
        for b in active:
            velocity = math.hypot(b["vx"], b["vy"])
            if velocity <= STOP_SPEED:
                b["vx"] = b["vy"] = 0.0
                continue
            next_velocity = max(0.0, velocity - ROLLING_DECELERATION * dt)
            scale = next_velocity / velocity
            b["vx"] *= scale
            b["vy"] *= scale
            moving = moving or next_velocity > STOP_SPEED
        if step % frame_steps == 0:
            frames.append(_frame(balls))
        if not moving and step > 5:
            break
    frames.append(_frame(balls))
    stride = 1
    if len(frames) > 270:
        stride = math.ceil(len(frames) / 270)
        frames = frames[::stride] + (
            [frames[-1]] if frames[-1] != frames[::stride][-1] else []
        )
    return {
        "first_contact": first_contact,
        "pocketed": pocketed,
        "scratch": 0 in pocketed,
        "rail_after_contact": rail_after_contact,
        "frames": frames,
        "frame_ms": round(1000 * frame_steps * stride / PHYSICS_HZ, 2),
        "object_rails": sorted(object_rails),
        "pocket_map": pocket_map,
    }


class BilliardsTable:
    def __init__(self, tid, creator, name, rules=None):
        self.id = tid
        self.creator = creator
        self.rules = settings(rules or {})
        self.players = []
        self.phase = "waiting"
        self.created_at = self.updated_at = time.time()
        self.revision = 0
        self.match_number = 0
        self.match = None
        self.actor = None
        self.deadline = None
        self.balls = fresh_balls(self.rules["mode"])
        self.ball_in_hand = False
        self.groups = {}
        self.events = []
        self.last_shot = None
        self.started_at = None
        self.add_player(creator, name)

    def to_dict(self):
        return copy.deepcopy(self.__dict__)

    @classmethod
    def from_dict(cls, data):
        table = cls.__new__(cls)
        table.__dict__.update(copy.deepcopy(data))
        table.rules = settings(table.rules)
        table.__dict__.setdefault("last_shot", None)
        table.__dict__.setdefault("groups", {})
        return table

    def player(self, uid):
        return next((p for p in self.players if p["id"] == uid), None)

    def active_players(self):
        return [p for p in self.players if not p.get("left")]

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
            raise BilliardsError("Присоединиться можно только до начала партии", 409)
        if len(self.players) >= self.rules["max_players"]:
            raise BilliardsError("За столом нет свободных мест", 409)
        self.players.append(
            {
                "id": uid,
                "name": name,
                "ready": False,
                "left": False,
                "score": 0,
                "shots": 0,
                "fouls": 0,
                "timeouts": 0,
            }
        )
        for p in self.players:
            p["ready"] = False
        self.touch(f"{name} присоединился к столу")

    def change_settings(self, values):
        if self.phase not in ("waiting", "finished"):
            raise BilliardsError("Настройки нельзя менять во время партии", 409)
        new = settings(values)
        if new["max_players"] < len(self.active_players()):
            raise BilliardsError("За столом уже больше участников")
        if self.phase == "finished":
            self.rematch()
        self.rules = new
        self.balls = fresh_balls(new["mode"])
        for p in self.players:
            p["ready"] = False
        self.touch("Настройки изменены. Подтвердите готовность заново")

    def start(self):
        active = self.active_players()
        expected = 1 if self.rules["mode"] == "practice" else self.rules["max_players"]
        if (
            self.phase != "waiting"
            or len(active) != expected
            or not all(p["ready"] for p in active)
        ):
            raise BilliardsError(
                f"Для старта нужны готовность и {expected} участник(а/ов)", 409
            )
        self.match_number += 1
        entry = self.rules["entry_cents"]
        self.match = {
            "number": self.match_number,
            "participants": [{"id": p["id"], "name": p["name"]} for p in active],
            "entry_cents": entry,
            "bank_cents": entry * len(active),
            "result": None,
            "settled": self.rules["mode"] == "practice",
            "rules_version": 2,
        }
        self.balls = fresh_balls(self.rules["mode"])
        self.groups = {}
        self.ball_in_hand = False
        self.last_shot = None
        self.started_at = time.time()
        for p in active:
            p.update(score=0, shots=0, fouls=0, timeouts=0)
        self.actor = active[0]["id"]
        self.phase = "playing"
        self.deadline = time.time() + self.rules["turn_seconds"]
        self.touch(f"Партия №{self.match_number} началась")

    def next_player(self, uid):
        active = self.active_players()
        ids = [p["id"] for p in active]
        if not ids:
            return None
        if uid not in ids:
            return ids[0]
        return ids[(ids.index(uid) + 1) % len(ids)]

    def group_remaining(self, group):
        ids = range(1, 8) if group == "solids" else range(9, 16)
        return any(
            not next(b for b in self.balls if b["id"] == bid)["pocketed"] for bid in ids
        )

    def place_cue(self, x, y):
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (x, y)):
            raise BilliardsError("Некорректная позиция битка")
        if not PLAY_LEFT <= x <= PLAY_RIGHT or not PLAY_TOP <= y <= PLAY_BOTTOM:
            raise BilliardsError("Биток должен находиться внутри игрового поля")
        if any(
            (x - px) ** 2 + (y - py) ** 2 < (POCKET_RADIUS + 5) ** 2
            for px, py in POCKETS
        ):
            raise BilliardsError("Нельзя поставить биток в лузу")
        if any(
            not b["pocketed"]
            and b["id"]
            and (x - b["x"]) ** 2 + (y - b["y"]) ** 2 < (BALL_RADIUS * 2.05) ** 2
            for b in self.balls
        ):
            raise BilliardsError("Биток пересекается с другим шаром")
        cue = next(b for b in self.balls if b["id"] == 0)
        cue.update(x=float(x), y=float(y), vx=0.0, vy=0.0, pocketed=False)

    def finish(self, winners, reason):
        winners = list(dict.fromkeys(winners))
        self.phase = "finished"
        self.actor = None
        self.deadline = None
        self.ball_in_hand = False
        self.match["result"] = {"winners": winners, "reason": reason, "payouts": {}}
        self.touch(reason)

    def _rotation_finish(self):
        if any(not b["pocketed"] for b in self.balls if b["id"]):
            return False
        best = max(p["score"] for p in self.active_players())
        winners = [p["id"] for p in self.active_players() if p["score"] == best]
        if self.rules["mode"] == "practice":
            winners = [self.players[0]["id"]]
            self.finish(
                winners, f"Тренировка завершена за {self.players[0]['shots']} ударов"
            )
        else:
            self.finish(winners, "Все шары забиты; победа по сумме очков")
        return True

    def shoot(self, uid, angle, power, cue_x=None, cue_y=None, called_pocket=None):
        if self.phase != "playing" or uid != self.actor:
            raise BilliardsError("Сейчас ход другого игрока", 409)
        if self.deadline and time.time() >= self.deadline:
            raise BilliardsError("Время хода истекло. Обновите состояние стола", 409)
        if self.last_shot and time.time() < self.last_shot.get("available_at", 0):
            raise BilliardsError("Дождитесь остановки шаров", 409)
        if type(angle) not in (int, float) or not math.isfinite(angle):
            raise BilliardsError("Некорректное направление удара")
        if (
            type(power) not in (int, float)
            or not math.isfinite(power)
            or not 0.05 <= power <= 1
        ):
            raise BilliardsError("Сила удара должна быть от 5% до 100%")
        if called_pocket is not None and (
            type(called_pocket) is not int or not 0 <= called_pocket < 6
        ):
            raise BilliardsError("Выберите одну из шести луз")
        group_before = self.groups.get(str(uid))
        target_eight = bool(group_before and not self.group_remaining(group_before))
        breaking = sum(p["shots"] for p in self.players) == 0
        modern_eight = (
            self.rules["mode"] == "eight" and self.match.get("rules_version", 1) >= 2
        )
        if modern_eight and target_eight and called_pocket is None:
            raise BilliardsError("Перед ударом по восьмёрке выберите лузу")
        if self.ball_in_hand:
            self.place_cue(cue_x, cue_y)
        cue = next(b for b in self.balls if b["id"] == 0)
        if cue["pocketed"]:
            raise BilliardsError("Сначала поставьте биток на стол", 409)
        before = _frame(self.balls)
        player = self.player(uid)
        player["shots"] += 1
        result = simulate(self.balls, float(angle), float(power))
        object_pockets = [bid for bid in result["pocketed"] if bid]
        foul = None
        expected = None
        if result["first_contact"] is None:
            foul = "Биток не коснулся прицельного шара"
        elif self.rules["mode"] == "rotation":
            remaining_before = [row[0] for row in before if row[0] and not row[3]]
            expected = min(remaining_before) if remaining_before else None
            if result["first_contact"] != expected:
                foul = f"Сначала нужно коснуться шара №{expected}"
        elif self.rules["mode"] == "eight":
            group = group_before
            if target_eight and result["first_contact"] != 8:
                foul = "Сначала нужно коснуться восьмёрки"
            elif not target_eight and result["first_contact"] == 8:
                foul = "Восьмёрку нельзя трогать первой"
            elif (
                not target_eight
                and group == "solids"
                and not 1 <= result["first_contact"] <= 7
            ):
                foul = "Сначала нужно коснуться сплошного шара"
            elif (
                not target_eight
                and group == "stripes"
                and not 9 <= result["first_contact"] <= 15
            ):
                foul = "Сначала нужно коснуться полосатого шара"
        if result["scratch"]:
            foul = "Биток упал в лузу"
        elif (
            result["first_contact"] is not None
            and not object_pockets
            and not result["rail_after_contact"]
        ):
            foul = "После касания ни один шар не дошёл до борта"

        if modern_eight and breaking:
            if not object_pockets and len(result.get("object_rails", [])) < 4:
                foul = (
                    foul
                    or "При разбое нужно забить шар или довести четыре прицельных шара до борта"
                )
            if 8 in object_pockets:
                # House rule: eight on the break is spotted, never an instant win/loss.
                eight = next(b for b in self.balls if b["id"] == 8)
                candidates = [
                    (690.0 + delta, HEIGHT / 2) for delta in range(0, 240, 30)
                ]
                candidates += [
                    (float(x), float(y))
                    for x in range(100, 901, 30)
                    for y in range(80, 421, 30)
                ]
                x, y = next(
                    (x, y)
                    for x, y in candidates
                    if all(
                        b["id"] == 8
                        or b["pocketed"]
                        or math.hypot(x - b["x"], y - b["y"]) >= BALL_RADIUS * 2.05
                        for b in self.balls
                    )
                )
                eight.update(x=x, y=y, vx=0.0, vy=0.0, pocketed=False)
                object_pockets.remove(8)
                self.touch("Восьмёрка на разбое возвращена на стол")

        if self.rules["mode"] == "eight":
            if not foul and not group_before and not (modern_eight and breaking):
                assign = next(
                    (
                        "solids" if 1 <= bid <= 7 else "stripes"
                        for bid in object_pockets
                        if bid != 8
                    ),
                    None,
                )
                if assign:
                    opponent = next(p for p in self.active_players() if p["id"] != uid)
                    self.groups[str(uid)] = assign
                    self.groups[str(opponent["id"])] = (
                        "stripes" if assign == "solids" else "solids"
                    )
                    self.touch(
                        f"{player['name']}: {'сплошные' if assign == 'solids' else 'полосатые'}"
                    )
            if 8 in object_pockets:
                legal = bool(target_eight and not foul)
                if modern_eight:
                    legal = (
                        legal and result.get("pocket_map", {}).get("8") == called_pocket
                    )
                opponent = next(p for p in self.active_players() if p["id"] != uid)
                self.finish(
                    [uid] if legal else [opponent["id"]],
                    f"{player['name']} законно забил восьмёрку"
                    if legal
                    else f"{player['name']} забил восьмёрку с нарушением правил",
                )
        else:
            player["score"] += sum(object_pockets)
            self._rotation_finish()

        if self.phase == "playing":
            if foul:
                player["fouls"] += 1
                self.actor = self.next_player(uid)
                self.ball_in_hand = True
                self.touch(f"Фол: {foul}. Биток с руки у следующего игрока")
            else:
                if self.rules["mode"] == "eight":
                    group = self.groups.get(str(uid))
                    own = [
                        bid
                        for bid in object_pockets
                        if (group == "solids" and 1 <= bid <= 7)
                        or (group == "stripes" and 9 <= bid <= 15)
                    ]
                    continue_turn = bool(own or (not group and object_pockets))
                else:
                    continue_turn = bool(object_pockets)
                if not continue_turn:
                    self.actor = self.next_player(uid)
                self.ball_in_hand = False
                self.touch(
                    f"{player['name']} забил: {', '.join(map(str, object_pockets))}"
                    if object_pockets
                    else f"{player['name']} завершил удар"
                )
            cue = next(b for b in self.balls if b["id"] == 0)
            if cue["pocketed"]:
                self.ball_in_hand = True
            duration = max(0, len(result["frames"]) - 1) * result["frame_ms"] / 1000
            self.deadline = time.time() + duration + self.rules["turn_seconds"]
        started_at = time.time()
        duration = max(0, len(result["frames"]) - 1) * result["frame_ms"] / 1000
        self.last_shot = {
            "id": self.revision + 1,
            "by": uid,
            "frames": result["frames"],
            "frame_ms": result["frame_ms"],
            "pocketed": result["pocketed"],
            "foul": foul,
            "angle": float(angle),
            "power": float(power),
            "started_at": started_at,
            "available_at": started_at + duration,
            "called_pocket": called_pocket,
            "pocket_map": result.get("pocket_map", {}),
        }
        self.touch()

    def leave(self, uid):
        player = self.player(uid)
        if not player or player.get("left"):
            return
        player["left"] = True
        player["ready"] = False
        if self.phase == "waiting":
            self.players = [p for p in self.players if p["id"] != uid]
            if self.players and self.creator == uid:
                self.creator = self.players[0]["id"]
            self.touch(f"{player['name']} покинул стол")
            return
        if self.phase == "playing":
            remaining = self.active_players()
            if self.rules["mode"] == "practice":
                self.finish([], "Тренировка завершена")
            elif len(remaining) == 1:
                self.finish([remaining[0]["id"]], f"{player['name']} сдался")
            elif self.actor == uid:
                self.actor = self.next_player(uid)
                self.ball_in_hand = True
                self.deadline = time.time() + self.rules["turn_seconds"]
                self.touch(f"{player['name']} сдался; следующий играет с руки")

    def timeout(self, now=None):
        now = time.time() if now is None else now
        if self.phase != "playing" or not self.deadline or now < self.deadline:
            return
        player = self.player(self.actor)
        player["timeouts"] += 1
        if player["timeouts"] >= 3 and self.rules["mode"] != "practice":
            self.leave(player["id"])
            return
        player["fouls"] += 1
        self.actor = self.next_player(player["id"])
        self.ball_in_hand = True
        self.deadline = now + self.rules["turn_seconds"]
        self.touch(f"{player['name']} пропустил ход по таймеру")

    def rematch(self):
        if self.phase != "finished":
            raise BilliardsError("Текущая партия ещё не завершена", 409)
        self.phase = "waiting"
        self.match = None
        self.actor = None
        self.deadline = None
        self.balls = fresh_balls(self.rules["mode"])
        self.groups = {}
        self.ball_in_hand = False
        self.last_shot = None
        self.players = [p for p in self.players if not p.get("left")]
        for p in self.players:
            p.update(ready=False, score=0, shots=0, fouls=0, timeouts=0)
        self.touch("Новая партия готова к запуску")

    def public_state(self, uid):
        return {
            "server_time": time.time(),
            "id": self.id,
            "creator": self.creator,
            "rules": self.rules,
            "phase": self.phase,
            "revision": self.revision,
            "match_number": self.match_number,
            "match": self.match,
            "actor": self.actor,
            "deadline": self.deadline,
            "balls": _frame(self.balls),
            "ball_in_hand": self.ball_in_hand,
            "groups": self.groups,
            "events": self.events[-30:],
            "last_shot": self.last_shot,
            "started_at": self.started_at,
            "players": copy.deepcopy(self.players),
            "your_id": uid,
        }
