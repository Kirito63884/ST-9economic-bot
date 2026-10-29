"""Group visibility, point/range online evidence and safe assembly summaries."""

import json
from datetime import datetime, timedelta

import pytest

import groups_admin as groups
from assembly_attendance import AssemblyAssessment, AssemblyReportCheck, AssemblyWindow
from conftest import wallet
from database import GameHistory, SessionLocal, User
from roster_membership import DiscordMemberRoleSnapshot, RosterMembership
from squad_monitor import SERVER_IDS
from squad_play_history import SquadPlaySession

NOW = datetime(2026, 10, 29, 12)
START = datetime(2026, 10, 24, 16)
END = datetime(2026, 10, 24, 19)
RECON = "group_2"
STEAM = "76561198000000000"
OTHER = "76561198000000001"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(groups, "utcnow", lambda: NOW)
    with SessionLocal.begin() as db:
        for model in (
            DiscordMemberRoleSnapshot,
            RosterMembership,
            SquadPlaySession,
            AssemblyAssessment,
            AssemblyReportCheck,
            AssemblyWindow,
        ):
            db.query(model).delete()


@pytest.fixture
def admin(make_user, login_as):
    uid = make_user(101, is_admin=True, is_senior_admin=True)
    for user_id, steam in ((102, STEAM), (103, OTHER), (104, "76561198000000002")):
        make_user(user_id, steam_id=steam)
        with SessionLocal.begin() as db:
            db.add(
                DiscordMemberRoleSnapshot(
                    discord_id=str(user_id),
                    role_ids=json.dumps(["1457398618192351334"]),
                    updated_at=NOW,
                )
            )
    return login_as(uid)


def play(steam=STEAM, start=None, end=None, server=None):
    start = start or START + timedelta(minutes=10)
    end = end or START + timedelta(minutes=20)
    with SessionLocal.begin() as db:
        db.add(
            SquadPlaySession(
                steam_id=steam,
                server_id=server or SERVER_IDS[0],
                server_name="Тестовый сервер",
                started_at=start,
                last_seen_at=end,
                ended_at=end,
                sample_count=5,
            )
        )


def event(*, voice=True, bm=False, reports_ready=True, expected=None):
    health = {sid: 360 for sid in SERVER_IDS} if bm else {}
    if voice:
        health["discord_voice"] = {
            "first": START.isoformat(),
            "last": (END - timedelta(minutes=1)).isoformat(),
            "max_gap": 60,
            "count": 180,
        }
    with SessionLocal.begin() as db:
        db.add(
            AssemblyWindow(
                event_date="2026-10-24",
                starts_at=START,
                ends_at=END,
                first_sample=START,
                last_sample=END - timedelta(seconds=30),
                max_gap_seconds=30,
                sample_count=360 if bm else 0,
                healthy_servers=json.dumps(health),
                attended="{}",
                member_ids=json.dumps(expected or ["102", "103", "104"]),
                status="completed",
            )
        )
        db.add(
            AssemblyReportCheck(
                event_date="2026-10-24",
                status="ready" if reports_ready else "error",
                thread_id="123",
                member_ids=json.dumps(expected or ["102", "103", "104"]),
                member_names="{}",
                reports=json.dumps({"103": (START - timedelta(hours=2)).isoformat()}),
                checked_at=END,
            )
        )


def assembly(client):
    response = client.get(f"/api/admin/groups/{RECON}/assembly")
    assert response.status_code == 200, response.text
    return response.json()


def test_catalog_contains_every_group_and_uses_current_roles(admin):
    result = admin.get("/api/admin/groups")
    assert result.status_code == 200
    assert "no-store" in result.headers["cache-control"]
    catalog = {g["name"]: g for g in result.json()["groups"]}
    assert catalog["Разведывательная группа"]["members_count"] == 3
    assert all(
        name in catalog
        for name in (
            "Десантно-штурмовая группа",
            "Бронетанковая группа",
            "Группа обеспечения",
            "Учебная группа",
        )
    )
    with SessionLocal.begin() as db:
        db.get(DiscordMemberRoleSnapshot, "102").role_ids = '["1467925735674216478"]'
    catalog = {g["name"]: g for g in admin.get("/api/admin/groups").json()["groups"]}
    assert catalog["Разведывательная группа"]["members_count"] == 2
    assert catalog["Бронетанковая группа"]["members_count"] == 1


@pytest.mark.parametrize(
    "role", ["anonymous", "player", "junior", "flag_only", "archived"]
)
def test_private_group_routes_deny_other_roles(make_user, login_as, client, role):
    if role != "anonymous":
        options = {
            "is_admin": role in ("junior", "archived"),
            "is_senior_admin": role in ("flag_only", "archived"),
        }
        if role == "archived":
            options["deleted_at"] = NOW
        client = login_as(make_user(**options))
    for path in (
        "/api/admin/groups",
        f"/api/admin/groups/{RECON}/online?at=2026-10-24T19:15:00%2B03:00",
        f"/api/admin/groups/{RECON}/assembly",
    ):
        assert client.get(path).status_code == 403


@pytest.mark.parametrize("role", ["senior", "chief", "owner"])
def test_group_routes_allow_senior_chief_owner(make_user, login_as, monkeypatch, role):
    import main

    uid = make_user(
        is_admin=True, is_senior_admin=role == "senior", is_chief_admin=role == "chief"
    )
    if role == "owner":
        monkeypatch.setattr(main, "ADMIN_DISCORD_ID", str(uid))
    assert login_as(uid).get("/api/admin/groups").status_code == 200


def test_online_point_and_period_and_group_filter(admin):
    play()
    play(OTHER, start=START + timedelta(minutes=25), end=START + timedelta(minutes=30))
    with SessionLocal.begin() as db:
        db.get(DiscordMemberRoleSnapshot, "103").role_ids = '["1467925735674216478"]'
    path = f"/api/admin/groups/{RECON}/online"
    result = admin.get(path, params={"at": "2026-10-24T19:15:00+03:00"}).json()
    assert (
        result["members_count"] == 1
        and result["servers"][0]["members"][0]["user_id"] == 102
    )
    assert (
        admin.get(path, params={"at": "2026-10-24T19:25:00+03:00"}).json()[
            "members_count"
        ]
        == 0
    )
    period = {"start": "2026-10-24T19:00:00+03:00", "end": "2026-10-24T19:40:00+03:00"}
    assert admin.get(path, params=period).json()["members_count"] == 1
    # Entry exactly at the exclusive end is outside the chosen interval.
    assert (
        admin.get(path, params={**period, "end": "2026-10-24T19:10:00+03:00"}).json()[
            "members_count"
        ]
        == 0
    )
    with SessionLocal.begin() as db:
        row = db.query(SquadPlaySession).filter_by(steam_id=STEAM).one()
        row.ended_at = None
    assert (
        admin.get(path, params={"at": "2026-10-24T21:00:00+03:00"}).json()[
            "members_count"
        ]
        == 0
    )


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"start": "2026-10-24T16:00:00Z"},
        {
            "at": "2026-10-24T16:00:00Z",
            "start": "2026-10-24T16:00:00Z",
            "end": "2026-10-24T17:00:00Z",
        },
        {"start": "2026-10-24T17:00:00Z", "end": "2026-10-24T16:00:00Z"},
        {"start": "2026-10-24T16:00:00Z", "end": "2026-10-24T16:00:00Z"},
        {"at": "2030-10-24T16:00:00Z"},
        {"at": "bad"},
    ],
)
def test_invalid_online_query_rejected(admin, params):
    assert (
        admin.get(f"/api/admin/groups/{RECON}/online", params=params).status_code == 400
    )


def test_invalid_group_is_not_found(admin):
    assert admin.get("/api/admin/groups/fake/assembly").status_code == 404


@pytest.mark.parametrize(
    "now,day",
    [
        (datetime(2026, 10, 24, 15), "2026-10-17"),
        (datetime(2026, 10, 24, 18, 59, 59), "2026-10-17"),
        (datetime(2026, 10, 24, 19), "2026-10-24"),
        (datetime(2026, 10, 25, 0), "2026-10-24"),
        (NOW, "2026-10-24"),
    ],
)
def test_last_completed_saturday_has_exact_moscow_hours(now, day):
    date, start, end = groups.latest_assembly(now)
    assert (
        date == day
        and start.hour == 16
        and end.hour == 19
        and end - start == timedelta(hours=3)
    )


def test_assembly_present_reported_and_absent_are_separate_and_read_only(admin):
    event()
    play()
    result = assembly(admin)
    statuses = {m["user_id"]: m["status"] for m in result["members"]}
    assert statuses == {
        102: "present",
        103: "absent_with_report",
        104: "absent_without_report",
    }
    assert (
        result["date"] == "2026-10-24" and result["starts_at"] == "2026-10-24T16:00:00Z"
    )
    assert wallet(104) == 1000
    with SessionLocal() as db:
        assert (
            db.query(GameHistory).count() == 0
            and db.query(AssemblyAssessment).count() == 0
        )


def test_partial_sources_never_prove_absence(admin):
    event(voice=False, bm=False)
    play()
    result = assembly(admin)
    assert (
        result["counts"]["present"] == 1 and result["counts"]["absent_with_report"] == 1
    )
    assert (
        result["counts"]["absent_without_report"] == 0
        and result["counts"]["unknown"] == 1
    )


def test_unchecked_reports_never_prove_unexcused_absence(admin):
    event(reports_ready=False)
    assert assembly(admin)["counts"]["absent_without_report"] == 0


def test_missing_sources_and_reports_show_unknown(admin):
    result = assembly(admin)
    assert result["members"] == []
    assert result["membership_complete"] is False
    assert result["counts"]["absent_without_report"] == 0


def test_voice_presence_is_valid_and_afk_not_inferred(admin):
    event()
    with SessionLocal.begin() as db:
        row = db.get(AssemblyWindow, "2026-10-24")
        row.attended = json.dumps(
            {
                "discord:102": {
                    "at": (START + timedelta(minutes=2)).isoformat(),
                    "source": "discord_voice",
                    "channel_name": "Сбор",
                }
            }
        )
    result = assembly(admin)
    assert (
        next(m for m in result["members"] if m["user_id"] == 102)["status"] == "present"
    )


def test_late_report_is_not_accepted_and_present_report_is_marked(admin):
    event()
    play(OTHER)
    with SessionLocal.begin() as db:
        row = db.get(AssemblyReportCheck, "2026-10-24")
        reports = json.loads(row.reports)
        reports["104"] = (START + timedelta(minutes=1)).isoformat()
        row.reports = json.dumps(reports)
    result = assembly(admin)
    member = next(m for m in result["members"] if m["user_id"] == 103)
    assert member["status"] == "present" and member["has_report"] is True
    assert (
        next(m for m in result["members"] if m["user_id"] == 104)["status"]
        == "absent_without_report"
    )


def test_unlinked_member_without_voice_coverage_is_unknown(admin):
    event(voice=False, bm=True)
    with SessionLocal.begin() as db:
        db.get(User, 104).steam_id = None
    assert (
        next(m for m in assembly(admin)["members"] if m["user_id"] == 104)["status"]
        == "unknown"
    )


def test_different_assembly_hours_do_not_support_absence(admin):
    event()
    with SessionLocal.begin() as db:
        db.get(AssemblyWindow, "2026-10-24").starts_at += timedelta(hours=1)
    result = assembly(admin)
    assert (
        result["window_matches"] is False
        and result["counts"]["absent_without_report"] == 0
    )


def test_non_monitored_server_is_not_assembly_presence(admin):
    event()
    play(server="999")
    assert (
        next(m for m in assembly(admin)["members"] if m["user_id"] == 102)["status"]
        == "absent_without_report"
    )


def test_current_member_not_in_historical_roster_is_not_marked_absent(admin):
    event(expected=["102", "103"])
    assert all(m["user_id"] != 104 for m in assembly(admin)["members"])


def test_overall_assembly_is_unique_across_multiple_group_roles(admin):
    event()
    play()
    with SessionLocal.begin() as db:
        db.get(
            DiscordMemberRoleSnapshot, "102"
        ).role_ids = '["1457398618192351334","1446836770141507696"]'
    response = admin.get("/api/admin/groups/all/assembly")
    assert response.status_code == 200
    result = response.json()
    assert result["counts"]["present"] == 1
    assert result["counts"]["absent_with_report"] == 1
    assert result["counts"]["absent_without_report"] == 1
    assert len({m["user_id"] for m in result["members"]}) == len(result["members"])
    all_group = next(
        g for g in admin.get("/api/admin/groups").json()["groups"] if g["id"] == "all"
    )
    assert all_group["members_count"] == 4


def test_training_role_is_a_full_group_for_catalog_online_and_assembly(admin):
    with SessionLocal.begin() as db:
        db.get(DiscordMemberRoleSnapshot, "102").role_ids = '["1485577945316720780"]'
    catalog = admin.get("/api/admin/groups").json()["groups"]
    training = next(g for g in catalog if g["name"] == "Учебная группа")
    assert training["members_count"] == 1
    play()
    event()
    online = admin.get(
        f"/api/admin/groups/{training['id']}/online",
        params={"at": "2026-10-24T19:15:00+03:00"},
    ).json()
    assert (
        online["members_count"] == 1
        and online["servers"][0]["members"][0]["user_id"] == 102
    )
    result = admin.get(f"/api/admin/groups/{training['id']}/assembly").json()
    assert [(m["user_id"], m["status"]) for m in result["members"]] == [
        (102, "present")
    ]
    # This web-only mapping must not mutate the existing roster/export catalog.
    from assembly_export import GROUPS

    assert "1485577945316720780" not in next(
        ids for name, ids in GROUPS if name == "Учебная группа"
    )


def test_game_presence_and_report_do_not_admit_nonmember_to_assembly(admin):
    event(expected=["102", "103"])
    play(steam="76561198000000002")
    with SessionLocal.begin() as db:
        row = db.get(AssemblyReportCheck, "2026-10-24")
        row.member_ids = json.dumps(["102", "103", "104"])
        reports = json.loads(row.reports)
        reports["104"] = (START - timedelta(hours=1)).isoformat()
        row.reports = json.dumps(reports)
    result = assembly(admin)
    assert all(m["user_id"] != 104 for m in result["members"])
    assert sum(result["counts"].values()) == 2


def test_report_before_event_does_not_prove_membership_during_event(admin):
    event(expected=["102"])
    result = assembly(admin)
    assert [m["user_id"] for m in result["members"]] == [102]


def test_after_event_roster_is_not_historical_membership(admin):
    event()
    with SessionLocal.begin() as db:
        row = db.get(AssemblyWindow, "2026-10-24")
        row.member_ids = None
        health = json.loads(row.healthy_servers)
        health["roster_basis"] = "after_event"
        health["roster"] = {"102": {}, "103": {}, "104": {}}
        row.healthy_servers = json.dumps(health)
    result = assembly(admin)
    assert result["members"] == [] and result["membership_complete"] is False


def test_during_event_frozen_roster_proves_membership_without_member_ids(admin):
    event()
    with SessionLocal.begin() as db:
        row = db.get(AssemblyWindow, "2026-10-24")
        row.member_ids = None
        health = json.loads(row.healthy_servers)
        health["roster_basis"] = "during_event"
        health["roster"] = {"102": {}}
        row.healthy_servers = json.dumps(health)
    result = assembly(admin)
    assert [m["user_id"] for m in result["members"]] == [102]
    assert result["membership_complete"] is True


def test_report_check_during_event_can_prove_membership(admin):
    event()
    with SessionLocal.begin() as db:
        db.get(AssemblyWindow, "2026-10-24").member_ids = None
        check = db.get(AssemblyReportCheck, "2026-10-24")
        check.checked_at = START + timedelta(minutes=30)
        check.member_ids = '["102"]'
    assert [m["user_id"] for m in assembly(admin)["members"]] == [102]


def test_voice_observation_admits_member_who_joined_during_assembly(admin):
    event(expected=["102"])
    with SessionLocal.begin() as db:
        row = db.get(AssemblyWindow, "2026-10-24")
        row.attended = json.dumps(
            {
                "discord:104": {
                    "at": (START + timedelta(minutes=30)).isoformat(),
                    "source": "discord_voice",
                    "channel_name": "Сбор",
                }
            }
        )
    result = assembly(admin)
    assert (
        next(m for m in result["members"] if m["user_id"] == 104)["status"] == "present"
    )
    assert all(m["user_id"] != 103 for m in result["members"])


def test_voice_outside_window_does_not_admit_nonmember(admin):
    event(expected=["102"])
    with SessionLocal.begin() as db:
        row = db.get(AssemblyWindow, "2026-10-24")
        row.attended = json.dumps(
            {
                "discord:104": {
                    "at": END.isoformat(),
                    "source": "discord_voice",
                    "channel_name": "Сбор",
                }
            }
        )
    assert [m["user_id"] for m in assembly(admin)["members"]] == [102]


def test_known_empty_assembly_roster_does_not_fall_back_to_current_members(admin):
    event()
    with SessionLocal.begin() as db:
        db.get(AssemblyWindow, "2026-10-24").member_ids = "[]"
    result = assembly(admin)
    assert result["members"] == [] and result["membership_complete"] is True
