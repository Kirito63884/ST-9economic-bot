import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from automation_control import AutomationSetting
from database import SessionLocal
from discord_roster import (
    ROLES,
    RosterError,
    RosterPublisher,
    render_roster,
)


def member(uid, name, *roles, bot=False):
    return {
        "user": {"id": str(uid), "username": name, "bot": bot},
        "nick": name,
        "roles": [ROLES[r] for r in roles],
    }


def sample_members():
    result = []

    def add(name, *roles):
        result.append(member(len(result) + 1, name, *roles))

    add("✪【ST-9】BISHOP", "founder")
    add("✪【ST-9】More", "field")
    add("✪【ST-9】 PonChek", "security", "security_head")
    add("✪【ST-9】Belkа", "security")
    add("✪【ST-9】Kenik", "security")
    for unit, leader, regulars, subleaders in (
        (
            "recon",
            "Front (Кирилл)",
            ["DEVIL", "don", "Capibara", "Sershant"],
            ["DangerBorshik", "Teacher"],
        ),
        (
            "assault",
            "дед",
            [
                "Яхай Баля",
                "Госуслуг(Сеня)",
                "грустный",
                "Ankrat",
                "Desmont Doss",
                "Timon",
                "Mor",
                "пельмени",
                "THUNDERBOLT",
            ],
            ["Noxxx (Миша)", "Багет(Олег)", "KO4EBHUK_GLAVNIY [Роман]"],
        ),
        (
            "armor",
            "Khan Ye",
            [
                "ЕСТЬ ПРОБИТИЕ",
                "Калуга",
                "Aryon",
                "No Sleep",
                "Signal",
                "Warmlove",
                "Z-Dawg",
            ],
            ["Ilyshenka (вась)", "Jeremi"],
        ),
        (
            "support",
            "Wex",
            [
                "| Dwight Power",
                "сыр",
                "Bóbr kurwa",
                "Chedupelka",
                "Herring or Tuna",
                "RAVEN",
                "Yk1zima",
                "ZиMa",
                "чугун",
            ],
            ["AVB (Андрей)", "chik306"],
        ),
    ):
        add(leader, unit, "leader")
        for name in regulars:
            add(name, unit)
        for name in subleaders:
            add(name, unit, "subleader")
    add("reg_pl", "chief_instructor", "instructor")
    for name in ("Artemka", "BlitZ", "Memory_Of_Madness(Кирилл)"):
        add(name, "instructor")
    return result


@pytest.fixture(autouse=True)
def clear_roster_state(monkeypatch):
    monkeypatch.delenv("DISCORD_ROSTER_MESSAGE_IDS", raising=False)
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("discord_roster:%")
        ).delete(synchronize_session=False)


def test_full_user_example_fits_two_messages_and_keeps_everyone():
    raw = sample_members()
    texts = render_roster(raw)
    assert len(texts) == 2
    assert all(len(t) <= 2000 for t in texts)
    text = "\n".join(texts)
    assert text.count("【ST-9】") == len(raw)
    assert text.count("✪【ST-9】PonChek") == 1
    assert text.count(r"➒【ST-9】reg\_pl") == 1
    assert "➊【ST-9】Front (Кирилл)" in text
    assert "➌【ST-9】Khan Ye" in text
    assert "__Командиры подгрупп:__" in text
    assert "• захват  ключевых точек;" in text


def test_roles_add_remove_promote_and_rename_without_duplicates():
    raw = [member(1, "Name", "assault"), member(2, "Other", "recon")]
    first = "\n".join(render_roster(raw))
    assert "➋【ST-9】Name" in first
    raw[0]["roles"] = [ROLES["armor"], ROLES["leader"], ROLES["subleader"]]
    raw[0]["nick"] = "➌【ST-9】New"
    raw[1]["roles"] = []
    second = "\n".join(render_roster(raw))
    assert "Name" not in second and "Other" not in second
    assert second.count("➌【ST-9】New") == 1


def test_markdown_and_mentions_in_names_are_neutralized_and_bots_excluded():
    text = "\n".join(
        render_roster(
            [
                member(1, "**Name** @everyone\nNext", "recon"),
                member(2, "Bot", "recon", bot=True),
            ]
        )
    )
    assert r"\*\*Name\*\*" in text
    assert "@everyone" not in text
    assert "\nNext" not in text
    assert "【ST-9】Bot" not in text


def test_overflow_and_incomplete_roster_fail_without_truncating():
    with pytest.raises(RosterError, match="не помещается"):
        render_roster([member(i, "a" * 32 + str(i), "recon") for i in range(1, 200)])
    with pytest.raises(RosterError):
        render_roster([])
    with pytest.raises(RosterError):
        render_roster([member(1, "a", "recon"), member(1, "b", "recon")])


class Message:
    def __init__(self, uid, text, author=42):
        self.id, self.content = uid, text
        self.author = SimpleNamespace(id=author)
        self.webhook_id = None
        self.edits = []

    async def edit(self, content, **kwargs):
        self.content = content
        self.edits.append((content, kwargs))


class Channel:
    def __init__(self, messages=()):
        self.messages = {m.id: m for m in messages}
        self.guild = SimpleNamespace(
            id=7,
            fetch_roles=AsyncMock(
                return_value=[SimpleNamespace(id=int(rid)) for rid in ROLES.values()]
            ),
        )

    async def fetch_message(self, uid):
        if uid not in self.messages:
            raise RosterError("message deleted")
        return self.messages[uid]

    async def history(self, **kwargs):
        for m in list(self.messages.values()):
            yield m


def worker(channel):
    bot = SimpleNamespace(user=SimpleNamespace(id=42))
    sends = []

    async def request(route, json):
        sends.append(copy.deepcopy(json))
        message = Message(100 + len(sends), json["content"])
        channel.messages[message.id] = message
        return {"id": str(message.id)}

    bot.http = SimpleNamespace(request=request)
    bot.get_channel = lambda _: channel
    bot.st9_roster_sync = SimpleNamespace(
        members=AsyncMock(return_value=sample_members())
    )
    instance = RosterPublisher(bot)
    instance.guild_id = "7"
    return instance, sends


def test_first_publish_then_restart_edits_without_new_messages():
    channel = Channel()
    instance, sends = worker(channel)
    texts = render_roster(sample_members())
    asyncio.run(instance.publish(channel, texts))
    assert len(sends) == 2
    assert all(
        s["allowed_mentions"] == {"parse": []} and s["enforce_nonce"] for s in sends
    )
    state = instance.load_state()
    assert all(slot.get("id") and not slot.get("pending") for slot in state)
    restarted = RosterPublisher(instance.bot)
    asyncio.run(restarted.publish(channel, texts))
    assert len(sends) == 2
    assert not any(m.edits for m in channel.messages.values())
    changed = [texts[0].replace("BISHOP", "BISHOP2"), texts[1]]
    asyncio.run(restarted.publish(channel, changed))
    assert len(sends) == 2
    assert sum(len(m.edits) for m in channel.messages.values()) == 1
    edited = channel.messages[int(state[0]["id"])]
    assert edited.edits[0][1]["allowed_mentions"].to_dict()["parse"] == []


def test_adopts_existing_bot_messages_but_never_edits_human_roster():
    texts = render_roster([member(1, "a", "recon")])
    channel = Channel(
        [Message(1, texts[0], author=99), Message(2, texts[1], author=99)]
    )
    instance, sends = worker(channel)
    asyncio.run(instance.publish(channel, texts))
    assert len(sends) == 2
    assert not channel.messages[1].edits and not channel.messages[2].edits
    # Known IDs from a prior publication: reuse them even if text split has changed.
    state = instance.load_state()
    assert [s["id"] for s in state] == ["101", "102"]


def test_adopts_preexisting_own_roster_without_sending():
    texts = render_roster([member(1, "a", "recon")])
    channel = Channel([Message(1, texts[0]), Message(2, texts[1])])
    instance, sends = worker(channel)
    asyncio.run(instance.publish(channel, texts))
    assert sends == []
    assert [s["id"] for s in instance.load_state()] == ["1", "2"]


def test_lost_send_response_is_recovered_without_duplicate():
    channel = Channel()
    instance, sends = worker(channel)
    texts = render_roster([member(1, "a", "recon")])
    original = instance.bot.http.request

    async def lost(route, json):
        await original(route, json)
        raise TimeoutError("lost response")

    instance.bot.http.request = lost
    with pytest.raises(TimeoutError):
        asyncio.run(instance.publish(channel, texts))
    assert len(sends) == 1 and instance.load_state()[0]["pending"] == texts[0]
    instance.bot.http.request = original
    asyncio.run(RosterPublisher(instance.bot).publish(channel, texts))
    assert len(sends) == 2
    assert len(channel.messages) == 2


def test_deleted_or_foreign_saved_messages_do_not_create_replacements(monkeypatch):
    monkeypatch.setenv("DISCORD_ROSTER_MESSAGE_IDS", "1,2")
    texts = render_roster([member(1, "a", "recon")])
    channel = Channel([Message(1, texts[0]), Message(2, texts[1], author=99)])
    instance, sends = worker(channel)
    with pytest.raises(RosterError, match="чужое"):
        asyncio.run(instance.publish(channel, texts))
    assert sends == [] and not channel.messages[1].edits
    del channel.messages[2]
    with pytest.raises(RosterError, match="deleted"):
        asyncio.run(instance.publish(channel, texts))
    assert sends == []


def test_missing_roles_or_wrong_channel_guild_leave_messages_untouched():
    channel = Channel()
    instance, sends = worker(channel)
    channel.guild.fetch_roles = AsyncMock(return_value=[])
    with pytest.raises(RosterError, match="Не найдены роли"):
        asyncio.run(instance.sync_once())
    assert sends == []
    channel.guild.id = 9
    with pytest.raises(RosterError, match="другом"):
        asyncio.run(instance.sync_once())
    assert sends == []


def test_ready_reconnect_does_not_start_duplicate_loop():
    instance, _ = worker(Channel())

    async def check():
        instance.loop = AsyncMock(side_effect=lambda: asyncio.sleep(60))

        # Keep the task pending without making a network request.
        async def pending():
            await asyncio.Event().wait()

        instance.loop = pending
        await instance.ready()
        task = instance.task
        await instance.ready()
        assert instance.task is task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(check())
