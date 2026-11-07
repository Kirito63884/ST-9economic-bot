import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from automation_control import AutomationSetting
from database import SessionLocal
import forum_events as events
from forum_event_delivery import Announcements, STAGES, reminder_due, has_work
from forum_event_extras import (
    TEMPLATE_FIELDS,
    delete_template,
    save_result,
    save_template,
    templates,
)

EID = "b" * 32
UID = "123456789012345678"
BASE = datetime(2030, 11, 10, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean():
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("forum%event%:%")
        ).delete()
    yield
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("forum%event%:%")
        ).delete()


def saved():
    with SessionLocal() as db:
        return events.get(db, EID)


def fixture(monkeypatch, count=1):
    clock = [BASE]
    monkeypatch.setattr(events, "now", lambda: clock[0])
    config = dict(
        title="Тестовый матч",
        starts_at=(BASE + timedelta(days=4)).isoformat(),
        capacity=20,
        teams="Fixture",
        map="Map",
        server="Test",
        rules="Rules",
        image_url="",
        status="open",
    )
    with SessionLocal.begin() as db:
        event = events.create(db, EID, UID, config)
        event["thread_id"] = "777"
        event["responses"] = {
            str(int(UID) + i): {"choice": "yes", "name": "Fixture"}
            for i in range(count)
        }
        event["responses"]["999999999999999999"] = {"choice": "maybe", "name": "Maybe"}
        events.save(db, event)
    messages = []
    fail = [0]

    async def send(content=None, **kwargs):
        message = SimpleNamespace(
            id=800 + len(messages),
            author=SimpleNamespace(id=999),
            embeds=[kwargs["embed"]],
            edit=AsyncMock(),
        )
        messages.append(message)
        if fail[0] == len(messages):
            raise TimeoutError("Discord accepted; reply lost")
        return message

    async def history(**kwargs):
        for message in reversed(messages):
            yield message

    thread = SimpleNamespace(
        id=777,
        parent_id=events.FORUM_ID,
        guild=SimpleNamespace(id=123),
        owner_id=999,
        archived=False,
        send=AsyncMock(side_effect=send),
        history=history,
        fetch_message=AsyncMock(
            side_effect=lambda mid: next(m for m in messages if m.id == mid)
        ),
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999), fetch_channel=AsyncMock(return_value=thread)
    )
    worker = SimpleNamespace(bot=bot, guild_id=123)
    return Announcements(worker), clock, thread, messages, fail


@pytest.mark.parametrize("seconds,label", STAGES)
def test_all_eight_stages_once_with_only_yes_mentions(monkeypatch, seconds, label):
    ann, clock, thread, messages, fail = fixture(monkeypatch)
    clock[0] = BASE + timedelta(days=4, seconds=-seconds)

    async def run():
        await ann.tick(EID)
        await ann.tick(EID)
        await Announcements(ann.worker).tick(EID)

    asyncio.run(run())
    thread.send.assert_awaited_once()
    assert thread.send.call_args.args[0] == f"<@{UID}>"
    mentions = thread.send.call_args.kwargs["allowed_mentions"]
    assert [user.id for user in mentions.users] == [int(UID)]
    assert not mentions.everyone and not mentions.roles
    assert label in messages[0].embeds[0].description


def test_old_milestones_not_sent_after_downtime_or_late_creation(monkeypatch):
    ann, clock, thread, messages, fail = fixture(monkeypatch)
    clock[0] = BASE + timedelta(days=3, minutes=6)
    asyncio.run(ann.tick(EID))
    thread.send.assert_not_awaited()
    with SessionLocal.begin() as db:
        event = events.get(db, EID)
        event["created_at"] = (BASE + timedelta(days=3, minutes=1)).isoformat()
        events.save(db, event)
    clock[0] = BASE + timedelta(days=3, minutes=2)
    assert not reminder_due(saved(), 86400, clock[0])
    asyncio.run(ann.tick(EID))
    thread.send.assert_not_awaited()


def test_withdrawn_cancelled_and_rescheduled_events(monkeypatch):
    ann, clock, thread, messages, fail = fixture(monkeypatch)
    clock[0] = BASE + timedelta(days=3)

    async def run():
        await ann.plan(EID)
        with SessionLocal.begin() as db:
            event = events.get(db, EID)
            event["responses"][UID]["choice"] = "no"
            events.save(db, event)
        await ann.tick(EID)
        thread.send.assert_not_awaited()
        with SessionLocal.begin() as db:
            event = events.get(db, EID)
            event["responses"][UID]["choice"] = "yes"
            events.save(db, event)
            events.update(
                db,
                EID,
                event["revision"],
                {
                    **event["config"],
                    "starts_at": (BASE + timedelta(days=5)).isoformat(),
                },
                [],
            )
        clock[0] = BASE + timedelta(days=4)
        await ann.tick(EID)
        assert thread.send.await_count == 1
        with SessionLocal.begin() as db:
            event = events.get(db, EID)
            events.update(
                db,
                EID,
                event["revision"],
                {**event["config"], "status": "cancelled"},
                [],
            )
        clock[0] += timedelta(hours=12)
        await ann.tick(EID)
        assert thread.send.await_count == 1

    asyncio.run(run())


def test_batched_delivery_reconciles_lost_response_and_restart(monkeypatch):
    ann, clock, thread, messages, fail = fixture(monkeypatch, count=120)
    clock[0] = BASE + timedelta(days=3)
    fail[0] = 2

    async def run():
        await asyncio.gather(ann.tick(EID), Announcements(ann.worker).tick(EID))
        assert len(messages) == 3
        clock[0] += timedelta(seconds=181)
        await Announcements(ann.worker).tick(EID)
        assert len(messages) == 3

    asyncio.run(run())
    assert all(
        len(call.args[0]) < 2000 and len(call.kwargs["allowed_mentions"].users) <= 50
        for call in thread.send.call_args_list
    )
    parts = next(iter(saved()["reminder_plans"].values()))["parts"]
    assert all(part["status"] == "sent" for part in parts)


def test_expired_failed_reminder_is_not_sent_late(monkeypatch):
    ann, clock, thread, messages, fail = fixture(monkeypatch)
    clock[0] = BASE + timedelta(days=3)
    thread.send.side_effect = TimeoutError()

    async def run():
        await ann.tick(EID)
        clock[0] += timedelta(minutes=10)
        await ann.tick(EID)

    asyncio.run(run())
    assert thread.send.await_count == 1
    assert (
        next(iter(saved()["reminder_plans"].values()))["parts"][0]["status"]
        == "skipped"
    )


def test_results_edit_existing_message_stop_reminders_and_preserve_revision(
    monkeypatch,
):
    ann, clock, thread, messages, fail = fixture(monkeypatch)
    with SessionLocal.begin() as db:
        with pytest.raises(events.EventError):
            save_result(db, EID, 1, dict(summary="Done", score="2:1", images=[]))
    clock[0] = BASE + timedelta(days=4)
    with SessionLocal.begin() as db:
        save_result(db, EID, 1, dict(summary="Good match", score="2:1", images=[]))

    async def run():
        await ann.tick(EID)
        assert len(messages) == 1
        assert thread.send.call_args.kwargs["allowed_mentions"].to_dict()["parse"] == []
        with SessionLocal.begin() as db:
            event = events.get(db, EID)
            with pytest.raises(events.EventError):
                save_result(db, EID, 1, dict(summary="stale", score="0:0", images=[]))
            save_result(
                db,
                EID,
                event["revision"],
                dict(summary="Corrected", score="3:1", images=[]),
            )
        await Announcements(ann.worker).tick(EID)
        assert len(messages) == 1
        assert messages[0].edit.call_args.kwargs["embed"].description == "Corrected"
        assert saved()["config"]["status"] == "closed"
        assert not has_work(saved())
        assert not saved().get("reminder_plans")

    asyncio.run(run())


def test_result_uncertain_send_reuses_message(monkeypatch):
    ann, clock, thread, messages, fail = fixture(monkeypatch)
    clock[0] = BASE + timedelta(days=4)
    with SessionLocal.begin() as db:
        save_result(db, EID, 1, dict(summary="Done", score="", images=[]))
    fail[0] = 1

    async def run():
        await ann.tick(EID)
        clock[0] += timedelta(seconds=181)
        await Announcements(ann.worker).tick(EID)

    asyncio.run(run())
    assert len(messages) == 1
    assert saved()["result_delivery"]["message_id"] == "800"


def test_templates_do_not_copy_date_roster_or_results(monkeypatch):
    ann, clock, thread, messages, fail = fixture(monkeypatch)
    base = {
        key: value for key, value in saved()["config"].items() if key in TEMPLATE_FIELDS
    }
    with SessionLocal.begin() as db:
        row = save_template(db, "c" * 32, None, "Тренировка", base)
        assert save_template(db, "c" * 32, None, "Тренировка", base) == row
        assert (
            not {"starts_at", "responses", "lineup", "result", "status"}
            & row["config"].keys()
        )
        new = events.create(
            db,
            "d" * 32,
            UID,
            {
                **row["config"],
                "starts_at": (BASE + timedelta(days=7)).isoformat(),
                "status": "open",
            },
        )
        assert new["responses"] == {} and new["lineup"] == []
        save_template(db, row["id"], 1, "Тренировка 2", {**base, "title": "Changed"})
        assert new["config"]["title"] == "Тестовый матч"
        with pytest.raises(events.EventError):
            delete_template(db, row["id"], 1)
        delete_template(db, row["id"], 2)
        assert not templates(db)
        assert events.get(db, new["id"])["config"]["title"] == "Тестовый матч"


def test_template_and_result_api_permissions_csrf_and_stale_revision(
    make_user, login_as, monkeypatch
):
    ann, clock, thread, messages, fail = fixture(monkeypatch)
    make_user(101, is_admin=True)
    client = login_as(101)
    base = {
        key: value for key, value in saved()["config"].items() if key in TEMPLATE_FIELDS
    }
    body = dict(id="c" * 32, revision=None, name="Match", config=base)
    endpoint = "/api/admin/forum-events"
    assert client.post(endpoint + "/templates", json=body).status_code == 403
    assert client.post(endpoint + f"/{EID}/result", json={}).status_code == 403
    make_user(102, is_admin=True, is_senior_admin=True)
    client = login_as(102)
    assert (
        client.post(
            endpoint + "/templates", json=body, headers={"X-ST9-CSRF": "wrong"}
        ).status_code
        == 403
    )
    response = client.post(endpoint + "/templates", json=body)
    assert response.status_code == 200, response.text
    assert len(client.get(endpoint).json()["templates"]) == 1
    body["revision"] = 1
    body["name"] = "Renamed"
    assert client.post(endpoint + "/templates", json=body).status_code == 200
    assert client.post(endpoint + "/templates", json=body).status_code == 400
    clock[0] += timedelta(days=4)
    result = {
        "revision": 1,
        "result": dict(summary="Done", score="1:0", images=["javascript:alert(1)"]),
    }
    assert client.post(endpoint + f"/{EID}/result", json=result).status_code == 400
    result["result"]["images"] = ["https://example.test/image.png"]
    assert client.post(endpoint + f"/{EID}/result", json=result).status_code == 200
    assert client.post(endpoint + f"/{EID}/result", json=result).status_code == 400


def test_history_failure_retains_pending_without_resending(monkeypatch):
    ann, clock, thread, messages, fail = fixture(monkeypatch)
    clock[0] = BASE + timedelta(days=3)
    fail[0] = 1

    async def broken(**kwargs):
        raise PermissionError("History unavailable")
        yield

    async def run():
        await ann.tick(EID)
        clock[0] += timedelta(seconds=181)
        thread.history = broken
        await ann.tick(EID)

    asyncio.run(run())
    assert len(messages) == 1
    assert (
        next(iter(saved()["reminder_plans"].values()))["parts"][0]["status"]
        == "pending"
    )
