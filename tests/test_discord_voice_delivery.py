"""Only synthetic gateway packets and a fake Discord HTTP transport."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

import discord_event_log as logs
from database import SessionLocal, engine


@pytest.fixture(autouse=True)
def clear_event_tables():
    for model in (
        logs.DiscordEventLog,
        logs.DiscordVoicePresence,
        logs.DiscordChannelCatalog,
    ):
        model.__table__.create(engine, checkfirst=True)
    with SessionLocal.begin() as db:
        for model in (
            logs.DiscordEventLog,
            logs.DiscordVoicePresence,
            logs.DiscordChannelCatalog,
        ):
            db.query(model).delete()


class Http:
    def __init__(self):
        self.sent = []
        self.audit = {"audit_log_entries": []}
        self.fail_audit = False
        self.timeout_after_accept = False
        self.responses = {}
        self.attempts = []

    async def request(self, route, **kwargs):
        if route.method == "GET":
            if self.fail_audit:
                raise TimeoutError("synthetic audit outage")
            return self.audit
        data = kwargs["json"]
        self.attempts.append(data)
        nonce = data["nonce"]
        if nonce not in self.responses:
            self.sent.append(data)
            self.responses[nonce] = {"id": str(9000 + len(self.sent))}
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise TimeoutError("accepted but reply was lost")
        return self.responses[nonce]


def worker():
    http = Http()
    obj = logs.DiscordEventWorker(SimpleNamespace(http=http))
    obj.guild_id = "1234"
    return obj, http


def packet(event, seq, **data):
    return json.dumps({"op": 0, "t": event, "s": seq, "d": data})


async def boot(obj, session="synthetic-session"):
    await obj.raw_packet(packet("READY", 1, session_id=session))
    await obj.raw_packet(
        packet(
            "GUILD_CREATE",
            2,
            id="1234",
            voice_states=[],
            members=[],
            channels=[
                {"id": "10", "name": "Один", "type": 2},
                {"id": "11", "name": "Два", "type": 2},
            ],
        )
    )


async def voice(obj, seq, channel):
    await obj.raw_packet(
        packet(
            "VOICE_STATE_UPDATE",
            seq,
            guild_id="1234",
            user_id="50",
            channel_id=channel,
            member={"user": {"id": "50", "username": "Игрок"}},
        )
    )


def test_retrying_leave_cannot_be_overtaken_by_later_join(monkeypatch):
    current = [logs.utcnow()]
    monkeypatch.setattr(logs, "utcnow", lambda: current[0])

    async def run():
        obj, http = worker()
        await boot(obj)
        await voice(obj, 3, "10")
        await voice(obj, 4, None)
        await voice(obj, 5, "11")
        assert await obj.process_one()
        current[0] += timedelta(seconds=3)
        assert not await obj.process_one()  # audit is not ready
        assert not await obj.process_one()  # third event must wait behind the retry
        assert len(http.sent) == 1
        current[0] += timedelta(seconds=6)
        assert await obj.process_one()  # fallback still records the actual leave
        assert await obj.process_one()
        texts = [p["embeds"][0]["description"] for p in http.sent]
        assert (
            "присоединился" in texts[0]
            and "вышел" in texts[1]
            and "присоединился" in texts[2]
        )
        assert all(
            p["allowed_mentions"] == {"parse": []} and p["enforce_nonce"]
            for p in http.sent
        )

    asyncio.run(run())


def test_voice_disconnect_logs_even_when_audit_is_unavailable(monkeypatch):
    current = [logs.utcnow()]
    monkeypatch.setattr(logs, "utcnow", lambda: current[0])

    async def run():
        obj, http = worker()
        await boot(obj)
        await voice(obj, 3, "10")
        assert await obj.process_one()
        await voice(obj, 4, None)
        http.fail_audit = True
        current[0] += timedelta(seconds=3)
        assert not await obj.process_one()
        current[0] += timedelta(seconds=6)
        assert await obj.process_one()
        assert "вышел" in http.sent[-1]["embeds"][0]["description"]
        with SessionLocal() as db:
            row = db.query(logs.DiscordEventLog).filter_by(kind="voice_leave").one()
            assert row.sent_at and not row.ignored_at
            assert json.loads(row.payload)["audit_status"] == "unavailable"

    asyncio.run(run())


def test_forced_voice_kick_is_recorded_with_confirmed_actor(monkeypatch):
    current = [logs.utcnow()]
    monkeypatch.setattr(logs, "utcnow", lambda: current[0])

    async def run():
        obj, http = worker()
        await boot(obj)
        await voice(obj, 3, "10")
        await obj.process_one()
        await voice(obj, 4, None)
        stamp = int(current[0].replace(tzinfo=logs.timezone.utc).timestamp() * 1000)
        http.audit = {
            "audit_log_entries": [
                {
                    "id": str((stamp - logs.DISCORD_EPOCH_MS) << 22),
                    "action_type": 27,
                    "target_id": "50",
                    "user_id": "777",
                    "reason": "тест",
                }
            ]
        }
        current[0] += timedelta(seconds=3)
        assert await obj.process_one()
        embed = http.sent[-1]["embeds"][0]
        assert (
            "принудительно отключён" in embed["description"]
            and "<@777>" in embed["description"]
        )
        assert embed["timestamp"] and len(embed["footer"]["text"].split(":")) == 3

    asyncio.run(run())


def test_gateway_session_namespace_and_old_replay_do_not_lose_or_invent_events():
    async def run():
        obj, _ = worker()
        await boot(obj, "session-one")
        await voice(obj, 3, "10")
        await voice(obj, 4, None)
        await voice(obj, 3, "10")  # replay cannot put a departed user back in voice
        assert "50" not in obj.channels
        with SessionLocal() as db:
            assert db.get(logs.DiscordVoicePresence, "50") is None
        await boot(obj, "session-two")
        await voice(obj, 3, "10")
        await voice(obj, 4, None)
        with SessionLocal() as db:
            rows = (
                db.query(logs.DiscordEventLog).order_by(logs.DiscordEventLog.id).all()
            )
            assert [r.kind for r in rows] == [
                "voice_join",
                "voice_leave",
                "voice_join",
                "voice_leave",
            ]
            assert len({r.event_key for r in rows}) == 4

    asyncio.run(run())


def test_transient_database_failure_retries_capture_before_next_packet(monkeypatch):
    actual = logs.metadata_transaction
    fail = [True]

    @asynccontextmanager
    async def flaky():
        async with actual() as db:
            yield db
            if fail[0]:
                fail[0] = False
                raise OperationalError("synthetic", {}, Exception("temporary DB lock"))

    async def no_sleep(_):
        return None

    async def run():
        obj, _ = worker()
        await boot(obj)
        monkeypatch.setattr(logs, "metadata_transaction", flaky)
        monkeypatch.setattr(logs.asyncio, "sleep", no_sleep)
        await asyncio.gather(voice(obj, 3, "10"), voice(obj, 4, None))
        assert "50" not in obj.channels
        with SessionLocal() as db:
            assert [
                r.kind
                for r in db.query(logs.DiscordEventLog).order_by(
                    logs.DiscordEventLog.id
                )
            ] == ["voice_join", "voice_leave"]
            assert db.get(logs.DiscordVoicePresence, "50") is None

    asyncio.run(run())


def test_nonce_retry_after_accepted_message_avoids_duplicate(monkeypatch):
    current = [logs.utcnow()]
    monkeypatch.setattr(logs, "utcnow", lambda: current[0])

    async def run():
        obj, http = worker()
        await boot(obj)
        await voice(obj, 3, "10")
        http.timeout_after_accept = True
        assert not await obj.process_one()
        current[0] += timedelta(seconds=6)
        assert await obj.process_one()
        assert len(http.sent) == 1 and len(http.attempts) == 2
        assert http.attempts[0]["nonce"] == http.attempts[1]["nonce"]
        with SessionLocal() as db:
            assert db.query(logs.DiscordEventLog).one().message_id == "9001"

    asyncio.run(run())


def test_audit_resolution_keeps_lease_until_message_is_finished():
    async def run():
        obj, _ = worker()
        await boot(obj)
        await voice(obj, 3, "10")
        async with logs.metadata_transaction() as db:
            job = logs.claim(db, logs.utcnow())
        async with logs.metadata_transaction() as db:
            logs.finish(db, job, logs.utcnow(), actor="777", reason="test")
        async with logs.metadata_transaction() as db:
            assert logs.claim(db, logs.utcnow()) is None
            row = db.get(logs.DiscordEventLog, job["id"])
            assert row.lease_until == job["lease_until"]
            logs.finish(db, job, logs.utcnow(), message="100")

    asyncio.run(run())


def test_targetless_audit_entry_does_not_blame_an_unconfirmed_moderator():
    moment = logs.utcnow()
    stamp = int(moment.replace(tzinfo=logs.timezone.utc).timestamp() * 1000)
    job = {
        "kind": "voice_leave",
        "target_id": "50",
        "occurred_at": moment,
        "attempts": 3,
    }
    document = {
        "audit_log_entries": [
            {
                "id": str((stamp - logs.DISCORD_EPOCH_MS) << 22),
                "action_type": 27,
                "target_id": None,
                "user_id": "777",
            }
        ]
    }
    assert logs.matching_audit(document, job) == ("", "")
