"""Private sea-battle HTTP API and atomic, equal-stake winner-takes-all bank."""

from decimal import Decimal
import secrets

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from battleship import (
    BattleshipError,
    BattleshipTable,
    MAX_CENTS,
    random_fleet,
    settings,
    validate_fleet,
)
from database import GameHistory, User
from table_admin import management
from table_lifecycle import expired


def cents(value):
    amount = Decimal(str(value)) * 100
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise BattleshipError("Баланс участника требует проверки", 409)
    return int(amount)


def require(user):
    if not user:
        raise BattleshipError("Войдите в свой профиль", 401)


async def body(request, allowed):
    try:
        data = await request.json()
    except (ValueError, UnicodeError):
        raise BattleshipError("Ожидается JSON")
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise BattleshipError("Некорректные поля запроса")
    return data


def revision(table, data):
    if type(data.get("revision")) is not int or data["revision"] != table.revision:
        raise BattleshipError("Состояние стола изменилось. Обновите страницу", 409)
    if expired(table):
        raise BattleshipError(
            "Срок неактивности стола истёк. Дождитесь его закрытия", 409
        )


class BattleshipService:
    def __init__(self):
        self.tables = {}

    def table(self, tid):
        table = self.tables.get(tid)
        if table is None:
            raise BattleshipError("Стол не найден или уже закрыт", 404)
        return table

    def member(self, tid, uid):
        table = self.table(tid)
        player = table.player(uid)
        if not player or player["left"]:
            raise BattleshipError("Сначала присоединитесь к столу", 403)
        return table

    def ensure_one_table(self, uid):
        if any(
            table.player(uid) and not table.player(uid)["left"]
            for table in self.tables.values()
        ):
            raise BattleshipError("Сначала покиньте другой стол морского боя", 409)

    @staticmethod
    def available(user):
        if not user or user.deleted_at:
            raise BattleshipError("Профиль участника недоступен", 409)
        if cents(user.balance) < 0:
            raise BattleshipError("Для участия нужен неотрицательный баланс", 409)

    def limit(self, table, db):
        balances = []
        for player in table.active_players():
            user = db.get(User, player["id"])
            if not user or user.deleted_at or cents(user.balance) < 0:
                return 0, False
            balances.append(cents(user.balance))
        return (min(balances), True) if balances else (0, False)

    def validate_stake(self, table, db, entry):
        maximum, available = self.limit(table, db)
        if not available:
            raise BattleshipError(
                "Для участия нужны действующие профили с неотрицательным балансом", 409
            )
        if entry > maximum:
            raise BattleshipError(
                "Ставка превышает меньший баланс участников. Снизьте её и подтвердите готовность заново",
                409,
            )
        return maximum

    def refresh(self, table, db):
        changed = False
        for player in table.players:
            user = db.get(User, player["id"])
            if user:
                name = user.display_name or user.guild_nickname or user.username
                if name != player["name"]:
                    player["name"], changed = name, True
        if changed:
            table.touch()

    def start(self, table, db):
        entry = table.rules["entry_cents"]
        maximum = self.validate_stake(table, db, entry)
        for player in table.active_players():
            user = db.get(User, player["id"])
            self.available(user)
            if cents(user.balance) + entry > MAX_CENTS:
                raise BattleshipError(
                    "Баланс участника не вместит возможный выигрыш. Снизьте ставку", 409
                )
        table.start()
        table.match["stake_limit_at_start"] = maximum
        for player in table.active_players():
            user = db.get(User, player["id"])
            user.balance = (cents(user.balance) - entry) / 100

    def settle(self, table, db):
        match = table.match
        if not match or not match.get("result") or match.get("settled"):
            return
        participants = match["participants"]
        winner = match["result"]["winner"]
        entry, bank = match["entry_cents"], match["bank_cents"]
        if (
            len(participants) != 2
            or len({p["id"] for p in participants}) != 2
            or winner not in {p["id"] for p in participants}
            or type(entry) is not int
            or entry < 0
            or type(bank) is not int
            or bank != entry * 2
        ):
            raise BattleshipError("Банк или участники боя требуют проверки", 409)
        recipient = db.get(User, winner)
        if recipient is None:
            raise BattleshipError(
                "Для выплаты нужно восстановить профиль победителя", 409
            )
        if cents(recipient.balance) + bank > MAX_CENTS:
            raise BattleshipError("Баланс победителя достиг лимита", 409)
        recipient.balance = (cents(recipient.balance) + bank) / 100
        for participant in participants:
            uid = participant["id"]
            if db.get(User, uid):
                payout = bank if uid == winner else 0
                db.add(
                    GameHistory(
                        user_id=uid,
                        game_type="battleship",
                        result="won" if uid == winner else "lost",
                        amount=(payout - entry) / 100,
                        details=f"Морской бой, стол {table.id}, бой #{match['number']}; взнос {entry / 100:.2f} БК, получено {payout / 100:.2f} БК; {match['result']['reason']}",
                    )
                )
        match["result"]["payouts"] = {
            str(p["id"]): bank if p["id"] == winner else 0 for p in participants
        }
        match["settled"] = True

    def state(self, table, user, db):
        if not expired(table):
            table.tick()
        self.settle(table, db)
        self.refresh(table, db)
        data = table.public_state(user.id)
        if table.phase == "waiting":
            maximum, available = self.limit(table, db)
        else:
            maximum, available = (
                table.match.get("stake_limit_at_start", table.rules["entry_cents"]),
                True,
            )
        data.update(
            balance=user.balance,
            stake_limit_cents=maximum,
            stake_valid=available and table.rules["entry_cents"] <= maximum,
            expired=expired(table),
        )
        return data

    def detach(self, uid, db):
        for tid, table in list(self.tables.items()):
            if table.player(uid):
                table.leave(uid)
                self.settle(table, db)
            if not table.players or all(player["left"] for player in table.players):
                self.tables.pop(tid, None)

    def tick_table(self, tid, db):
        from table_lifecycle import expire_match

        if expire_match(self, tid, db, "battleship"):
            return
        table = self.tables.get(tid)
        if table:
            table.tick()
            self.settle(table, db)

    def restore(self, values, pause=0):
        self.tables.clear()
        for tid, value in values.items():
            table = BattleshipTable.from_dict(value)
            if pause and table.deadline:
                table.deadline += pause
            self.tables[tid] = table


def build_router(service, get_user, get_db, templates, may_manage=lambda user: False):
    router = APIRouter()

    def view(table, user, db):
        return service.state(table, user, db) | management(
            "battleship", table, may_manage(user), user.id
        )

    @router.get("/games/battleship")
    @router.get("/games/battleship/lobby")
    async def lobby(request: Request, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        return templates.TemplateResponse(
            request=request, name="battleship_lobby.html", context={"user": user}
        )

    @router.get("/games/battleship/table/{tid}")
    async def page(request: Request, tid: str, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        service.table(tid)
        return templates.TemplateResponse(
            request=request,
            name="battleship_table.html",
            context={"user": user, "table_id": tid},
        )

    @router.get("/api/battleship/limits")
    async def limits(response: Response, user=Depends(get_user)):
        require(user)
        response.headers["Cache-Control"] = "private, no-store"
        balance = cents(user.balance)
        return {"max_entry_cents": max(0, balance), "can_play": balance >= 0}

    @router.get("/api/battleship/tables")
    async def tables(response: Response, user=Depends(get_user)):
        require(user)
        response.headers["Cache-Control"] = "private, no-store"
        return {
            "tables": [
                {
                    "id": tid,
                    "rules": table.rules,
                    "phase": table.phase,
                    "players": len(table.active_players()),
                    "creator": next(
                        (p["name"] for p in table.players if p["id"] == table.creator),
                        "Создатель",
                    ),
                    "mine": bool(
                        table.player(user.id) and not table.player(user.id)["left"]
                    ),
                    **management("battleship", table, may_manage(user), user.id),
                }
                for tid, table in service.tables.items()
                if not expired(table)
                and (
                    not table.rules["private"]
                    or may_manage(user)
                    or (table.player(user.id) and not table.player(user.id)["left"])
                )
            ]
        }

    @router.post("/api/battleship/tables")
    async def create(request: Request, user=Depends(get_user), db=Depends(get_db)):
        require(user)
        service.available(user)
        data = await body(request, ["rules"])
        rules = settings(data.get("rules", {}))
        service.ensure_one_table(user.id)
        table = BattleshipTable(
            secrets.token_urlsafe(18),
            user.id,
            user.display_name or user.guild_nickname or user.username,
            rules,
        )
        service.validate_stake(table, db, rules["entry_cents"])
        service.tables[table.id] = table
        return {"ok": True, "table_id": table.id}

    @router.get("/api/battleship/table/{tid}/preview")
    async def preview(tid: str, response: Response, user=Depends(get_user)):
        require(user)
        table = service.table(tid)
        response.headers["Cache-Control"] = "private, no-store"
        return {
            "rules": table.rules,
            "players": len(table.active_players()),
            "phase": table.phase,
            "expired": expired(table),
        }

    @router.post("/api/battleship/table/{tid}/join")
    async def join(tid: str, user=Depends(get_user)):
        require(user)
        service.available(user)
        table = service.table(tid)
        if expired(table):
            raise BattleshipError("Срок неактивности стола истёк", 409)
        if not table.player(user.id):
            service.ensure_one_table(user.id)
            table.add_player(
                user.id, user.display_name or user.guild_nickname or user.username
            )
        elif table.player(user.id)["left"]:
            raise BattleshipError("Вернуться можно после завершения боя", 409)
        return {"ok": True}

    @router.get("/api/battleship/table/{tid}/state")
    async def state(
        tid: str, response: Response, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        response.headers["Cache-Control"] = "private, no-store"
        table = service.table(tid)
        finished_participant = (
            table.phase == "finished"
            and table.match
            and any(p["id"] == user.id for p in table.match["participants"])
        )
        if not finished_participant:
            service.member(tid, user.id)
        return view(table, user, db)

    @router.post("/api/battleship/table/{tid}/fleet")
    async def fleet(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "fleet", "random"])
        revision(table, data)
        if "random" in data and type(data["random"]) is not bool:
            raise BattleshipError("Некорректный способ расстановки")
        if "random" in data and "fleet" in data:
            raise BattleshipError("Выберите один способ расстановки")
        table.set_fleet(
            user.id, random_fleet() if data.get("random") is True else data.get("fleet")
        )
        return view(table, user, db)

    @router.post("/api/battleship/table/{tid}/ready")
    async def ready(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "ready"])
        revision(table, data)
        if table.phase != "waiting" or type(data.get("ready")) is not bool:
            raise BattleshipError("Готовность меняется только до старта", 409)
        if data["ready"]:
            validate_fleet(table.player(user.id)["fleet"])
            service.validate_stake(table, db, table.rules["entry_cents"])
        table.player(user.id)["ready"] = data["ready"]
        table.touch()
        return view(table, user, db)

    @router.post("/api/battleship/table/{tid}/settings")
    async def change_settings(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "rules"])
        revision(table, data)
        if table.creator != user.id:
            raise BattleshipError("Условия меняет создатель стола", 403)
        rules = settings(data.get("rules"))
        service.validate_stake(table, db, rules["entry_cents"])
        table.change_settings(rules)
        return view(table, user, db)

    @router.post("/api/battleship/table/{tid}/start")
    async def start(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise BattleshipError("Бой запускает создатель стола", 403)
        service.start(table, db)
        return view(table, user, db)

    @router.post("/api/battleship/table/{tid}/fire")
    async def fire(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "cell"])
        revision(table, data)
        table.fire(user.id, data.get("cell"))
        service.settle(table, db)
        return view(table, user, db)

    @router.post("/api/battleship/table/{tid}/leave")
    async def leave(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        table.leave(user.id)
        service.settle(table, db)
        if not table.players or all(player["left"] for player in table.players):
            service.tables.pop(tid, None)
        return {"ok": True}

    @router.post("/api/battleship/table/{tid}/rematch")
    async def rematch(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise BattleshipError("Новый бой создаёт создатель стола", 403)
        table.rematch()
        return view(table, user, db)

    return router
