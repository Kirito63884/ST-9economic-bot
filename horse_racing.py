Warning: truncated output (original token count: 5399)
Total output lines: 510

"""Shared server-owned horse races with atomic pari-mutuel settlement."""

from __future__ import annotations

import secrets
import time
import math
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from database import GameHistory, User

HORSES = (
    {"id": 1, "name": "Гром", "color": "#e1ad54"},
    {"id": 2, "name": "Искра", "color": "#ed776a"},
    {"id": 3, "name": "Шторм", "color": "#79bda9"},
    {"id": 4, "name": "Феникс", "color": "#ac91dc"},
    {"id": 5, "name": "Комета", "color": "#67acd0"},
    {"id": 6, "name": "Туман", "color": "#d976a8"},
)
HORSE_BY_ID = {horse["id"]: horse for horse in HORSES}
BETTING_SECONDS = 18
RESULT_SECONDS = 8
MIN_BET_CENTS = 1_000
MAX_BET_CENTS = 100_000_000  # 1 000 000 БК
MAX_CENTS = 100_000_000_000_000


class HorseRacingError(HTTPException):
    def __init__(self, detail, status_code=400):
        super().__init__(status_code=status_code, detail=detail)


def cents(value):
    try:
        amount = Decimal(str(value)) * 100
    except (InvalidOperation, ValueError, TypeError):
        raise HorseRacingError("Баланс участника требует проверки", 409)
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise HorseRacingError("Баланс участника требует проверки", 409)
    return int(amount)


class HorseRacingService:
    """One public race at a time; all wallet changes run in runtime transaction."""

    def __init__(self, clock=time.time, random_source=None):
        self.clock = clock
        self.random = random_source or secrets.SystemRandom()
        self.current = None
        self.last_finish = None
        self.next_number = 1

    def _new_race(self, now):
        race = {
            "number": self.next_number,
            "phase": "betting",
            "created_at": now,
            "betting_ends_at": now + BETTING_SECONDS,
            "bets": [],
            "pool_cents": 0,
            "started_at": None,
            "durations_ms": None,
            "winner_id": None,
            "finished_at": None,
            "result_until": None,
            "settled": False,
            "result": None,
        }
        self.next_number += 1
        return race

    def ensure(self, now=None):
        if self.current is None:
            self.current = self._new_race(self.clock() if now is None else now)
        return self.current

    @staticmethod
    def _winner_share(pool, winners):
        total = sum(bet["amount_cents"] for bet in winners)
        if total <= 0:
            raise HorseRacingError("Ставки победителей требуют проверки", 409)
        shares = {}
        remainder_order = []
        for bet in winners:
            numerator = pool * bet["amount_cents"]
            amount, remainder = divmod(numerator, total)
            uid = bet["user_id"]
            shares[uid] = amount
            remainder_order.append((remainder, uid))
        remainder_cents = pool - sum(shares.values())
        for _, uid in sorted(remainder_order, key=lambda item: (-item[0], item[1]))[
            :remainder_cents
        ]:
            shares[uid] += 1
        return shares

    def _settle(self, race, db):
        if race["settled"]:
            return
        bets = race["bets"]
        pool = race["pool_cents"]
        if type(pool) is not int or pool != sum(b["amount_cents"] for b in bets):
            raise HorseRacingError("Общий пул требует проверки", 409)
        if len({b["user_id"] for b in bets}) != len(bets):
            raise HorseRacingError("Повторная ставка требует проверки", 409)

        winning_bets = [b for b in bets if b["horse_id"] == race["winner_id"]]
        payouts = (
            self._winner_share(pool, winning_bets)
            if winning_bets
            else {b["user_id"]: b["amount_cents"] for b in bets}
        )
        user_ids = {bet["user_id"] for bet in bets}
        users = {
            user.id: user for user in db.query(User).filter(User.id.in_(user_ids)).all()
        }
        balances = {}
        for bet in bets:
            uid = bet["user_id"]
            user = users.get(uid)
            if not user or user.deleted_at:
                raise HorseRacingError(
                    "Не удалось найти профиль участника; выплата сохранена для повтора",
                    409,
                )
            payout = payouts[uid]
            balance = cents(user.balance)
            if payout < 0 or balance < 0 or balance + payout > MAX_CENTS:
                raise HorseRacingError(
                    "Баланс участника достиг лимита; выплата сохранена для повтора",
                    409,
                )
            balances[uid] = (balance, payout)

        for bet in bets:
            uid = bet["user_id"]
            user = users[uid]
            balance, payout = balances[uid]
            amount = bet["amount_cents"]
            user.balance = (balance + payout) / 100
            result = (
                "push"
                if not winning_bets
                else "won"
                if bet["horse_id"] == race["winner_id"]
                else "lost"
            )
            db.add(
                GameHistory(
                    user_id=uid,
                    game_type="horse_racing",
                    result=result,
                    amount=(payout - amount) / 100,
                    details=(
                        f"Скачки, заезд №{race['number']}; {HORSE_BY_ID[bet['horse_id']]['name']}; "
                        f"ставка {amount / 100:.2f} БК, выплата {payout / 100:.2f} БК; "
                        f"первой пришла лошадь {HORSE_BY_ID[race['winner_id']]['name']}"
                    ),
                )
            )
        ticket_results = {
            str(bet["user_id"]): {
                "horse_id": bet["horse_id"],
                "amount_cents": bet["amount_cents"],
                "payout_cents": payouts[bet["user_id"]],
            }
            for bet in bets
        }
        race["result"] = {
            "winner_id": race["winner_id"],
            "pool_cents": pool,
            "bet_count": len(bets),
            "winner_bet_count": len(winning_bets),
            "refunded": not winning_bets,
            "payouts": {str(uid): value for uid, value in payouts.items()},
            "ticket_results": ticket_results,
        }
        race["settled"] = True

    def _start(self, race, now):
        race["phase"] = "running"
        race["started_at"] = now
        # Distinct server-generated finishing times give every race a stable,
        # smooth visual path and a result the browser cannot influence.
        durations = self.random.sample(range(8_000, 14_001), len(HORSES))
        race["durations_ms"] = {
            str(horse["id"]): duration
            for horse, duration in zip(HORSES, durations, strict=True)
        }
        race["winner_id"] = min(
            HORSES, key=lambda horse: race["durations_ms"][str(horse["id"])]
        )["id"]
        race["finished_at"] = now + race["durations_ms"][str(race["winner_id"])] / 1000

    def advance(self, db, now=None):
        now = self.clock() if now is None else now
        race = self.ensure(now)
        if race["phase"] == "betting" and now >= race["betting_ends_at"]:
            self._start(race, now)
        if race["phase"] == "running" and now >= race["finished_at"]:
            self._settle(race, db)
            race["phase"] = "finished"
            race["result_until"] = race["finished_at"] + RESULT_SECONDS
            self.last_finish = {
                "number": race["number"],
                "winner_id": race["winner_id"],
                "pool_cents": race["pool_cents"],…1399 tokens truncated…    }
                if result
                else None
            ),
            "last_finish": (
                {
                    key: value
                    for key, value in last.items()
                    if key not in {"payouts", "ticket_results"}
                }
                if last
                else None
            ),
            "balance_cents": cents(user.balance),
        }

    @staticmethod
    def _my_result(uid, result):
        if not result or str(uid) not in result.get("ticket_results", {}):
            return None
        ticket = result["ticket_results"][str(uid)]
        return {
            **ticket,
            "pool_cents": result["pool_cents"],
            "refunded": result["refunded"],
            "winner_id": result["winner_id"],
            "won": ticket["horse_id"] == result["winner_id"] and not result["refunded"],
            "net_cents": ticket["payout_cents"] - ticket["amount_cents"],
        }

    def detach(self, uid, db):
        race = self.current
        if not race:
            return
        bet = next((b for b in race["bets"] if b["user_id"] == uid), None)
        if not bet:
            return
        if race["phase"] == "finished":
            return
        if race["phase"] != "betting":
            raise HorseRacingError(
                "Нельзя удалить участника до завершения уже начавшегося заезда",
                409,
            )
        user = db.get(User, uid)
        if not user:
            raise HorseRacingError("Не удалось вернуть ставку участнику", 409)
        balance = cents(user.balance)
        if balance + bet["amount_cents"] > MAX_CENTS:
            raise HorseRacingError(
                "Невозможно вернуть ставку: баланс достиг лимита", 409
            )
        user.balance = (balance + bet["amount_cents"]) / 100
        race["bets"].remove(bet)
        race["pool_cents"] -= bet["amount_cents"]
        db.add(
            GameHistory(
                user_id=uid,
                game_type="horse_racing",
                result="cancelled",
                amount=0,
                details=f"Скачки, заезд №{race['number']}; ставка возвращена при удалении аккаунта",
            )
        )

    def to_dict(self):
        return {
            "version": 1,
            "next_number": self.next_number,
            "current": self.current,
            "last_finish": self.last_finish,
        }

    def restore(self, data, pause=0):
        if not data:
            self.current = None
            self.last_finish = None
            self.next_number = 1
            self.ensure(self.clock())
            return
        if not isinstance(data, dict) or data.get("version") != 1:
            raise RuntimeError("Unsupported horse racing state version")
        current = data.get("current")
        if not isinstance(current, dict) or current.get("phase") not in {
            "betting",
            "running",
            "finished",
        }:
            raise RuntimeError("Invalid saved horse racing state")
        current = dict(current)
        if (
            type(current.get("number")) is not int
            or current["number"] < 1
            or type(current.get("pool_cents")) is not int
            or not 0 <= current["pool_cents"] <= MAX_CENTS
            or type(current.get("settled")) is not bool
            or not isinstance(current.get("bets"), list)
        ):
            raise RuntimeError("Invalid saved horse racing state")
        bets = []
        for value in current["bets"]:
            if not isinstance(value, dict):
                raise RuntimeError("Invalid saved horse racing bet")
            bet = dict(value)
            if (
                type(bet.get("user_id")) is not int
                or bet["user_id"] < 1
                or type(bet.get("horse_id")) is not int
                or bet["horse_id"] not in HORSE_BY_ID
                or type(bet.get("amount_cents")) is not int
                or not MIN_BET_CENTS <= bet["amount_cents"] <= MAX_BET_CENTS
            ):
                raise RuntimeError("Invalid saved horse racing bet")
            bets.append(bet)
        if (
            len({bet["user_id"] for bet in bets}) != len(bets)
            or sum(bet["amount_cents"] for bet in bets) != current["pool_cents"]
        ):
            raise RuntimeError("Invalid saved horse racing pool")
        current["bets"] = bets
        for key in ("created_at", "betting_ends_at"):
            if type(current.get(key)) not in (int, float) or not math.isfinite(
                current[key]
            ):
                raise RuntimeError("Invalid saved horse racing clock")
        if current["phase"] == "betting":
            if (
                current.get("settled")
                or current.get("result") is not None
                or current.get("started_at") is not None
                or current.get("durations_ms") is not None
                or current.get("winner_id") is not None
                or current.get("finished_at") is not None
                or current.get("result_until") is not None
            ):
                raise RuntimeError("Invalid saved horse racing phase")
        else:
            durations = current.get("durations_ms")
            if (
                not isinstance(durations, dict)
                or set(durations) != {str(horse["id"]) for horse in HORSES}
                or any(
                    type(duration) is not int or not 8_000 <= duration <= 14_000
                    for duration in durations.values()
                )
                or type(current.get("winner_id")) is not int
                or current["winner_id"] not in HORSE_BY_ID
                or min(HORSES, key=lambda horse: durations[str(horse["id"])])["id"]
                != current["winner_id"]
                or type(current.get("started_at")) not in (int, float)
                or not math.isfinite(current["started_at"])
                or type(current.get("finished_at")) not in (int, float)
                or not math.isfinite(current["finished_at"])
            ):
                raise RuntimeError("Invalid saved horse racing result")
            expected_finish = current["started_at"] + (
                durations[str(current["winner_id"])] / 1000
            )
            if abs(current["finished_at"] - expected_finish) > 0.001:
                raise RuntimeError("Invalid saved horse racing finish time")
        if current["phase"] == "running" and (
            current.get("settled")
            or current.get("result") is not None
            or current.get("result_until") is not None
        ):
            raise RuntimeError("Invalid saved horse racing phase")
        if current["phase"] == "finished" and (
            not current.get("settled")
            or not isinstance(current.get("result"), dict)
            or type(current.get("result_until")) not in (int, float)
            or not math.isfinite(current["result_until"])
            or current["result"].get("winner_id") != current["winner_id"]
            or current["result"].get("pool_cents") != current["pool_cents"]
            or current["result"].get("bet_count") != len(current["bets"])
        ):
            raise RuntimeError("Invalid saved horse racing settlement")
        if pause:
            for key in (
                "created_at",
                "betting_ends_at",
                "started_at",
                "finished_at",
                "result_until",
            ):
                if current.get(key) is not None:
                    current[key] += pause
        self.current = current
        self.last_finish = (
            dict(data["last_finish"]) if data.get("last_finish") else None
        )
        self.next_number = max(int(data.get("next_number", 1)), current["number"] + 1)

