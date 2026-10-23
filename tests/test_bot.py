import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord_bot


def test_bot_imports_shared_schema_without_logging_in():
    assert discord_bot.User.__table__.c.profile_public is not None
    assert not discord_bot.bot.intents.message_content
    assert not discord_bot.bot.intents.members


def test_bot_respects_hidden_profiles(make_user):
    from database import SessionLocal, User

    uid = make_user(profile_public=False)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=999),
        response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
    )
    with SessionLocal() as db:
        asyncio.run(discord_bot.send_profile_embed(interaction, db.get(User, uid)))
    assert interaction.response.send_message.await_args.args[0] == "🔒 Профиль скрыт"
    assert interaction.response.send_message.await_args.kwargs["ephemeral"]


def test_bot_role_check_rejects_wrong_guild(monkeypatch):
    monkeypatch.setattr(discord_bot, "REQUIRED_GUILD_ID", "123")
    interaction = SimpleNamespace(
        guild_id=456,
        response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
    )
    assert not asyncio.run(discord_bot.verify_interaction(interaction))


def test_steam_lookup_cannot_bypass_hidden_profile(make_user):
    make_user(profile_public=False, steam_id="76561198000000000")
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=999),
        response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
    )
    asyncio.run(
        discord_bot.steam_lookup_command.callback(interaction, "76561198000000000")
    )
    assert interaction.response.send_message.await_args.args[0] == "🔒 Профиль скрыт"
