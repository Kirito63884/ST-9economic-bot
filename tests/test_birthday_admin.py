import json

import pytest
from itsdangerous.timed import TimestampSigner

from automation_control import AutomationSetting
from database import SessionLocal
from discord_birthdays import REGISTRY_KEY, read
from roster_settings import write

URL = "/api/admin/birthdays"
SID = "123456789012345678"
ROW = {"name": "Игрок <img src=x>", "date": "15.06.1814"}


@pytest.fixture(autouse=True)
def clear_registry():
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("discord_birthdays:%")
        ).delete()


def payload(client, records):
    return {
        "source": json.dumps(records, ensure_ascii=False),
        "revision": client.get(URL).json()["revision"],
    }


def test_junior_edit_export_and_round_trip(make_user, login_as):
    make_user(101, is_admin=True)
    client = login_as(101)
    revision = client.get(URL).json()["revision"]
    saved = client.post(URL + "/save", json={"id": SID, **ROW, "revision": revision})
    assert saved.status_code == 200
    exported = client.get(URL + "/export")
    assert exported.json() == {SID: ROW}
    assert exported.headers["cache-control"] == "private, no-store"
    assert "attachment" in exported.headers["content-disposition"]
    data = payload(client, {SID: ROW})
    data["source"] = "\ufeff" + data["source"]
    preview = client.post(URL + "/import/preview", json=data).json()
    assert preview["unchanged"] == 1
    apply = {**data, "confirmation": preview["confirmation"]}
    assert client.post(URL + "/import/apply", json=apply).status_code == 200
    assert client.post(URL + "/import/apply", json=apply).status_code == 409


def test_import_changes_only_confirmed_records_and_preserves_absent(
    make_user, login_as
):
    make_user(101, is_admin=True)
    with SessionLocal.begin() as db:
        write(db, REGISTRY_KEY, {SID: ROW, "223456789012345678": ROW})
    client = login_as(101)
    incoming = {
        SID: {"name": "Updated", "date": "20.10.2000"},
        "323456789012345678": {"name": "Future", "date": "01.01.2999"},
    }
    data = payload(client, incoming)
    plan = client.post(URL + "/import/preview", json=data).json()
    assert (plan["added"], plan["changed"]) == (1, 1)
    assert plan["paused"] == ["323456789012345678"]
    assert client.get(URL + "/export").json()[SID] == ROW
    result = client.post(
        URL + "/import/apply", json={**data, "confirmation": plan["confirmation"]}
    )
    assert result.status_code == 200
    assert result.json()["records"] == {**incoming, "223456789012345678": ROW}


def test_import_proof_binds_actor_file_expiry_and_discord_revision(
    make_user, login_as, monkeypatch
):
    make_user(101, is_admin=True)
    make_user(102, is_admin=True)
    client = login_as(101)
    data = payload(client, {SID: ROW})
    token = client.post(URL + "/import/preview", json=data).json()["confirmation"]
    apply = {**data, "confirmation": token}
    assert login_as(102).post(URL + "/import/apply", json=apply).status_code == 409
    client = login_as(101)
    assert (
        client.post(URL + "/import/apply", json={**apply, "source": "{}"}).status_code
        == 409
    )
    stamp = TimestampSigner.get_timestamp
    with monkeypatch.context() as patch:
        patch.setattr(TimestampSigner, "get_timestamp", lambda self: stamp(self) + 301)
        assert client.post(URL + "/import/apply", json=apply).status_code == 409
    with SessionLocal.begin() as db:
        write(db, REGISTRY_KEY, {"423456789012345678": ROW})
    assert client.post(URL + "/import/apply", json=apply).status_code == 409


def test_invalid_files_are_atomic_and_authorization_required(
    make_user, login_as, client
):
    assert client.get(URL + "/export").status_code == 403
    make_user(101)
    for path in ("", "/export"):
        assert login_as(101).get(URL + path).status_code == 403
    for path in ("/save", "/import/preview", "/import/apply"):
        assert login_as(101).post(URL + path, json={}).status_code == 403
    make_user(102, is_admin=True)
    client = login_as(102)
    for source in (
        "[]",
        '{"bad":{}}',
        '{"123456789012345678":{},"123456789012345678":{}}',
        "x" * (128 * 1024 + 1),
        json.dumps({SID: {**ROW, "date": "31.02.2000"}}),
    ):
        data = {"source": source, "revision": client.get(URL).json()["revision"]}
        assert client.post(URL + "/import/preview", json=data).status_code == 400
    assert (
        client.post(
            URL + "/save",
            json={"id": SID, **ROW, "revision": client.get(URL).json()["revision"]},
            headers={"X-ST9-CSRF": "wrong"},
        ).status_code
        == 403
    )
    with SessionLocal() as db:
        assert read(db, REGISTRY_KEY, {}) == {}
