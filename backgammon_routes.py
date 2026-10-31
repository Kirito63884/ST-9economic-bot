"""Backgammon lobby and atomic fixed-stake settlement through shared runtime."""

import secrets
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from backgammon_game import BackgammonError, BackgammonTable
from database import GameHistory, User
from table_admin import management

MAX_CENTS = 100_000_000_000_000


def cents(value):
    amount = Decimal(str(value)) * 100
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise BackgammonError("Некорректный баланс", 409)
    return int(amount)


class BackgammonService:
    def __init__(self):
        self.tables = {}

    def table(self, tid):
        table = self.tables.get(tid)
        if table is None:
            raise BackgammonError("Стол не найден или уже закрыт", 404)
        return table

    def member(self, tid, uid):
        table = self.table(tid)
        if not table.player(uid) or table.player(uid)["left"]:
            raise BackgammonError("Сначала присоединитесь к столу", 403)
        return table

    def ensure_one_table(self, uid):
        if any(
            t.player(uid) and not t.player(uid)["left"] for t in self.tables.values()
        ):
            raise BackgammonError("Сначала покиньте другой нардовый стол", 409)

    def settle(self, table, db):
        match = table.match
        if not match or not match.get("result") or match.get("settled"):
            return
        winners = match["result"]["winners"]
        entry, bank = match["entry_cents"], match["bank_cents"]
        ids = [p["id"] for p in match["participants"]]
        if (
            type(entry) is not int
            or entry < 0
            or bank != entry * len(ids)
            or len(ids) != 2
            or len(set(ids)) != 2
            or len(winners) != 1
            or winners[0] not in ids
        ):
            raise BackgammonError("Банк стола не совпадает со взносами", 409)
        payouts = {str(winners[0]): bank}
        for p in match["participants"]:
            user = db.get(User, p["id"])
            amount = payouts.get(str(p["id"]), 0)
            if amount and not user:
                raise BackgammonError(
                    "Для выплаты нужно восстановить профиль игрока", 409
                )
            if user and cents(user.balance) + amount > MAX_CENTS:
                raise BackgammonError("Баланс игрока достиг лимита", 409)
        for p in match["participants"]:
            user = db.get(User, p["id"])
            amount = payouts.get(str(p["id"]), 0)
            if user:
                user.balance = (cents(user.balance) + amount) / 100
            result = "won" if p["id"] in winners else "lost"
            db.add(
                GameHistory(
                    user_id=p["id"],
                    game_type="backgammon",
                    result=result,
                    amount=(amount - entry) / 100,
                    details=f"Нарды, стол {table.id}, партия #{match['number']}; взнос {entry / 100:.2f} БК, выплата {amount / 100:.2f} БК; {match['result']['reason']}",
                )
            )
        match["result"]["payouts"] = payouts
        match["settled"] = True

    def state(self, table, user, db):
        self.settle(table, db)
        data = table.public_state(user.id)
        data["balance"] = user.balance
        return data

    def detach(self, uid, db):
        for tid, table in list(self.tables.items()):
            player = table.player(uid)
            if not player or player["left"]:
                continue
            if table.phase == "playing":
                winner = next(
                    p["id"] for p in table.match["participants"] if p["id"] != uid
                )
                table.finish([winner], "Соперник покинул стол")
                self.settle(table, db)
            self.remove_player(table, uid)

    def remove_player(self, table, uid):
        table.player(uid)["left"] = True
        if table.phase == "waiting":
            table.players = table.active_players()
            for p in table.players:
                p["ready"] = False
            if table.players:
                table.creator = table.players[0]["id"]
                table.touch("Игрок покинул стол; подтвердите готовность заново")
        else:
            table.touch("Игрок покинул стол")
        if not table.active_players():
            self.tables.pop(table.id, None)
        elif table.player(table.creator)["left"]:
            table.creator = table.active_players()[0]["id"]

    def tick_table(self, tid, db):
        from table_lifecycle import expire_match

        expire_match(self, tid, db, "backgammon")

    def restore(self, values):
        self.tables.clear()
        for tid, value in values.items():
            self.tables[tid] = BackgammonTable.from_dict(value)


async def body(request, allowed):
    try:
        data = await request.json()
    except (ValueError, UnicodeError):
        raise BackgammonError("Ожидается JSON")
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise BackgammonError("Некорректные поля запроса")
    return data


def require(user):
    if not user:
        raise BackgammonError("Войдите в свой профиль", 401)


def revision(table, data):
    if type(data.get("revision")) is not int or data["revision"] != table.revision:
        raise BackgammonError("Состояние стола изменилось. Обновите страницу", 409)


def build_router(service, get_user, get_db, templates, may_manage=lambda user: False):
    router = APIRouter()

    def view(table, user, db):
        state = service.state(table, user, db)
        state.update(management("backgammon", table, may_manage(user), user.id))
        return state

    @router.get("/games/backgammon")
    @router.get("/games/backgammon/lobby")
    async def lobby(request: Request, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        return templates.TemplateResponse(
            request=request, name="backgammon_lobby.html", context={"user": user}
        )

    @router.get("/games/backgammon/table/{tid}")
    async def page(request: Request, tid: str, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        service.table(tid)
        return templates.TemplateResponse(
            request=request,
            name="backgammon_table.html",
            context={"user": user, "table_id": tid},
        )

    @router.get("/api/backgammon/tables")
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
                    **management("backgammon", t, may_manage(user), user.id),
                }
                for t in visible
            ]
        }

    @router.post("/api/backgammon/tables")
    async def create(request: Request, user=Depends(get_user)):
        require(user)
        data = await body(request, ["rules"])
        if cents(user.balance) < 0:
            raise BackgammonError("Для игры нужен неотрицательный баланс", 409)
        service.ensure_one_table(user.id)
        table = BackgammonTable(
            secrets.token_urlsafe(18),
            user.id,
            user.display_name or user.guild_nickname or user.username,
            data.get("rules", {}),
        )
        service.tables[table.id] = table
        return {"ok": True, "table_id": table.id}

    @router.post("/api/backgammon/table/{tid}/join")
    async def join(tid: str, user=Depends(get_user)):
        require(user)
        if cents(user.balance) < 0:
            raise BackgammonError("Для игры нужен неотрицательный баланс", 409)
        table = service.table(tid)
        if not table.player(user.id):
            service.ensure_one_table(user.id)
            table.add_player(
                user.id, user.display_name or user.guild_nickname or user.username
            )
        elif table.player(user.id)["left"]:
            raise BackgammonError("Вернуться можно после завершения партии", 409)
        return {"ok": True}

    @router.get("/api/backgammon/table/{tid}/preview")
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

    @router.get("/api/backgammon/table/{tid}/state")
    async def state(
        tid: str, response: Response, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        response.headers["Cache-Control"] = "private, no-store"
        return view(service.member(tid, user.id), user, db)

    @router.post("/api/backgammon/table/{tid}/ready")
    async def ready(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "ready"])
        revision(table, data)
        if table.phase != "waiting" or type(data.get("ready")) is not bool:
            raise BackgammonError("Готовность меняется только до начала партии", 409)
        if data["ready"] and cents(user.balance) < table.rules["entry_cents"]:
            raise BackgammonError("Недостаточно бишопкоинов")
        table.player(user.id)["ready"] = data["ready"]
        table.touch()
        return view(table, user, db)

    @router.post("/api/backgammon/table/{tid}/settings")
    async def settings_route(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "rules"])
        revision(table, data)
        if table.creator != user.id:
            raise BackgammonError("Настройки меняет создатель стола", 403)
        table.change_settings(data.get("rules"))
        return view(table, user, db)

    @router.post("/api/backgammon/table/{tid}/start")
    async def start(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise BackgammonError("Партию запускает создатель стола", 403)
        entry = table.rules["entry_cents"]
        for p in table.active_players():
            player = db.get(User, p["id"])
            if not player or player.deleted_at or cents(player.balance) < entry:
                raise BackgammonError("У игрока недостаточно средств для взноса", 409)
            if cents(player.balance) + entry > MAX_CENTS:
                raise BackgammonError(
                    "На балансе игрока недостаточно места для возможной выплаты", 409
                )
        table.start()
        for p in table.active_players():
            player = db.get(User, p["id"])
            player.balance = (cents(player.balance) - entry) / 100
        return view(table, user, db)

    @router.post("/api/backgammon/table/{tid}/action")
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

    @router.post("/api/backgammon/table/{tid}/leave")
    async def leave(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.phase == "playing":
            table.action(user.id, "resign")
        service.settle(table, db)
        service.remove_player(table, user.id)
        return {"ok": True}

    @router.post("/api/backgammon/table/{tid}/rematch")
    async def rematch(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.phase != "finished" or not table.match or not table.match["settled"]:
            raise BackgammonError("Сначала завершите расчёт партии", 409)
        if table.creator != user.id:
            raise BackgammonError("Новую партию предлагает создатель стола", 403)
        table.players = table.active_players()
        for player in table.players:
            player["ready"] = False
        table.phase, table.actor, table.round = "waiting", None, None
        table.touch("Новая партия: подтвердите готовность заново")
        return view(table, user, db)

    return router
