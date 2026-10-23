"""Fallback backfill, fair retries, safe playtime parsing and honest counters."""

from datetime import datetime, timedelta
import json
import time

import pytest

from database import SessionLocal, User
from medals import ProtocolProfile
from squad_play_history import BattleMetricsHistorySync
import battlemetrics_awards as bm
from squad_monitor import SourceError, SERVER_IDS

BASE = 76561198000000000
NOW = datetime(2026, 10, 15, 12)


@pytest.fixture(autouse=True)
def clean_fallback():
    with SessionLocal.begin() as db:
        db.query(bm.BattleMetricsPlaytime).delete()
        db.query(ProtocolProfile).delete()
        db.query(BattleMetricsHistorySync).delete()


def players(make_user, count):
    for i in range(count):
        make_user(200 + i, steam_id=str(BASE + i))
    return [str(BASE + i) for i in range(count)]


def document(pid="42", seconds=3600):
    return {
        "data": {
            "type": "player",
            "id": pid,
            "relationships": {
                "servers": {
                    "data": [
                        {
                            "id": SERVER_IDS[0],
                            "type": "server",
                            "meta": {"timePlayed": seconds},
                        }
                    ]
                }
            },
        }
    }


class Client:
    def __init__(self, error=None):
        self.matched = []
        self.paths = []
        self.error = error

    def match_player(self, sid):
        self.matched.append(sid)
        if self.error:
            raise SourceError(self.error)
        return {"data": [{"type": "player", "id": str(int(sid) - BASE + 42)}]}

    def player_server(self, pid, server_id):
        self.paths.append(f"/players/{pid}/servers/{server_id}")
        if self.error:
            raise SourceError(self.error)
        return {
            "data": {
                "type": "playerServer",
                "id": f"{pid}:{server_id}",
                "attributes": {"timePlayed": 720, "online": False},
            }
        }


def test_new_player_without_cache_is_matched(make_user):
    sid = players(make_user, 1)[0]
    client = Client()
    assert bm.collect_fallback(now=NOW, client=client, identities={})["attempted"] == 1
    assert client.matched == [sid]
    with SessionLocal() as db:
        assert bm.playtime(db, sid, NOW)["minutes"] == 60
        assert bm.status(db, NOW)["failed"] == 0


def test_missing_cache_file_does_not_skip_players(make_user, monkeypatch):
    sid = players(make_user, 1)[0]
    client = Client()

    def missing(*args):
        raise FileNotFoundError()

    monkeypatch.setattr(bm, "read_json", missing)
    bm.collect_fallback(now=NOW, client=client)
    assert client.matched == [sid]


def test_failed_first_players_do_not_starve_new_players(make_user):
    ids = players(make_user, 7)
    with SessionLocal.begin() as db:
        for sid in ids[:5]:
            db.add(
                bm.BattleMetricsPlaytime(
                    steam_id=sid,
                    attempted_at=NOW - timedelta(minutes=5),
                    last_error="source_unavailable",
                )
            )
    client = Client()
    bm.collect_fallback(now=NOW, client=client, identities={})
    assert set(ids[5:]).issubset(client.matched)
    assert len(client.matched) == 5


def test_oldest_attempts_win_when_everyone_has_errors(make_user):
    ids = players(make_user, 7)
    with SessionLocal.begin() as db:
        for i, sid in enumerate(ids):
            db.add(
                bm.BattleMetricsPlaytime(
                    steam_id=sid,
                    attempted_at=NOW - timedelta(minutes=10 + i),
                    last_error="source_unavailable",
                )
            )
    client = Client()
    bm.collect_fallback(now=NOW, client=client, identities={})
    assert client.matched == list(reversed(ids[2:]))


def test_history_player_id_reused_without_live_cache(make_user):
    sid = players(make_user, 1)[0]
    with SessionLocal.begin() as db:
        db.add(
            BattleMetricsHistorySync(
                steam_id=sid, player_id="888", status="ready", next_attempt_at=NOW
            )
        )
    client = Client()
    bm.collect_fallback(now=NOW, client=client, identities={})
    assert not client.matched and client.paths == [
        f"/players/888/servers/{sid}" for sid in SERVER_IDS
    ]


def test_ambiguous_cache_does_not_choose_an_identity(make_user):
    sid = players(make_user, 1)[0]
    client = Client()
    identities = {pid: {"at": time.time(), "steam_ids": [sid]} for pid in ("1", "2")}
    bm.collect_fallback(now=NOW, client=client, identities=identities)
    assert not client.paths and not client.matched
    with SessionLocal() as db:
        assert db.get(bm.BattleMetricsPlaytime, sid).last_error == "ambiguous_identity"


@pytest.mark.parametrize("error", ["authentication", "permissions", "rate_limited"])
def test_global_errors_stop_the_batch(make_user, error):
    players(make_user, 7)
    client = Client(error)
    assert bm.collect_fallback(now=NOW, client=client, identities={})["attempted"] == 1
    assert len(client.matched) == 1


def test_failures_preserve_verified_hours(make_user):
    sid = players(make_user, 1)[0]
    with SessionLocal.begin() as db:
        db.add(
            bm.BattleMetricsPlaytime(
                steam_id=sid,
                attempted_at=NOW - timedelta(hours=2),
                checked_at=NOW - timedelta(hours=2),
                payload=json.dumps({"minutes": 60}),
            )
        )
    bm.collect_fallback(now=NOW, client=Client("permissions"), identities={})
    with SessionLocal() as db:
        row = db.get(bm.BattleMetricsPlaytime, sid)
        assert (
            json.loads(row.payload)["minutes"] == 60 and row.last_error == "permissions"
        )
        assert bm.playtime(db, sid, NOW)["minutes"] == 60


def test_counter_ignores_archived_and_fresh_primary_profiles(make_user):
    ids = players(make_user, 4)
    with SessionLocal.begin() as db:
        for sid in ids:
            db.add(
                bm.BattleMetricsPlaytime(
                    steam_id=sid, attempted_at=NOW, last_error="permissions"
                )
            )
        db.get(User, 201).deleted_at = NOW
        db.add(ProtocolProfile(steam_id=ids[2], payload="{}", checked_at=NOW))
        # An old profile still needs fallback.
        db.add(
            ProtocolProfile(
                steam_id=ids[3], payload="{}", checked_at=NOW - timedelta(days=2)
            )
        )
    with SessionLocal() as db:
        data = bm.status(db, NOW)
        assert data["total"] == 3 and data["needed"] == 2 and data["failed"] == 2
        assert data["ignored_errors"] == 2
        assert (
            data["errors"][0]["code"] == "permissions"
            and data["errors"][0]["count"] == 2
        )
        assert {p["user_id"] for p in data["players"]} == {200, 203}


def test_pending_player_is_not_counted_as_failure(make_user):
    players(make_user, 1)
    with SessionLocal() as db:
        data = bm.status(db, NOW)
        assert data["pending"] == 1 and data["failed"] == 0


@pytest.mark.parametrize(
    "seconds", [True, -1, float("nan"), float("inf"), "3600", None]
)
def test_unverified_time_is_rejected(seconds):
    with pytest.raises(SourceError):
        bm.parse_playtime(document(seconds=seconds), "42", str(BASE))


def test_fractional_seconds_are_rounded_down():
    assert (
        bm.parse_playtime(document(seconds=3599.99), "42", str(BASE))["minutes"] == 59
    )


def test_missing_server_time_has_specific_reason():
    doc = document()
    doc["data"]["relationships"]["servers"]["data"] = []
    with pytest.raises(SourceError) as error:
        bm.parse_playtime(doc, "42", str(BASE))
    assert error.value.code == "no_server_history"


class ServerClient:
    def __init__(self, values):
        self.values = values
        self.requests = []

    def player_server(self, pid, sid):
        self.requests.append((pid, sid))
        value = self.values[sid]
        if isinstance(value, str):
            raise SourceError(value)
        return {
            "data": {
                "type": "playerServer",
                "id": "opaque-resource-id",
                "attributes": {
                    "firstSeen": "2020-01-01T00:00:00Z",
                    "lastSeen": "2026-10-15T00:00:00Z",
                    "timePlayed": value,
                    "online": False,
                },
            }
        }


def test_real_server_response_values():
    values = [427252, 603356, 1036672, 379282, 32100]
    c = ServerClient(dict(zip(SERVER_IDS, values)))
    result = bm.collect_player_playtime(c, "42", str(BASE))
    assert result["minutes"] == sum(values) // 60
    assert result["server_seconds"] == dict(zip(SERVER_IDS, values))
    assert len(c.requests) == 5 and result["verified"] is True


def test_missing_server_record_does_not_prevent_verified_minimum():
    c = ServerClient(
        {sid: ("not_found" if i else 3600) for i, sid in enumerate(SERVER_IDS)}
    )
    assert bm.collect_player_playtime(c, "42", str(BASE))["minutes"] == 60


def test_all_servers_missing_is_not_a_zero_hour_success():
    with pytest.raises(SourceError) as exc:
        bm.collect_player_playtime(
            ServerClient({sid: "not_found" for sid in SERVER_IDS}), "42", str(BASE)
        )
    assert exc.value.code == "no_server_history"


@pytest.mark.parametrize(
    "error", ["permissions", "authentication", "rate_limited", "source_unavailable"]
)
def test_partial_server_data_does_not_hide_an_api_error(error):
    values = {sid: 3600 for sid in SERVER_IDS}
    values[SERVER_IDS[1]] = error
    c = ServerClient(values)
    with pytest.raises(SourceError) as exc:
        bm.collect_player_playtime(c, "42", str(BASE))
    assert exc.value.code == error and len(c.requests) == 2


@pytest.mark.parametrize("value", [True, None, "3600", -1, float("inf"), float("nan")])
def test_server_response_rejects_invalid_seconds(value):
    with pytest.raises(SourceError):
        bm.parse_server_playtime({"data": {"attributes": {"timePlayed": value}}})


def test_server_response_fractional_seconds_rounded_down():
    assert (
        bm.parse_server_playtime({"data": {"attributes": {"timePlayed": 3599.9}}})
        == 3599
    )


def test_uses_server_endpoint_not_profile(make_user):
    sid = players(make_user, 1)[0]
    client = Client()
    bm.collect_fallback(now=NOW, client=client, identities={})
    assert len(client.paths) == 5 and all(
        "/servers/" in p and "?" not in p for p in client.paths
    )
    with SessionLocal() as db:
        assert db.get(bm.BattleMetricsPlaytime, sid).last_error is None


def test_client_server_endpoint_validates_scope():
    from squad_monitor import BattleMetrics

    client = BattleMetrics("not-a-real-token")
    requests = []
    client._request = lambda method, path: requests.append((method, path)) or {}
    client.player_server("42", SERVER_IDS[0])
    assert requests == [("GET", f"/players/42/servers/{SERVER_IDS[0]}")]
    for pid, sid in [
        ("42/other", SERVER_IDS[0]),
        ("42", "untracked"),
        ("https://other", "1"),
    ]:
        with pytest.raises(SourceError):
            client.player_server(pid, sid)
    assert len(requests) == 1
