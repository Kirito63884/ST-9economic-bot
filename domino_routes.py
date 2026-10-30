"""Domino HTTP API and wallet settlement."""

import secrets
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from domino import DominoError, DominoTable
from database import GameHistory, User
from table_admin import management

MAX_CENTS = 100_000_000_000_000


def cents(value):
    return int(Decimal(str(value)) * 100)


class DominoService:
    def __init__(self):
        self.tables = {}

    def table(self, tid):
        table = self.tables.get(tid)
        if table is None:
            raise DominoError("Стол не найден или уже закрыт", 404)
        return table

    def member(self, tid, uid):
        table = self.table(tid)
        player = table.player(uid)
        if not player or player.get("left"):
            raise DominoError("Сначала присоединитесь к столу", 403)
        return table

    def ensure_one_table(self, uid):
        if any(
            t.player(uid) and not t.player(uid).get("left")
            for t in self.tables.values()
        ):
            raise DominoError("Сначала покиньте другой стол домино", 409)

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
                raise DominoError(
                    "Один из игроков больше не имеет доступа к сайту", 409
                )
            if cents(user.balance) < entry:
                raise DominoError(player["name"] + ": недостаточно бишопкоинов", 409)
            if cents(user.balance) + entry * (len(active) - 1) > MAX_CENTS:
                raise DominoError(
                    player["name"] + ": на балансе недостаточно места для выплаты", 409
                )
        table.start()
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
                raise DominoError(
                    "Для выплаты нужно восстановить профиль победителя", 409
                )
            if user and cents(user.balance) + amount > MAX_CENTS:
                raise DominoError("Баланс победителя достиг лимита", 409)
            changes.append((participant, user, amount))
        for participant, user, amount in changes:
            if user:
                user.balance = (cents(user.balance) + amount) / 100
            result = (
                "push"
                if len(winners) == len(match["participants"])
                else "won"
                if participant["id"] in winners
                else "lost"
            )
            db.add(
                GameHistory(
                    user_id=participant["id"],
                    game_type="domino",
                    result=result,
                    amount=(amount - match["entry_cents"]) / 100,
                    details=(
                        f"Домино, стол {table.id}, партия #{match['number']}; "
                        f"ставка {match['entry_cents'] / 100:.2f} БК, получено {amount / 100:.2f} БК; "
                        f"{match['result']['reason']}"
                    ),
                )
            )
        match["result"]["payouts"] = payouts
        match["settled"] = True

    def state(self, table, user, db):
        table.tick()
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
        from table_lifecycle import expire_match

        if expire_match(self, tid, db, "domino"):
            return
        table = self.tables.get(tid)
        if table:
            table.tick()
            self.settle(table, db)

    def restore(self, values, pause=0):
        self.tables.clear()
        for tid, value in values.items():
            table = DominoTable.from_dict(value)
            if pause and table.deadline:
                table.deadline += pause
            self.tables[tid] = table


async def body(request, allowed):
    try:
        value = await request.json()
    except (ValueError, UnicodeError):
        raise DominoError("Ожидается JSON")
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise DominoError("Некорректные поля запроса")
    return value


def require(user):
    if not user:
        raise DominoError("Войдите в свой профиль", 401)


def revision(table, value):
    if type(value.get("revision")) is not int or value["revision"] != table.revision:
        raise DominoError(
            "Состояние стола изменилось. Обновите страницу и повторите действие", 409
        )


def build_router(service, get_user, get_db, templates, may_manage=lambda user: False):
    router = APIRouter()

    def view(table, user, db):
        data = service.state(table, user, db)
        data.update(management("domino", table, may_manage(user), user.id))
        return data

    @router.get("/games/domino")
    @router.get("/games/domino/lobby")
    async def lobby(request: Request, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        return templates.TemplateResponse(
            request=request, name="domino_lobby.html", context={"user": user}
        )

    @router.get("/games/domino/table/{tid}")
    async def page(request: Request, tid: str, user=Depends(get_user)):
        if not user:
            return RedirectResponse("/login")
        service.table(tid)
        return templates.TemplateResponse(
            request=request,
            name="domino_table.html",
            context={"user": user, "table_id": tid},
        )

    @router.get("/api/domino/tables")
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
                    **management("domino", t, may_manage(user), user.id),
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

    @router.post("/api/domino/tables")
    async def create(request: Request, user=Depends(get_user)):
        require(user)
        data = await body(request, ["rules"])
        service.ensure_one_table(user.id)
        if not isinstance(data.get("rules", {}), dict):
            raise DominoError("Настройки должны быть объектом")
        tid = secrets.token_urlsafe(18)
        table = DominoTable(
            tid,
            user.id,
            user.display_name or user.guild_nickname or user.username,
            data.get("rules", {}),
        )
        service.tables[tid] = table
        return {"ok": True, "table_id": tid}

    @router.get("/api/domino/table/{tid}/preview")
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

    @router.post("/api/domino/table/{tid}/join")
    async def join(tid: str, user=Depends(get_user)):
        require(user)
        table = service.table(tid)
        if not table.player(user.id):
            service.ensure_one_table(user.id)
            table.add_player(
                user.id, user.display_name or user.guild_nickname or user.username
            )
        elif table.player(user.id).get("left"):
            raise DominoError("Вернуться можно после завершения партии", 409)
        return {"ok": True}

    @router.get("/api/domino/table/{tid}/state")
    async def state(
        tid: str, response: Response, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        response.headers["Cache-Control"] = "private, no-store"
        return view(service.member(tid, user.id), user, db)

    @router.post("/api/domino/table/{tid}/ready")
    async def ready(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "ready"])
        revision(table, data)
        if table.phase != "waiting" or type(data.get("ready")) is not bool:
            raise DominoError("Готовность меняется только до начала партии", 409)
        if data["ready"] and cents(user.balance) < table.rules["entry_cents"]:
            raise DominoError("Недостаточно бишопкоинов для ставки")
        table.player(user.id)["ready"] = data["ready"]
        table.touch()
        return view(table, user, db)

    @router.post("/api/domino/table/{tid}/settings")
    async def change_settings(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "rules"])
        revision(table, data)
        if table.creator != user.id:
            raise DominoError("Настройки меняет создатель стола", 403)
        table.change_settings(data.get("rules"))
        return view(table, user, db)

    @router.post("/api/domino/table/{tid}/start")
    async def start(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise DominoError("Партию запускает создатель стола", 403)
        service.start(table, db)
        return view(table, user, db)

    @router.post("/api/domino/table/{tid}/action")
    async def action(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "kind", "tile", "side", "direction"])
        revision(table, data)
        table.action(
            user.id,
            data.get("kind"),
            data.get("tile"),
            data.get("side"),
            direction=data.get("direction", "auto"),
        )
        service.settle(table, db)
        return view(table, user, db)

    @router.post("/api/domino/table/{tid}/leave")
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

    @router.post("/api/domino/table/{tid}/rematch")
    async def rematch(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise DominoError("Новую партию создаёт создатель стола", 403)
        table.rematch()
        return view(table, user, db)

    return router
