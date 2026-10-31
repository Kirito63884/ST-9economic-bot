"""Only idle tables may be removed; use the same wallet/runtime transaction."""

import hashlib
import json
from fastapi import APIRouter, Depends, HTTPException, Request


def removable(game, table):
    if game == "poker":
        return (
            table.phase == "waiting"
            and abs(table.pot) < 0.001
            and all(abs(p.get("invested", 0)) < 0.001 for p in table.players)
        )
    return table.phase == "waiting" or (
        table.phase == "finished" and bool(table.match and table.match["settled"])
    )


def version(game, table):
    values = [game, table.id, table.phase]
    if game == "poker":
        values += [
            table.hand_number,
            table.action_revision,
            [p["user_id"] for p in table.players],
        ]
    else:
        values += [table.match_number, table.revision, [p["id"] for p in table.players]]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:24]


def management(game, table, allowed, uid=None):
    creator = table.creator_id if game == "poker" else table.creator
    may_close = bool(allowed or (uid is not None and uid == creator))
    return {
        "can_manage": bool(allowed),
        "can_delete": bool(allowed and removable(game, table)),
        "delete_version": version(game, table) if allowed else None,
        "can_close_table": may_close,
        "can_close": bool(may_close and removable(game, table)),
        "close_version": version(game, table) if may_close else None,
    }


def make_router(
    get_user,
    get_db,
    may_manage,
    read_body,
    poker,
    requests,
    durak,
    billiards=None,
    log_game=None,
    domino=None,
    battleship=None,
    chess=None,
    draughts=None,
):
    # Older main.py versions pass log_game in the billiards position. Keeping this
    # compatible makes an emergency source rollback safe after this module exists.
    if log_game is None:
        log_game, billiards = billiards, None
    router = APIRouter()

    @router.post("/api/game-tables/{game}/{tid}/close")
    async def close_table(
        game: str,
        tid: str,
        request: Request,
        user=Depends(get_user),
        db=Depends(get_db),
    ):
        if not user:
            raise HTTPException(401, "Войдите в профиль")
        games = {"poker": poker, "durak": durak.tables}
        if billiards is not None:
            games["billiards"] = billiards.tables
        if domino is not None:
            games["domino"] = domino.tables
        if battleship is not None:
            games["battleship"] = battleship.tables
        if chess is not None:
            games["chess"] = chess.tables
        if draughts is not None:
            games["draughts"] = draughts.tables
        if game not in games:
            raise HTTPException(400, "Неизвестная игра")
        table = games[game].get(tid)
        if table is None:
            return {"ok": True, "deleted": False, "message": "Стол уже расформирован"}
        rights = management(
            game, table, bool(user.is_admin and may_manage(user)), user.id
        )
        if not rights["can_close_table"]:
            raise HTTPException(
                403, "Расформировать стол может его создатель или главный администратор"
            )
        if not rights["can_close"]:
            raise HTTPException(
                409,
                "Сначала завершите партию. Для выхода во время игры используйте сдачу",
            )
        data = await read_body(request)
        if data.get("version") != rights["close_version"]:
            raise HTTPException(
                409,
                "Состояние стола изменилось. Обновите его и подтвердите расформирование снова",
            )
        games[game].pop(tid)
        if game == "poker":
            requests.pop(tid, None)
        log_game(
            db, user.id, "admin", "table_closed", 0, f"Расформирован стол {game}: {tid}"
        )
        return {
            "ok": True,
            "deleted": True,
            "message": "Стол расформирован. Балансы сохранены",
        }

    def require_chief(user=Depends(get_user)):
        if not user or not user.is_admin or not may_manage(user):
            raise HTTPException(
                403, "Удалять столы могут только главные администраторы."
            )
        return user

    @router.get("/api/admin/game-tables")
    async def list_tables(admin=Depends(require_chief)):
        rows = []
        games = [("poker", poker), ("durak", durak.tables)]
        if billiards is not None:
            games.append(("billiards", billiards.tables))
        if domino is not None:
            games.append(("domino", domino.tables))
        if battleship is not None:
            games.append(("battleship", battleship.tables))
        if chess is not None:
            games.append(("chess", chess.tables))
        if draughts is not None:
            games.append(("draughts", draughts.tables))
        for game, values in games:
            for tid, table in values.items():
                if game == "poker":
                    creator = next(
                        (
                            p["display_name"]
                            for p in table.players
                            if p["user_id"] == table.creator_id
                        ),
                        "Создатель",
                    )
                    private = table.is_private
                else:
                    creator = next(
                        (p["name"] for p in table.players if p["id"] == table.creator),
                        "Создатель",
                    )
                    private = table.rules["private"]
                rows.append(
                    {
                        "id": tid,
                        "game": game,
                        "creator": creator,
                        "phase": table.phase,
                        "players": len(table.players),
                        "private": private,
                        **management(game, table, True),
                    }
                )
        return {"tables": rows}

    @router.post("/api/admin/game-tables/{game}/{tid}/delete")
    async def remove(
        game: str,
        tid: str,
        request: Request,
        admin=Depends(require_chief),
        db=Depends(get_db),
    ):
        games = {"poker": poker, "durak": durak.tables}
        if billiards is not None:
            games["billiards"] = billiards.tables
        if domino is not None:
            games["domino"] = domino.tables
        if battleship is not None:
            games["battleship"] = battleship.tables
        if chess is not None:
            games["chess"] = chess.tables
        if draughts is not None:
            games["draughts"] = draughts.tables
        if game not in games:
            raise HTTPException(400, "Неизвестная игра")
        values = games[game]
        table = values.get(tid)
        if table is None:
            return {"ok": True, "deleted": False, "message": "Стол уже удалён."}
        if not removable(game, table):
            raise HTTPException(
                409, "Идёт активная игра. Дождитесь её завершения; стол не удалён."
            )
        body = await read_body(request)
        if body.get("version") != version(game, table):
            raise HTTPException(
                409,
                "Состояние стола изменилось. Обновите список и подтвердите удаление снова.",
            )
        del values[tid]
        if game == "poker":
            requests.pop(tid, None)
        log_game(
            db,
            admin.id,
            "admin",
            "table_deleted",
            0,
            f"Удалён неактивный стол {game}: {tid}; участников {len(table.players)}",
        )
        return {
            "ok": True,
            "deleted": True,
            "message": "Стол удалён. Балансы участников сохранены.",
        }

    return router
