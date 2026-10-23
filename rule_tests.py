"""Assigned rule tests. Snapshots and answers never appear in player summaries."""

import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import Column, DateTime, Integer, String, Text

from database import Base, User
from server_rules_questions import list_server_rule_questions, utcnow


class RuleTest(Base):
    __tablename__ = "rule_tests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    player_name = Column(String(100), nullable=False)
    assigned_by = Column(String(20), nullable=False)
    assigned_by_name = Column(String(100), nullable=False)
    assigned_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    questions_json = Column(Text, nullable=False)
    answers_json = Column(Text, nullable=False, default="[]")
    score = Column(Integer, nullable=True)


def can_manage_tests(user, is_senior_or_super):
    return bool(user and user.is_admin and is_senior_or_super(user))


def summary(row):
    questions = json.loads(row.questions_json)
    answers = json.loads(row.answers_json)
    return {
        "id": row.id,
        "user_id": row.user_id,
        "player_name": row.player_name,
        "assigned_by": row.assigned_by_name,
        "assigned_at": row.assigned_at.isoformat() + "Z",
        "completed_at": row.completed_at.isoformat() + "Z"
        if row.completed_at
        else None,
        "total": len(questions),
        "answered": len(answers),
        "score": row.score,
        "percent": round(row.score * 100 / len(questions), 1)
        if row.score is not None
        else None,
    }


def player_summaries(db, uid):
    return [
        summary(row)
        for row in db.query(RuleTest)
        .filter(RuleTest.user_id == uid)
        .order_by(RuleTest.id.desc())
        .all()
    ]


def feedback(question, answer):
    return {
        "text": question["text"],
        "options": question["options"],
        "selected": answer["selected"],
        "correct": question["correct"],
        "is_correct": answer["is_correct"],
        "explanation": question["explanation"],
        "source_quote": question.get("source_quote", ""),
        "rule": question["rule"],
    }


def player_state(row):
    result = summary(row)
    # A finished test has no question content, answers or explanations for players.
    if row.completed_at:
        return result
    questions = json.loads(row.questions_json)
    answers = json.loads(row.answers_json)
    if len(answers) < len(questions):
        question = questions[len(answers)]
        result["question"] = {
            "position": len(answers),
            "text": question["text"],
            "options": question["options"],
        }
    if answers:
        result["feedback"] = feedback(questions[len(answers) - 1], answers[-1])
    result["ready_to_finish"] = len(answers) == len(questions)
    return result


def make_router(get_user, get_db, is_senior_or_super, templates, read_json_object):
    router = APIRouter()

    def manager(user):
        if not can_manage_tests(user, is_senior_or_super):
            raise HTTPException(
                403, "Доступ только для старшего или главного администратора"
            )

    def owned(db, test_id, user):
        if not user:
            raise HTTPException(401, "Войдите на сайт")
        row = db.get(RuleTest, test_id)
        if not row or row.user_id != user.id:
            raise HTTPException(404, "Тест не найден")
        return row

    def private(data):
        return JSONResponse(data, headers={"Cache-Control": "private, no-store"})

    def question_pool(db, count):
        questions = list_server_rule_questions(db)
        if type(count) is not int or not 1 <= count <= len(questions):
            raise HTTPException(400, f"Количество вопросов: от 1 до {len(questions)}")
        return questions

    def create_assignment(db, actor, target, questions, count):
        row = RuleTest(
            user_id=target.id,
            player_name=target.display_name or target.username,
            assigned_by=actor.discord_id,
            assigned_by_name=actor.display_name or actor.username,
            assigned_at=utcnow(),
            questions_json=json.dumps(
                secrets.SystemRandom().sample(questions, count), ensure_ascii=False
            ),
            answers_json="[]",
        )
        db.add(row)
        db.flush()
        return summary(row)

    @router.post("/api/admin/rule-tests/batch")
    async def assign_batch(
        request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        manager(user)
        body = await read_json_object(request)
        ids = body.get("user_ids")
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 1000
            or any(type(uid) is not int or uid <= 0 for uid in ids)
        ):
            raise HTTPException(400, "Выберите от 1 до 1000 игроков")
        ids = list(dict.fromkeys(ids))
        count = body.get("count")
        questions = question_pool(db, count)
        targets = {
            row.id: row
            for row in db.query(User)
            .filter(User.id.in_(ids), User.deleted_at.is_(None))
            .all()
        }
        if len(targets) != len(ids):
            raise HTTPException(
                404,
                "Некоторые игроки не найдены или находятся в архиве. Обновите список",
            )
        active = {
            uid
            for (uid,) in db.query(RuleTest.user_id)
            .filter(RuleTest.user_id.in_(ids), RuleTest.completed_at.is_(None))
            .all()
        }
        assigned, skipped = [], []
        for uid in ids:
            target = targets[uid]
            if uid in active:
                skipped.append(
                    {
                        "user_id": uid,
                        "name": target.display_name or target.username,
                        "reason": "Уже есть незавершённый тест",
                    }
                )
            else:
                assigned.append(create_assignment(db, user, target, questions, count))
        return private({"assigned": assigned, "skipped": skipped})

    @router.post("/api/admin/rule-tests")
    async def assign(request: Request, user=Depends(get_user), db=Depends(get_db)):
        manager(user)
        body = await read_json_object(request)
        uid, count = body.get("user_id"), body.get("count")
        if type(uid) is not int or type(count) is not int:
            raise HTTPException(400, "Укажите игрока и целое число вопросов")
        target = (
            db.query(User).filter(User.id == uid, User.deleted_at.is_(None)).first()
        )
        if not target:
            raise HTTPException(404, "Игрок не найден")
        questions = question_pool(db, count)
        if (
            db.query(RuleTest.id)
            .filter(RuleTest.user_id == uid, RuleTest.completed_at.is_(None))
            .first()
        ):
            raise HTTPException(409, "У игрока уже есть незавершённый тест")
        return private(create_assignment(db, user, target, questions, count))

    @router.get("/api/admin/rule-tests")
    async def admin_list(
        user_id: int | None = None, user=Depends(get_user), db=Depends(get_db)
    ):
        manager(user)
        query = db.query(RuleTest)
        if user_id is not None:
            query = query.filter(RuleTest.user_id == user_id)
        return private(
            {
                "tests": [
                    summary(row) for row in query.order_by(RuleTest.id.desc()).all()
                ]
            }
        )

    @router.get("/api/admin/rule-tests/{test_id}")
    async def review(test_id: int, user=Depends(get_user), db=Depends(get_db)):
        manager(user)
        row = db.get(RuleTest, test_id)
        if not row:
            raise HTTPException(404, "Тест не найден")
        questions, answers = (
            json.loads(row.questions_json),
            json.loads(row.answers_json),
        )
        result = summary(row)
        result["answers"] = [
            dict(feedback(question, answer), position=index + 1)
            for index, (question, answer) in enumerate(zip(questions, answers))
        ]
        return private(result)

    @router.get("/rule-tests", response_class=HTMLResponse)
    async def page(request: Request, user=Depends(get_user), db=Depends(get_db)):
        if not user:
            raise HTTPException(401, "Войдите на сайт")
        return templates.TemplateResponse(
            request=request,
            name="rule_tests.html",
            context={"user": user},
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/api/rule-tests")
    async def my_tests(user=Depends(get_user), db=Depends(get_db)):
        if not user:
            raise HTTPException(401, "Войдите на сайт")
        return private({"tests": player_summaries(db, user.id)})

    @router.get("/api/rule-tests/{test_id}")
    async def state(test_id: int, user=Depends(get_user), db=Depends(get_db)):
        return private(player_state(owned(db, test_id, user)))

    @router.post("/api/rule-tests/{test_id}/answer")
    async def answer(
        test_id: int, request: Request, user=Depends(get_user), db=Depends(get_db)
    ):
        row = owned(db, test_id, user)
        if row.completed_at:
            raise HTTPException(409, "Тест уже завершён")
        body = await read_json_object(request)
        questions, answers = (
            json.loads(row.questions_json),
            json.loads(row.answers_json),
        )
        position = body.get("position")
        if (
            type(position) is not int
            or position != len(answers)
            or position >= len(questions)
        ):
            raise HTTPException(
                409, "Ответ уже сохранён или вопрос изменился. Обновите тест"
            )
        selected = body.get("selected")
        question = questions[position]
        if (
            not isinstance(selected, list)
            or not selected
            or len(selected) > len(question["options"])
            or any(
                type(i) is not int or not 0 <= i < len(question["options"])
                for i in selected
            )
            or len(set(selected)) != len(selected)
        ):
            raise HTTPException(
                400, "Выберите один или несколько разных вариантов ответа"
            )
        selected = sorted(selected)
        answers.append(
            {
                "selected": selected,
                "is_correct": selected == sorted(question["correct"]),
                "answered_at": utcnow().isoformat() + "Z",
            }
        )
        row.answers_json = json.dumps(answers)
        db.flush()
        return private(player_state(row))

    @router.post("/api/rule-tests/{test_id}/finish")
    async def finish(test_id: int, user=Depends(get_user), db=Depends(get_db)):
        row = owned(db, test_id, user)
        if not row.completed_at:
            answers = json.loads(row.answers_json)
            if len(answers) != len(json.loads(row.questions_json)):
                raise HTTPException(409, "Сначала ответьте на все вопросы")
            row.score = sum(answer["is_correct"] for answer in answers)
            row.completed_at = utcnow()
            db.flush()
        return private(summary(row))

    return router
