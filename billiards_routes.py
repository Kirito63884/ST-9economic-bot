"""Billiards HTTP API and wallet settlement."""

import secrets
import time
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from billiards import BilliardsError, BilliardsTable
from database import GameHistory, User
from table_admin import management

MAX_CENTS = 100_000_000_000_000


def cents(value):
    return int(Decimal(str(value)) * 100)


class BilliardsService:
    def __init__(self):
        self.tables = {}

    def table(self, tid):
        table = self.tables.get(tid)
        if table is None:
            raise BilliardsError("Стол не найден или уже закрыт", 404)
        return table

    def member(self, tid, uid):
        table = self.table(tid)
        player = table.player(uid)
        if not player or player.get("left"):
            raise BilliardsError("Сначала присоединитесь к столу", 403)
        return table

    def ensure_one_table(self, uid):
        if any(
            t.player(uid) and not t.player(uid).get("left")
            for t in self.tables.values()
        ):
            raise BilliardsError("Сначала покиньте другой бильярдный стол", 409)

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
        active = table.active_players()
        for player in active:
            user = db.get(User, player["id"])
            if not user or user.deleted_at:
                raise BilliardsError(
                    "Один из игроков больше не имеет доступа к сайту", 409
                )
            if cents(user.balance) < entry:
                raise BilliardsError(player["name"] + ": недостаточно бишопкоинов", 409)
            if cents(user.balance) + entry * (len(active) - 1) > MAX_CENTS:
                raise BilliardsError(
                    player["name"] + ": на балансе недостаточно места для выплаты", 409
                )
        table.start()
        if table.rules["mode"] != "practice":
            for player in active:
                user = db.get(User, player["id"])
                user.balance = (cents(user.balance) - entry) / 100

    def settle(self, table, db):
        match = table.match
        if not match or not match.get("result") or match.get("settled"):
            return
        winners = match["result"]["winners"]
        if not winners:
            match["settled"] = True
            return
        bank = match["bank_cents"]
        base, remainder = divmod(bank, len(winners))
        payouts = {
            str(uid): base + (1 if index < remainder else 0)
            for index, uid in enumerate(sorted(winners))
        }
        changes = []
        for participant in match["participants"]:
            user = db.get(User, participant["id"])
            amount = payouts.get(str(participant["id"]), 0)
            if amount and not user:
                raise BilliardsError(
                    "Для выплаты нужно восстановить профиль победителя", 409
                )
            if user and cents(user.balance) + amount > MAX_CENTS:
                raise BilliardsError("Баланс победителя достиг лимита", 409)
            changes.append((participant, user, amount))
        for participant, user, amount in changes:
            if user:
                user.balance = (cents(user.balance) + amount) / 100
            result = "won" if participant["id"] in winners else "lost"
            db.add(
                GameHistory(
                    user_id=participant["id"],
                    game_type="billiards",
                    result=result,
                    amount=(amount - match["entry_cents"]) / 100,
                    details=(
                        f"Бильярд, стол {table.id}, партия #{match['number']}; "
                        f"ставка {match['entry_cents'] / 100:.2f} БК, получено {amount / 100:.2f} БК; "
                        f"{match['result']['reason']}"
                    ),
                )
            )
        match["result"]["payouts"] = payouts
        match["settled"] = True

    def state(self, table, user, db):
        table.timeout()
        self.settle(table, db)
        self.refresh(table, db)
        data = table.public_state(user.id)
        data["balance"] = user.balance
        return data

    def detach(self, uid, db):
        for tid, table in list(self.tables.items()):
            if table.player(uid):
                table.leave(uid)
                self.settle(table, db)
            if not table.players or all(p.get("left") for p in table.players):
                self.tables.pop(tid, None)

    def tick_table(self, tid, db):
        table = self.tables.get(tid)
        if not table:
            return
        table.timeout()
        self.settle(table, db)
        now = time.time()
        if table.phase in ("waiting", "finished") and now - table.updated_at > (
            86400 if table.phase == "waiting" else 7200
        ):
            self.tables.pop(tid, None)

    def restore(self, values, pause=0):
        self.tables.clear()
        for tid, value in values.items():
            table = BilliardsTable.from_dict(value)
            if pause and table.deadline:
                table.deadline += pause
            self.tables[tid] = table


async def body(request, allowed):
    try:
        value = await request.json()
    except (ValueError, UnicodeError):
        raise BilliardsError("Ожидается JSON")
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise BilliardsError("Некорректные поля запроса")
    return value


def require(user):
    if not user:
        raise BilliardsError("Войдите в свой профиль", 401)


def revision(table, value):
    if type(value.get("revision")) is not int or value["revision"] != table.revision:
        raise BilliardsError(
            "Состояние стола изменилось. Обновите страницу и повторите действие", 409
        )


def build_router(service, get_user, get_db, templates, may_manage=lambda user: False):
    router = APIRouter()

    @router.get("/games/billiards")
    @router.get("/games/billiards/lobby")
    async def lobby(request: Request, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        return templates.TemplateResponse(
            request=request, name="billiards_lobby.html", context={"user": user}
        )

    @router.get("/games/billiards/table/{tid}")
    async def page(request: Request, tid: str, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        service.table(tid)
        return templates.TemplateResponse(
            request=request,
            name="billiards_table.html",
            context={"user": user, "table_id": tid},
        )

    @router.get("/api/billiards/tables")
    async def tables(user=Depends(get_user)):
        require(user)
        visible = [
            t
            for t in service.tables.values()
            if may_manage(user)
            or not t.rules["private"]
            or (t.player(user.id) and not t.player(user.id).get("left"))
        ]
        return {
            "tables": [
                {
                    "id": t.id,
                    **management("billiards", t, may_manage(user)),
                    "creator": next(
                        (p["name"] for p in t.players if p["id"] == t.creator),
                        "Создатель",
                    ),
                    "players": len(t.active_players()),
                    "phase": t.phase,
                    "rules": t.rules,
                    "mine": bool(
                        t.player(user.id) and not t.player(user.id).get("left")
                    ),
                }
                for t in visible
            ]
        }

    @router.post("/api/billiards/tables")
    async def create(request: Request, user=Depends(get_user)):
        require(user)
        data = await body(request, ["rules"])
        service.ensure_one_table(user.id)
        tid = secrets.token_urlsafe(18)
        table = BilliardsTable(
            tid,
            user.id,
            user.display_name or user.guild_nickname or user.username,
            data.get("rules", {}),
        )
        service.tables[tid] = table
        return {"ok": True, "table_id": tid}

    @router.get("/api/billiards/table/{tid}/preview")
    async def preview(tid: str, user=Depends(get_user)):
        require(user)
        table = service.table(tid)
        return {
            "rules": table.rules,
            "players": len(table.active_players()),
            "phase": table.phase,
            "in_table": bool(
                table.player(user.id) and not table.player(user.id).get("left")
            ),
            "creator": next(
                (p["name"] for p in table.players if p["id"] == table.creator),
                "Создатель",
            ),
        }

    @router.post("/api/billiards/table/{tid}/join")
    async def join(tid: str, user=Depends(get_user)):
        require(user)
        table = service.table(tid)
        if table.rules["mode"] == "practice" and table.creator != user.id:
            raise BilliardsError("Тренировочный стол принадлежит создателю", 403)
        if not table.player(user.id):
            service.ensure_one_table(user.id)
            table.add_player(
                user.id, user.display_name or user.guild_nickname or user.username
            )
        elif table.player(user.id).get("left"):
            raise BilliardsError("Вернуться можно после завершения партии", 409)
        return {"ok": True}

    @router.get("/api/billiards/table/{tid}/state")
    async def state(tid: str, user=Depends(get_user), db=Depends(get_db)):
        require(user)
        return service.state(service.member(tid, user.id), user, db)

    @router.post("/api/billiards/table/{tid}/ready")
    async def ready(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "ready"])
        revision(table, data)
        if table.phase != "waiting" or type(data.get("ready")) is not bool:
            raise BilliardsError("Готовность меняется только до начала партии", 409)
        if data["ready"] and cents(user.balance) < table.rules["entry_cents"]:
            raise BilliardsError("Недостаточно бишопкоинов для ставки")
        table.player(user.id)["ready"] = data["ready"]
        table.touch()
        return service.state(table, user, db)

    @router.post("/api/billiards/table/{tid}/settings")
    async def change_settings(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "rules"])
        revision(table, data)
        if table.creator != user.id:
            raise BilliardsError("Настройки меняет создатель стола", 403)
        table.change_settings(data.get("rules"))
        return service.state(table, user, db)

    @router.post("/api/billiards/table/{tid}/start")
    async def start(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise BilliardsError("Партию запускает создатель стола", 403)
        service.start(table, db)
        return service.state(table, user, db)

    @router.post("/api/billiards/table/{tid}/shoot")
    async def shoot(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "angle", "power", "cue_x", "cue_y"])
        revision(table, data)
        table.shoot(
            user.id,
            data.get("angle"),
            data.get("power"),
            data.get("cue_x"),
            data.get("cue_y"),
        )
        service.settle(table, db)
        return service.state(table, user, db)

    @router.post("/api/billiards/table/{tid}/leave")
    async def leave(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        table.leave(user.id)
        service.settle(table, db)
        if not table.players or all(p.get("left") for p in table.players):
            service.tables.pop(tid, None)
        return {"ok": True}

    @router.post("/api/billiards/table/{tid}/rematch")
    async def rematch(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise BilliardsError("Новую партию создаёт создатель стола", 403)
        table.rematch()
        return service.state(table, user, db)

    return router
