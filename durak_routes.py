"""Durak HTTP API; all handlers run inside main's durable game/wallet transaction."""

import secrets
import time
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from database import User, GameHistory
from durak import DurakError, DurakTable
from table_admin import management

MAX_CENTS = 100_000_000_000_000


def cents(value):
    return int(Decimal(str(value)) * 100)


class DurakService:
    def __init__(self):
        self.tables = {}

    def table(self, tid):
        table = self.tables.get(tid)
        if not table:
            raise DurakError("Стол не найден или уже закрыт", 404)
        return table

    def member(self, tid, uid):
        table = self.table(tid)
        p = table.player(uid)
        if not p or p.get("left"):
            raise DurakError("Сначала присоединитесь к столу", 403)
        return table

    def ensure_one_table(self, uid):
        if any(
            t.player(uid) and not t.player(uid).get("left")
            for t in self.tables.values()
        ):
            raise DurakError("Сначала покиньте другой стол дурака", 409)

    def refresh(self, table, db):
        changed = False
        for p in table.players:
            user = db.get(User, p["id"])
            if user:
                name = user.display_name or user.guild_nickname or user.username
                if name != p["name"]:
                    p["name"], changed = name, True
        if changed:
            table.touch()

    def settle(self, table, db):
        match = table.match
        if not match or not match["result"] or match["settled"]:
            return
        result = match["result"]
        changes = []
        for p in match["participants"]:
            uid = p["id"]
            received = result["payouts"].get(str(uid), 0)
            user = db.get(User, uid)
            if received and not user:
                raise DurakError(
                    "Для расчёта партии требуется восстановить участника", 409
                )
            if user and cents(user.balance) + received > MAX_CENTS:
                raise DurakError(
                    "Баланс победителя достиг лимита. Администратор должен освободить место для выплаты",
                    409,
                )
            changes.append((p, user, received))
        for p, user, received in changes:
            if user:
                user.balance = (cents(user.balance) + received) / 100
            db.add(
                GameHistory(
                    user_id=p["id"],
                    game_type="durak",
                    result="push"
                    if result["draw"]
                    else "lost"
                    if p["id"] == result["loser"]
                    else "won",
                    amount=(received - match["entry_cents"]) / 100,
                    details=f"Дурак, стол {table.id}, партия #{match['number']}; взнос {match['entry_cents'] / 100:.2f} БК, получено {received / 100:.2f} БК; {result['reason']}",
                )
            )
        match["settled"] = True

    def start(self, table, db):
        entry = table.rules["entry_cents"]
        for p in table.players:
            user = db.get(User, p["id"])
            if not user or user.deleted_at:
                raise DurakError("Один из игроков больше не имеет доступа к сайту", 409)
            if cents(user.balance) < entry:
                raise DurakError(p["name"] + ": недостаточно бишопкоинов", 409)
            if cents(user.balance) + entry * (len(table.players) - 1) > MAX_CENTS:
                raise DurakError(
                    p["name"] + ": недостаточно места на балансе для возможной выплаты",
                    409,
                )
        table.start()
        for p in table.players:
            user = db.get(User, p["id"])
            user.balance = (cents(user.balance) - entry) / 100

    def state(self, table, user, db):
        table.tick()
        self.settle(table, db)
        self.refresh(table, db)
        data = table.public_state(user.id)
        data["your_id"], data["balance"] = user.id, user.balance
        return data

    def detach(self, uid, db):
        for tid, table in list(self.tables.items()):
            if table.player(uid):
                table.leave(uid)
                self.settle(table, db)
            if not table.players or all(p.get("left") for p in table.players):
                self.tables.pop(tid, None)

    def tick_table(self, tid, db):
        now = time.time()
        table = self.tables.get(tid)
        if table:
            table.tick(now)
            self.settle(table, db)
            if table.phase in ("waiting", "finished") and now - table.updated_at > (
                86400 if table.phase == "waiting" else 7200
            ):
                self.tables.pop(tid, None)

    def restore(self, values, pause=0):
        self.tables.clear()
        for tid, value in values.items():
            table = DurakTable.from_dict(value)
            if pause and table.deadline:
                table.deadline += pause
            self.tables[tid] = table


async def body(request, allowed):
    try:
        value = await request.json()
    except (ValueError, UnicodeError):
        raise DurakError("Ожидается JSON с настройками или действием")
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise DurakError("Некорректные поля запроса")
    return value


def require(user):
    if not user:
        raise DurakError("Войдите в свой профиль", 401)


def revision(table, value):
    if type(value.get("revision")) is not int or value["revision"] != table.revision:
        raise DurakError(
            "Состояние стола изменилось. Обновляем данные; повторите действие", 409
        )


def build_router(service, get_user, get_db, templates, may_manage=lambda user: False):
    router = APIRouter()

    @router.get("/games/durak")
    @router.get("/games/durak/lobby")
    async def lobby(request: Request, user=Depends(get_user)):
        from fastapi.responses import RedirectResponse

        if not user:
            return RedirectResponse("/login")
        return templates.TemplateResponse(
            request=request, name="durak_lobby.html", context={"user": user}
        )

    @router.get("/games/durak/table/{tid}")
    async def page(request: Request, tid: str, user=Depends(get_user)):
        from fastapi.responses import RedirectResponse

        if not user:
            return RedirectResponse("/login")
        service.table(tid)
        return templates.TemplateResponse(
            request=request,
            name="durak_table.html",
            context={"user": user, "table_id": tid},
        )

    @router.get("/api/durak/tables")
    async def tables(user=Depends(get_user)):
        require(user)
        visible = [
            t
            for t in service.tables.values()
            if may_manage(user)
            or not t.rules["private"]
            or t.player(user.id)
            and not t.player(user.id).get("left")
        ]
        return {
            "tables": [
                {
                    "id": t.id,
                    **management("durak", t, may_manage(user)),
                    "creator": next(
                        (p["name"] for p in t.players if p["id"] == t.creator),
                        "Создатель",
                    ),
                    "players": len(t.players),
                    "phase": t.phase,
                    "rules": t.rules,
                    "mine": bool(
                        t.player(user.id) and not t.player(user.id).get("left")
                    ),
                }
                for t in visible
            ]
        }

    @router.post("/api/durak/tables")
    async def create(request: Request, user=Depends(get_user)):
        require(user)
        data = await body(request, ["rules"])
        service.ensure_one_table(user.id)
        tid = secrets.token_urlsafe(18)
        table = DurakTable(
            tid,
            user.id,
            user.display_name or user.guild_nickname or user.username,
            data.get("rules", {}),
        )
        service.tables[tid] = table
        return {"ok": True, "table_id": tid}

    @router.get("/api/durak/table/{tid}/preview")
    async def preview(tid: str, user=Depends(get_user)):
        require(user)
        table = service.table(tid)
        return {
            "rules": table.rules,
            "players": len(table.players),
            "phase": table.phase,
            "in_table": bool(
                table.player(user.id) and not table.player(user.id).get("left")
            ),
            "creator": next(
                (p["name"] for p in table.players if p["id"] == table.creator),
                "Создатель",
            ),
        }

    @router.post("/api/durak/table/{tid}/join")
    async def join(tid: str, user=Depends(get_user)):
        require(user)
        table = service.table(tid)
        if not table.player(user.id):
            service.ensure_one_table(user.id)
            table.add_player(
                user.id, user.display_name or user.guild_nickname or user.username
            )
        elif table.player(user.id).get("left"):
            raise DurakError("Дождитесь окончания партии, чтобы вернуться", 409)
        return {"ok": True}

    @router.get("/api/durak/table/{tid}/state")
    async def state(tid: str, user=Depends(get_user), db=Depends(get_db)):
        require(user)
        return service.state(service.member(tid, user.id), user, db)

    @router.post("/api/durak/table/{tid}/ready")
    async def ready(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "ready"])
        revision(table, data)
        if table.phase != "waiting" or type(data.get("ready")) is not bool:
            raise DurakError("Готовность можно менять только до начала партии", 409)
        if data["ready"] and cents(user.balance) < table.rules["entry_cents"]:
            raise DurakError("Недостаточно бишопкоинов для взноса")
        table.player(user.id)["ready"] = data["ready"]
        table.touch()
        return service.state(table, user, db)

    @router.post("/api/durak/table/{tid}/settings")
    async def rules(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "rules"])
        revision(table, data)
        if table.creator != user.id:
            raise DurakError("Настройки меняет создатель стола", 403)
        table.change_settings(data.get("rules"))
        return service.state(table, user, db)

    @router.post("/api/durak/table/{tid}/start")
    async def start(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise DurakError("Партию запускает создатель стола", 403)
        service.start(table, db)
        return service.state(table, user, db)

    @router.post("/api/durak/table/{tid}/action")
    async def action(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision", "kind", "card", "target"])
        revision(table, data)
        if table.phase not in ("waiting", "finished") and time.time() >= table.deadline:
            raise DurakError("Время хода истекло. Обновите состояние стола", 409)
        table.action(user.id, data.get("kind"), data.get("card"), data.get("target"))
        service.settle(table, db)
        return service.state(table, user, db)

    @router.post("/api/durak/table/{tid}/leave")
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

    @router.post("/api/durak/table/{tid}/rematch")
    async def rematch(
        tid: str, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        require(user)
        table = service.member(tid, user.id)
        data = await body(request, ["revision"])
        revision(table, data)
        if table.creator != user.id:
            raise DurakError("Новую партию создаёт создатель стола", 403)
        table.rematch()
        return service.state(table, user, db)

    return router
