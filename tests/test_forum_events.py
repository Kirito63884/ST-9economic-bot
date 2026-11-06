import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import forum_events as events
from automation_control import AutomationSetting
from database import SessionLocal
from discord_forum_events import (
    EventView,
    ForumWorker,
    register_forum_events,
    render,
    staff,
)

EID = "a" * 32
UID = "123456789012345678"
GUILD = 999999999999999999
PATH = "/api/admin/forum-events"


def data():
    return dict(
        title="ST-9 vs Fixture",
        starts_at=(events.now() + timedelta(days=2))
        .astimezone(events.ZONE)
        .isoformat(),
        capacity=2,
        teams="ST-9 / Fixture",
        map="Map",
        server="Test",
        rules="Правила",
        image_url="",
        status="open",
    )


@pytest.fixture(autouse=True)
def clean_events():
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("forum%event%:%")
        ).delete()
    yield
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("forum%event%:%")
        ).delete()


def seed():
    with SessionLocal.begin() as db:
        return events.create(db, EID, UID, data())


def saved():
    with SessionLocal() as db:
        return events.get(db, EID)


def test_site_permissions_idempotent_creation_and_stale_edit(make_user, login_as):
    make_user(101, is_admin=True)
    client = login_as(101)
    body = dict(id=EID, revision=None, config=data(), lineup=[])
    assert client.get(PATH).status_code == 403
    assert client.post(PATH, json=body).status_code == 403
    make_user(102, is_admin=True, is_senior_admin=True)
    client = login_as(102)
    first = client.post(PATH, json=body)
    assert first.status_code == 200, first.text
    assert client.post(PATH, json=body).json()["event"]["id"] == EID
    assert len(client.get(PATH).json()["events"]) == 1
    body["revision"] = 1
    body["config"]["title"] = "Новое название"
    assert client.post(PATH, json=body).status_code == 200
    assert client.post(PATH, json=body).status_code == 400
    assert saved()["config"]["title"] == "Новое название"


def test_rsvp_lineup_capacity_repeat_and_close():
    seed()
    with SessionLocal.begin() as db:
        event = events.get(db, EID)
        event["thread_id"] = "777"
        events.save(db, event)
        first = events.respond(db, EID, UID, "Fixture", "yes", "777")
        again = events.respond(db, EID, UID, "Fixture", "yes", "777")
        assert again["revision"] == first["revision"]
        for uid in ("2", "3"):
            event = events.respond(db, EID, uid, "Other", "yes", "777")
        with pytest.raises(events.EventError):
            events.update(db, EID, event["revision"], event["config"], [UID, "2", "3"])
        event = events.update(db, EID, event["revision"], event["config"], [UID, "2"])
        event = events.respond(db, EID, UID, "Fixture", "maybe", "777")
        assert event["lineup"] == ["2"]
        with pytest.raises(events.EventError):
            events.respond(db, EID, UID, "Fixture", "yes", "wrong-thread")
        event = events.update(
            db,
            EID,
            event["revision"],
            {**event["config"], "status": "closed"},
            event["lineup"],
        )
        with pytest.raises(events.EventError):
            events.respond(db, EID, UID, "Fixture", "yes", "777")


@pytest.mark.parametrize(
    "change",
    [
        {"starts_at": "bad"},
        {"capacity": True},
        {"capacity": 101},
        {"image_url": "javascript:alert(1)"},
        {"image_url": "https://user:secret@example.test/x"},
        {"title": ""},
        {"status": "invented"},
    ],
)
def test_bad_config_rejected(change):
    with pytest.raises(events.EventError):
        events.config({**data(), **change})


def fake_worker(monkeypatch):
    monkeypatch.setenv("REQUIRED_GUILD_ID", str(GUILD))
    forum = MagicMock(spec=discord.ForumChannel)
    forum.guild = SimpleNamespace(id=GUILD, active_threads=AsyncMock(return_value=[]))
    forum.flags = SimpleNamespace(require_tag=False)
    forum.available_tags = []

    async def archived(**kwargs):
        for item in []:
            yield item

    forum.archived_threads = archived
    thread = SimpleNamespace(
        id=777,
        parent_id=events.FORUM_ID,
        owner_id=999,
        name="ST-9 vs Fixture • #1",
        archived=False,
        edit=AsyncMock(),
        fetch_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
    )
    forum.create_thread = AsyncMock(return_value=SimpleNamespace(thread=thread))
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_channel=lambda _: forum,
        fetch_channel=AsyncMock(return_value=thread),
        add_view=MagicMock(),
    )
    return ForumWorker(bot), forum, thread


def test_publish_edit_restart_and_ambiguous_creation(monkeypatch):
    seed()
    worker, forum, thread = fake_worker(monkeypatch)

    async def run():
        # Simulate Discord accepting the post but dropping the response.
        forum.create_thread.side_effect = TimeoutError()
        await worker.publish(EID)
        assert saved()["thread_id"] is None
        forum.guild.active_threads.return_value = [thread]
        with SessionLocal.begin() as db:
            event = events.get(db, EID)
            event["delivery"]["until"] = 0
            events.save(db, event)
        restarted = ForumWorker(worker.bot)
        await restarted.publish(EID)
        assert saved()["thread_id"] == "777"
        assert forum.create_thread.await_count == 1
        restarted.restore_views()
        assert worker.bot.add_view.call_args.kwargs["message_id"] == 777
        await restarted.publish(EID)
        assert forum.create_thread.await_count == 1
        with SessionLocal.begin() as db:
            event = events.get(db, EID)
            events.update(
                db, EID, event["revision"], {**event["config"], "title": "Updated"}, []
            )
        await restarted.publish(EID)
        thread.edit.assert_awaited_with(name="Updated • #1")

    asyncio.run(run())


def test_concurrent_workers_one_creation_and_autoclose(monkeypatch):
    event = seed()
    worker, forum, thread = fake_worker(monkeypatch)

    async def run():
        await asyncio.gather(worker.publish(EID), ForumWorker(worker.bot).publish(EID))
        assert forum.create_thread.await_count == 1
        future = events.now() + timedelta(days=3)
        monkeypatch.setattr(events, "now", lambda: future)
        await worker.publish(EID)
        assert saved()["config"]["status"] == "closed"
        assert saved()["published_revision"] == saved()["revision"]

    asyncio.run(run())
    assert event["config"]["status"] == "open"


def test_history_failure_never_creates_duplicate(monkeypatch):
    seed()
    worker, forum, thread = fake_worker(monkeypatch)
    forum.guild.active_threads.side_effect = RuntimeError("unavailable")
    asyncio.run(worker.publish(EID))
    forum.create_thread.assert_not_awaited()
    assert saved()["error"]


def test_render_limits_mentions_and_private_participant_data():
    event = seed()
    event["responses"] = {
        str(i): {
            "choice": list(("yes", "maybe", "no"))[i % 3],
            "name": "@everyone **" + "x" * 90,
        }
        for i in range(1000)
    }
    event["lineup"] = ["0", "3"]
    embed = render(event)
    assert len(embed) < 6000
    assert all(
        len(field.value) <= 1024 and "@everyone" not in field.value
        for field in embed.fields
    )


def test_commands_registered_and_staff_from_site(make_user):
    from discord.ext import commands

    make_user(int(UID), is_admin=True)
    with SessionLocal() as db:
        with pytest.raises(events.EventError):
            staff(db, UID)
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    register_forum_events(bot)
    assert {command.name for command in bot.tree.get_commands()} == {
        "event_create",
        "event_edit",
        "event_result",
        "event_from_template",
    }

    async def run():
        view = EventView(bot.st9_forum_events, EID)
        assert view.is_persistent()

    asyncio.run(run())


def test_web_csrf_and_unknown_fields(make_user, login_as):
    make_user(101, is_admin=True, is_senior_admin=True)
    client = login_as(101)
    body = dict(id=EID, revision=None, config=data(), lineup=[])
    assert (
        client.post(
            PATH, json=body, headers={"Origin": "https://foreign.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(PATH, json=body, headers={"X-ST9-CSRF": "wrong"}).status_code == 403
    )
    body["unexpected"] = True
    assert client.post(PATH, json=body).status_code == 400
    with SessionLocal() as db:
        assert events.all_events(db) == []


def test_button_checks_live_membership_thread_and_preserves_other_responses(
    monkeypatch,
):
    seed()
    worker, forum, thread = fake_worker(monkeypatch)
    member = SimpleNamespace(id=int(UID), display_name="Fixture", roles=[], bot=False)
    guild = SimpleNamespace(fetch_member=AsyncMock(return_value=member))
    interaction = SimpleNamespace(
        guild_id=GUILD,
        guild=guild,
        user=member,
        channel_id=777,
        message=SimpleNamespace(id=777),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    async def run():
        await worker.publish(EID)
        view = EventView(worker, EID)
        interaction.channel_id = 888
        await view.action(interaction, "yes")
        assert not saved()["responses"]
        interaction.channel_id = 777
        monkeypatch.setenv("REQUIRED_ROLE_ID", "123")
        await view.action(interaction, "yes")
        assert not saved()["responses"]
        member.roles = [SimpleNamespace(id=123)]
        await view.action(interaction, "yes")
        assert saved()["responses"][UID]["choice"] == "yes"
        await view.action(interaction, "maybe")
        assert saved()["responses"][UID]["choice"] == "maybe"
        interaction.guild_id = 1
        await view.action(interaction, "no")
        assert saved()["responses"][UID]["choice"] == "maybe"

    asyncio.run(run())


def test_installer_preserves_birthday_and_other_hooks(tmp_path):
    from pathlib import Path
    from scripts.patch_forum_events import patch

    root = Path(__file__).resolve().parents[1]
    app, stage = tmp_path / "app", tmp_path / "stage"
    (app / "templates").mkdir(parents=True)
    for name in ("discord_bot.py", "automation_admin.py", "templates/admin.html"):
        (app / name).write_text((root / name).read_text())
    bot = app / "discord_bot.py"
    bot.write_text(
        bot.read_text().replace(
            "# ST9_SQUAD_BOT_END", "register_birthdays(bot)\n# ST9_SQUAD_BOT_END"
        )
    )
    patch(app, stage)
    assert "register_birthdays(bot)" in (stage / "discord_bot.py").read_text()
    assert (stage / "discord_bot.py").read_text().count(
        "register_forum_events(bot)"
    ) == 1
    assert (stage / "templates/admin.html").read_text().count(
        'id="tab-forum-events"'
    ) == 1
    patched = (stage / "discord_bot.py").read_text()
    patch(stage, stage)
    assert (stage / "discord_bot.py").read_text() == patched
    (app / "automation_admin.py").write_text("unknown layout")
    before = (stage / "discord_bot.py").read_text()
    with pytest.raises(ValueError):
        patch(app, stage)
    assert (stage / "discord_bot.py").read_text() == before
