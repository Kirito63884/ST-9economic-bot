"""Global date policy, exact preview, worker enforcement and once-only refunds."""

import json
from datetime import datetime, timedelta

import pytest
from conftest import wallet

from admin_hub import AdminActionLog, AssemblyAppeal
from assembly_attendance import (
    AssemblyAssessment,
    AssemblyWindow,
    apply_assessments,
    bounds,
    finalize_penalties,
)
from assembly_waivers import active_waiver
from automation_control import AutomationSetting
from bulk_operations import BulkConfirmation
from database import GameHistory, SessionLocal, User

DAY = "2026-10-24"
PATH = "/api/admin/assembly/waive"


@pytest.fixture(autouse=True)
def clear():
    with SessionLocal.begin() as db:
        for model in (
            AssemblyAppeal,
            AssemblyAssessment,
            AssemblyWindow,
            AdminActionLog,
            BulkConfirmation,
        ):
            db.query(model).delete()
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("assembly_waiver:%")
        ).delete(synchronize_session=False)


def event(day=DAY):
    start, end = bounds(day)
    health = {
        "discord_voice": {
            "first": start.isoformat(),
            "last": (end - timedelta(minutes=1)).isoformat(),
            "count": 180,
            "max_gap": 60,
        }
    }
    with SessionLocal.begin() as db:
        db.add(
            AssemblyWindow(
                event_date=day,
                starts_at=start,
                ends_at=end,
                first_sample=start,
                last_sample=end - timedelta(seconds=30),
                sample_count=360,
                max_gap_seconds=30,
                healthy_servers=json.dumps(health),
                member_ids='["102","103"]',
                attended="{}",
                status="review_pending",
            )
        )
    return end


def assessment(uid, outcome="pending_penalty", day=DAY, charged=0):
    with SessionLocal.begin() as db:
        row = AssemblyAssessment(
            event_date=day,
            discord_id=str(uid),
            user_id=uid,
            outcome=outcome,
            assessed_cents=80000,
            charged_cents=charged,
            created_at=datetime(2026, 10, 24, 20),
        )
        db.add(row)
        db.flush()
        return row.id


@pytest.fixture
def chief(make_user, login_as):
    uid = make_user(999, is_admin=True, is_chief_admin=True)
    return login_as(uid)


def preview(client, refund=False, day=DAY):
    body = {"event_date": day, "reason": "Сбор отменён командованием", "refund": refund}
    r = client.post(PATH, json={**body, "preview": True})
    assert r.status_code == 200, r.text
    return body, r.json()


def commit(client, body, plan):
    return client.post(
        PATH, json=body, headers={"X-ST9-Confirmation": plan["confirmation"]}
    )


def test_default_cancels_pending_only_preview_is_read_only_and_next_date_unchanged(
    chief, make_user
):
    make_user(102)
    make_user(103, balance=200)
    event()
    p = assessment(102)
    paid = assessment(103, "penalized", charged=80000)
    other = assessment(102, day="2026-10-31")
    with SessionLocal.begin() as db:
        db.add(
            AssemblyAppeal(
                assessment_id=p,
                user_id=102,
                reason="Проверить",
                status="pending",
                created_at=datetime(2026, 10, 25),
                refunded_cents=0,
            )
        )
    body, plan = preview(chief)
    assert plan["pending_count"] == 1 and plan["refund_cents"] == 0
    with SessionLocal() as db:
        assert active_waiver(db, DAY) is None
        assert db.get(AssemblyAssessment, p).outcome == "pending_penalty"
        assert db.query(AdminActionLog).count() == 0
    assert commit(chief, body, plan).status_code == 200
    assert wallet(102) == 1000 and wallet(103) == 200
    with SessionLocal() as db:
        assert active_waiver(db, DAY)
        assert db.get(AssemblyAssessment, p).outcome == "waived"
        assert db.get(AssemblyAppeal, p).status == "approved"
        assert db.get(AssemblyAppeal, p).refunded_cents == 0
        assert db.get(AssemblyAssessment, paid).charged_cents == 80000
        assert db.get(AssemblyAssessment, other).outcome == "pending_penalty"
        assert db.query(GameHistory).count() == 0
    assert commit(chief, body, plan).status_code == 409


def test_waiver_before_collection_prevents_worker_assessment_and_late_finalize(
    chief, make_user
):
    make_user(102, steam_id="76561198000000102")
    body, plan = preview(chief, day="2026-10-31")
    assert plan["action_needed"] and not plan["rows"]
    assert commit(chief, body, plan).status_code == 200
    end = event("2026-10-31")
    with SessionLocal.begin() as db:
        db.get(AssemblyWindow, "2026-10-31").status = "collecting"
        apply_assessments(db, "2026-10-31", {"102"}, {}, now=end)
        row = db.query(AssemblyAssessment).filter_by(event_date="2026-10-31").one()
        assert row.outcome == "waived"
    # Even a queued decision created later by an older worker cannot charge.
    late = assessment(103, day="2026-10-31")
    make_user(103, steam_id="76561198000000103")
    with SessionLocal.begin() as db:
        db.get(AssemblyWindow, "2026-10-31").status = "review_pending"
        result = finalize_penalties(db, "2026-10-31", now=end)
        assert (
            result["charged"] == 0
            and db.get(AssemblyAssessment, late).outcome == "waived"
        )
    assert wallet(102) == 1000 and wallet(103) == 1000


def test_explicit_refund_once_and_pending_appeal_cannot_refund_again(chief, make_user):
    make_user(102, balance=200)
    event()
    aid = assessment(102, "penalized", charged=80000)
    with SessionLocal.begin() as db:
        db.add(
            AssemblyAppeal(
                assessment_id=aid,
                user_id=102,
                reason="Проверьте",
                status="pending",
                created_at=datetime(2026, 10, 25),
                refunded_cents=0,
            )
        )
    body, plan = preview(chief, True)
    assert plan["refund_cents"] == 80000 and wallet(102) == 200
    assert commit(chief, body, plan).status_code == 200
    assert wallet(102) == 1000
    again_body, again = preview(chief, True)
    assert not again["action_needed"] and again["refund_cents"] == 0
    with SessionLocal() as db:
        row = db.get(AssemblyAssessment, aid)
        assert row.charged_cents == 0 and row.outcome == "waived"
        assert db.get(AssemblyAppeal, aid).status == "approved"
        assert db.query(GameHistory).filter_by(result="assembly_refund").count() == 1
    r = chief.post(
        f"/api/admin/assembly/appeals/{aid}/decision", json={"decision": "approved"}
    )
    assert r.status_code == 409 and wallet(102) == 1000


def test_already_refunded_appeal_not_paid_again(chief, make_user):
    make_user(102, balance=1000)
    event()
    aid = assessment(102, "appeal_approved", charged=80000)
    with SessionLocal.begin() as db:
        db.add(
            AssemblyAppeal(
                assessment_id=aid,
                user_id=102,
                reason="Проверили",
                status="approved",
                created_at=datetime(2026, 10, 25),
                refunded_cents=80000,
            )
        )
    body, plan = preview(chief, True)
    assert plan["refund_cents"] == 0
    assert commit(chief, body, plan).status_code == 200 and wallet(102) == 1000


def test_preview_invalidated_by_charge_balance_or_request_changes(chief, make_user):
    make_user(102, balance=200)
    event()
    assessment(102, "penalized", charged=80000)
    body, plan = preview(chief, True)
    assert commit(chief, {**body, "refund": False}, plan).status_code == 409
    with SessionLocal.begin() as db:
        db.get(User, 102).balance += 10
    assert commit(chief, body, plan).status_code == 409
    body, plan = preview(chief)
    assessment(103)
    assert commit(chief, body, plan).status_code == 409
    assert chief.post(PATH, json=body).status_code == 409


@pytest.mark.parametrize("role", ["anonymous", "player", "junior", "senior"])
def test_preserves_chief_only_access(role, client, make_user, login_as):
    if role != "anonymous":
        client = login_as(
            make_user(
                is_admin=role in ("junior", "senior"), is_senior_admin=role == "senior"
            )
        )
    assert (
        client.post(
            PATH, json={"event_date": DAY, "reason": "Проверка", "preview": True}
        ).status_code
        == 403
    )
    assert client.get("/api/admin/assembly/waiver?event_date=" + DAY).status_code == 403


def test_csrf_validation_and_saturday_scope(chief):
    payload = {"event_date": DAY, "reason": "Проверка", "preview": True}
    assert (
        chief.post(PATH, json=payload, headers={"X-ST9-CSRF": "wrong"}).status_code
        == 403
    )
    assert (
        chief.post(PATH, json={**payload, "event_date": "2026-10-23"}).status_code
        == 400
    )
    assert chief.post(PATH, json={**payload, "refund": "true"}).status_code == 400


def test_confirmation_cannot_be_used_by_another_chief_or_after_expiry(
    chief, make_user, login_as
):
    body, plan = preview(chief)
    another = login_as(make_user(998, is_admin=True, is_chief_admin=True))
    assert commit(another, body, plan).status_code == 409
    chief = login_as(999)
    with SessionLocal.begin() as db:
        for row in db.query(BulkConfirmation):
            row.expires_at = datetime(2000, 1, 1)
    assert commit(chief, body, plan).status_code == 409
    with SessionLocal() as db:
        assert active_waiver(db, DAY) is None
