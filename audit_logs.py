"""Shared event presentation and filtering; stored audit records stay unchanged."""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import or_, and_, func
from database import GameHistory, User

MOSCOW = timezone(timedelta(hours=3))
GAMES = ("blackjack", "roulette", "poker", "crash", "durak", "billiards", "domino", "battleship", "chess", "draughts", "backgammon", "monopoly", "go", "horse_racing")
EVENTS = (
    "name_change",
    "role_change",
    "user_deleted",
    "user_archived",
    "user_restored",
    "note_updated",
    "note_cleared",
    "steam_set",
    "steam_cleared",
    "bug_report",
    "login_link_used",
    "login_link_issued",
    "login_link_revoked",
    "medal_awarded",
    "medal_revoked",
)
LABELS = {
    "won": "Победа",
    "lost": "Проигрыш",
    "push": "Ничья",
    "cancelled": "Игра отменена",
    "bonus": "Ежедневный бонус",
    "admin_adjustment": "Изменение баланса",
    "name_change": "Смена ника",
    "role_change": "Изменение прав",
    "user_deleted": "Удаление аккаунта",
    "user_archived": "Профиль в архиве",
    "user_restored": "Восстановление профиля",
    "note_updated": "Заметка обновлена",
    "note_cleared": "Заметка удалена",
    "steam_set": "Steam ID привязан",
    "steam_cleared": "Steam ID отвязан",
    "bug_report": "Сообщение об ошибке",
    "login_link_used": "Вход по ссылке",
    "login_link_issued": "Ссылка входа выдана",
    "login_link_revoked": "Ссылка входа отозвана",
    "medal_awarded": "Награда получена",
    "medal_revoked": "Награда снята",
    "award_reward": "Начисление за награду",
    "assembly_penalty": "Штраф за сбор",
    "assembly_refund": "Возврат за сбор",
}
TYPES = {
    "blackjack": "Блэкджек",
    "roulette": "Рулетка",
    "poker": "Покер",
    "crash": "Crash",
    "durak": "Дурак",
    "billiards": "Бильярд",
    "domino": "Домино",
    "battleship": "Морской бой",
    "chess": "Шахматы",
    "draughts": "Русские шашки",
    "backgammon": "Нарды",
    "monopoly": "Монополия",
    "go": "Го",
    "horse_racing": "Скачки",
    "admin": "Управление",
    "daily": "Бонус",
    "medals": "Награды",
}


def is_money_log(row):
    if row.result in EVENTS:
        return False
    return (
        row.game_type in (*GAMES, "daily")
        or row.result == "admin_adjustment"
        or row.amount != 0
    )


def money_expression():
    return and_(
        ~GameHistory.result.in_(EVENTS),
        or_(
            GameHistory.game_type.in_((*GAMES, "daily")),
            GameHistory.result == "admin_adjustment",
            GameHistory.amount != 0,
        ),
    )


def log_details(row, show_private=False):
    if row.result in ("note_updated", "note_cleared") and not show_private:
        return LABELS[row.result]
    return row.details or ""


def present(row, show_private=False):
    stamp = row.created_at
    if stamp:
        stamp = (
            stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        ).astimezone(MOSCOW)
    return {
        "id": row.id,
        "user_id": row.user_id,
        "label": LABELS.get(row.result, row.result),
        "kind": TYPES.get(row.game_type, row.game_type),
        "details": log_details(row, show_private),
        "financial": is_money_log(row),
        "amount": row.amount,
        "amount_label": "Итог игры" if row.game_type in GAMES else "Баланс",
        "time": stamp.strftime("%H:%M:%S") if stamp else "—",
        "date": stamp.strftime("%d.%m.%Y") if stamp else "Дата неизвестна",
    }


def page_context(
    db,
    *,
    uid=None,
    q="",
    game="",
    outcome="",
    date_from="",
    date_to="",
    before=None,
    player_id=None,
    show_private=False,
):
    target = db.get(User, uid) if uid is not None else None
    if uid is not None and target is None:
        raise HTTPException(404)
    query = db.query(GameHistory).outerjoin(User, User.id == GameHistory.user_id)
    if uid is not None:
        query = query.filter(GameHistory.user_id == uid)
    elif player_id is not None:
        query = query.filter(GameHistory.user_id == player_id)
    q = q.strip()[:200]
    if q:
        detail_match = GameHistory.details.contains(q, autoescape=True)
        if not show_private:
            detail_match = and_(
                ~GameHistory.result.in_(("note_updated", "note_cleared")), detail_match
            )
        query = query.filter(
            or_(
                detail_match,
                GameHistory.result.contains(q, autoescape=True),
                User.username.contains(q, autoescape=True),
                User.display_name.contains(q, autoescape=True),
                User.discord_id.contains(q, autoescape=True),
                User.steam_id.contains(q, autoescape=True),
            )
        )
    if game:
        query = query.filter(GameHistory.game_type == game)
    financial = money_expression()
    if outcome == "positive":
        query = query.filter(financial, GameHistory.amount > 0)
    elif outcome == "negative":
        query = query.filter(financial, GameHistory.amount < 0)
    elif outcome == "zero":
        query = query.filter(financial, GameHistory.amount == 0)
    elif outcome == "events":
        query = query.filter(~financial)
    try:
        if date_from:
            query = query.filter(
                GameHistory.created_at
                >= datetime.strptime(date_from, "%Y-%m-%d") - timedelta(hours=3)
            )
        if date_to:
            query = query.filter(
                GameHistory.created_at
                < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1, hours=-3)
            )
        if date_from and date_to and date_from > date_to:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "Неверная дата")
    total = query.count()
    money = query.filter(financial)
    summary = {
        "financial_count": money.count(),
        "positive": money.filter(GameHistory.amount > 0)
        .with_entities(func.coalesce(func.sum(GameHistory.amount), 0))
        .scalar(),
        "negative": money.filter(GameHistory.amount < 0)
        .with_entities(func.coalesce(func.sum(GameHistory.amount), 0))
        .scalar(),
    }
    if before is not None:
        query = query.filter(GameHistory.id < before)
    rows = query.order_by(GameHistory.id.desc()).limit(101).all()
    history = rows[:100]
    people = {
        u.id: u
        for u in db.query(User).filter(User.id.in_({h.user_id for h in history})).all()
    }
    entries = []
    for h in history:
        user = people.get(h.user_id)
        entries.append(
            {
                **present(h, show_private),
                "user_name": (user.display_name or user.username)
                if user
                else f"Удалённый игрок #{h.user_id}",
                "user_exists": user is not None,
            }
        )
    filters = dict(
        q=q, game=game, outcome=outcome, date_from=date_from, date_to=date_to
    )
    if uid is None:
        filters["player_id"] = player_id or ""
    base = f"/admin/logs/{uid}" if uid is not None else "/admin/logs"
    return {
        "target_user": target,
        "history": history,
        "entries": entries,
        "summary": summary,
        "filters": filters,
        "total_records": total,
        "base_url": base,
        "next_url": base + "?" + urlencode({**filters, "before": history[-1].id})
        if len(rows) > 100
        else None,
        "reset_url": base + "?" + urlencode(filters),
        "log_players": db.query(User).order_by(User.display_name, User.id).all()
        if uid is None
        else [],
    }
