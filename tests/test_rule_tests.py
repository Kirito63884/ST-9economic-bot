"""Permissions, answer secrecy, immutable results and transaction safety."""

import json
import asyncio
import httpx

import pytest
from fastapi.testclient import TestClient

import main
from database import SessionLocal
from rule_tests import RuleTest
from server_rules_questions import ServerRuleQuestion
from conftest import session_cookie


@pytest.fixture(autouse=True)
def clean_rule_tests():
    main.request_guard.reset()
    with SessionLocal() as db:
        db.query(RuleTest).delete()
        db.commit()


def account(uid):
    client = TestClient(main.app)
    client.cookies.set("session", session_cookie(uid))
    return client


def assign(admin, uid=102, count=3):
    response = admin.post(
        "/api/admin/rule-tests", json={"user_id": uid, "count": count}
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def snapshots(test_id):
    with SessionLocal() as db:
        return json.loads(db.get(RuleTest, test_id).questions_json)


def complete(player, test_id, mistakes=False):
    for i, q in enumerate(snapshots(test_id)):
        selected = (
            [j for j in range(len(q["options"])) if j not in q["correct"]]
            if mistakes
            else q["correct"]
        )
        if not selected:
            selected = q["correct"][:1]
        r = player.post(
            f"/api/rule-tests/{test_id}/answer",
            json={"position": i, "selected": selected},
        )
        assert r.status_code == 200, r.text
    r = player.post(f"/api/rule-tests/{test_id}/finish", json={})
    assert r.status_code == 200
    return r.json()


@pytest.mark.parametrize(
    "flags,allowed",
    [
        ({}, False),
        ({"is_admin": True}, False),
        ({"is_admin": True, "is_senior_admin": True}, True),
        ({"is_admin": True, "is_chief_admin": True}, True),
        ({"is_senior_admin": True}, False),
    ],
)
def test_assignment_permissions(make_user, flags, allowed):
    make_user(101, **flags)
    make_user(102)
    r = account(101).post("/api/admin/rule-tests", json={"user_id": 102, "count": 2})
    assert r.status_code == (200 if allowed else 403)


def test_owner_can_manage(make_user):
    make_user(100000000000000000, is_admin=True)
    make_user(102)
    assign(account(100000000000000000))


def test_random_unique_questions_and_no_secrets(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    tid = assign(account(101), count=100)
    q = snapshots(tid)
    assert len(q) == len({x["id"] for x in q}) == 100
    r = account(102).get(f"/api/rule-tests/{tid}")
    assert r.status_code == 200
    assert set(r.json()["question"]) == {"position", "text", "options"}
    assert "feedback" not in r.json()
    assert "questions" not in account(102).get("/api/rule-tests").text
    assert r.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("count", [0, -1, 101, True, 1.5, "3", None])
def test_count_validation(make_user, count):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    assert (
        account(101)
        .post("/api/admin/rule-tests", json={"user_id": 102, "count": count})
        .status_code
        == 400
    )


def test_cannot_read_other_players_test_or_admin_results(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    make_user(103, is_admin=True)
    tid = assign(account(101))
    for uid in (102, 103):
        assert account(uid).get("/api/admin/rule-tests").status_code == 403
        assert account(uid).get(f"/api/admin/rule-tests/{tid}").status_code == 403
    for suffix in ("", "/answer", "/finish"):
        c = account(103)
        r = (
            c.get(f"/api/rule-tests/{tid}")
            if not suffix
            else c.post(f"/api/rule-tests/{tid}{suffix}", json={})
        )
        assert r.status_code == 404
    assert TestClient(main.app).get("/api/rule-tests").status_code == 401


def test_answer_feedback_retry_and_finish(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    admin, player = account(101), account(102)
    tid = assign(admin, count=1)
    q = snapshots(tid)[0]
    assert player.post(f"/api/rule-tests/{tid}/finish", json={}).status_code == 409
    payload = {"position": 0, "selected": q["correct"]}
    r = player.post(f"/api/rule-tests/{tid}/answer", json=payload)
    assert r.json()["feedback"]["explanation"] == q["explanation"]
    assert r.json()["feedback"]["is_correct"] is True
    assert r.json()["ready_to_finish"] is True
    assert player.post(f"/api/rule-tests/{tid}/answer", json=payload).status_code == 409
    done = player.post(f"/api/rule-tests/{tid}/finish", json={}).json()
    assert done["score"] == 1 and done["percent"] == 100
    assert player.post(f"/api/rule-tests/{tid}/finish", json={}).json() == done
    state = player.get(f"/api/rule-tests/{tid}").json()
    assert not {"question", "feedback", "answers", "questions"} & state.keys()
    assert player.post(f"/api/rule-tests/{tid}/answer", json=payload).status_code == 409
    detail = admin.get(f"/api/admin/rule-tests/{tid}").json()
    assert detail["answers"][0]["selected"] == q["correct"]
    profile = player.get("/api/user/102/public-profile").json()
    assert profile["rule_tests"][0]["score"] == 1
    assert (
        account(101).get("/api/user/102/public-profile").json()["can_review_rule_tests"]
        is True
    )
    assert "rtProfileResults" in player.get("/profile").text
    assert player.get("/rule-tests").status_code == 200
    assert admin.get("/admin").status_code == 200


def test_snapshot_preserved_after_question_edit(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    admin, player = account(101), account(102)
    tid = assign(admin, count=1)
    q = snapshots(tid)[0]
    with SessionLocal() as db:
        row = db.get(ServerRuleQuestion, q["id"])
        before = row.question_text
        row.question_text = "Новая формулировка"
        db.commit()
    try:
        assert (
            player.get(f"/api/rule-tests/{tid}").json()["question"]["text"] == q["text"]
        )
        assert complete(player, tid)["score"] == 1
    finally:
        with SessionLocal() as db:
            db.get(ServerRuleQuestion, q["id"]).question_text = before
            db.commit()


def test_duplicate_assignment_then_new_assignment_after_finish(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    admin, player = account(101), account(102)
    tid = assign(admin, count=1)
    assert (
        admin.post(
            "/api/admin/rule-tests", json={"user_id": 102, "count": 1}
        ).status_code
        == 409
    )
    complete(player, tid)
    assert assign(admin, count=1) != tid
    assert len(player.get("/api/rule-tests").json()["tests"]) == 2


def test_concurrent_answer_only_recorded_once(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    tid = assign(account(101), count=1)
    q = snapshots(tid)[0]

    async def race():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            cookies={"session": session_cookie(102)},
        ) as c:
            return await asyncio.gather(
                *(
                    c.post(
                        f"/api/rule-tests/{tid}/answer",
                        json={"position": 0, "selected": q["correct"]},
                    )
                    for _ in range(2)
                )
            )

    assert sorted(r.status_code for r in asyncio.run(race())) == [200, 409]
    assert account(102).get(f"/api/rule-tests/{tid}").json()["answered"] == 1


def test_private_results_not_visible_to_other_players_or_junior(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    make_user(103)
    make_user(104, is_admin=True)
    tid = assign(account(101), count=1)
    complete(account(102), tid, mistakes=True)
    for uid in (103, 104):
        data = account(uid).get("/api/user/102/public-profile").json()
        assert data["rule_tests"] is None and data["can_review_rule_tests"] is False
    detail = account(101).get(f"/api/admin/rule-tests/{tid}").json()
    assert detail["score"] == 0 and detail["answers"][0]["is_correct"] is False


@pytest.mark.parametrize("selected", [[], [True], [99], [-1], [0, 0], "0", None])
def test_invalid_answer_does_not_advance(make_user, selected):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    tid = assign(account(101), count=1)
    c = account(102)
    assert (
        c.post(
            f"/api/rule-tests/{tid}/answer", json={"position": 0, "selected": selected}
        ).status_code
        == 400
    )
    assert c.get(f"/api/rule-tests/{tid}").json()["answered"] == 0


def test_concurrent_assignment_only_creates_one_active_test(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)

    async def race():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            cookies={"session": session_cookie(101)},
        ) as c:
            return await asyncio.gather(
                *(
                    c.post("/api/admin/rule-tests", json={"user_id": 102, "count": 2})
                    for _ in range(2)
                )
            )

    assert sorted(r.status_code for r in asyncio.run(race())) == [200, 409]
    with SessionLocal() as db:
        assert db.query(RuleTest).count() == 1


def test_archived_player_cannot_continue(make_user):
    from datetime import datetime
    from database import User

    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    tid = assign(account(101), count=1)
    with SessionLocal() as db:
        db.get(User, 102).deleted_at = datetime.now()
        db.commit()
    assert account(102).get(f"/api/rule-tests/{tid}").status_code == 401


def test_cross_origin_assignment_is_rejected(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    assert (
        account(101)
        .post(
            "/api/admin/rule-tests",
            json={"user_id": 102, "count": 1},
            headers={"Origin": "https://other.example"},
        )
        .status_code
        == 403
    )
    with SessionLocal() as db:
        assert db.query(RuleTest).count() == 0


def test_batch_assigns_selected_players_and_skips_active(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    for uid in (102, 103, 104, 105):
        make_user(uid)
    c = account(101)
    active = assign(c, uid=102, count=2)
    r = c.post(
        "/api/admin/rule-tests/batch",
        json={"user_ids": [102, 103, 104, 103], "count": 4},
    )
    assert r.status_code == 200
    data = r.json()
    assert {t["user_id"] for t in data["assigned"]} == {103, 104}
    assert [t["user_id"] for t in data["skipped"]] == [102]
    for t in data["assigned"]:
        assert (
            len(snapshots(t["id"])) == len({q["id"] for q in snapshots(t["id"])}) == 4
        )
    with SessionLocal() as db:
        assert db.query(RuleTest).filter_by(user_id=102).one().id == active
        assert db.query(RuleTest).filter_by(user_id=105).count() == 0


@pytest.mark.parametrize(
    "ids", [[], [True], [0], [-1], ["102"], None, list(range(1, 1002))]
)
def test_batch_validates_ids_without_creating_tests(make_user, ids):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    assert (
        account(101)
        .post("/api/admin/rule-tests/batch", json={"user_ids": ids, "count": 2})
        .status_code
        == 400
    )
    with SessionLocal() as db:
        assert db.query(RuleTest).count() == 0


def test_batch_missing_player_rolls_back_entire_assignment(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    assert (
        account(101)
        .post(
            "/api/admin/rule-tests/batch", json={"user_ids": [102, 99999], "count": 2}
        )
        .status_code
        == 404
    )
    with SessionLocal() as db:
        assert db.query(RuleTest).count() == 0


def test_junior_cannot_batch_assign(make_user):
    make_user(101, is_admin=True)
    make_user(102)
    assert (
        account(101)
        .post("/api/admin/rule-tests/batch", json={"user_ids": [102], "count": 2})
        .status_code
        == 403
    )


def test_batch_concurrent_requests_do_not_duplicate(make_user):
    make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    make_user(103)

    async def race():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            cookies={"session": session_cookie(101)},
        ) as c:
            return await asyncio.gather(
                *(
                    c.post(
                        "/api/admin/rule-tests/batch",
                        json={"user_ids": [102, 103], "count": 2},
                    )
                    for _ in range(2)
                )
            )

    results = asyncio.run(race())
    assert [r.status_code for r in results] == [200, 200]
    assert sorted(len(r.json()["assigned"]) for r in results) == [0, 2]
    with SessionLocal() as db:
        assert db.query(RuleTest).count() == 2
