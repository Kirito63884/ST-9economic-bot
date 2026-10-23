import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import main
import pytest
from admin_hub import AwardCandidateExclusion, AwardWeekControl, _preview
from automation_control import AutomationSetting
from database import SessionLocal, User
from medals import MedalAward, evaluate, progress
from protocol_award_exports import (
    KIT_AWARDS,
    ClanUnavailable,
    ProtocolAwardExport,
    ProtocolExportHealth,
    award_exports,
    collect_exports,
    fetch_exports,
    parse_ids,
    parse_page,
)

NOW = datetime(2026, 10, 15, 21, tzinfo=timezone.utc).replace(tzinfo=None)
AFTER = datetime(2026, 10, 19, 0, tzinfo=timezone.utc).replace(tzinfo=None)
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def clean_exports():
    main.request_guard.reset()
    with SessionLocal.begin() as db:
        for model in (
            ProtocolAwardExport,
            ProtocolExportHealth,
            MedalAward,
            AwardWeekControl,
            AwardCandidateExclusion,
            AutomationSetting,
        ):
            db.query(model).delete()
    yield
    with SessionLocal.begin() as db:
        db.query(ProtocolAwardExport).delete()
        db.query(ProtocolExportHealth).delete()


def documents(now=NOW):
    body = (FIXTURES / "protocol_exports.html").read_text()
    if now == AFTER:
        body = body.replace("12.10–18.10", "19.10–25.10").replace(
            "05.10–11.10", "12.10–18.10"
        )
    return parse_page(body, now)


def store(data, stamp=NOW):
    with SessionLocal.begin() as db:
        for key, doc in data.items():
            row = db.get(ProtocolAwardExport, key)
            if not row:
                row = ProtocolAwardExport(key=key, week=doc["week"], kind=doc["kind"])
                db.add(row)
            row.payload = json.dumps(
                {**doc, "file_verified": True, "file_count": len(doc["rows"])}
            )
            row.checked_at = stamp


def test_real_page_and_downloads_match():
    data = documents()
    assert len(data) == 23
    mg = data["2026-10-05:mg"]
    assert [r["steam_id"] for r in mg["rows"]] == parse_ids(
        (FIXTURES / "protocol_mg_prev.txt").read_text()
    )
    assert mg["rows"][0]["value"] == 43.8
    fob = data["all:fob"]
    assert [r["steam_id"] for r in fob["rows"]] == parse_ids(
        (FIXTURES / "protocol_fob.txt").read_text()
    )[:20]
    assert fob["rows"][0]["value"] == 6
    assert not mg["exact_kits"]


def test_parse_rejects_wrong_period_or_other_clan():
    body = (FIXTURES / "protocol_exports.html").read_text()
    for changed in [
        body.replace("05.10–11.10", "04.10–10.10"),
        body.replace("STRIKE TEAM 9", "OTHER"),
        body.replace("Бои без сида", "Неизвестные бои"),
    ]:
        with pytest.raises(ClanUnavailable):
            parse_page(changed, NOW)


@pytest.mark.parametrize(
    "body",
    [
        "<html>login</html>",
        "76561199041069488\n76561199041069488",
        "not steam",
        "7656\n",
    ],
)
def test_bad_dump_is_rejected(body):
    with pytest.raises(ClanUnavailable):
        parse_ids(body)


def test_fetch_checks_txt_membership_and_isolates_failed_category(monkeypatch):
    data = documents()
    body = (FIXTURES / "protocol_exports.html").read_text()
    by_url = {
        d["file"]: "\n".join(r["steam_id"] for r in d["rows"]) for d in data.values()
    }
    by_url[data["2026-10-05:mg"]["file"]] = "76561198000000000\n"

    def transport(request):
        return httpx.Response(
            200,
            text=body
            if request.url.path == "/clan/achievements"
            else by_url[str(request.url)],
        )

    monkeypatch.setattr(
        "protocol_clan_source.make_client",
        lambda: httpx.Client(transport=httpx.MockTransport(transport)),
    )
    accepted, failed = fetch_exports(NOW)
    assert failed == ["2026-10-05:mg"]
    assert len(accepted) == 22
    assert all(d["file_verified"] for d in accepted.values())


def test_collection_failure_preserves_previous_snapshots():
    data = documents()
    for d in data.values():
        d["file_verified"] = True
    assert collect_exports(NOW, fetch=lambda _: (data, []))

    def fail(_):
        raise ClanUnavailable("temporary")

    assert not collect_exports(NOW + timedelta(hours=24), fetch=fail)
    with SessionLocal() as db:
        assert db.query(ProtocolAwardExport).count() == 23
        assert db.get(ProtocolExportHealth, 1).status == "unavailable"


@pytest.mark.parametrize("kind,code", list(KIT_AWARDS.items()))
def test_exact_kit_award_needs_closed_week_and_chief_review(make_user, kind, code):
    data = documents(AFTER)
    doc = data["2026-10-12:" + kind]
    uid = make_user(steam_id=doc["rows"][0]["steam_id"])
    store({doc["week"] + ":" + kind: doc}, AFTER)
    with SessionLocal.begin() as db:
        user = db.get(User, uid)
        award_exports(db, [user], AFTER)
        assert not db.query(MedalAward).filter_by(medal=code).first()
        control = db.get(AwardWeekControl, "2026-10-12")
        assert control.status == "review"
        control.status = "approved"
        db.flush()
        award_exports(db, [user], AFTER)
        award = db.query(MedalAward).filter_by(medal=code).one()
        assert json.loads(award.evidence)["file_verified"]
        assert award.sent_at is None  # Existing bot notification queue handles it.
        award_exports(db, [user], AFTER)
        assert db.query(MedalAward).filter_by(medal=code).count() == 1


def test_old_kit_estimates_and_current_week_do_not_grant(make_user):
    data = documents()
    sid = data["2026-10-05:mg"]["rows"][0]["steam_id"]
    uid = make_user(steam_id=sid)
    store(data)
    with SessionLocal.begin() as db:
        db.add(AwardWeekControl(week="2026-10-05", status="approved", created_at=NOW))
        db.flush()
        award_exports(db, [db.get(User, uid)], NOW)
        assert not db.query(MedalAward).filter_by(medal="best_machine_gunner").first()
    # A cached current week cannot silently turn into a final snapshot on Monday.
    with SessionLocal.begin() as db:
        db.query(ProtocolAwardExport).filter(
            ProtocolAwardExport.week != "2026-10-12"
        ).delete()
        db.add(AwardWeekControl(week="2026-10-12", status="approved", created_at=AFTER))
        db.flush()
        award_exports(db, [db.get(User, uid)], AFTER)
        assert not db.query(MedalAward).filter_by(medal="best_machine_gunner").first()


@pytest.mark.parametrize(
    "place,star", [(0, "gold_star"), (1, "silver_star"), (2, "bronze_star")]
)
def test_stars_and_merit_use_exact_steam_id(make_user, place, star):
    data = documents()
    doc = data["2026-10-05:kills"]
    sid = doc["rows"][place]["steam_id"]
    uid = make_user(steam_id=sid)
    store({"2026-10-05:kills": doc})
    with SessionLocal.begin() as db:
        db.add(AwardWeekControl(week="2026-10-05", status="approved", created_at=NOW))
        db.flush()
        covered = award_exports(db, [db.get(User, uid)], NOW)
        assert ("2026-10-05", "kills") in covered
        assert {a.medal for a in db.query(MedalAward)} == {star, "merit"}


@pytest.mark.parametrize(
    "kind,code",
    [("cmdwins", "outstanding_service"), ("revives", "medical_aid"), ("wins", "merit")],
)
def test_other_weekly_criteria_from_exports(make_user, kind, code):
    doc = documents()["2026-10-05:" + kind]
    uid = make_user(steam_id=doc["rows"][0]["steam_id"])
    store({"2026-10-05:" + kind: doc})
    with SessionLocal.begin() as db:
        db.add(AwardWeekControl(week="2026-10-05", status="approved", created_at=NOW))
        db.flush()
        award_exports(db, [db.get(User, uid)], NOW)
        assert db.query(MedalAward).filter_by(medal=code).count() == 1


def test_fob_threshold_uses_counts_not_list_position(make_user):
    doc = copy.deepcopy(documents()["all:fob"])
    sid = doc["rows"][0]["steam_id"]
    uid = make_user(steam_id=sid)
    store({"all:fob": doc})
    with SessionLocal.begin() as db:
        award_exports(db, [db.get(User, uid)], NOW)
        assert not db.query(MedalAward).filter_by(medal="best_demolition").first()
    doc["rows"][0]["value"] = 50
    store({"all:fob": doc})
    with SessionLocal.begin() as db:
        award_exports(db, [db.get(User, uid)], NOW)
        assert db.query(MedalAward).filter_by(medal="best_demolition").count() == 1


def test_excluded_player_does_not_receive_weekly_award(make_user):
    doc = documents(AFTER)["2026-10-12:cmdwins"]
    uid = make_user(steam_id=doc["rows"][0]["steam_id"])
    store({"2026-10-12:cmdwins": doc}, AFTER)
    with SessionLocal.begin() as db:
        db.add(AwardWeekControl(week="2026-10-12", status="approved", created_at=AFTER))
        db.add(
            AwardCandidateExclusion(
                week="2026-10-12",
                discord_id=str(uid),
                medal="outstanding_service",
                reason="excluded",
                actor_id="1",
                created_at=AFTER,
            )
        )
        db.flush()
        award_exports(db, [db.get(User, uid)], AFTER)
        assert not db.query(MedalAward).first()


def test_progress_shows_export_hours_and_fob_without_fake_zero(make_user):
    data = documents()
    uid = make_user(steam_id=data["2026-10-05:mg"]["rows"][0]["steam_id"])
    store(data)
    with SessionLocal() as db:
        result = progress(db, db.get(User, uid), NOW)
        mg = next(c for c in result["cards"] if c["code"] == "best_machine_gunner")
        # Current week is authoritative for live progress, membership can be unknown.
        assert mg["values"][0]["target_rank"] == 3
        assert mg["values"][0]["available"] is True
        assert "2026-10-12" in mg["values"][0]["label"]
        demolition = next(c for c in result["cards"] if c["code"] == "best_demolition")
        assert demolition["values"][0]["value"] is None


def test_preview_discovers_new_weekly_candidates_without_issuing(make_user):
    from unittest.mock import patch

    doc = documents(AFTER)["2026-10-12:mg"]
    uid = make_user(steam_id=doc["rows"][0]["steam_id"])
    store({"2026-10-12:mg": doc}, AFTER)
    with SessionLocal.begin() as db, patch("admin_hub.utcnow", return_value=AFTER):
        rows = _preview(db, "2026-10-12")
        assert any(
            r["user_id"] == uid and r["medal"] == "best_machine_gunner" for r in rows
        )
        assert not db.query(MedalAward).filter_by(medal="best_machine_gunner").first()


def test_exports_work_when_sqstat_is_disabled(make_user):
    doc = documents()["2026-10-05:kills"]
    make_user(steam_id=doc["rows"][0]["steam_id"])
    store({"2026-10-05:kills": doc})
    with SessionLocal.begin() as db:
        db.add(
            AutomationSetting(
                key="source_sqstat_enabled", value="false", updated_at=NOW
            )
        )
        db.add(AwardWeekControl(week="2026-10-05", status="approved", created_at=NOW))
        db.flush()
        evaluate(db, NOW)
        assert db.query(MedalAward).filter_by(medal="gold_star").count() == 1


def test_infantry_only_sl_dump_cannot_prove_overall_sl_top(make_user):
    doc = documents(AFTER)["2026-10-12:slwins"]
    uid = make_user(steam_id=doc["rows"][0]["steam_id"])
    store({"2026-10-12:slwins": doc}, AFTER)
    with SessionLocal.begin() as db:
        covered = award_exports(db, [db.get(User, uid)], AFTER)
        assert ("2026-10-12", "slwins") not in covered
        assert not db.query(MedalAward).filter_by(medal="squad_leader").first()


def test_protocol_rank_takes_priority_over_conflicting_sqstat_rank(make_user):
    from medals import award_week

    doc = documents()["2026-10-05:kills"]
    sid = doc["rows"][2]["steam_id"]
    uid = make_user(steam_id=sid)
    store({"2026-10-05:kills": doc})
    with SessionLocal.begin() as db:
        user = db.get(User, uid)
        db.info["protocol_export_primary"] = award_exports(db, [user], NOW)
        award_week(
            db,
            user,
            "2026-10-05",
            {
                "week": "2026-10-05",
                "complete": True,
                "ranks": {"kills": {sid: {"rank": 1, "value": 999}}},
            },
        )
        codes = {a.medal for a in db.query(MedalAward)}
        assert "bronze_star" in codes and "gold_star" not in codes


def test_hourly_exports_throttle_success_and_failure():
    calls = []
    data = documents()
    for doc in data.values():
        doc["file_verified"] = True

    def fetch(_):
        calls.append(1)
        if len(calls) > 1:
            raise ClanUnavailable("temporary")
        return data, []

    collect_exports(NOW, fetch=fetch)
    for hours in (0.1, 0.5, 0.99):
        collect_exports(NOW + timedelta(hours=hours), fetch=fetch)
    assert len(calls) == 1
    collect_exports(NOW + timedelta(hours=1), fetch=fetch)
    collect_exports(NOW + timedelta(hours=1.99), fetch=fetch)
    assert len(calls) == 2
    collect_exports(NOW + timedelta(hours=2), fetch=fetch)
    assert len(calls) == 3
    with SessionLocal() as db:
        assert db.query(ProtocolAwardExport).count() == 23


def test_hourly_public_week_failure_is_persisted():
    from protocol_week_source import (
        ProtocolPublishedHealth,
        ProtocolPublishedWeek,
        PublicWeekUnavailable,
        collect_due_previous,
    )

    with SessionLocal.begin() as db:
        db.query(ProtocolPublishedHealth).delete()
        db.query(ProtocolPublishedWeek).delete()
    calls = []

    def fetch(**_):
        calls.append(1)
        raise PublicWeekUnavailable("temporary")

    for hours in (0, 0.1, 0.99, 1, 1.1):
        collect_due_previous(NOW + timedelta(hours=hours), fetch=fetch)
    assert len(calls) == 2
