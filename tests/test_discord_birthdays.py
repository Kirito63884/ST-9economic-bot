import asyncio
from datetime import date, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from automation_control import AutomationSetting
from database import SessionLocal
from discord_birthdays import (
    BirthdayWorker,
    CHANNEL_ID,
    REGISTRY_KEY,
    ZONE,
    age_text,
    anniversary,
    greeting,
    may_edit,
    parse_date,
    read,
    register_birthdays,
    validate_registry,
)
from roster_settings import write
from scripts.import_birthdays import import_file, import_plan

UID = "123456789012345678"
GUILD = 987654321012345678
MOMENT = datetime(2026, 11, 5, 7, tzinfo=ZONE)
RECORD = {"name": "Вымышленный игрок", "date": "05.11.2000"}


@pytest.fixture(autouse=True)
def clear_birthdays():
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("discord_birthdays:%")
        ).delete()
    yield
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("discord_birthdays:%")
        ).delete()


def saved(key=REGISTRY_KEY):
    with SessionLocal() as db:
        return read(db, key, {})


def fixture(monkeypatch):
    monkeypatch.setenv("REQUIRED_GUILD_ID", str(GUILD))
    member = SimpleNamespace(
        id=int(UID),
        mention=f"<@{UID}>",
        bot=False,
        display_avatar=SimpleNamespace(url="https://example.test/avatar.png"),
    )
    message = SimpleNamespace(
        id=555,
        author=SimpleNamespace(id=999),
        embeds=[],
        thread=None,
        add_reaction=AsyncMock(),
        create_thread=AsyncMock(),
    )
    channel = SimpleNamespace(
        id=CHANNEL_ID,
        guild=SimpleNamespace(id=GUILD, fetch_member=AsyncMock(return_value=member)),
        send=AsyncMock(return_value=message),
    )

    async def history(**kwargs):
        yield message

    channel.history = history
    bot = SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda _: channel)
    with SessionLocal.begin() as db:
        write(db, REGISTRY_KEY, {UID: RECORD})
    return BirthdayWorker(bot), channel, message, member


def test_calendar_and_russian_age():
    assert anniversary(date(2000, 2, 29), 2027) == date(2027, 3, 1)
    assert anniversary(date(2000, 2, 29), 2028) == date(2028, 2, 29)
    assert [age_text(n) for n in (1, 2, 5, 11, 21, 24, 111)] == [
        "1 год",
        "2 года",
        "5 лет",
        "11 лет",
        "21 год",
        "24 года",
        "111 лет",
    ]
    for bad in ("31.02.2000", "1.1.2000", "01.01.3000", None):
        with pytest.raises(ValueError):
            parse_date(bad)
    assert validate_registry({UID: RECORD}) == {UID: RECORD}


def test_moscow_schedule_duplicate_and_restart(monkeypatch):
    worker, channel, message, member = fixture(monkeypatch)

    async def run():
        await worker.tick(MOMENT - timedelta(minutes=1))
        channel.send.assert_not_awaited()
        await asyncio.gather(
            worker.tick(MOMENT), BirthdayWorker(worker.bot).tick(MOMENT)
        )
        await BirthdayWorker(worker.bot).tick(MOMENT + timedelta(hours=1))

    asyncio.run(run())
    channel.send.assert_awaited_once()
    kwargs = channel.send.call_args.kwargs
    assert kwargs["allowed_mentions"].users == [member]
    assert not kwargs["allowed_mentions"].everyone
    assert "26 лет" in kwargs["embed"].description
    assert "5 ноября 2000 года" in kwargs["embed"].description
    assert saved(f"discord_birthdays:sent:2026:{UID}")["message_id"] == "555"
    message.create_thread.assert_awaited_once()


def test_uncertain_send_recovers_history_without_duplicate(monkeypatch):
    worker, channel, message, member = fixture(monkeypatch)
    message.embeds = [greeting(member, date(2000, 11, 5), MOMENT)]
    channel.send.side_effect = TimeoutError("accepted but response lost")

    async def run():
        await worker.tick(MOMENT)
        await BirthdayWorker(worker.bot).tick(MOMENT + timedelta(minutes=2))
        channel.send.side_effect = None
        await BirthdayWorker(worker.bot).tick(MOMENT + timedelta(minutes=11))

    asyncio.run(run())
    assert channel.send.await_count == 1
    assert saved(f"discord_birthdays:sent:2026:{UID}")["status"] == "sent"


def test_failed_send_retries_only_after_history_was_read(monkeypatch):
    worker, channel, message, member = fixture(monkeypatch)

    async def empty(**kwargs):
        for item in []:
            yield item

    channel.history = empty
    channel.send.side_effect = TimeoutError()

    async def run():
        await worker.tick(MOMENT)
        channel.send.side_effect = None
        await worker.tick(MOMENT + timedelta(minutes=11))

    asyncio.run(run())
    assert channel.send.await_count == 2
    assert saved(f"discord_birthdays:sent:2026:{UID}")["status"] == "sent"


def test_history_permission_failure_never_resends(monkeypatch):
    worker, channel, message, member = fixture(monkeypatch)

    async def broken(**kwargs):
        raise PermissionError()
        yield  # pragma: no cover

    channel.history = broken
    with SessionLocal.begin() as db:
        write(
            db, f"discord_birthdays:sent:2026:{UID}", {"status": "pending", "until": 0}
        )
    asyncio.run(worker.tick(MOMENT))
    channel.send.assert_not_awaited()
    assert saved() == {UID: RECORD}


def test_departed_member_kept_and_wrong_guild_blocked(monkeypatch):
    worker, channel, message, member = fixture(monkeypatch)
    channel.guild.fetch_member.side_effect = discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"), "missing"
    )
    asyncio.run(worker.tick(MOMENT))
    assert saved() == {UID: RECORD}
    channel.send.assert_not_awaited()
    channel.guild.id = 3
    with pytest.raises(RuntimeError):
        asyncio.run(worker.tick(MOMENT))


def test_removed_record_cancels_claim(monkeypatch):
    worker, channel, message, member = fixture(monkeypatch)
    with SessionLocal.begin() as db:
        write(db, REGISTRY_KEY, {})
    asyncio.run(worker.deliver(channel, member, UID, RECORD, MOMENT))
    channel.send.assert_not_awaited()


def test_edit_permission():
    actor = SimpleNamespace(id=1, guild_permissions=SimpleNamespace(manage_guild=False))
    interaction = SimpleNamespace(user=actor)
    assert may_edit(interaction, actor)
    assert not may_edit(interaction, SimpleNamespace(id=2))
    actor.guild_permissions.manage_guild = True
    assert may_edit(interaction, SimpleNamespace(id=2))


def test_import_idempotent_conflict_and_stale_plan(tmp_path):
    source = tmp_path / "birthdays.json"
    source.write_text(json.dumps({UID: RECORD}))

    async def run():
        plan = await import_file(source)
        assert plan["additions"] == 1 and not saved()
        with pytest.raises(ValueError):
            await import_file(source, "wrong-digest")
        await import_file(source, plan["digest"])
        assert (await import_file(source))["additions"] == 0

    asyncio.run(run())
    assert saved() == {UID: RECORD}
    changed = {UID: {**RECORD, "date": "06.11.2000"}}
    with pytest.raises(ValueError):
        import_plan(saved(), changed)
    assert saved() == {UID: RECORD}


def test_commands_register_without_connecting(monkeypatch):
    from discord.ext import commands

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    register_birthdays(bot)
    assert {c.name for c in bot.tree.get_commands()} == {
        "add_bd",
        "remove_bd",
        "my_bd",
        "bd_help",
    }
    assert "on_member_remove" not in bot.extra_events


def test_explicit_legacy_year_preserved_and_announced(monkeypatch, tmp_path):
    worker, channel, message, member = fixture(monkeypatch)
    old = {UID: {**RECORD, "date": "05.11.1814"}}
    assert validate_registry(old) == old
    with SessionLocal.begin() as db:
        write(db, REGISTRY_KEY, old)
    asyncio.run(worker.tick(MOMENT))
    channel.send.assert_awaited_once()
    assert "212 лет" in channel.send.call_args.kwargs["embed"].description
    assert saved() == old
    source = tmp_path / "old.json"
    source.write_text(json.dumps(old))
    assert asyncio.run(import_file(source))["paused"] == 0


def test_commands_enforce_owner_scope_and_guild(monkeypatch):
    from discord.ext import commands

    monkeypatch.setenv("REQUIRED_GUILD_ID", str(GUILD))
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    register_birthdays(bot)
    actor = SimpleNamespace(
        id=int(UID),
        bot=False,
        display_name="Fixture",
        guild_permissions=SimpleNamespace(manage_guild=False),
    )
    target = SimpleNamespace(id=int(UID) + 1, bot=False, display_name="Other fixture")
    response = SimpleNamespace(
        is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock()
    )
    interaction = SimpleNamespace(
        user=actor,
        guild_id=GUILD,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )

    async def run():
        await bot.tree.get_command("add_bd").callback(interaction, "05.11.2000", target)
        assert not saved()
        await bot.tree.get_command("add_bd").callback(interaction, "05.11.2000")
        assert saved()[UID]["date"] == "05.11.2000"
        await bot.tree.get_command("remove_bd").callback(interaction, target)
        assert UID in saved()
        interaction.guild_id = 1
        await bot.tree.get_command("remove_bd").callback(interaction)
        assert UID in saved()
        interaction.guild_id = GUILD
        await bot.tree.get_command("remove_bd").callback(interaction)
        assert not saved()

    asyncio.run(run())
    assert all(
        call.kwargs["ephemeral"] for call in response.send_message.call_args_list
    )


def test_installer_patch_preserves_other_features_and_is_idempotent(tmp_path):
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/update_birthdays.sh").read_text()
    patch = script.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    source, target = tmp_path / "installed.py", tmp_path / "new.py"
    text = "LOCAL_FEATURE = True\n# ST9_SQUAD_BOT_END\n"
    source.write_text(text)
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-", str(source), str(target)],
            input=patch,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert target.read_text().count("register_birthdays(bot)") == 1
        assert "LOCAL_FEATURE = True" in target.read_text()
        source.write_text(target.read_text())
    source.write_text("UNKNOWN_STRUCTURE = True\n")
    result = subprocess.run(
        [sys.executable, "-", str(source), str(target)],
        input=patch,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert source.read_text() == "UNKNOWN_STRUCTURE = True\n"
