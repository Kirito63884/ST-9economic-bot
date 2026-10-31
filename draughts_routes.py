"""Draughts lobby, durable tables, and atomic wallet settlement."""

import secrets
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from draughts_game import DraughtsError, DraughtsTable
from database import GameHistory, User
from table_admin import management

MAX_CENTS = 100_000_000_000_000


def cents(value):
    return int(Decimal(str(value)) * 100)


class DraughtsService:
    def __init__(self):
        self.tables = {}

    def table(self, tid):
        table = self.tables.get(tid)
        if table is None:
            raise DraughtsError("Стол не найден или уже закрыт", 404)
        return table

    def member(self, tid, uid):
        table = self.table(tid)
        if not table.player(uid) or table.player(uid)["left"]:
            raise DraughtsError("Сначала присоединитесь к столу", 403)
        return table

    def available(self, user):
        if cents(user.balance) < 0:
            raise DraughtsError("При отрицательном балансе участие недоступно", 409)

    def ensure_one_table(self, uid):
        if any(
            t.player(uid) and not t.player(uid)["left"] for t in self.tables.values()
        ):
            raise DraughtsError("Сначала покиньте другой шашечный стол", 409)

    def settle(self, table, db):
        match = table.match
        if not match or not match.get("result") or match.get("settled"):
            return
        winners = match["result"]["winners"]
        entry, bank = match["entry_cents"], match["bank_cents"]
        if bank != entry * len(match["participants"]):
            raise DraughtsError("Банк стола не совпадает со взносами", 409)
        payouts = {}
        if not winners:
            payouts = {str(p["id"]): entry for p in match["participants"]}
        else:
            payouts[str(winners[0])] = bank
        for p in match["participants"]:
            user = db.get(User, p["id"])
            amount = payouts.get(str(p["id"]), 0)
            if amount and not user:
                raise DraughtsError(
                    "Для выплаты нужно восстановить профиль игрока", 409
                )
            if user and cents(user.balance) + amount > MAX_CENTS:
                raise DraughtsError("Баланс игрока достиг лимита", 409)
        for p in match["participants"]:
            user = db.get(User, p["id"])
            amount = payouts.get(str(p["id"]), 0)
            if user:
                user.balance = (cents(user.balance) + amount) / 100
            result = "push" if not winners else "won" if p["id"] in winners else "lost"
            db.add(
                GameHistory(
                    user_id=p["id"],
                    game_type="draughts",
                    result=result,
                    amount=(amount - entry) / 100,
                    details=f"Русские шашки, стол {table.id}, партия #{match['number']}; взнос {entry / 100:.2f} БК, выплата {amount / 100:.2f} БК; {match['result']['reason']}",
                )
            )
        match["result"]["payouts"] = payouts
        match["settled"] = True

    def state(self, table, user, db):
        self.settle(table, db)
        data = table.public_state(user.id)
        data["balance"] = user.balance
        return data

    def leave(self, table, uid, db):
        player = table.player(uid)
        if table.phase == "playing":
            table.action(uid, "resign")
        self.settle(table, db)
        player["left"] = True
        if table.phase == "waiting":
            table.players.remove(player)
            for p in table.players:
                p["ready"] = False
        if not table.active_players():
            self.tables.pop(table.id, None)
        else:
            if table.creator == uid:
                table.creator = table.active_players()[0]["id"]
            table.touch("Игрок покинул стол")

    def detach(self, uid, db):
        for table in list(self.tables.values()):
            if table.player(uid) and not table.player(uid)["left"]:
                self.leave(table, uid, db)

    def tick_table(self, tid, db):
        from table_lifecycle import expire_match

        expire_match(self, tid, db, "draughts")

    def restore(self, values):
        self.tables.clear()
        for tid, value in values.items():
            self.tables[tid] = DraughtsTable.from_dict(value)


async def body(request, allowed):
    try:
        data = await request.json()
    except (ValueError, UnicodeError):
        raise DraughtsError("Ожидается JSON")
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise DraughtsError("Некорректные поля запроса")
    return data


def require(user):
    if not user:
        raise DraughtsError("Войдите в свой профиль", 401)


def revision(table, data):
    if type(data.get("revision")) is not int or data["revision"] != table.revision:
        raise DraughtsError("Состояние стола изменилось. Обновите страницу", 409)


def build_router(service, get_user, get_db, templates, may_manage=lambda user: False):
    router = APIRouter()

    def view(table, user, db):
        state = service.state(table, user, db)
        state.update(management("draughts", table, may_manage(user), user.id))
        return state

    @router.get("/games/draughts")
    @router.get("/games/draughts/lobby")
    async def lobby(request: Request, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        return templates.TemplateResponse(
            request=request, name="draughts_lobby.html", context={"user": user}
        )

    @router.get("/games/draughts/table/{tid}")
    async def page(request: Request, tid: str, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        service.table(tid)
        return templates.TemplateResponse(
            request=request,
            name="draughts_table.html",
            context={"user": user, "table_id": tid},
        )

    @router.get("/api/draughts/tables")
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
                    **management("draughts", t, may_manage(user), user.id),
                }
                for t in visible
            ]
        }

    @router.post("/api/draughts/tables")
    async def create(request: Request, user=Depends(get_user)):
        require(user)
        service.available(user)
        data = await body(request, ["rules"])
        service.ensure_one_table(user.id)
        table = DraughtsTable(
            secrets.token_urlsafe(18),
            user.id,
            user.display_name or user.guild_nickname or user.username,
            data.get("rules", {}),
        )
        service.tables[table.id] = table
        return {"ok": True, "table_id": table.id}

    @router.post("/api/draughts/table/{tid}/join")
    async def join(tid: str, user=Depends(get_user)):
        require(user)
        service.available(user)
        table = service.table(tid)
        if not table.player(user.id):
            service.ensure_one_table(user.id)
            table.add_player(
                user.id, user.display_name or user.guild_nickname or user.username
            )
        elif table.player(user.id)["left"]:
            raise DraughtsError("Вернуться можно после завершения партии", 409)
        return {"ok": True}

    @router.get("/api/draughts/table/{tid}/preview")
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

    @router.get("/api/draughts/table/{tid}/state")
    async def state(
        tid: str, response: Response, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        response.headers["Cache-Control"] = "private, no-store"
        return view(service.member(tid, user.id), user, db)

    @router.post("/api/draughts/table/{tid}/ready")
    async def ready(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "ready"])
        revision(table, data)
        if table.phase != "waiting" or type(data.get("ready")) is not bool:
            raise DraughtsError("Готовность меняется только до начала партии", 409)
        if data["ready"] and cents(user.balance) < table.rules["entry_cents"]:
            raise DraughtsError("Недостаточно бишопкоинов")
        table.player(user.id)["ready"] = data["ready"]
        table.touch()
        return view(table, user, db)

    @router.post("/api/draughts/table/{tid}/settings")
    async def settings_route(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "rules"])
        revision(table, data)
        if table.creator != user.id:
            raise DraughtsError("Настройки меняет создатель стола", 403)
        table.change_settings(data.get("rules"))
        return view(table, user, db)

    @router.post("/api/draughts/table/{tid}/start")
    async def start(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise DraughtsError("Партию запускает создатель стола", 403)
        entry = table.rules["entry_cents"]
        for p in table.active_players():
            player = db.get(User, p["id"])
            if not player or player.deleted_at or cents(player.balance) < entry:
                raise DraughtsError("У игрока недостаточно средств для взноса", 409)
            if cents(player.balance) + entry > MAX_CENTS:
                raise DraughtsError(
                    "На балансе игрока недостаточно места для возможной выплаты", 409
                )
        table.start()
        for p in table.active_players():
            player = db.get(User, p["id"])
            player.balance = (cents(player.balance) - entry) / 100
        return view(table, user, db)

    @router.post("/api/draughts/table/{tid}/action")
    async def action(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "kind", "move"])
        revision(table, data)
        table.action(user.id, data.get("kind"), data.get("move"))
        service.settle(table, db)
        return view(table, user, db)

    @router.post("/api/draughts/table/{tid}/rematch")
    async def rematch(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise DraughtsError("Новую партию запускает создатель стола", 403)
        table.rematch()
        return view(table, user, db)

    @router.post("/api/draughts/table/{tid}/leave")
    async def leave(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        service.leave(table, user.id, db)
        return {"ok": True}

    return router
