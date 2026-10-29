"""All cached Protocol sources are exposed without granting awards or inventing ranks."""

import json
from datetime import datetime

import pytest

from database import GameHistory, SessionLocal
from medals import MedalAward
from protocol_award_exports import URL as EXPORT_URL
from protocol_award_exports import ProtocolAwardExport
from protocol_clan_source import CLAN, ProtocolClanStats
from protocol_clan_source import URL as CLAN_URL
from protocol_rankings import CATEGORIES
from protocol_week_source import EXTRA_LABELS, LABELS, ProtocolPublishedWeek
from protocol_week_source import URL as WEEK_URL

WEEK = "2026-10-19"
NOW = datetime(2026, 10, 29, 12)
SID = "76561198000000000"


@pytest.fixture(autouse=True)
def clean():
    with SessionLocal.begin() as db:
        for model in (
            ProtocolAwardExport,
            ProtocolPublishedWeek,
            ProtocolClanStats,
            MedalAward,
        ):
            db.query(model).delete()


def export(kind, ids=None):
    week = None if kind == "fob" else WEEK
    ids = ids or [SID]
    doc = {
        "schema": 1,
        "source": EXPORT_URL,
        "kind": kind,
        "week": week,
        "final": bool(week),
        "file_verified": True,
        "file_ids": ids,
        "file_count": len(ids),
        "title": kind,
        "unit": "hours"
        if kind in ("pilot", "crew", "mg", "sniper", "sapper", "medic")
        else "count",
        "rows": [
            {
                "steam_id": ids[0],
                "name": "Pilot <img src=x onerror=alert(1)>",
                "rank": 1,
                "value": 12.5,
            }
        ],
    }
    with SessionLocal.begin() as db:
        db.add(
            ProtocolAwardExport(
                key=(week or "all") + ":" + kind,
                week=week,
                kind=kind,
                payload=json.dumps(doc),
                checked_at=NOW,
            )
        )


def sources():
    for kind in CATEGORIES:
        export(kind)
    categories = {
        kind: {
            "name": name,
            "rows": [
                {
                    "steam_id": SID,
                    "name": "Player",
                    "rank": 1,
                    "value": 4,
                    "matches": 10,
                }
            ],
        }
        for kind, name in {**LABELS, **EXTRA_LABELS}.items()
    }
    with SessionLocal.begin() as db:
        db.add(
            ProtocolPublishedWeek(
                week=WEEK,
                payload=json.dumps(
                    {
                        "source": WEEK_URL,
                        "week": WEEK,
                        "final": True,
                        "categories": categories,
                    }
                ),
                checked_at=NOW,
            )
        )
        db.add(
            ProtocolClanStats(
                id=1,
                payload=json.dumps(
                    {
                        "schema": 1,
                        "source": CLAN_URL,
                        "clan": CLAN,
                        "players": {
                            SID: {
                                "steam_id": SID,
                                "name": "Roster Player",
                                "verified": True,
                                "hours": 100,
                                "seed_hours": 5,
                                "kills": 10,
                                "deaths": 2,
                                "kd": 5,
                            }
                        },
                    }
                ),
                checked_at=NOW,
                attempted_at=NOW,
                status="ready",
            )
        )


@pytest.mark.parametrize("role", ["anonymous", "player", "junior", "senior"])
def test_lists_have_same_admin_only_access_as_existing_exports(
    role, client, make_user, login_as
):
    if role != "anonymous":
        client = login_as(
            make_user(
                is_admin=role in ("junior", "senior"), is_senior_admin=role == "senior"
            )
        )
    expected = (
        200 if role in ("junior", "senior") else (401 if role == "anonymous" else 403)
    )
    response = client.get("/api/admin/protocol-lists")
    assert response.status_code == expected
    if expected == 200:
        assert "no-store" in response.headers["cache-control"]
    else:
        assert (
            client.get("/api/admin/protocol-lists/clan:roster").status_code == expected
        )


def test_all_18_export_types_all_8_weekly_types_and_clan_roster_are_visible(
    make_user, login_as
):
    sources()
    client = login_as(make_user(is_admin=True))
    data = client.get("/api/admin/protocol-lists").json()
    assert {
        item["kind"] for item in data["items"] if item["group"] == "achievements"
    } == set(CATEGORIES)
    assert len(CATEGORIES) == 18
    assert {item["kind"] for item in data["items"] if item["group"] == "weekly"} == set(
        LABELS
    ) | set(EXTRA_LABELS)
    assert any(i["id"] == "clan:roster" for i in data["items"])
    assert {
        c["kind"] for c in data["categories"] if c["group"] == "achievements"
    } == set(CATEGORIES)
    assert any(
        i["kind"] == "pilot" and i["preview"][0]["rank"] == 1 for i in data["items"]
    )
    with SessionLocal() as db:
        assert not db.query(MedalAward).first() and not db.query(GameHistory).first()


def test_full_export_paging_never_invents_rank_or_numbers_for_txt_only_members(
    make_user, login_as
):
    ids = [str(int(SID) + i) for i in range(56)]
    export("atgm15", ids)
    client = login_as(make_user(is_admin=True))
    one = client.get("/api/admin/protocol-exports/" + WEEK + ":atgm15").json()
    assert one["total"] == 56 and len(one["rows"]) == 50
    assert one["rows"][0]["rank"] == 1
    assert all(r["rank"] is None and r["value"] is None for r in one["rows"][1:])
    two = client.get("/api/admin/protocol-exports/" + WEEK + ":atgm15?offset=50").json()
    assert len(two["rows"]) == 6 and two["rows"][0]["position"] == 51
    assert not {r["steam_id"] for r in one["rows"]} & {
        r["steam_id"] for r in two["rows"]
    }


def test_published_and_roster_details_use_source_fields_and_missing_stays_unknown(
    make_user, login_as
):
    sources()
    client = login_as(make_user(is_admin=True))
    data = client.get(
        "/api/admin/protocol-lists/published:" + WEEK + ":driver_hours"
    ).json()
    assert data["unit"] == "hours" and data["rows"][0]["value"] == 4
    assert data["rows"][0]["matches"] == 10
    with SessionLocal.begin() as db:
        row = db.get(ProtocolClanStats, 1)
        doc = json.loads(row.payload)
        del doc["players"][SID]["seed_hours"]
        row.payload = json.dumps(doc)
    roster = client.get("/api/admin/protocol-lists/clan:roster").json()
    assert roster["rows"][0]["metrics"]["seed_hours"] is None
    assert roster["rows"][0]["metrics"]["kd"] == 5 and roster["rows"][0]["rank"] is None
    assert (
        client.get("/api/admin/protocol-lists/clan:roster?q=NotFound").json()["total"]
        == 0
    )
    assert (
        client.get("/api/admin/protocol-lists/clan:roster?limit=1000").status_code
        == 422
    )


def test_future_collected_kind_is_not_lost_and_foreign_source_is_rejected(
    make_user, login_as
):
    export("new_category")
    client = login_as(make_user(is_admin=True))
    assert (
        client.get("/api/admin/protocol-lists").json()["items"][0]["kind"]
        == "new_category"
    )
    with SessionLocal.begin() as db:
        row = db.query(ProtocolAwardExport).one()
        doc = json.loads(row.payload)
        doc["source"] = "https://untrusted.invalid"
        row.payload = json.dumps(doc)
    assert not client.get("/api/admin/protocol-lists").json()["items"]
