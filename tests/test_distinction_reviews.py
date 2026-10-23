"""Every distinction waits for a qualified decision or a durable 24-hour deadline."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import main
from admin_hub import (
    ManualAwardRequest,
    AdminActionLog,
    AwardCandidateExclusion,
    AwardWeekControl,
)
from automation_control import AutomationException
from award_rewards import AwardRewardPolicy, AwardRewardPayment, pay
from award_revocation import AwardRevocation
from database import GameHistory, SessionLocal, User
from distinction_reviews import DistinctionReview, decide, process_due
from distinctions_catalog import ROLE_IDS
from medals import MedalAward, grant

NOW = datetime(2026, 10, 23, 12)


@pytest.fixture(autouse=True)
def clean_reviews():
    with SessionLocal.begin() as db:
        for model in (
            ManualAwardRequest,
            AdminActionLog,
            AwardCandidateExclusion,
            AwardWeekControl,
            AutomationException,
            AwardRevocation,
            AwardRewardPayment,
            AwardRewardPolicy,
            MedalAward,
            DistinctionReview,
        ):
            db.query(model).delete()


def reviewer():
    return SimpleNamespace(
        id=999,
        discord_id="999",
        display_name="Reviewer",
        username="reviewer",
        is_admin=True,
        is_senior_admin=True,
        is_chief_admin=False,
        deleted_at=None,
    )


def propose(db, uid, code="best_pilot", now=NOW):
    return grant(
        db,
        db.get(User, uid),
        code,
        "Подтверждённое достижение",
        {"source": "Protocol", "week": "2026-10-12", "verified": True},
        now,
    )


@pytest.mark.parametrize("code", list(ROLE_IDS))
def test_every_distinction_waits_then_issues_after_exactly_24_hours(make_user, code):
    uid = make_user(steam_id="76561198000000000")
    with SessionLocal.begin() as db:
        assert propose(db, uid, code) is None
        row = db.query(DistinctionReview).one()
        assert row.deadline == NOW + timedelta(hours=24)
        assert db.query(MedalAward).count() == 0
        assert db.query(GameHistory).count() == 0
        assert (
            process_due(db, NOW + timedelta(hours=24) - timedelta(microseconds=1)) == 0
        )
        assert process_due(db, NOW + timedelta(hours=24)) == 1
        award = db.query(MedalAward).one()
        assert award.medal == code and row.status == "issued"
        assert json.loads(award.evidence)["review"]["decision"] == "automatic"
        assert process_due(db, NOW + timedelta(days=3)) == 0
        assert db.query(MedalAward).count() == 1


def test_polling_and_restart_do_not_extend_deadline_and_payment_is_once(make_user):
    uid = make_user()
    with SessionLocal.begin() as db:
        db.add(
            AwardRewardPolicy(
                id=1,
                initialized_at=NOW,
                retroactive=True,
                prices=json.dumps({"best_pilot": 2000}),
            )
        )
        propose(db, uid)
        propose(db, uid, now=NOW + timedelta(hours=23))
    with SessionLocal.begin() as db:
        row = db.query(DistinctionReview).one()
        assert row.created_at == NOW and row.deadline == NOW + timedelta(hours=24)
        assert process_due(db, NOW + timedelta(days=2)) == 1
        pay(db, NOW + timedelta(days=2))
    with SessionLocal.begin() as db:
        assert process_due(db, NOW + timedelta(days=3)) == 0
        pay(db, NOW + timedelta(days=3))
        assert db.get(User, uid).balance == 3000
        assert db.query(AwardRewardPayment).count() == 1
        assert db.query(GameHistory).filter_by(result="award_reward").count() == 1


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_manual_decision_wins_and_cannot_repeat(make_user, decision):
    uid = make_user()
    with SessionLocal.begin() as db:
        propose(db, uid)
        row = db.query(DistinctionReview).one()
        award = decide(
            db, row, reviewer(), decision, "Проверено", now=NOW + timedelta(hours=2)
        )
        assert bool(award) == (decision == "approved")
        assert process_due(db, NOW + timedelta(days=2)) == 0
        propose(db, uid, now=NOW + timedelta(days=2))
        assert db.query(DistinctionReview).count() == 1
        assert db.query(MedalAward).count() == (1 if decision == "approved" else 0)
        with pytest.raises(main.HTTPException) as error:
            decide(db, row, reviewer(), "approved", now=NOW + timedelta(days=2))
        assert error.value.status_code == 409


def test_archived_player_waits_but_steam_change_cancels_old_proof(make_user):
    uid = make_user()
    with SessionLocal.begin() as db:
        propose(db, uid)
        db.get(User, uid).deleted_at = NOW
        assert process_due(db, NOW + timedelta(days=2)) == 0
        assert db.query(DistinctionReview).one().status == "pending"
        db.get(User, uid).deleted_at = None
        db.get(User, uid).steam_id = "76561198000000000"
        assert process_due(db, NOW + timedelta(days=2)) == 0
        assert db.query(DistinctionReview).one().status == "cancelled"
        propose(db, uid, now=NOW + timedelta(days=2))
        assert db.query(DistinctionReview).one().deadline == NOW + timedelta(days=3)


def test_week_exclusion_blocks_before_and_after_queue(make_user):
    uid = make_user()
    with SessionLocal.begin() as db:
        propose(db, uid)
        db.add(
            AwardCandidateExclusion(
                week="2026-10-12",
                discord_id=str(uid),
                medal="best_pilot",
                reason="Не подходит",
                actor_id="999",
                created_at=NOW,
            )
        )
        db.flush()
        assert process_due(db, NOW + timedelta(days=2)) == 0
        assert not db.query(MedalAward).count()


@pytest.mark.parametrize(
    "role,allowed",
    [
        ({}, False),
        ({"is_admin": True}, False),
        ({"is_admin": True, "is_senior_admin": True}, True),
        ({"is_admin": True, "is_chief_admin": True}, True),
        ({"is_admin": True, "owner": True}, True),
        ({"is_senior_admin": True}, False),
    ],
)
def test_decision_api_requires_senior_chief_or_owner(
    make_user, login_as, role, allowed
):
    role = role.copy()
    owner = role.pop("owner", False)
    actor = make_user(101, **role)
    target = make_user(102)
    with SessionLocal.begin() as db:
        if owner:
            db.get(User, actor).discord_id = main.ADMIN_DISCORD_ID
        propose(db, target)
        review_id = db.query(DistinctionReview).one().id
    client = login_as(actor)
    assert client.get("/api/admin/distinction-reviews").status_code == (
        200 if allowed else 403
    )
    response = client.post(
        f"/api/admin/distinction-reviews/{review_id}/decision",
        json={"decision": "approved", "comment": "Проверено"},
    )
    assert response.status_code == (200 if allowed else 403), response.text
    with SessionLocal() as db:
        assert db.query(MedalAward).count() == int(allowed)


def test_manual_request_shares_queue_and_auto_decision(make_user, login_as):
    actor = make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    client = login_as(actor)
    response = client.post(
        "/api/admin/manual-awards",
        json={
            "user_id": 102,
            "medal": "best_pistol",
            "comment": "Проверено по записи",
            "evidence_url": "https://example.com/replay",
        },
    )
    assert response.status_code == 200, response.text
    with SessionLocal.begin() as db:
        row = db.query(DistinctionReview).one()
        assert db.query(MedalAward).count() == 0
        assert process_due(db, row.deadline) == 1
        request = db.query(ManualAwardRequest).one()
        assert request.status == "approved" and request.award_id == row.award_id


def test_manual_request_can_be_approved_by_senior_immediately(make_user, login_as):
    actor = make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    client = login_as(actor)
    request = client.post(
        "/api/admin/manual-awards",
        json={
            "user_id": 102,
            "medal": "best_atgm",
            "comment": "15 уничтожений на записи",
        },
    ).json()
    response = client.post(
        f"/api/admin/manual-awards/{request['id']}/decision",
        json={"decision": "approved"},
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        assert db.query(MedalAward).one().medal == "best_atgm"


def test_legacy_manual_request_gets_full_new_window(make_user):
    uid = make_user()
    with SessionLocal.begin() as db:
        db.add(
            ManualAwardRequest(
                user_id=uid,
                medal="best_pistol",
                comment="Старая заявка",
                status="pending",
                created_at=NOW - timedelta(days=7),
                created_by="999",
            )
        )
        db.flush()
        assert process_due(db, NOW) == 0
        assert db.query(DistinctionReview).one().deadline == NOW + timedelta(hours=24)


def test_rejected_request_does_not_get_resurrected_by_legacy_scanner(
    make_user, login_as
):
    actor = make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    client = login_as(actor)
    request = client.post(
        "/api/admin/manual-awards",
        json={"user_id": 102, "medal": "best_pistol", "comment": "Заявка"},
    ).json()
    assert (
        client.post(
            f"/api/admin/manual-awards/{request['id']}/decision",
            json={"decision": "rejected"},
        ).status_code
        == 200
    )
    with SessionLocal.begin() as db:
        assert process_due(db, datetime(2030, 1, 1)) == 0
        assert db.query(MedalAward).count() == 0
        assert db.query(ManualAwardRequest).one().status == "rejected"


def test_queue_does_not_touch_already_awarded_distinctions(make_user):
    uid = make_user()
    with SessionLocal.begin() as db:
        db.add(
            MedalAward(
                discord_id=str(uid),
                medal="best_pilot",
                reason="Раньше выдано",
                evidence="{}",
                earned_at=NOW,
            )
        )
        db.flush()
        propose(db, uid)
        assert db.query(DistinctionReview).count() == 0


def test_background_evaluation_issues_due_even_when_collectors_are_disabled(make_user):
    from automation_control import AutomationSetting
    from medals import evaluate

    uid = make_user()
    with SessionLocal.begin() as db:
        propose(db, uid)
        for name in ("protocol", "sqstat", "battlemetrics"):
            key = "source_" + name + "_enabled"
            row = db.get(AutomationSetting, key)
            if row:
                row.value = "false"
            else:
                db.add(AutomationSetting(key=key, value="false", updated_at=NOW))
        db.flush()
        evaluate(db, NOW + timedelta(hours=24))
        assert db.query(MedalAward).filter_by(medal="best_pilot").count() == 1
        for name in ("protocol", "sqstat", "battlemetrics"):
            db.query(AutomationSetting).filter_by(
                key="source_" + name + "_enabled"
            ).delete()


def test_existing_week_approval_does_not_bypass_individual_review(make_user):
    uid = make_user()
    with SessionLocal.begin() as db:
        db.add(AwardWeekControl(week="2026-10-12", status="approved", created_at=NOW))
        db.flush()
        assert propose(db, uid) is None
        assert db.query(DistinctionReview).one().status == "pending"
        assert db.query(MedalAward).count() == 0


def test_parallel_approvals_issue_and_pay_once(make_user):
    from conftest import session_cookie

    actor = make_user(101, is_admin=True, is_senior_admin=True)
    target = make_user(102)
    with SessionLocal.begin() as db:
        db.add(
            AwardRewardPolicy(
                id=1, initialized_at=NOW, retroactive=True, prices='{"best_pilot":2000}'
            )
        )
        propose(db, target)
        review_id = db.query(DistinctionReview).one().id

    import asyncio
    import httpx

    async def requests():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            cookies={"session": session_cookie(actor)},
            headers={"X-ST9-CSRF": "test-csrf-token"},
        ) as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        f"/api/admin/distinction-reviews/{review_id}/decision",
                        json={"decision": "approved"},
                    )
                    for _ in range(2)
                ]
            )
            return [r.status_code for r in responses]

    statuses = asyncio.run(requests())
    assert sorted(statuses) == [200, 409]
    with SessionLocal() as db:
        assert db.query(MedalAward).count() == 1
        assert db.query(AwardRewardPayment).count() == 1
        assert db.get(User, target).balance == 3000


def test_manual_distinction_cannot_be_proposed_by_junior(make_user, login_as):
    actor = make_user(101, is_admin=True)
    make_user(102)
    response = login_as(actor).post(
        "/api/admin/manual-awards",
        json={"user_id": 102, "medal": "best_pistol", "comment": "Без проверки"},
    )
    assert response.status_code == 403
    with SessionLocal() as db:
        assert db.query(DistinctionReview).count() == 0
        assert db.query(ManualAwardRequest).count() == 0


def test_late_rejection_cannot_override_expired_automatic_deadline(make_user):
    uid = make_user()
    with SessionLocal.begin() as db:
        propose(db, uid)
        row = db.query(DistinctionReview).one()
        award = decide(db, row, reviewer(), "rejected", now=NOW + timedelta(hours=24))
        assert award is not None and row.status == "issued"
        assert row.decided_by == "automatic"


def test_source_evaluation_failure_does_not_rollback_due_awards(make_user, monkeypatch):
    import asyncio
    import medals_routes

    uid = make_user()
    with SessionLocal.begin() as db:
        propose(db, uid)
    monkeypatch.setattr("distinction_reviews.utcnow", lambda: NOW + timedelta(days=2))

    def broken_source(db):
        raise ValueError("Broken source snapshot")

    async def stop_after_one_cycle(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(medals_routes, "evaluate", broken_source)
    monkeypatch.setattr(medals_routes.asyncio, "sleep", stop_after_one_cycle)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await medals_routes.medal_checker()

    asyncio.run(run())
    with SessionLocal() as db:
        assert db.query(MedalAward).filter_by(medal="best_pilot").count() == 1
        assert db.query(DistinctionReview).one().status == "issued"
