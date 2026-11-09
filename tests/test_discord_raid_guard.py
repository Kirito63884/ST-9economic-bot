import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands
import pytest

from automation_control import AutomationSetting
from database import SessionLocal
import discord_raid_guard as guard

GUILD = 987654321012345678


@pytest.fixture(autouse=True)
def clear():
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("discord_raid_guard:%")
        ).delete()
    yield
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("discord_raid_guard:%")
        ).delete()


def fixture(monkeypatch, previous=15):
    monkeypatch.setenv("REQUIRED_GUILD_ID", str(GUILD))
    clock = [1800000000.0]
    monkeypatch.setattr(guard, "now", lambda: clock[0])
    guest = MagicMock(spec=discord.TextChannel)
    guest.id = guard.GUEST_CHANNEL
    guest.guild = SimpleNamespace(id=GUILD)
    guest.slowmode_delay = previous

    async def edit(**kwargs):
        guest.slowmode_delay = kwargs["slowmode_delay"]

    guest.edit = AsyncMock(side_effect=edit)
    alert = MagicMock(spec=discord.TextChannel)
    alert.id = guard.ALERT_CHANNEL
    alert.guild = SimpleNamespace(id=GUILD)
    messages = []

    async def send(content, **kwargs):
        message = SimpleNamespace(
            id=1000 + len(messages),
            author=SimpleNamespace(id=999),
            embeds=[kwargs["embed"]],
            edit=AsyncMock(),
        )
        messages.append(message)
        return message

    alert.send = AsyncMock(side_effect=send)
    alert.fetch_message = AsyncMock(
        side_effect=lambda mid: next(m for m in messages if m.id == mid)
    )

    async def history(**kwargs):
        for message in messages:
            yield message

    alert.history = history
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        fetch_channel=AsyncMock(
            side_effect=lambda cid: {
                guard.GUEST_CHANNEL: guest,
                guard.ALERT_CHANNEL: alert,
            }[cid]
        ),
        intents=SimpleNamespace(members=True),
    )
    return guard.RaidGuard(bot), guest, alert, clock, messages


def member(uid, moment, *, bot=False, guild=GUILD):
    return SimpleNamespace(
        id=uid,
        guild=SimpleNamespace(id=guild),
        bot=bot,
        joined_at=datetime.fromtimestamp(moment, timezone.utc),
    )


def saved(worker):
    with SessionLocal() as db:
        return guard.load(db, worker.key)


async def trigger(worker, clock, start=1):
    for uid in range(start, start + 10):
        await worker.joined(member(uid, clock[0]))


def test_tenth_distinct_human_triggers_and_only_target_role_is_mentioned(monkeypatch):
    worker, guest, alert, clock, messages = fixture(monkeypatch)

    async def run():
        for uid in range(1, 10):
            await worker.joined(member(uid, clock[0]))
        for _ in range(4):
            await worker.joined(member(1, clock[0]))
        await worker.joined(member(10, clock[0], bot=True))
        await worker.joined(member(11, clock[0], guild=777))
        await worker.tick()
        guest.edit.assert_not_awaited()
        alert.send.assert_not_awaited()
        await worker.joined(member(10, clock[0]))
        await asyncio.gather(worker.tick(), guard.RaidGuard(worker.bot).tick())
        assert guest.slowmode_delay == 300
        assert guest.edit.await_count == 1 and alert.send.await_count == 1
        kwargs = alert.send.call_args.kwargs
        assert alert.send.call_args.args == (f"<@&{guard.ALERT_ROLE}>",)
        mentions = kwargs["allowed_mentions"].to_dict()
        assert mentions["roles"] == [guard.ALERT_ROLE] and mentions.get("parse") == []
        assert str(guard.GUEST_CHANNEL) in kwargs["embed"].description
        for uid in range(11, 20):
            clock[0] += 1
            await worker.joined(member(uid, clock[0]))
            await worker.tick()
        assert alert.send.await_count == 1

    asyncio.run(run())


def test_rolling_window_boundary_and_old_replays(monkeypatch):
    worker, guest, alert, clock, _ = fixture(monkeypatch)

    async def run():
        await worker.joined(member(1, clock[0]))
        clock[0] += 601
        for uid in range(2, 11):
            await worker.joined(member(uid, clock[0]))
        await worker.tick()
        assert saved(worker)["incident"] is None
        await worker.joined(member(22, clock[0] - 601))
        await worker.joined(member(23, clock[0] + 31))
        assert len(saved(worker)["joins"]) == 9
        await worker.joined(member(11, clock[0]))
        await worker.tick()
        assert guest.slowmode_delay == 300

    asyncio.run(run())


def test_restart_restores_original_after_two_hours_from_latest_join(monkeypatch):
    worker, guest, alert, clock, _ = fixture(monkeypatch, previous=20)

    async def run():
        await trigger(worker, clock)
        await worker.tick()
        clock[0] += 3600
        await worker.joined(member(20, clock[0]))
        restarted = guard.RaidGuard(worker.bot)
        clock[0] += 3601
        await restarted.tick()
        assert guest.slowmode_delay == 300
        clock[0] += 3599
        await restarted.tick()
        assert guest.slowmode_delay == 20 and saved(worker)["incident"] is None
        await restarted.tick()
        assert alert.send.await_count == 1
        clock[0] += 1
        await trigger(restarted, clock, start=30)
        await restarted.tick()
        assert alert.send.await_count == 2

    asyncio.run(run())


def test_moderator_override_and_stricter_initial_delay_are_preserved(monkeypatch):
    worker, guest, alert, clock, _ = fixture(monkeypatch, previous=600)

    async def run():
        await trigger(worker, clock)
        await worker.tick()
        guest.edit.assert_not_awaited()
        await worker.request_release()
        await worker.tick()
        assert guest.slowmode_delay == 600
        clock[0] += 1
        guest.slowmode_delay = 10
        await trigger(worker, clock, start=30)
        await worker.tick()
        guest.slowmode_delay = 60
        await worker.tick()
        assert saved(worker)["incident"] is None and guest.slowmode_delay == 60

    asyncio.run(run())


def test_manual_release_and_duplicate_gateway_replay_do_not_rearm(monkeypatch):
    worker, guest, alert, clock, _ = fixture(monkeypatch)

    async def run():
        await trigger(worker, clock)
        await worker.tick()
        assert await worker.request_release()
        await worker.tick()
        assert guest.slowmode_delay == 15
        assert not await worker.request_release()
        await trigger(worker, clock)
        await worker.tick()
        assert saved(worker)["incident"] is None
        assert alert.send.await_count == 1

    asyncio.run(run())


def test_join_during_restoration_keeps_protection(monkeypatch):
    worker, guest, alert, clock, _ = fixture(monkeypatch)

    async def run():
        await trigger(worker, clock)
        await worker.tick()
        clock[0] += guard.QUIET

        async def edit(**kwargs):
            guest.slowmode_delay = kwargs["slowmode_delay"]
            if guest.slowmode_delay == 15:
                await worker.joined(member(50, clock[0]))

        guest.edit.side_effect = edit
        await worker.tick()
        assert guest.slowmode_delay == 300
        assert saved(worker)["incident"]["last_join"] == clock[0]

    asyncio.run(run())


def test_uncertain_alert_delivery_recovers_without_second_ping(monkeypatch):
    worker, guest, alert, clock, messages = fixture(monkeypatch)
    actual = alert.send.side_effect

    async def send(*args, **kwargs):
        await actual(*args, **kwargs)
        raise TimeoutError()

    alert.send.side_effect = send

    async def run():
        await trigger(worker, clock)
        await worker.tick()
        assert len(messages) == 1
        clock[0] += 61
        await guard.RaidGuard(worker.bot).tick()
        assert alert.send.await_count == 1
        assert saved(worker)["incident"]["message_id"] == str(messages[0].id)

    asyncio.run(run())


def test_unavailable_history_does_not_repeat_uncertain_alert(monkeypatch):
    worker, guest, alert, clock, _ = fixture(monkeypatch)
    alert.send.side_effect = TimeoutError()

    async def history(**kwargs):
        raise PermissionError()
        yield

    alert.history = history

    async def run():
        await trigger(worker, clock)
        await worker.tick()
        clock[0] += 61
        await worker.tick()
        assert alert.send.await_count == 1
        assert saved(worker)["incident"]["error"]

    asyncio.run(run())


def test_failed_slowmode_still_warns_then_updates_without_new_ping(monkeypatch):
    worker, guest, alert, clock, messages = fixture(monkeypatch)
    actual = guest.edit.side_effect
    guest.edit.side_effect = PermissionError()

    async def run():
        await trigger(worker, clock)
        await worker.tick()
        assert "Не удалось" in alert.send.call_args.kwargs["embed"].description
        assert guest.slowmode_delay == 15
        guest.edit.side_effect = actual
        clock[0] += 61
        await worker.tick()
        assert guest.slowmode_delay == 300 and alert.send.await_count == 1
        assert messages[0].edit.await_count == 1

    asyncio.run(run())


def test_channels_must_belong_to_configured_guild(monkeypatch):
    worker, guest, alert, clock, _ = fixture(monkeypatch)
    guest.guild.id = 777
    alert.guild.id = 777

    async def run():
        await trigger(worker, clock)
        await worker.tick()
        guest.edit.assert_not_awaited()
        alert.send.assert_not_awaited()

    asyncio.run(run())


def test_commands_require_manage_channels_and_right_guild(monkeypatch):
    monkeypatch.setenv("REQUIRED_GUILD_ID", str(GUILD))
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    worker = guard.register_raid_guard(bot)
    assert worker.joined in bot.extra_events["on_member_join"]
    assert worker.ready in bot.extra_events["on_resumed"]

    async def run():
        for guild, manage in ((GUILD, False), (777, True), (None, True)):
            interaction = SimpleNamespace(
                guild_id=guild,
                permissions=SimpleNamespace(manage_channels=manage),
                response=SimpleNamespace(send_message=AsyncMock()),
            )
            await bot.tree.get_command("raid_off").callback(interaction)
            assert interaction.response.send_message.call_args.kwargs["ephemeral"]
        assert saved(worker)["incident"] is None
        interaction = SimpleNamespace(
            guild_id=GUILD,
            permissions=SimpleNamespace(manage_channels=True),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await bot.tree.get_command("raid_status").callback(interaction)
        assert (
            "Server Members Intent"
            in interaction.response.send_message.call_args.args[0]
        )

    asyncio.run(run())


def test_exact_ten_minute_boundary_is_included(monkeypatch):
    worker, guest, alert, clock, _ = fixture(monkeypatch)

    async def run():
        await worker.joined(member(1, clock[0]))
        clock[0] += 600
        for uid in range(2, 11):
            await worker.joined(member(uid, clock[0]))
        await worker.tick()
        assert guest.slowmode_delay == 300

    asyncio.run(run())


def test_uncertain_channel_edit_preserves_original_for_later_restore(monkeypatch):
    worker, guest, alert, clock, _ = fixture(monkeypatch, previous=20)
    actual = guest.edit.side_effect

    async def edit(**kwargs):
        await actual(**kwargs)
        raise TimeoutError()

    guest.edit.side_effect = edit

    async def run():
        await trigger(worker, clock)
        await worker.tick()
        assert guest.slowmode_delay == 300
        assert saved(worker)["incident"]["previous"] == 20
        guest.edit.side_effect = actual
        clock[0] += 61
        await worker.tick()
        assert saved(worker)["incident"]["enabled"]
        await worker.request_release()
        await worker.tick()
        assert guest.slowmode_delay == 20

    asyncio.run(run())
