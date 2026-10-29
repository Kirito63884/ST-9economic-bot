"""Role safety, atomic toggles and recovery of the persistent Discord panel."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from automation_control import AutomationSetting
from database import SessionLocal
from discord_specialties import (
    CHANNEL_ID,
    PANEL_MARKER,
    SPECIALTIES,
    SpecialtyError,
    SpecialtyPanel,
    SpecialtyView,
    check_role,
    panel_embed,
    resolve_roles,
    validate_mapping,
)


class Role:
    def __init__(self, rid, name, position=1, permissions=None, managed=False):
        self.id, self.name, self.position = rid, name, position
        self.permissions = permissions or discord.Permissions.none()
        self.managed = managed

    def __ge__(self, other):
        return self.position >= other.position


def roles():
    return [
        Role(100 + i, "✥ " + label.replace("ё", "е"))
        for i, (_, label, *_) in enumerate(SPECIALTIES)
    ]


def mapping():
    return {key: str(100 + i) for i, (key, *_) in enumerate(SPECIALTIES)}


@pytest.fixture(autouse=True)
def clear_state(monkeypatch):
    monkeypatch.setenv("REQUIRED_GUILD_ID", "1")
    monkeypatch.delenv("DISCORD_SPECIALTY_ROLE_IDS", raising=False)
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("specialty_panel:%")
        ).delete(synchronize_session=False)


def setup_panel():
    guild = SimpleNamespace(id=1)
    available = roles()
    member = SimpleNamespace(id=42, bot=False, roles=[Role(900, "Unrelated")])

    async def add(role, **kwargs):
        member.roles.append(role)

    async def remove(role, **kwargs):
        member.roles[:] = [r for r in member.roles if r.id != role.id]

    member.add_roles = AsyncMock(side_effect=add)
    member.remove_roles = AsyncMock(side_effect=remove)
    bot_member = SimpleNamespace(
        id=99,
        top_role=Role(999, "Bot", 20),
        guild_permissions=discord.Permissions(manage_roles=True),
    )
    guild.fetch_roles = AsyncMock(return_value=available)
    guild.fetch_member = AsyncMock(
        side_effect=lambda uid: bot_member if uid == 99 else member
    )
    message = SimpleNamespace(
        id=2000,
        author=SimpleNamespace(id=99),
        webhook_id=None,
        embeds=[panel_embed()],
        edit=AsyncMock(),
    )
    history = []

    async def iter_history(**kwargs):
        for item in history:
            yield item

    async def send(**kwargs):
        history.append(message)
        return message

    channel = SimpleNamespace(
        id=CHANNEL_ID,
        guild=guild,
        send=AsyncMock(side_effect=send),
        fetch_message=AsyncMock(return_value=message),
        history=iter_history,
        permissions_for=lambda _: discord.Permissions(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
        ),
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=99), get_channel=lambda _: channel)
    panel = SpecialtyPanel(bot)
    interaction = SimpleNamespace(
        guild_id=1,
        guild=guild,
        channel_id=CHANNEL_ID,
        channel=channel,
        user=SimpleNamespace(id=42),
        message=message,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    return panel, interaction, member, available, channel, history


def test_unique_existing_names_with_icons_and_spelling_variants():
    assert resolve_roles(roles()) == mapping()
    duplicates = roles() + [Role(500, "Медик")]
    with pytest.raises(SpecialtyError, match="найдено ролей 2"):
        resolve_roles(duplicates)
    with pytest.raises(SpecialtyError, match="найдено ролей 0"):
        resolve_roles(roles()[1:])
    # A longer name is never guessed from a substring.
    r = roles()
    r[0].name = "Лучший стрелок"
    with pytest.raises(SpecialtyError):
        resolve_roles(r)


def test_saved_ids_survive_rename_and_override_requires_complete_unique_map(
    monkeypatch,
):
    assert resolve_roles([], mapping()) == mapping()
    monkeypatch.setenv("DISCORD_SPECIALTY_ROLE_IDS", "{not-json")
    with pytest.raises(SpecialtyError):
        resolve_roles(roles())
    for invalid in (
        {},
        {**mapping(), "pilot": "100"},
        {**mapping(), "pilot": "0"},
        {**mapping(), "pilot": 108},
        {**mapping(), "pilot": "1" * 100},
    ):
        with pytest.raises(SpecialtyError):
            validate_mapping(invalid)


@pytest.mark.parametrize(
    "permission",
    [
        "administrator",
        "manage_roles",
        "manage_guild",
        "ban_members",
        "manage_channels",
        "manage_webhooks",
        "moderate_members",
        "manage_messages",
        "mention_everyone",
        "move_members",
        "manage_nicknames",
        "manage_expressions",
    ],
)
def test_privileged_roles_cannot_be_self_assigned(permission):
    role = Role(100, "Стрелок", permissions=discord.Permissions(**{permission: True}))
    bot_member = SimpleNamespace(
        top_role=Role(999, "Bot", 20),
        guild_permissions=discord.Permissions(manage_roles=True),
    )
    with pytest.raises(SpecialtyError):
        check_role(role, SimpleNamespace(id=1), bot_member)


def test_everyone_managed_membership_and_hierarchy_guards(monkeypatch):
    bot_member = SimpleNamespace(
        top_role=Role(999, "Bot", 20),
        guild_permissions=discord.Permissions(manage_roles=True),
    )
    guild = SimpleNamespace(id=1)
    for role in (
        None,
        Role(1, "@everyone"),
        Role(100, "Managed", managed=True),
        Role(101, "Above bot", position=20),
    ):
        with pytest.raises(SpecialtyError):
            check_role(role, guild, bot_member)
    monkeypatch.setenv("REQUIRED_ROLE_ID", "100")
    with pytest.raises(SpecialtyError):
        check_role(Role(100, "Membership"), guild, bot_member)
    monkeypatch.delenv("REQUIRED_ROLE_ID")
    bot_member.guild_permissions.manage_roles = False
    with pytest.raises(SpecialtyError):
        check_role(Role(100, "Стрелок"), guild, bot_member)


def test_atomic_toggle_preserves_other_specialties_and_unrelated_roles():
    async def run():
        panel, interaction, member, _, _, _ = setup_panel()
        await panel.save_state(
            {"guild_id": "1", "roles": mapping(), "message_id": "2000"}
        )
        await panel.toggle(interaction, "medic")
        await panel.toggle(interaction, "pilot")
        assert {r.id for r in member.roles} == {900, 101, 108}
        await panel.toggle(interaction, "medic")
        assert {r.id for r in member.roles} == {900, 108}
        assert member.add_roles.call_args.kwargs["atomic"] is True
        assert member.remove_roles.call_args.kwargs["atomic"] is True
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
        assert panel.bot.get_channel(CHANNEL_ID).guild.fetch_member.await_count == 6

    asyncio.run(run())


@pytest.mark.parametrize(
    "attack",
    [
        "guild",
        "channel",
        "message",
        "author",
        "webhook",
        "key",
        "bot",
        "hidden",
        "permissions",
    ],
)
def test_forged_panel_and_changed_permissions_fail_closed(attack):
    async def run():
        panel, i, member, available, channel, _ = setup_panel()
        await panel.save_state(
            {"guild_id": "1", "roles": mapping(), "message_id": "2000"}
        )
        key = "medic"
        if attack == "guild":
            i.guild_id = 2
        if attack == "channel":
            i.channel_id = 2
        if attack == "message":
            i.message.id = 2
        if attack == "author":
            i.message.author.id = 5
        if attack == "webhook":
            i.message.webhook_id = 5
        if attack == "key":
            key = "administrator"
        if attack == "bot":
            member.bot = True
        if attack == "hidden":
            channel.permissions_for = lambda _: discord.Permissions.none()
        if attack == "permissions":
            available[1].permissions.administrator = True
        await panel.toggle(i, key)
        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()

    asyncio.run(run())


def test_restart_reuses_message_and_pinned_roles_even_if_renamed():
    async def run():
        panel, _, _, available, channel, _ = setup_panel()
        await panel.sync_once()
        channel.send.assert_awaited_once()
        assert panel.load_state()["message_id"] == "2000"
        available[1].name = "Переименованный медик"
        restarted = SpecialtyPanel(panel.bot)
        await restarted.sync_once()
        channel.send.assert_awaited_once()
        channel.fetch_message.return_value.edit.assert_awaited_once()
        view = SpecialtyView(restarted)
        assert view.is_persistent()
        assert len({button.custom_id for button in view.children}) == 9
        assert all(
            button.custom_id.startswith("st9:specialty:") for button in view.children
        )

    asyncio.run(run())


def test_interrupted_save_recovers_own_message_and_ignores_probot():
    async def run():
        panel, _, _, _, channel, history = setup_panel()
        foreign = copy.copy(channel.fetch_message.return_value)
        foreign.author = SimpleNamespace(id=10000)
        history.append(foreign)
        original_save = panel.save_state
        calls = 0

        async def interrupted(state):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Crash after Discord accepted send")
            await original_save(state)

        panel.save_state = interrupted
        with pytest.raises(RuntimeError):
            await panel.sync_once()
        assert panel.load_state()["message_id"] is None
        restarted = SpecialtyPanel(panel.bot)
        await restarted.sync_once()
        channel.send.assert_awaited_once()
        assert restarted.load_state()["message_id"] == "2000"
        assert history[0].author.id == 10000
        assert panel_embed().footer.text == PANEL_MARKER

    asyncio.run(run())


def test_missing_roles_prevent_partial_publication():
    async def run():
        panel, _, _, available, channel, _ = setup_panel()
        available.pop()
        with pytest.raises(SpecialtyError):
            await panel.sync_once()
        channel.send.assert_not_awaited()
        assert panel.load_state() is None

    asyncio.run(run())
