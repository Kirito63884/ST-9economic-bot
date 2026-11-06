from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient
import main
from automation_control import AutomationSetting
from database import SessionLocal, User
import forum_events as events
from forum_calendar import month_info

BASE = datetime(2030, 11, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(events, "now", lambda: BASE)
    monkeypatch.setenv("REQUIRED_GUILD_ID", "999999999999999999")
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("forum%event%:%")
        ).delete()
    yield
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("forum%event%:%")
        ).delete()


def seed(number, start, responses, **changes):
    with SessionLocal.begin() as db:
        event = events.create(
            db,
            f"{number:032x}",
            "999",
            dict(
                title=f"Match {number}",
                starts_at=(BASE + timedelta(days=100)).isoformat(),
                capacity=20,
                teams="Team",
                map="Map",
                server="Server",
                rules="Rules",
                image_url="",
                status="open",
            ),
        )
        event["config"]["starts_at"] = start
        event["responses"] = {
            str(uid): {"choice": choice, "name": "Private participant"}
            for uid, choice in responses.items()
        }
        event.update(
            thread_id="777777777777777777",
            delivery={"token": "secret-worker-token"},
            **changes,
        )
        events.save(db, event)
        return event


def test_only_own_events_and_allowlisted_projection(make_user, login_as):
    make_user(101)
    make_user(102)
    seed(
        1,
        "2030-11-14T16:30:00+00:00",
        {101: "yes", 102: "yes"},
        lineup=["101"],
        result={"summary": "Done", "score": "2:1", "images": [], "revision": 3},
    )
    seed(2, "2030-11-15T16:30:00+00:00", {101: "maybe"})
    seed(3, "2030-11-16T16:30:00+00:00", {101: "no"})
    seed(4, "2030-11-17T16:30:00+00:00", {102: "yes"})
    client = login_as(101)
    response = client.get("/api/calendar?month=2030-11&user_id=102")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    rows = response.json()["events"]
    assert [row["number"] for row in rows] == [1, 2]
    assert rows[0]["when"] == "14.11.2030 19:30"
    assert rows[0]["in_lineup"] and rows[0]["status"] == "finished"
    assert rows[1]["response"] == "maybe"
    assert rows[0]["result"] == {"summary": "Done", "score": "2:1"}
    assert (
        rows[0]["discord_url"]
        == "https://discord.com/channels/999999999999999999/777777777777777777"
    )
    raw = json.dumps(response.json())
    for private in (
        "secret-worker-token",
        "Private participant",
        "responses",
        "creator",
        "published_revision",
        "reminder_plans",
        "creation_hash",
    ):
        assert private not in raw
    other = login_as(102).get("/api/calendar?month=2030-11").json()["events"]
    assert [row["number"] for row in other] == [1, 4]


def test_moscow_month_boundaries_and_personal_refresh(make_user, login_as):
    make_user(101)
    seed(1, "2030-11-30T22:30:00+00:00", {101: "yes"})
    event = seed(2, "2030-10-31T22:30:00+00:00", {101: "yes"})
    client = login_as(101)
    assert [
        e["number"] for e in client.get("/api/calendar?month=2030-11").json()["events"]
    ] == [2]
    assert [
        e["number"] for e in client.get("/api/calendar?month=2030-12").json()["events"]
    ] == [1]
    with SessionLocal.begin() as db:
        event = events.get(db, event["id"])
        event["responses"]["101"]["choice"] = "no"
        events.save(db, event)
    assert client.get("/api/calendar?month=2030-11").json()["events"] == []


def test_authentication_and_expired_technical_admin(make_user, login_as):
    with TestClient(main.app, follow_redirects=False) as guest:
        assert guest.get("/calendar").status_code == 303
        assert guest.get("/api/calendar").status_code == 401
    make_user(101, is_admin=True, is_tech_admin=True, auth_version=2)
    client = login_as(101)
    with SessionLocal.begin() as db:
        db.get(User, 101).auth_version = 3
    assert client.get("/api/calendar").status_code == 403


def test_month_validation_leap_day_navigation(make_user, login_as):
    make_user(101)
    client = login_as(101)
    for value in ("2030-13", "2030-00", "0000-01", "bad", "2030-2"):
        assert client.get("/api/calendar", params={"month": value}).status_code == 400
    assert client.get("/api/calendar?mode=other").status_code == 422
    leap = month_info("2028-02", BASE.date())
    assert (
        leap["days"] == 29
        and leap["previous"] == "2028-01"
        and leap["next"] == "2028-03"
    )
    assert month_info("2030-12", BASE.date())["next"] == "2031-01"
    assert month_info("2030-01", BASE.date())["previous"] == "2029-12"


def test_upcoming_sorted_limited_and_cancellations_visible(make_user, login_as):
    make_user(101)
    for i in range(1, 53):
        seed(i, (BASE + timedelta(days=i)).isoformat(), {101: "yes"})
    with SessionLocal.begin() as db:
        event = events.get(db, f"{1:032x}")
        event["config"]["status"] = "cancelled"
        events.save(db, event)
    response = login_as(101).get("/api/calendar?mode=upcoming").json()
    assert response["total"] == 52 and len(response["events"]) == 50
    assert response["events"][0]["status"] == "cancelled"
    assert [e["number"] for e in response["events"]] == list(range(1, 51))
