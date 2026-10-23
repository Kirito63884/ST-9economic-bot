"""Player profile online evidence, scoped history and prioritized refresh."""

from datetime import timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

import main
from conftest import session_cookie
from fastapi.testclient import TestClient
from database import SessionLocal
from squad_monitor import SERVER_IDS, SourceError, parse_session_page
import squad_play_history as history

STEAM = "76561198000000000"
OTHER = "76561198000000001"


@pytest.fixture(autouse=True)
def clean_history():
    main.request_guard.reset()
    with SessionLocal.begin() as db:
        db.query(history.SquadPlaySession).delete()
        db.query(history.SquadPresence).delete()
        db.query(history.BattleMetricsHistorySync).delete()


def snapshots(moment, online=False, partial=False, stale=False):
    stamp = moment.replace(tzinfo=timezone.utc).timestamp()
    return [
        {
            "id": sid,
            "name": "Protocol " + str(i),
            "status": "ready",
            "updated_at": stamp,
            "stale": stale,
            "pending": 1 if partial else 0,
            "roster": [{"steam_ids": [STEAM], "name": "Player"}]
            if online and i == 0
            else ([{"steam_ids": [], "name": "Unknown"}] if partial else []),
        }
        for i, sid in enumerate(SERVER_IDS)
    ]


def session_document(moment, player=None):
    relationships = {"server": {"data": {"id": SERVER_IDS[0], "type": "server"}}}
    if player is not None:
        relationships["player"] = {"data": {"id": player, "type": "player"}}
    return {
        "data": [
            {
                "type": "session",
                "id": "1",
                "attributes": {
                    "start": (moment - timedelta(hours=2)).isoformat() + "Z",
                    "stop": None,
                },
                "relationships": relationships,
            }
        ],
        "included": [],
    }


def test_scoped_session_does_not_require_redundant_player():
    rows, _ = parse_session_page(session_document(history.utcnow()), "42")
    assert len(rows) == 1 and rows[0]["server_id"] == SERVER_IDS[0]


def test_explicit_wrong_player_still_rejected():
    with pytest.raises(SourceError):
        parse_session_page(session_document(history.utcnow(), "99"), "42")


def test_session_server_names_from_included():
    doc = session_document(history.utcnow())
    doc["included"] = [
        {"type": "server", "id": SERVER_IDS[0], "attributes": {"name": "Protocol #1"}}
    ]
    assert parse_session_page(doc, "42")[0][0]["server_name"] == "Protocol #1"


def test_fresh_snapshot_shows_online_even_without_session_rows():
    now = history.utcnow()
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, snapshots(now, online=True), now)
    assert data["online"] is True and data["current"]["server_id"] == SERVER_IDS[0]
    assert data["current"]["started_at"] is None and not data["sessions"]


def test_stale_snapshot_is_unknown_not_offline():
    now = history.utcnow()
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, snapshots(now, stale=True), now)
    assert data["online"] is None and data["presence_status"] == "unknown"


def test_partial_roster_is_unknown_not_offline():
    now = history.utcnow()
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, snapshots(now, partial=True), now)
    assert data["online"] is None


def test_complete_fresh_rosters_confirm_offline():
    now = history.utcnow()
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, snapshots(now), now)
    assert data["online"] is False and data["presence_status"] == "offline"


def test_positive_match_does_not_require_every_identifier_resolved():
    now = history.utcnow()
    servers = snapshots(now, online=True, partial=True)
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, servers, now)
    assert data["online"] is True


def test_one_fresh_empty_server_is_not_proof_of_global_offline():
    now = history.utcnow()
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, snapshots(now)[:1], now)
    assert data["online"] is None


def test_verified_player_check_shows_online_without_live_snapshot():
    now = history.utcnow()
    with SessionLocal.begin() as db:
        history.observe_player_presence(
            db,
            STEAM,
            [{"server_id": SERVER_IDS[0], "server_name": "Protocol", "online": True}],
            now,
        )
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, [], now)
    assert data["online"] is True and data["sessions"][0]["active"] is True


def test_newer_confirmed_offline_wins_over_old_snapshot_online():
    now = history.utcnow()
    with SessionLocal.begin() as db:
        history.observe_player_presence(
            db,
            STEAM,
            [
                {"server_id": sid, "server_name": sid, "online": False}
                for sid in SERVER_IDS
            ],
            now,
        )
    with SessionLocal() as db:
        data = history.profile_history(
            db, STEAM, snapshots(now - timedelta(seconds=20), online=True), now
        )
    assert data["online"] is False


def test_partial_server_snapshot_does_not_close_a_known_session():
    now = history.utcnow()
    history.observe_sessions(snapshots(now, online=True), now)
    history.observe_sessions(
        snapshots(now + timedelta(seconds=30), partial=True),
        now + timedelta(seconds=30),
    )
    with SessionLocal() as db:
        assert (
            db.query(history.SquadPlaySession).filter_by(steam_id=STEAM).one().ended_at
            is None
        )


def test_individual_offline_check_never_closes_other_players():
    now = history.utcnow()
    with SessionLocal.begin() as db:
        for sid in (STEAM, OTHER):
            db.add(
                history.SquadPlaySession(
                    steam_id=sid,
                    server_id=SERVER_IDS[0],
                    server_name="Protocol",
                    started_at=now,
                    last_seen_at=now,
                )
            )
        db.flush()
        history.observe_player_presence(
            db,
            STEAM,
            [
                {"server_id": sid, "server_name": sid, "online": False}
                for sid in SERVER_IDS
            ],
            now,
        )
    with SessionLocal() as db:
        assert (
            db.query(history.SquadPlaySession).filter_by(steam_id=STEAM).one().ended_at
            is not None
        )
        assert (
            db.query(history.SquadPlaySession).filter_by(steam_id=OTHER).one().ended_at
            is None
        )


def test_refresh_priority_over_normal_queue(make_user):
    make_user(101, steam_id=OTHER)
    make_user(102, steam_id=STEAM)
    now = history.utcnow()
    with SessionLocal.begin() as db:
        history.queue_history_refresh(db, STEAM, now)
    job = history._claim_history_sync(now)
    assert job["steam_id"] == STEAM and job["priority"] is True


def test_retry_fairness_before_repeated_failed_profiles(make_user):
    make_user(101, steam_id=OTHER)
    make_user(102, steam_id=STEAM)
    now = history.utcnow()
    with SessionLocal.begin() as db:
        db.add(
            history.BattleMetricsHistorySync(
                steam_id=OTHER,
                status="error",
                next_attempt_at=now,
                last_checked_at=now - timedelta(hours=1),
            )
        )
    assert history._claim_history_sync(now)["steam_id"] == STEAM


def test_refresh_deduplicated_and_throttled(make_user):
    make_user(101, steam_id=STEAM)
    now = history.utcnow()
    with SessionLocal.begin() as db:
        assert history.queue_history_refresh(db, STEAM, now)["queued"]
        assert (
            history.queue_history_refresh(db, STEAM, now)["reason"] == "already_queued"
        )
    job = history._claim_history_sync(now)
    history._finish_history_sync(job, now, player_id="42")
    with SessionLocal.begin() as db:
        assert (
            history.queue_history_refresh(db, STEAM, now + timedelta(seconds=10))[
                "reason"
            ]
            == "fresh"
        )


def test_simultaneous_claims_do_not_claim_the_same_player(make_user):
    make_user(101, steam_id=STEAM)
    now = history.utcnow()
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: history._claim_history_sync(now), range(2)))
    assert sum(job is not None for job in jobs) == 1


class Client:
    def __init__(self, error=None):
        self.error = error

    def match_player(self, sid):
        return {"data": [{"type": "player", "id": "42"}]}

    def player_server(self, pid, sid):
        if self.error:
            raise SourceError(self.error)
        return {"data": {"attributes": {"online": sid == SERVER_IDS[0]}}}

    def session_page(self, pid, next_url=None):
        return session_document(history.utcnow())


def test_worker_refreshes_presence_and_imports_history(make_user):
    make_user(101, steam_id=STEAM)
    with SessionLocal.begin() as db:
        history.queue_history_refresh(db, STEAM)
    assert history.sync_one_history(Client(), {SERVER_IDS[0]: "Protocol"}) is True
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, [])
        assert data["online"] is True and data["sync"]["status"] == "ready"
        assert (
            len(data["sessions"]) == 1
        )  # Import merges into the just-observed session.
        assert data["sessions"][0]["duration_seconds"] >= 7190


def test_history_error_does_not_hide_verified_online(make_user):
    make_user(101, steam_id=STEAM)

    class BrokenHistory(Client):
        def session_page(self, *args):
            raise SourceError("permissions")

    with SessionLocal.begin() as db:
        history.queue_history_refresh(db, STEAM)
    assert history.sync_one_history(BrokenHistory()) is False
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, [])
        assert data["online"] is True and data["sync"]["error"] == "permissions"


def test_error_keeps_cached_presence_and_old_sessions():
    now = history.utcnow()
    with SessionLocal.begin() as db:
        history.observe_player_presence(
            db,
            STEAM,
            [{"server_id": SERVER_IDS[0], "server_name": "Protocol", "online": True}],
            now,
        )
    assert (
        history.refresh_player_presence(Client("permissions"), STEAM, "42")
        == "permissions"
    )
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, [])
        assert data["online"] is True and data["presence_error"] == "permissions"


def test_api_refresh_is_admin_only_and_cached_status_uses_snapshot(
    make_user, monkeypatch
):
    make_user(101, is_admin=True)
    make_user(102, steam_id=STEAM)
    servers = snapshots(history.utcnow(), online=True)
    monkeypatch.setattr("squad_routes.load_snapshot", lambda now: servers)
    c = TestClient(main.app)
    assert c.get("/api/admin/player-squad-history/102").status_code == 403
    assert c.post("/api/admin/player-squad-history/102/refresh").status_code == 403
    c.cookies.set("session", session_cookie(102))
    assert c.post("/api/admin/player-squad-history/102/refresh").status_code == 403
    c.cookies.set("session", session_cookie(101))
    assert (
        c.post("/api/admin/player-squad-history/102/refresh").json()["queued"] is True
    )
    r = c.get("/api/admin/player-squad-history/102")
    assert r.status_code == 200 and r.json()["online"] is True
    assert r.headers["cache-control"] == "private, no-store"


def test_recent_observed_session_is_positive_fallback_when_snapshot_unavailable():
    now = history.utcnow()
    with SessionLocal.begin() as db:
        db.add(
            history.SquadPlaySession(
                steam_id=STEAM,
                server_id=SERVER_IDS[0],
                server_name="Protocol",
                started_at=now - timedelta(hours=1),
                last_seen_at=now - timedelta(seconds=10),
            )
        )
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, [], now)
    assert data["online"] is True


def test_old_open_session_is_not_falsely_online():
    now = history.utcnow()
    with SessionLocal.begin() as db:
        db.add(
            history.SquadPlaySession(
                steam_id=STEAM,
                server_id=SERVER_IDS[0],
                server_name="Protocol",
                started_at=now - timedelta(hours=2),
                last_seen_at=now - timedelta(hours=1),
            )
        )
    with SessionLocal() as db:
        data = history.profile_history(db, STEAM, [], now)
    assert data["online"] is None


def test_refresh_and_wallet_requests_can_complete_together(make_user):
    import asyncio
    import httpx

    make_user(101, is_admin=True)
    make_user(102, steam_id=STEAM)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
            cookies={"session": session_cookie(101)},
        ) as c:
            return await asyncio.wait_for(
                asyncio.gather(
                    *(
                        c.post("/api/admin/player-squad-history/102/refresh")
                        if i % 2
                        else c.post("/api/daily-bonus")
                        for i in range(6)
                    )
                ),
                timeout=5,
            )

    responses = asyncio.run(run())
    assert all(r.status_code == 200 for r in responses)
    from database import User

    with SessionLocal() as db:
        assert db.get(User, 101).balance == 1100
