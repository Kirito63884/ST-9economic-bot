"""Private, paginated Bishopcoin history and questions tied to a real operation."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    or_,
)

from audit_logs import GAMES, money_expression, present
from database import Base, GameHistory, User


class WalletQuestion(Base):
    __tablename__ = "wallet_questions"
    __table_args__ = (UniqueConstraint("operation_id", "user_id"),)
    id = Column(Integer, primary_key=True)
    operation_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    reason = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="pending", index=True)
    created_at = Column(DateTime, nullable=False)
    answer = Column(Text, nullable=True)
    answered_at = Column(DateTime, nullable=True)
    answered_by = Column(String(20), nullable=True)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def stamp(moment):
    return moment.isoformat() + "Z" if moment else None


def categories():
    return {
        "awards": or_(
            GameHistory.game_type == "medals", GameHistory.result == "award_reward"
        ),
        "assemblies": or_(
            GameHistory.result.in_(
                ("assembly_penalty", "assembly_refund", "assembly_appeal_refund")
            ),
            func.coalesce(GameHistory.details, "").startswith(
                "Возврат штрафа за сбор ", autoescape=True
            ),
        ),
        "games": GameHistory.game_type.in_(GAMES),
        "bonus": GameHistory.game_type == "daily",
    }


def history_page(
    db, user_id, category="", direction="", date_from="", date_to="", q="", before=None
):
    filters = categories()
    if category not in ("", "admin", *filters) or direction not in (
        "",
        "positive",
        "negative",
    ):
        raise HTTPException(400, "Неизвестный фильтр истории.")
    query = db.query(GameHistory).filter(
        GameHistory.user_id == user_id, money_expression()
    )
    if category == "admin":
        query = query.filter(~or_(*filters.values()))
    elif category:
        query = query.filter(filters[category])
    if direction:
        query = query.filter(
            GameHistory.amount > 0
            if direction == "positive"
            else GameHistory.amount < 0
        )
    if len(q) > 200:
        raise HTTPException(400, "Поисковая строка слишком длинная.")
    if q.strip():
        query = query.filter(GameHistory.details.contains(q.strip(), autoescape=True))
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
            raise ValueError
    except (ValueError, OverflowError):
        raise HTTPException(400, "Проверьте даты периода.")
    summary = {
        "count": query.count(),
        "credited": query.filter(GameHistory.amount > 0)
        .with_entities(func.coalesce(func.sum(GameHistory.amount), 0))
        .scalar(),
        "debited": query.filter(GameHistory.amount < 0)
        .with_entities(func.coalesce(func.sum(GameHistory.amount), 0))
        .scalar(),
    }
    if before is not None:
        if not 0 < before < 2**63:
            raise HTTPException(400, "Неверная страница.")
        query = query.filter(GameHistory.id < before)
    rows = query.order_by(GameHistory.id.desc()).limit(51).all()
    questions = {
        r.operation_id: r
        for r in db.query(WalletQuestion)
        .filter(
            WalletQuestion.user_id == user_id,
            WalletQuestion.operation_id.in_([r.id for r in rows]),
        )
        .all()
    }
    return {
        "summary": summary,
        "entries": [
            {
                **present(row, show_private=False),
                "created_at": stamp(row.created_at),
                "question": question_data(questions[row.id])
                if row.id in questions
                else None,
            }
            for row in rows[:50]
        ],
        "next_before": rows[49].id if len(rows) > 50 else None,
    }


def question_data(row):
    return {
        "id": row.id,
        "operation_id": row.operation_id,
        "user_id": row.user_id,
        "reason": row.reason,
        "status": row.status,
        "created_at": stamp(row.created_at),
        "answer": row.answer,
        "answered_at": stamp(row.answered_at),
    }


async def body_text(request, key, minimum=3):
    try:
        data = await request.json()
        value = data.get(key)
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= 2000:
            raise ValueError
        return data, value.strip()
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Введите текст от 3 до 2000 символов.")


def make_router(get_user, get_db, templates):
    router = APIRouter()

    def member(user=Depends(get_user)):
        if not user or user.deleted_at is not None:
            raise HTTPException(401, "Войдите на сайт.")
        return user

    def admin(user=Depends(member)):
        if not user.is_admin:
            raise HTTPException(403, "Доступ только администраторам.")
        return user

    @router.get("/wallet", response_class=HTMLResponse)
    async def page(request: Request, user=Depends(member)):
        return templates.TemplateResponse(
            request=request,
            name="wallet.html",
            context={"user": user},
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/api/wallet/history")
    async def history(
        response: Response,
        category: str = "",
        direction: str = "",
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        before: int | None = None,
        user=Depends(member),
        db=Depends(get_db),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        return {
            **history_page(
                db, user.id, category, direction, date_from, date_to, q, before
            ),
            "balance": user.balance,
        }

    @router.post("/api/wallet/questions")
    async def ask(request: Request, user=Depends(member), db=Depends(get_db)):
        data, reason = await body_text(request, "reason")
        operation_id = data.get("operation_id")
        if type(operation_id) is not int or not 0 < operation_id < 2**63:
            raise HTTPException(400, "Укажите операцию.")
        operation = (
            db.query(GameHistory)
            .filter(
                GameHistory.id == operation_id,
                GameHistory.user_id == user.id,
                money_expression(),
            )
            .first()
        )
        if not operation:
            raise HTTPException(404, "Операция не найдена.")
        existing = (
            db.query(WalletQuestion)
            .filter_by(operation_id=operation_id, user_id=user.id)
            .first()
        )
        if existing:
            return question_data(existing)
        row = WalletQuestion(
            operation_id=operation_id,
            user_id=user.id,
            reason=reason,
            status="pending",
            created_at=utcnow(),
        )
        db.add(row)
        db.flush()
        return question_data(row)

    @router.get("/api/admin/wallet/questions")
    async def questions(
        response: Response,
        status: str = "pending",
        user=Depends(admin),
        db=Depends(get_db),
    ):
        if status not in ("pending", "answered"):
            raise HTTPException(400, "Неизвестный статус.")
        response.headers["Cache-Control"] = "private, no-store"
        rows = (
            db.query(WalletQuestion)
            .filter_by(status=status)
            .order_by(WalletQuestion.id.desc())
            .limit(200)
            .all()
        )
        result = []
        for row in rows:
            player = db.get(User, row.user_id)
            operation = db.get(GameHistory, row.operation_id)
            result.append(
                {
                    **question_data(row),
                    "name": (player.display_name or player.username)
                    if player
                    else f"Игрок #{row.user_id}",
                    "operation": present(operation, False) if operation else None,
                }
            )
        return {"items": result}

    @router.post("/api/admin/wallet/questions/{question_id}/answer")
    async def answer(
        question_id: int, request: Request, user=Depends(admin), db=Depends(get_db)
    ):
        _, text = await body_text(request, "answer")
        if not 0 < question_id < 2**63:
            raise HTTPException(404, "Обращение не найдено.")
        row = db.get(WalletQuestion, question_id)
        if row is None:
            raise HTTPException(404, "Обращение не найдено.")
        if row.status != "pending":
            raise HTTPException(409, "На обращение уже ответили.")
        row.answer, row.answered_at, row.answered_by, row.status = (
            text,
            utcnow(),
            user.discord_id,
            "answered",
        )
        from admin_hub import AdminActionLog

        db.add(
            AdminActionLog(
                actor_id=user.discord_id,
                actor_name=user.display_name or user.username,
                action="wallet_question_answer",
                target_user_id=row.user_id,
                details=f"Ответ по операции #{row.operation_id}; обращение #{row.id}",
                created_at=utcnow(),
            )
        )
        return question_data(row)

    return router
