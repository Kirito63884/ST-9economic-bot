"""Private-channel enforcement and durable Discord report delivery using fake I/O."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import assembly_reports
from assembly_attendance import AssemblyAssessment, AssemblyReportCheck, AssemblyWindow
from assembly_reports import AssemblyReportPublisher
from automation_control import AutomationSetting
from database import SessionLocal

NOW = datetime(2026, 10, 24, 19, 16)


@pytest.fixture(autouse=True)
def clear(monkeypatch):
    monkeypatch.setenv("REQUIRED_GUILD_ID", "1")
    with SessionLocal.begin() as db:
        for model in (AssemblyAssessment, AssemblyReportCheck, AssemblyWindow):
            db.query(model).delete()
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("assembly_report:%")
        ).delete(synchronize_session=False)
    monkeypatch.setattr(
        assembly_reports,
        "report_pages",
        lambda db, now: (
            "2026-10-24",
            ["Сводка группы: Игрок <img>", "Присутствовали: 2"],
        ),
    )


def publisher(open_channel=False):
    history = []
    default = SimpleNamespace(id=1)
    guild = SimpleNamespace(id=1, default_role=default)

    async def iterator(**kwargs):
        for message in history:
            yield message

    async def send(**kwargs):
        message = SimpleNamespace(
            id=300 + len(history),
            author=SimpleNamespace(id=99),
            webhook_id=None,
            embeds=[kwargs["embed"]],
            edit=AsyncMock(),
        )
        history.append(message)
        return message

    async def fetch(mid):
        return next(m for m in history if m.id == mid)

    channel = SimpleNamespace(
        guild=guild,
        permissions_for=lambda who: SimpleNamespace(view_channel=open_channel),
        history=iterator,
        send=AsyncMock(side_effect=send),
        fetch_message=AsyncMock(side_effect=fetch),
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=99), get_channel=lambda cid: channel)
    return AssemblyReportPublisher(bot), channel, history


def test_one_report_reused_on_retry_and_update():
    async def run():
        worker, channel, history = publisher()
        assert await worker.sync_once(NOW)
        assert channel.send.await_count == 2
        restarted = AssemblyReportPublisher(worker.bot)
        assert await restarted.sync_once(NOW)
        assert channel.send.await_count == 2
        assert channel.send.call_args.kwargs["allowed_mentions"].everyone is False
        assert all(m.embeds[0].title == "Итоги субботнего сбора" for m in history)

    asyncio.run(run())


def test_report_waits_until_2215_moscow_and_blocks_public_channel():
    async def run():
        worker, channel, _ = publisher(open_channel=True)
        assert await worker.sync_once(datetime(2026, 10, 24, 19, 14)) is False
        with pytest.raises(ValueError, match="@everyone"):
            await worker.sync_once(NOW)
        channel.send.assert_not_awaited()

    asyncio.run(run())


def test_interrupted_send_recovers_existing_message_instead_of_duplicate():
    async def run():
        worker, channel, history = publisher()
        original = worker.save
        calls = 0

        async def failing(key, pages):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("interrupted after Discord send")
            await original(key, pages)

        worker.save = failing
        with pytest.raises(RuntimeError):
            await worker.sync_once(NOW)
        assert len(history) == 1
        await AssemblyReportPublisher(worker.bot).sync_once(NOW)
        assert len(history) == 2 and channel.send.await_count == 2
        history[0].edit.assert_awaited_once()

    asyncio.run(run())


def test_report_edits_corrected_data_in_place(monkeypatch):
    async def run():
        worker, channel, history = publisher()
        await worker.sync_once(NOW)
        monkeypatch.setattr(
            assembly_reports,
            "report_pages",
            lambda db, now: ("2026-10-24", ["Исправленная сводка"]),
        )
        await worker.sync_once(NOW)
        assert channel.send.await_count == 2
        assert (
            history[0].edit.call_args.kwargs["embed"].description
            == "Исправленная сводка"
        )
        assert (
            "Сводка обновлена" in history[1].edit.call_args.kwargs["embed"].description
        )

    asyncio.run(run())
