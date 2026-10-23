import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from admin_hub import AwardCandidateExclusion, AwardWeekControl
from automation_control import AutomationSetting
from award_rewards import AwardRewardPayment, AwardRewardPolicy
from database import SessionLocal, User
from medals import MedalAward
from protocol_award_exports import (
    ClanAuthRequired,
    ProtocolAwardExport,
    ProtocolExportHealth,
    parse_page,
)
from protocol_week_check import process_requested, request_check, status
from protocol_week_source import ProtocolPublishedHealth, ProtocolPublishedWeek

NOW = datetime(2026, 10, 19, 0, tzinfo=timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def clean_week_check():
    with SessionLocal.begin() as db:
        for model in (
            AutomationSetting,
            MedalAward,
            ProtocolAwardExport,
            ProtocolExportHealth,
            AwardWeekControl,
            AwardCandidateExclusion,
            AwardRewardPayment,
            AwardRewardPolicy,
            ProtocolPublishedHealth,
            ProtocolPublishedWeek,
        ):
            db.query(model).delete()


def docs():
    body = (
        (Path(__file__).parent / "fixtures/protocol_exports.html")
        .read_text()
        .replace("12.10–18.10", "19.10–25.10")
        .replace("05.10–11.10", "12.10–18.10")
    )
    result = parse_page(body, NOW)
    for doc in result.values():
        doc["file_verified"] = True
    return result


def queue(make_user, now=NOW):
    uid = make_user(uid=999, is_admin=True, is_chief_admin=True)
    with SessionLocal.begin() as db:
        return request_check(db, db.get(User, uid), now)


def fail_public(_):
    raise ValueError("not available")


def test_manual_recheck_grants_only_previous_week_and_is_idempotent(make_user):
    data = docs()
    doc = data["2026-10-12:mg"]
    sid = doc["rows"][0]["steam_id"]
    uid = make_user(uid=101, steam_id=sid)
    queue(make_user)
    with SessionLocal.begin() as db:
        db.add(
            AwardRewardPolicy(
                id=1,
                initialized_at=NOW - timedelta(days=1),
                retroactive=True,
                prices='{"best_machine_gunner":2000}',
            )
        )
    assert process_requested(NOW, fetch=lambda _: (data, []), public_fetch=fail_public)
    with SessionLocal() as db:
        awards = db.query(MedalAward).all()
        assert awards and all(
            json.loads(a.evidence).get("week") == "2026-10-12" for a in awards
        )
        mg = (
            db.query(MedalAward)
            .filter_by(discord_id="101", medal="best_machine_gunner")
            .one()
        )
        assert mg.sent_at is None
        assert db.get(User, uid).balance == 3000
        assert status(db)["added"] == len(awards)
        original = len(awards)
    with SessionLocal.begin() as db:
        request_check(db, db.get(User, 999), NOW + timedelta(minutes=1))
    assert process_requested(
        NOW + timedelta(minutes=1), fetch=lambda _: (data, []), public_fetch=fail_public
    )
    with SessionLocal() as db:
        assert db.query(MedalAward).count() == original
        assert db.get(User, uid).balance == 3000
        assert status(db)["added"] == 0


def test_failed_category_does_not_award_from_old_cached_snapshot(make_user):
    data = docs()
    key = "2026-10-12:mg"
    doc = data.pop(key)
    make_user(uid=101, steam_id=doc["rows"][0]["steam_id"])
    with SessionLocal.begin() as db:
        db.add(
            ProtocolAwardExport(
                key=key,
                kind="mg",
                week="2026-10-12",
                payload=json.dumps(doc),
                checked_at=NOW,
            )
        )
    queue(make_user)
    assert process_requested(
        NOW, fetch=lambda _: (data, [key]), public_fetch=fail_public
    )
    with SessionLocal() as db:
        assert not db.query(MedalAward).filter_by(medal="best_machine_gunner").count()
        assert status(db)["status"] == "partial"


def test_excluded_candidate_remains_excluded(make_user):
    data = docs()
    sid = data["2026-10-12:mg"]["rows"][0]["steam_id"]
    make_user(uid=101, steam_id=sid)
    queue(make_user)
    with SessionLocal.begin() as db:
        db.add(
            AwardCandidateExclusion(
                week="2026-10-12",
                discord_id="101",
                medal="best_machine_gunner",
                reason="exclude",
                actor_id="999",
                created_at=NOW,
            )
        )
    process_requested(NOW, fetch=lambda _: (data, []), public_fetch=fail_public)
    with SessionLocal() as db:
        assert not db.query(MedalAward).filter_by(medal="best_machine_gunner").count()


def test_auth_error_does_not_issue_cached_awards_or_approve_week(make_user):
    queue(make_user)

    def fail(_):
        raise ClanAuthRequired("expired")

    assert not process_requested(NOW, fetch=fail, public_fetch=fail_public)
    with SessionLocal() as db:
        assert status(db)["status"] == "error"
        assert db.query(MedalAward).count() == 0
        assert db.get(AwardWeekControl, "2026-10-12") is None


def test_rollover_requires_new_request_and_never_fetches_wrong_week(make_user):
    queue(make_user)

    def forbidden(_):
        raise AssertionError("must not fetch")

    assert not process_requested(NOW + timedelta(days=7), fetch=forbidden)
    with SessionLocal() as db:
        assert status(db)["status"] == "error"
        assert "Неделя сменилась" in status(db)["message"]


def test_background_checks_do_not_hold_wallet_lock_during_fetch(make_user):
    from runtime import lock_runtime_row

    queue(make_user)

    def fetch(_):
        # A separate writer can acquire the wallet lock during network I/O.
        with SessionLocal.begin() as db:
            lock_runtime_row(db)
        return docs(), []

    assert process_requested(NOW, fetch=fetch, public_fetch=fail_public)


def test_job_deduplicates_queue_and_rechecks_actor_rights(make_user):
    queued = queue(make_user)
    with SessionLocal.begin() as db:
        assert request_check(db, db.get(User, 999), NOW)["id"] == queued["id"]
        db.get(User, 999).is_chief_admin = False
    assert not process_requested(
        NOW, fetch=lambda _: (docs(), []), public_fetch=fail_public
    )
    with SessionLocal() as db:
        assert status(db)["status"] == "error" and db.query(MedalAward).count() == 0


def test_endpoints_are_chief_only_and_queue_without_network(
    client, make_user, login_as, monkeypatch
):
    url = "/api/admin/protocol-week-check"
    assert client.get(url).status_code == 403
    uid = make_user(uid=999, is_admin=True, is_senior_admin=True)
    login_as(uid)
    assert (
        client.post(url, headers={"X-ST9-Action": "protocol-week-check"}).status_code
        == 403
    )
    with SessionLocal.begin() as db:
        db.get(User, uid).is_chief_admin = True

    def forbidden(*_):
        raise AssertionError("web must not call HTTP")

    monkeypatch.setattr("protocol_award_exports.fetch_exports", forbidden)
    response = client.post(url, headers={"X-ST9-Action": "protocol-week-check"})
    assert response.status_code == 202 and response.json()["status"] == "queued"
    response = client.get(url)
    assert (
        response.status_code == 200
        and response.headers["cache-control"] == "private, no-store"
    )
    assert (
        client.post(url, headers={"X-ST9-Action": "protocol-week-check"}).json()["id"]
        == response.json()["id"]
    )


def test_disabled_source_does_not_queue_or_fetch(make_user):
    uid = make_user(uid=999, is_admin=True, is_chief_admin=True)
    with SessionLocal.begin() as db:
        db.add(
            AutomationSetting(
                key="source_protocol_enabled", value="false", updated_at=NOW
            )
        )
        db.flush()
        with pytest.raises(ValueError, match="отключён"):
            request_check(db, db.get(User, uid), NOW)


def test_manual_check_bypasses_recent_cache_and_reads_new_recipients(make_user):
    data = docs()
    key = "2026-10-12:mg"
    old = copy.deepcopy(data[key])
    new_sid = "76561198000000101"
    make_user(uid=101, steam_id=new_sid)
    with SessionLocal.begin() as db:
        db.add(
            ProtocolAwardExport(
                key=key,
                week="2026-10-12",
                kind="mg",
                payload=json.dumps(old),
                checked_at=NOW,
            )
        )
        db.add(ProtocolExportHealth(id=1, attempted_at=NOW, status="ready"))
    data[key]["rows"][0]["steam_id"] = new_sid
    queue(make_user)
    assert process_requested(
        NOW + timedelta(minutes=1), fetch=lambda _: (data, []), public_fetch=fail_public
    )
    with SessionLocal() as db:
        assert (
            db.query(MedalAward)
            .filter_by(discord_id="101", medal="best_machine_gunner")
            .count()
            == 1
        )


def test_successful_public_table_complements_exports(make_user):
    from protocol_week_source import URL

    make_user(uid=101, steam_id="76561198000000101")
    queue(make_user)
    public = {
        "week": "2026-10-12",
        "source": URL,
        "final": True,
        "period": "12.10–18.10",
        "basis": "published",
        "categories": {
            key: {"rows": [], "name": key, "published_count": 0}
            for key in ("cmd", "sl", "revive")
        },
    }
    public["categories"]["sl"]["rows"] = [
        {"steam_id": "76561198000000101", "rank": 1, "value": 12}
    ]
    assert process_requested(
        NOW, fetch=lambda _: (docs(), []), public_fetch=lambda _: public
    )
    with SessionLocal() as db:
        assert status(db)["status"] == "ready"
        assert (
            db.query(MedalAward)
            .filter_by(discord_id="101", medal="squad_leader")
            .count()
            == 1
        )
