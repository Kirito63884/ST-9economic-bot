"""Go lobby, durable tables, and atomic wallet settlement."""

import secrets
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from go_game import GoError, GoTable
from database import GameHistory, User
from table_admin import management

MAX_CENTS = 100_000_000_000_000


def cents(value):
    return int(Decimal(str(value)) * 100)


class GoService:
    def __init__(self):
        self.tables = {}

    def table(self, tid):
        table = self.tables.get(tid)
        if table is None:
            raise GoError("Стол не найден или уже закрыт", 404)
        return table

    def member(self, tid, uid):
        table = self.table(tid)
        if not table.player(uid) or table.player(uid)["left"]:
            raise GoError("Сначала присоединитесь к столу", 403)
        return table

    def ensure_one_table(self, uid):
        if any(
            t.player(uid) and not t.player(uid)["left"] for t in self.tables.values()
        ):
            raise GoError("Сначала покиньте другой стол Го", 409)

    def settle(self, table, db):
        match = table.match
        if not match or not match.get("result") or match.get("settled"):
            return
        winners = match["result"]["winners"]
        entry, bank = match["entry_cents"], match["bank_cents"]
        if bank != entry * len(match["participants"]):
            raise GoError("Банк стола не совпадает со взносами", 409)
        payouts = {}
        if not winners:
            payouts = {str(p["id"]): entry for p in match["participants"]}
        else:
            payouts[str(winners[0])] = bank
        for p in match["participants"]:
            user = db.get(User, p["id"])
            amount = payouts.get(str(p["id"]), 0)
            if amount and not user:
                raise GoError("Для выплаты нужно восстановить профиль игрока", 409)
            if user and cents(user.balance) + amount > MAX_CENTS:
                raise GoError("Баланс игрока достиг лимита", 409)
        for p in match["participants"]:
            user = db.get(User, p["id"])
            amount = payouts.get(str(p["id"]), 0)
            if user:
                user.balance = (cents(user.balance) + amount) / 100
            result = "push" if not winners else "won" if p["id"] in winners else "lost"
            db.add(
                GameHistory(
                    user_id=p["id"],
                    game_type="go",
                    result=result,
                    amount=(amount - entry) / 100,
                    details=f"Го, стол {table.id}, партия #{match['number']}; взнос {entry / 100:.2f} БК, выплата {amount / 100:.2f} БК; {match['result']['reason']}",
                )
            )
        match["result"]["payouts"] = payouts
        match["settled"] = True

    def state(self, table, user, db):
        self.settle(table, db)
        data = table.public_state(user.id)
        data["balance"] = user.balance
        balances = [
            cents(db.get(User, p["id"]).balance)
            for p in table.active_players()
            if db.get(User, p["id"])
        ]
        data["stake_limit_cents"] = max(0, min(balances)) if balances else 0
        data["stake_valid"] = table.phase != "waiting" or (
            len(balances) == 2
            and all(b >= table.rules["entry_cents"] for b in balances)
        )
        return data

    def detach(self, uid, db):
        for tid, table in list(self.tables.items()):
            player = table.player(uid)
            if not player:
                continue
            if not player["left"]:
                table.leave(uid)
                self.settle(table, db)
            if not table.active_players():
                self.tables.pop(tid, None)

    def tick_table(self, tid, db):
        from table_lifecycle import expire_match

        expire_match(self, tid, db, "go")

    def restore(self, values):
        self.tables.clear()
        for tid, value in values.items():
            self.tables[tid] = GoTable.from_dict(value)


async def body(request, allowed):
    try:
        data = await request.json()
    except (ValueError, UnicodeError):
        raise GoError("Ожидается JSON")
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise GoError("Некорректные поля запроса")
    return data


def require(user):
    if not user:
        raise GoError("Войдите в свой профиль", 401)


def revision(table, data):
    if type(data.get("revision")) is not int or data["revision"] != table.revision:
        raise GoError("Состояние стола изменилось. Обновите страницу", 409)


def build_router(service, get_user, get_db, templates, may_manage=lambda user: False):
    router = APIRouter()

    def view(table, user, db):
        state = service.state(table, user, db)
        state.update(management("go", table, may_manage(user), user.id))
        return state

    @router.get("/games/go")
    @router.get("/games/go/lobby")
    async def lobby(request: Request, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        return templates.TemplateResponse(
            request=request, name="go_lobby.html", context={"user": user}
        )

    @router.get("/games/go/table/{tid}")
    async def page(request: Request, tid: str, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        service.table(tid)
        return templates.TemplateResponse(
            request=request,
            name="go_table.html",
            context={"user": user, "table_id": tid},
        )

    @router.get("/api/go/tables")
    async def tables(user=Depends(get_user)):
        require(user)
        visible = [
            t
            for t in service.tables.values()
            if may_manage(user)
            or not t.rules["private"]
            or (t.player(user.id) and not t.player(user.id)["left"])
        ]
        return {
            "tables": [
                {
                    "id": t.id,
                    "creator": next(
                        (p["name"] for p in t.players if p["id"] == t.creator),
                        "Создатель",
                    ),
                    "players": len(t.active_players()),
                    "phase": t.phase,
                    "rules": t.rules,
                    "mine": bool(t.player(user.id) and not t.player(user.id)["left"]),
                    **management("go", t, may_manage(user), user.id),
                }
                for t in visible
            ]
        }

    @router.post("/api/go/tables")
    async def create(request: Request, user=Depends(get_user)):
        require(user)
        data = await body(request, ["rules"])
        if cents(user.balance) < 0:
            raise GoError("Для участия нужен неотрицательный баланс", 409)
        service.ensure_one_table(user.id)
        table = GoTable(
            secrets.token_urlsafe(18),
            user.id,
            user.display_name or user.guild_nickname or user.username,
            data.get("rules", {}),
        )
        service.tables[table.id] = table
        return {"ok": True, "table_id": table.id}

    @router.post("/api/go/table/{tid}/join")
    async def join(tid: str, user=Depends(get_user)):
        require(user)
        table = service.table(tid)
        if not table.player(user.id):
            if cents(user.balance) < 0:
                raise GoError("Для участия нужен неотрицательный баланс", 409)
            service.ensure_one_table(user.id)
            table.add_player(
                user.id, user.display_name or user.guild_nickname or user.username
            )
        elif table.player(user.id)["left"]:
            raise GoError("Вернуться можно после завершения партии", 409)
        return {"ok": True}

    @router.get("/api/go/table/{tid}/preview")
    async def preview(tid: str, user=Depends(get_user)):
        require(user)
        table = service.table(tid)
        player = table.player(user.id)
        return {
            "rules": table.rules,
            "players": len(table.active_players()),
            "phase": table.phase,
            "in_table": bool(player and not player["left"]),
            "creator": next(
                (p["name"] for p in table.players if p["id"] == table.creator),
                "Создатель",
            ),
        }

    @router.get("/api/go/table/{tid}/state")
    async def state(
        tid: str, response: Response, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        response.headers["Cache-Control"] = "private, no-store"
        return view(service.member(tid, user.id), user, db)

    @router.post("/api/go/table/{tid}/ready")
    async def ready(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "ready"])
        revision(table, data)
        if table.phase != "waiting" or type(data.get("ready")) is not bool:
            raise GoError("Готовность меняется только до начала партии", 409)
        if data["ready"] and cents(user.balance) < table.rules["entry_cents"]:
            raise GoError("Недостаточно бишопкоинов")
        table.player(user.id)["ready"] = data["ready"]
        table.touch()
        return view(table, user, db)

    @router.post("/api/go/table/{tid}/settings")
    async def settings_route(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "rules"])
        revision(table, data)
        if table.creator != user.id:
            raise GoError("Настройки меняет создатель стола", 403)
        table.change_settings(data.get("rules"))
        return view(table, user, db)

    @router.post("/api/go/table/{tid}/start")
    async def start(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise GoError("Партию запускает создатель стола", 403)
        entry = table.rules["entry_cents"]
        for p in table.active_players():
            player = db.get(User, p["id"])
            if not player or player.deleted_at or cents(player.balance) < entry:
                raise GoError("У игрока недостаточно средств для взноса", 409)
            if cents(player.balance) + entry > MAX_CENTS:
                raise GoError(
                    "На балансе игрока недостаточно места для возможной выплаты", 409
                )
        table.start()
        for p in table.active_players():
            player = db.get(User, p["id"])
            player.balance = (cents(player.balance) - entry) / 100
        return view(table, user, db)

    @router.post("/api/go/table/{tid}/action")
    async def action(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "kind", "data"])
        revision(table, data)
        table.action(user.id, data.get("kind"), data.get("data", {}))
        service.settle(table, db)
        return view(table, user, db)

    @router.post("/api/go/table/{tid}/leave")
    async def leave(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        table.leave(user.id)
        service.settle(table, db)
        if not table.active_players():
            service.tables.pop(tid, None)
        return {"ok": True}

    @router.post("/api/go/table/{tid}/rematch")
    async def rematch(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise GoError("Новую партию создаёт владелец стола", 403)
        table.rematch()
        return view(table, user, db)

    return router
