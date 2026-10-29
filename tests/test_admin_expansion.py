"""Monthly evidence bounds, private wallet questions and source progress safety."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import groups_admin
from assembly_attendance import AssemblyAssessment, AssemblyReportCheck, AssemblyWindow
from database import GameHistory, SessionLocal, User
from group_analytics import GroupRosterSnapshot, month_bounds, roster_dynamics
from medals import MedalMonitor
from roster_membership import DiscordMemberRoleSnapshot
from squad_monitor import SERVER_IDS
from squad_play_history import SquadPlaySession
from statistics_freshness import collection_progress
from wallet_history import WalletQuestion

NOW = datetime(2026, 10, 29, 12)
STEAM = "76561198000000000"


@pytest.fixture(autouse=True)
def clean_expansion(monkeypatch):
    monkeypatch.setattr(groups_admin, "utcnow", lambda: NOW)
    with SessionLocal.begin() as db:
        for model in (
            AssemblyWindow,
            AssemblyAssessment,
            AssemblyReportCheck,
            GroupRosterSnapshot,
            WalletQuestion,
            SquadPlaySession,
            DiscordMemberRoleSnapshot,
        ):
            db.query(model).delete()


def operation(
    uid, amount=50, result="admin_adjustment", details="Проверка", created=None
):
    with SessionLocal.begin() as db:
        row = GameHistory(
            user_id=uid,
            game_type="admin",
            result=result,
            amount=amount,
            details=details,
            created_at=created or NOW,
        )
        db.add(row)
        db.flush()
        return row.id


def test_monthly_clips_moscow_boundaries_merges_overlap_and_stops_open_session(
    make_user, login_as
):
    admin = make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102, steam_id=STEAM)
    with SessionLocal.begin() as db:
        db.add(
            DiscordMemberRoleSnapshot(
                discord_id="102", role_ids='["1457398618192351334"]', updated_at=NOW
            )
        )
        for server, first, last, ended in (
            (SERVER_IDS[0], datetime(2026, 9, 30, 20), datetime(2026, 9, 30, 22), None),
            (
                SERVER_IDS[1],
                datetime(2026, 9, 30, 21, 30),
                datetime(2026, 9, 30, 23),
                datetime(2026, 9, 30, 23),
            ),
            ("unmonitored", datetime(2026, 10, 1), datetime(2026, 10, 5), None),
        ):
            db.add(
                SquadPlaySession(
                    steam_id=STEAM,
                    server_id=server,
                    server_name=server,
                    started_at=first,
                    last_seen_at=last,
                    ended_at=ended,
                    sample_count=1,
                )
            )
    client = login_as(admin)
    data = client.get("/api/admin/groups/group_2/monthly?month=2026-10").json()
    assert (
        data["observed_hours"] == 2
    )  # 21:00–23:00 UTC, not an indefinite open session.
    assert data["members_count"] == 1 and data["observed_members"] == 1
    assert len(data["servers"]) == 2
    assert data["attendance_percent"] is None
    assert data["roster"]["available"] is False
    assert data["starts_at"] == "2026-09-30T21:00:00Z"
    assert (
        client.get("/api/admin/groups/group_2/monthly?month=2027-01").status_code == 400
    )
    assert (
        client.get("/api/admin/groups/group_2/monthly?month=2026-1").status_code == 400
    )
    junior = login_as(make_user(103, is_admin=True))
    assert (
        junior.get("/api/admin/groups/group_2/monthly?month=2026-10").status_code == 403
    )
    assert junior.get("/api/admin/assemblies/summary").status_code == 403


def test_roster_history_does_not_invent_month_start_and_counts_observed_changes():
    start, end = month_bounds("2026-10", NOW)
    with SessionLocal.begin() as db:
        for stamp, ids in (
            (start - timedelta(days=5), ["old"]),
            (start + timedelta(days=2), ["1", "2"]),
            (start + timedelta(days=3), ["2", "3"]),
        ):
            db.add(
                GroupRosterSnapshot(
                    observed_at=stamp, payload=json.dumps({"group_2": ids})
                )
            )
    with SessionLocal() as db:
        result = roster_dynamics(db, "group_2", start, end)
    assert not result["has_month_start"]
    assert result["start_count"] == 2 and result["end_count"] == 2
    assert result["joined"] == 1 and result["left"] == 1


def test_wallet_only_own_financial_records_and_cursor_filters(make_user, login_as):
    uid = make_user(101)
    other = make_user(102)
    for n in range(55):
        operation(uid, details=f"Запись {n}")
    operation(uid, 100, result="note_updated", details="private secret")
    operation(other, 500, details="another account")
    client = login_as(uid)
    first = client.get("/api/wallet/history").json()
    assert first["summary"]["count"] == 55 and len(first["entries"]) == 50
    assert first["summary"]["credited"] == 2750
    next_page = client.get(
        "/api/wallet/history", params={"before": first["next_before"]}
    ).json()
    assert len(next_page["entries"]) == 5
    assert not {r["id"] for r in first["entries"]} & {
        r["id"] for r in next_page["entries"]
    }
    assert client.get("/api/wallet/history?q=secret").json()["summary"]["count"] == 0
    assert (
        client.get("/api/wallet/history?user_id=102").json()["summary"]["count"] == 55
    )
    assert client.get("/api/wallet/history?category=invalid").status_code == 400
    assert client.get("/api/wallet/history?before=" + "9" * 100).status_code == 400
    assert client.get("/api/wallet/history?date_to=9999-12-31").status_code == 200
    assert client.get("/api/wallet/history?date_from=0001-01-01").status_code == 400
    assert (
        client.get(
            "/api/wallet/history?date_from=2026-10-30&date_to=2026-10-01"
        ).status_code
        == 400
    )
    assert "no-store" in client.get("/wallet").headers["cache-control"]


def test_wallet_moscow_date_and_award_assembly_filters(make_user, login_as):
    uid = make_user()
    before = operation(uid, created=datetime(2026, 10, 23, 20, 59))
    actual = operation(
        uid, -100, result="assembly_penalty", created=datetime(2026, 10, 23, 21)
    )
    operation(
        uid,
        100,
        details="Возврат штрафа за сбор 2026-10-24 по одобренной апелляции",
        created=datetime(2026, 10, 24),
    )
    client = login_as(uid)
    data = client.get(
        "/api/wallet/history?category=assemblies&date_from=2026-10-24&date_to=2026-10-24"
    ).json()
    assert data["summary"]["count"] == 2
    assert before not in {r["id"] for r in data["entries"]}
    assert actual in {r["id"] for r in data["entries"]}
    assert (
        client.get("/api/wallet/history?direction=negative").json()["summary"][
            "debited"
        ]
        == -100
    )


def test_wallet_question_ownership_idempotency_csrf_and_answer_without_money_change(
    make_user, login_as
):
    uid = make_user(101)
    other = make_user(102)
    admin = make_user(103, is_admin=True)
    own = operation(uid)
    foreign = operation(other)
    client = login_as(uid)
    assert (
        client.post(
            "/api/wallet/questions",
            json={"operation_id": foreign, "reason": "Проверьте"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/wallet/questions",
            headers={"X-ST9-CSRF": "wrong"},
            json={"operation_id": own, "reason": "Проверьте"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/wallet/questions",
            json={"operation_id": 10**100, "reason": "Проверка"},
        ).status_code
        == 400
    )
    payload = {"operation_id": own, "reason": "Почему эта сумма?"}
    question = client.post("/api/wallet/questions", json=payload)
    assert question.status_code == 200, question.text
    qid = question.json()["id"]
    assert client.post("/api/wallet/questions", json=payload).json()["id"] == qid
    assert client.get("/api/admin/wallet/questions").status_code == 403
    administrator = login_as(admin)
    assert (
        administrator.get("/api/admin/wallet/questions").json()["items"][0]["reason"]
        == payload["reason"]
    )
    assert (
        administrator.post(
            f"/api/admin/wallet/questions/{qid}/answer",
            json={"answer": "Начислено за помощь группе."},
        ).status_code
        == 200
    )
    assert (
        administrator.post(
            f"/api/admin/wallet/questions/{qid}/answer", json={"answer": "Второй ответ"}
        ).status_code
        == 409
    )
    result = login_as(uid).get("/api/wallet/history").json()["entries"][0]
    assert result["question"]["answer"] == "Начислено за помощь группе."
    assert (
        login_as(other).get("/api/wallet/history").json()["entries"][0]["question"]
        is None
    )
    with SessionLocal() as db:
        assert db.get(User, uid).balance == 1000
        assert db.query(GameHistory).count() == 2
        assert db.query(WalletQuestion).count() == 1


def test_wallet_authentication_and_nonfinancial_operation_guard(
    make_user, login_as, client
):
    assert client.get("/api/wallet/history").status_code == 401
    uid = make_user()
    log = operation(uid, 0, result="steam_set")
    assert (
        login_as(uid)
        .post(
            "/api/wallet/questions", json={"operation_id": log, "reason": "Проверить"}
        )
        .status_code
        == 404
    )


def test_collection_progress_keeps_success_date_and_separates_phase():
    with SessionLocal.begin() as db:
        row = db.get(MedalMonitor, 1)
        if row is None:
            row = MedalMonitor(id=1)
            db.add(row)
        row.payload = json.dumps(
            {
                "status": "collecting",
                "phase": "profiles",
                "profiles_total": 100,
                "profiles_done": 42,
                "profiles_failed": 2,
                "current_profile_index": 43,
                "checked_at": "2026-10-06T23:38:32Z",
                "current_match": 123,
            }
        )
        row.updated_at = NOW
    with SessionLocal() as db:
        data = collection_progress(
            db.get(MedalMonitor, 1),
            NOW.replace(tzinfo=timezone.utc).timestamp(),
        )
    assert data["profile_percent"] == 42 and data["profiles_failed"] == 2
    assert data["checked_at"] == "2026-10-06T23:38:32Z"
    assert data["current_profile_index"] == 43 and data["state_age_seconds"] == 0


def test_monthly_attendance_reports_and_coverage_are_counted_separately(
    make_user, login_as
):
    admin = make_user(101, is_admin=True, is_senior_admin=True)
    for uid in (102, 103, 104):
        make_user(uid, steam_id=STEAM if uid == 102 else None)
    start = datetime(2026, 10, 24, 16)
    end = start + timedelta(hours=3)
    with SessionLocal.begin() as db:
        for uid in (102, 103, 104):
            db.add(
                DiscordMemberRoleSnapshot(
                    discord_id=str(uid),
                    role_ids='["1457398618192351334"]',
                    updated_at=NOW,
                )
            )
        db.add(
            AssemblyWindow(
                event_date="2026-10-24",
                starts_at=start,
                ends_at=end,
                first_sample=start,
                last_sample=end - timedelta(seconds=30),
                max_gap_seconds=30,
                member_ids='["102","103","104"]',
                attended="{}",
                status="completed",
                healthy_servers=json.dumps(
                    {
                        "discord_voice": {
                            "first": start.isoformat(),
                            "last": (end - timedelta(minutes=1)).isoformat(),
                            "max_gap": 60,
                            "count": 180,
                        }
                    }
                ),
            )
        )
        db.add(
            AssemblyReportCheck(
                event_date="2026-10-24",
                status="ready",
                thread_id="123",
                member_ids='["102","103","104"]',
                member_names="{}",
                checked_at=end,
                reports=json.dumps(
                    {"103": (start - timedelta(minutes=30)).isoformat()}
                ),
            )
        )
        db.add(
            SquadPlaySession(
                steam_id=STEAM,
                server_id=SERVER_IDS[0],
                server_name="Test",
                started_at=start,
                last_seen_at=start + timedelta(minutes=20),
                ended_at=start + timedelta(minutes=20),
                sample_count=3,
            )
        )
    client = login_as(admin)
    data = client.get("/api/admin/groups/group_2/monthly?month=2026-10").json()
    assert data["attendance"]["present"] == 1
    assert data["attendance"]["absent_with_report"] == 1
    assert data["attendance"]["absent_without_report"] == 1
    assert data["attendance_percent"] == 50
    summary = client.get("/api/admin/assemblies/summary?date=2026-10-24").json()
    assert (
        summary["counts"]["present"] == 1
        and summary["counts"]["absent_without_report"] == 1
    )
    assert (
        client.get("/api/admin/assemblies/summary?date=2026-10-23").status_code == 400
    )
    assert (
        client.get("/api/admin/assemblies/summary?date=2026-10-31").status_code == 400
    )


def test_complete_roster_capture_excludes_cargo_and_records_training_group():
    from group_analytics import capture_groups

    members = [
        ("1", "One", "One", {"participant", "1485577945316720780"}),
        ("2", "Two", "Two", {"participant", "cargo", "1457398618192351334"}),
        ("3", "Three", "Three", {"1457398618192351334"}),
    ]
    with SessionLocal.begin() as db:
        capture_groups(db, members, "participant", "cargo", NOW)
    with SessionLocal() as db:
        data = json.loads(db.get(GroupRosterSnapshot, NOW).payload)
    assert data["all"] == ["1"] and data["group_6"] == ["1"] and data["group_2"] == []
