"""Progress describes the current run, while successful evidence stays intact."""

import json
from datetime import datetime

import pytest

import medals_monitor as monitor
from database import SessionLocal
from medals import MedalMonitor, ProtocolProfile
from medals_source import SourceUnavailable

FIRST = "76561198000000000"
SECOND = "76561198000000001"
LAST_SUCCESS = "2026-10-06T23:38:32.877305Z"


class StopAfterProfiles(Exception):
    pass


def status():
    with SessionLocal() as db:
        return json.loads(db.get(MedalMonitor, 1).payload)


@pytest.fixture(autouse=True)
def clean_status(monkeypatch):
    monkeypatch.setattr(monitor, "utcnow", lambda: datetime(2026, 10, 29, 13))
    with SessionLocal.begin() as db:
        db.query(MedalMonitor).delete()
        db.query(ProtocolProfile).delete()
        db.add(
            MedalMonitor(
                id=1,
                updated_at=datetime(2026, 10, 29, 12),
                payload=json.dumps(
                    {
                        "status": "unavailable",
                        "phase": "archive",
                        "cursor": 20170,
                        "current_match": 20173,
                        "profiles_done": 100,
                        "profiles_total": 100,
                        "profiles_failed": 9,
                        "last_error": "Ошибка прошлого цикла",
                        "checked_at": LAST_SUCCESS,
                        "archive_failed": [20173],
                        "archive_skipped": [20100],
                    }
                ),
            )
        )
    monkeypatch.setattr(
        monitor,
        "profile",
        lambda body, sid: {"steam_id": sid, "verified": True, "games": []},
    )
    yield
    with SessionLocal.begin() as db:
        db.query(MedalMonitor).delete()
        db.query(ProtocolProfile).delete()


def test_new_cycle_resets_old_errors_and_marks_pending_profile(make_user):
    make_user(102, steam_id=FIRST)
    make_user(103, steam_id=SECOND)
    seen = []

    class Client:
        profile_cache = {}

        def player(self, sid):
            seen.append(status())
            if sid == SECOND:
                raise SourceUnavailable("Test failure in this cycle")
            return "verified synthetic profile"

        def get(self, path):
            raise StopAfterProfiles

    with pytest.raises(StopAfterProfiles):
        monitor.Monitor(Client()).cycle()
    assert len(seen) == 2
    first, second = seen
    assert first["phase"] == "profiles" and first["status"] == "collecting"
    assert first["profiles_total"] == 2 and first["profiles_done"] == 0
    assert first["profiles_failed"] == 0 and first["current_profile_index"] == 1
    assert first["current_match"] is None and first["last_error"] is None
    assert first["archive_failed"] == [] and first["archive_skipped"] == [20100]
    assert first["cycle_started_at"] == "2026-10-29T13:00:00Z"
    assert first["checked_at"] == LAST_SUCCESS and first["cursor"] == 20170
    assert second["current_profile_index"] == 2 and second["profiles_done"] == 1
    assert second["profiles_failed"] == 0
    final = status()
    assert final["phase"] == "archive" and final["status"] == "collecting"
    assert final["current_profile_index"] is None and final["current_match"] is None
    assert final["profiles_done"] == 2 and final["profiles_failed"] == 1
    assert final["checked_at"] == LAST_SUCCESS and final["cursor"] == 20170


def test_failed_profile_preserves_cached_verified_data(make_user):
    make_user(102, steam_id=FIRST)
    old = {"steam_id": FIRST, "verified": True, "games": [], "hours": 99}
    with SessionLocal.begin() as db:
        db.add(
            ProtocolProfile(
                steam_id=FIRST,
                checked_at=datetime(2026, 10, 1),
                payload=json.dumps(old),
            )
        )

    class Client:
        profile_cache = {}

        def player(self, sid):
            raise SourceUnavailable("Unavailable")

        def get(self, path):
            raise StopAfterProfiles

    with pytest.raises(StopAfterProfiles):
        monitor.Monitor(Client()).cycle()
    with SessionLocal() as db:
        row = db.get(ProtocolProfile, FIRST)
        assert json.loads(row.payload) == old and row.checked_at == datetime(
            2026, 10, 1
        )
    assert status()["profiles_failed"] == 1


def test_no_profiles_clears_stale_match_and_old_error():
    class Client:
        profile_cache = {}

        def get(self, path):
            current = status()
            assert current["phase"] == "archive"
            assert current["profiles_total"] == 0 and current["profiles_failed"] == 0
            assert current["current_profile_index"] is None
            assert current["current_match"] is None and current["last_error"] is None
            raise StopAfterProfiles

    with pytest.raises(StopAfterProfiles):
        monitor.Monitor(Client()).cycle()
