import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from assembly_attendance import (
    SERVER_IDS,
    AssemblyAssessment,
    AssemblyReportCheck,
    AssemblyWindow,
    AssemblyWorker,
    apply_assessments,
    battlemetrics_coverage,
    bounds,
    coverage,
    observe_snapshot,
    observe_voice_snapshot,
    voice_coverage,
)
from assembly_export import ExportUnavailable, absent_txt
from automation_control import AutomationSetting
from database import SessionLocal, User
from roster_membership import DiscordMemberRoleSnapshot

DAY = "2026-10-10"
START, END = bounds(DAY)
STEAM = "76561198000000101"


@pytest.fixture(autouse=True)
def clean_assembly():
    with SessionLocal.begin() as db:
        for model in (
            AssemblyAssessment,
            AssemblyReportCheck,
            AssemblyWindow,
            DiscordMemberRoleSnapshot,
            AutomationSetting,
        ):
            db.query(model).delete()


def event(voice=True, bm=False, attended=None, members=("101", "102"), reports=None):
    health = {sid: 360 for sid in SERVER_IDS} if bm else {}
    if voice:
        health["discord_voice"] = {
            "first": START.isoformat(),
            "last": (END - timedelta(seconds=60)).isoformat(),
            "count": 180,
            "max_gap": 60,
        }
    health["roster"] = {
        "101": {"name": "Absent", "roles": ["1457398618192351334"]},
        "102": {"name": "Present", "roles": ["1468548988826292409"]},
    }
    health["roster_basis"] = "during_event"
    with SessionLocal.begin() as db:
        db.add(
            AssemblyWindow(
                event_date=DAY,
                starts_at=START,
                ends_at=END,
                first_sample=START,
                last_sample=END - timedelta(seconds=30),
                max_gap_seconds=30,
                sample_count=360 if bm else 0,
                healthy_servers=json.dumps(health),
                attended=json.dumps(attended or {}),
                member_ids=json.dumps(list(members)),
                status="collecting",
            )
        )
        db.add(
            AssemblyReportCheck(
                event_date=DAY,
                status="ready",
                thread_id="999",
                member_ids=json.dumps(list(members)),
                member_names="{}",
                reports=json.dumps(reports or {}),
                checked_at=END,
            )
        )


def voice_presence():
    return {
        "at": (START + timedelta(minutes=1)).isoformat(),
        "source": "discord_voice",
        "channel_id": "55",
        "channel_name": "Сбор",
    }


def test_voice_fallback_prevents_penalty_and_absence_export(make_user):
    make_user(uid=101, steam_id=STEAM)
    make_user(uid=102, steam_id="76561198000000102")
    event(attended={"discord:102": voice_presence()})
    with SessionLocal.begin() as db:
        result = apply_assessments(
            db, DAY, {"101", "102"}, {}, now=END + timedelta(minutes=5), defer=True
        )
        assert result["penalized"] == 1 and result["charged"] == 0
    with SessionLocal() as db:
        assert (
            db.query(AssemblyAssessment).filter_by(discord_id="102").one().outcome
            == "present_voice"
        )
        assert db.get(User, 102).balance == 1000
        result = absent_txt(db, DAY, now=END + timedelta(minutes=6))
        assert "РАЗВЕДЫВАТЕЛЬНАЯ ГРУППА" in result and "Absent" in result
        assert "Present" not in result and "Discord ID: 102" not in result


def test_voice_can_confirm_presence_without_steam_or_site_profile(make_user):
    make_user(uid=101)
    event(attended={"discord:101": voice_presence(), "discord:102": voice_presence()})
    with SessionLocal.begin() as db:
        apply_assessments(db, DAY, {"101", "102"}, {}, now=END, defer=True)
    with SessionLocal() as db:
        assert {a.outcome for a in db.query(AssemblyAssessment)} == {"present_voice"}
        assert "Всего: 0" in absent_txt(db, DAY, now=END)


def test_bm_positive_presence_wins_and_report_excludes_absent(make_user):
    make_user(uid=101, steam_id=STEAM)
    event(
        voice=False,
        bm=True,
        attended={STEAM: {"at": START.isoformat(), "server_id": SERVER_IDS[0]}},
        reports={"102": START.isoformat()},
    )
    with SessionLocal() as db:
        assert "Всего: 0" in absent_txt(db, DAY, now=END)


def test_no_report_does_not_mean_absent_with_unknown_personal_data(make_user):
    make_user(uid=101)
    event(voice=False, bm=True, members=("101",))
    with SessionLocal() as db:
        text = absent_txt(db, DAY, now=END)
        assert "Всего: 0" in text and "персональных данных: 1" in text
        assert "Discord ID: 101" not in text


def test_incomplete_sources_never_fine_or_export_as_absent(make_user):
    make_user(uid=101, steam_id=STEAM)
    event(voice=False, bm=False)
    with SessionLocal.begin() as db:
        assert apply_assessments(db, DAY, {"101"}, {}, now=END, defer=True)[
            "source_incomplete"
        ]
    with SessionLocal() as db:
        assert db.query(AssemblyAssessment).count() == 0
        with pytest.raises(ExportUnavailable, match="Недостаточно записей"):
            absent_txt(db, DAY, now=END)
        assert db.get(User, 101).balance == 1000


def test_export_requires_completed_window_and_verified_reports():
    event()
    with SessionLocal() as db, pytest.raises(ExportUnavailable, match="не завершён"):
        absent_txt(db, DAY, now=START)
    with SessionLocal.begin() as db:
        db.get(AssemblyReportCheck, DAY).status = "error"
    with SessionLocal() as db, pytest.raises(ExportUnavailable, match="Рапорты"):
        absent_txt(db, DAY, now=END)


def test_historical_groups_are_frozen_even_after_transfer(make_user):
    make_user(uid=101, display_name="New name")
    event(members=("101",))
    with SessionLocal.begin() as db:
        db.add(
            DiscordMemberRoleSnapshot(
                discord_id="101", role_ids='["1468548988826292409"]', updated_at=END
            )
        )
    with SessionLocal() as db:
        text = absent_txt(db, DAY, now=END)
        assert "РАЗВЕДЫВАТЕЛЬНАЯ ГРУППА" in text and "Absent" in text
        assert "ГРУППА ОБЕСПЕЧЕНИЯ" not in text and "New name" not in text


def test_live_voice_excludes_configured_afk_and_named_afk():
    channels = [
        SimpleNamespace(id=1, name="Away"),
        SimpleNamespace(id=2, name="AFK"),
        SimpleNamespace(id=3, name="АФК"),
        SimpleNamespace(id=4, name="Общий"),
    ]
    for i, channel in enumerate(channels):
        channel.voice_states = {i + 101: SimpleNamespace(channel=channel)}
    guild = SimpleNamespace(
        afk_channel=channels[0], voice_channels=channels, stage_channels=[]
    )
    bot = SimpleNamespace(is_ready=lambda: True, get_guild=lambda _: guild)
    worker = AssemblyWorker(bot)
    worker.guild_id = "7"
    assert asyncio.run(worker.capture_voice(START))
    with SessionLocal() as db:
        attended = json.loads(db.get(AssemblyWindow, DAY).attended)
        assert set(attended) == {"discord:104"}
    bot.is_ready = lambda: False
    assert not asyncio.run(worker.capture_voice(START + timedelta(minutes=1)))


def test_voice_coverage_tracks_gaps_and_does_not_inflate_same_minute():
    assert observe_voice_snapshot({}, START)
    assert not observe_voice_snapshot({}, START + timedelta(seconds=10))
    assert observe_voice_snapshot({}, START + timedelta(minutes=10))
    with SessionLocal() as db:
        row = db.get(AssemblyWindow, DAY)
        assert json.loads(row.healthy_servers)["discord_voice"]["count"] == 2
        ok, problems = voice_coverage(row)
        assert not ok and "discord_monitor_gap" in problems
        assert not coverage(row)[0]
    assert not observe_voice_snapshot({"101": {"id": "1"}}, END)


def test_bm_unresolved_steam_identities_are_not_absence_evidence():
    servers = [
        {"id": sid, "status": "ready", "pending": 1, "roster": []} for sid in SERVER_IDS
    ]
    observe_snapshot(servers, START)
    with SessionLocal() as db:
        row = db.get(AssemblyWindow, DAY)
        assert all(not json.loads(row.healthy_servers).get(sid) for sid in SERVER_IDS)
        assert not battlemetrics_coverage(row)[0]


def test_bm_and_discord_observers_merge_without_losing_evidence():
    servers = [
        {"id": sid, "status": "ready", "pending": 0, "roster": [{"steam_ids": [STEAM]}]}
        for sid in SERVER_IDS
    ]
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(observe_snapshot, servers, START)
        b = pool.submit(
            observe_voice_snapshot, {"102": {"id": "9", "name": "Общий"}}, START
        )
        assert a.result() and b.result()
    with SessionLocal() as db:
        row = db.get(AssemblyWindow, DAY)
        assert {STEAM, "discord:102"} <= json.loads(row.attended).keys()
        assert "discord_voice" in json.loads(row.healthy_servers)


def test_capture_freezes_names_and_roles_during_assembly():
    observe_voice_snapshot({}, START)
    bot = SimpleNamespace()
    worker = AssemblyWorker(bot)
    worker.role_id = "7"
    raw = [
        {
            "user": {"id": "101", "username": "User"},
            "nick": "Old nick",
            "roles": ["7", "1457398618192351334"],
        }
    ]
    worker.members = AsyncMock(return_value=raw)
    asyncio.run(worker.capture_roster(START))
    raw[0]["nick"] = "New nick"
    asyncio.run(worker.capture_roster(START + timedelta(minutes=1)))
    with SessionLocal() as db:
        health = json.loads(db.get(AssemblyWindow, DAY).healthy_servers)
        assert health["roster"]["101"]["name"] == "Old nick"
        assert health["roster_basis"] == "during_event"


def test_download_requires_admin_and_returns_private_utf8_attachment(
    client, make_user, login_as
):
    event(members=("101",))
    url = f"/api/admin/assembly/{DAY}/absent.txt"
    assert client.get(url).status_code == 403
    uid = make_user(uid=999)
    login_as(uid)
    assert client.get(url).status_code == 403
    with SessionLocal.begin() as db:
        db.get(User, uid).is_admin = True
    response = client.get(url)
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "Absent" in response.text and "Discord ID: 101" in response.text
    assert "attachment;" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get("/api/admin/assembly/invalid/absent.txt").status_code == 400
    assert client.get("/api/admin/assembly/2026-10-09/absent.txt").status_code == 409
    assert client.get("/api/admin/assembly/2026-10-03/absent.txt").status_code == 409


def test_download_does_not_take_wallet_write_lock(
    client, make_user, login_as, monkeypatch
):
    import runtime

    event(members=("101",))
    login_as(make_user(uid=999, is_admin=True))

    def forbidden_lock(*_):
        raise AssertionError("Read-only TXT must not acquire a wallet write lock")

    monkeypatch.setattr(runtime, "lock_runtime_row", forbidden_lock)
    assert client.get(f"/api/admin/assembly/{DAY}/absent.txt").status_code == 200


def test_voice_observation_cannot_mask_late_bm_start():
    observe_voice_snapshot({}, START)
    servers = [
        {"id": sid, "status": "ready", "pending": 0, "roster": []} for sid in SERVER_IDS
    ]
    observe_snapshot(servers, START + timedelta(minutes=30))
    with SessionLocal() as db:
        _, problems = battlemetrics_coverage(db.get(AssemblyWindow, DAY))
        assert "late_start" in problems


def test_review_export_without_window_is_explicitly_non_final(
    client, make_user, login_as, monkeypatch
):
    from discord_event_log import DiscordEventLog

    monkeypatch.setenv("REQUIRED_GUILD_ID", "7")
    event(members=("101", "102"))
    with SessionLocal.begin() as db:
        db.delete(db.get(AssemblyWindow, DAY))
        db.get(
            AssemblyReportCheck, DAY
        ).member_names = '{"101":"Needs review","102":"Seen in voice"}'
        db.add(
            DiscordEventLog(
                event_key="review-positive-test",
                kind="voice_join",
                guild_id="7",
                target_id="102",
                channel_to="55",
                payload='{"channel_to_name":"Общий"}',
                occurred_at=START + timedelta(minutes=10),
                not_before=START,
            )
        )
    login_as(make_user(uid=999, is_admin=True))
    response = client.get(f"/api/admin/assembly/{DAY}/review.txt")
    assert response.status_code == 200
    assert "НЕ подтверждённый" in response.text and "Needs review" in response.text
    assert (
        "Seen in voice" not in response.text
        and "Кандидатов на проверку: 1" in response.text
    )
    with SessionLocal() as db:
        assert db.query(AssemblyAssessment).count() == 0
        assert db.get(User, 999).balance == 1000
        assert db.get(AssemblyWindow, DAY) is None
    assert client.get(f"/api/admin/assembly/{DAY}/absent.txt").status_code == 409


def test_voice_failure_does_not_block_reports_and_due_processing(monkeypatch):
    from unittest.mock import Mock

    bot = SimpleNamespace(
        is_closed=Mock(side_effect=[False, True]), is_ready=lambda: True
    )
    worker = AssemblyWorker(bot)
    worker.capture_voice = AsyncMock(side_effect=RuntimeError("unavailable"))
    worker.process_report_checks = AsyncMock()
    worker.process_due = AsyncMock()
    for method in (
        worker.capture_voice,
        worker.process_report_checks,
        worker.process_due,
    ):
        method.__name__ = "test_stage"
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    asyncio.run(worker.loop())
    assert (
        worker.process_report_checks.await_count == 1
        and worker.process_due.await_count == 1
    )


@pytest.mark.parametrize("stage", [False, True])
def test_capture_uses_real_discord_channel_voice_states_property(stage):
    import discord

    guild = SimpleNamespace(afk_channel=None, _voice_states={})
    channel = object.__new__(discord.StageChannel if stage else discord.VoiceChannel)
    channel.guild = guild
    channel.id = 55
    channel.name = "Общий"
    guild._voice_states = {101: SimpleNamespace(channel=channel)}
    guild.voice_channels = [] if stage else [channel]
    guild.stage_channels = [channel] if stage else []
    assert not hasattr(guild, "voice_states")
    worker = AssemblyWorker(
        SimpleNamespace(is_ready=lambda: True, get_guild=lambda _: guild)
    )
    worker.guild_id = "7"
    asyncio.run(worker.capture_voice(START))
    with SessionLocal() as db:
        assert "discord:101" in json.loads(db.get(AssemblyWindow, DAY).attended)


@pytest.mark.parametrize("legacy_defer", [None, False, True])
def test_assessment_always_waits_for_explicit_confirmation(make_user, legacy_defer):
    make_user(uid=101, steam_id=STEAM)
    event(members=("101",))
    kwargs = {} if legacy_defer is None else {"defer": legacy_defer}
    with SessionLocal.begin() as db:
        result = apply_assessments(db, DAY, {"101"}, {}, now=END, **kwargs)
        assert result["charged"] == 0
    with SessionLocal() as db:
        assert db.get(User, 101).balance == 1000
        assert db.get(AssemblyWindow, DAY).status == "review_pending"
        assert db.query(AssemblyAssessment).one().outcome == "pending_penalty"


def test_worker_never_finalizes_even_long_after_grace(make_user, monkeypatch):
    import assembly_attendance

    make_user(uid=101, steam_id=STEAM)
    event(members=("101",))
    with SessionLocal.begin() as db:
        apply_assessments(db, DAY, {"101"}, {}, now=END)
    monkeypatch.setattr(assembly_attendance, "utcnow", lambda: END + timedelta(days=30))
    worker = AssemblyWorker(SimpleNamespace())
    worker.guild_id = "7"
    worker.role_id = "8"
    worker.capture_roster = AsyncMock(return_value=False)
    assert asyncio.run(worker.process_due()) is False
    with SessionLocal() as db:
        assert db.get(User, 101).balance == 1000
        assert db.get(AssemblyWindow, DAY).status == "review_pending"


def test_only_chief_can_confirm_and_charge_once(client, make_user, login_as):
    from admin_hub import AdminActionLog

    make_user(uid=101, steam_id=STEAM)
    event(members=("101",))
    with SessionLocal.begin() as db:
        apply_assessments(db, DAY, {"101"}, {}, now=END)
    url = f"/api/admin/assembly/{DAY}/finalize"
    headers = {"X-ST9-Action": "finalize-assembly"}
    login_as(make_user(uid=999, is_admin=True))
    assert client.post(url, headers=headers).status_code == 403
    with SessionLocal.begin() as db:
        db.get(User, 999).is_chief_admin = True
    assert client.post(url).status_code == 403
    status = client.get("/api/admin/assembly-status")
    assert status.status_code == 200
    window = next(w for w in status.json()["windows"] if w["event_date"] == DAY)
    assert window["requires_confirmation"] is True and window["review_until"] is None
    response = client.post(url, headers=headers)
    assert response.status_code == 200 and response.json()["charged"] > 0
    charged = response.json()["charged"]
    assert client.post(url, headers=headers).status_code == 409
    with SessionLocal() as db:
        assert db.get(User, 101).balance == 1000 - charged
        assert db.get(AssemblyWindow, DAY).status == "completed"
        assert (
            db.query(AdminActionLog).filter_by(action="assembly_confirmed").count() == 1
        )
