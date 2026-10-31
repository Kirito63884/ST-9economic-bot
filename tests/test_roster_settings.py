"""Publication configuration preserves the established two-message layout."""

import asyncio
import copy
import hashlib
import json

import pytest

from automation_control import AutomationSetting, utcnow
from conftest import session_cookie
from database import SessionLocal
from discord_roster import FIRST_HEADER, SECOND_HEADER, render_roster
from roster_settings import CACHE_KEY, CONFIG_KEY, defaults, write
from test_discord_roster import Channel, sample_members, worker

PATH = "/api/admin/roster-publication"
NEW_ROLE = "1555555555555555555"


@pytest.fixture(autouse=True)
def clear_settings():
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("discord_roster:%")
        ).delete(synchronize_session=False)


def seed_cache():
    config = defaults()
    with SessionLocal.begin() as db:
        write(
            db,
            CACHE_KEY,
            {
                "checked_at": utcnow().isoformat() + "Z",
                "roles": [
                    {"id": rid, "name": key} for key, rid in config["role_ids"].items()
                ]
                + [{"id": NEW_ROLE, "name": "Новая роль"}],
                "members": sample_members()
                + [{"user": {"id": "999", "username": "Другой"}, "roles": [NEW_ROLE]}],
            },
        )


def chief(make_user, login_as):
    make_user(101, is_admin=True, is_chief_admin=True)
    return login_as(101)


def test_default_output_remains_byte_for_byte_identical():
    texts = render_roster(sample_members(), defaults())
    assert [hashlib.sha256(text.encode()).hexdigest() for text in texts] == [
        "718ba2ab7b7915721b332ed27c9e297099715c2b4dcd52d9c12e7d3cb2ef6470",
        "c7c37b6a549458eb810c331d91628161720c9cf1b6c8d315fc08690c3073123d",
    ]


def test_ids_descriptions_and_new_blocks_use_existing_styles():
    config = defaults()
    config["role_ids"]["founder"] = NEW_ROLE
    config["descriptions"]["founder"] = "Обязанности куратора"
    raw = [{"user": {"id": "1", "username": "Новый"}, "roles": [NEW_ROLE]}]
    texts = render_roster(raw, config)
    assert FIRST_HEADER in texts[0] and SECOND_HEADER in "\n".join(texts)
    assert (
        "__Основатель и куратор ST-9:__\nОбязанности куратора\n✪【ST-9】Новый"
        in texts[0]
    )
    config = defaults()
    config["extra_roles"] = [
        {
            "role_id": NEW_ROLE,
            "title": "Резерв",
            "description": "Поддержка",
            "kind": "group",
            "message": "second",
            "prefix": "➎",
        }
    ]
    texts = render_roster(raw, config)
    assert "**Резерв**" in texts[1] and "➎【ST-9】Новый" in texts[1]
    assert texts[1].index("**Резерв**") < texts[1].index("**УЧЕБНАЯ ГРУППА**")
    assert "__Командир группы:__" in texts[1] and "__Командиры подгрупп:__" in texts[1]


def test_saved_configuration_requires_same_actor_exact_preview_and_no_replay(
    make_user, login_as
):
    c = chief(make_user, login_as)
    seed_cache()
    initial = c.get(PATH).json()
    config = initial["config"]
    config["role_ids"]["founder"] = NEW_ROLE
    preview = c.post(
        PATH + "/preview", json={"revision": initial["revision"], "config": config}
    )
    assert preview.status_code == 200, preview.text
    assert len(preview.json()["texts"]) == 2
    body = {
        "revision": initial["revision"],
        "config": config,
        "confirmation": preview.json()["confirmation"],
    }
    make_user(102, is_admin=True, is_chief_admin=True)
    c.cookies.set("session", session_cookie(102))
    assert c.post(PATH + "/settings", json=body).status_code == 409
    c.cookies.set("session", session_cookie(101))
    altered = copy.deepcopy(body)
    altered["config"]["descriptions"]["founder"] = "Иное"
    assert c.post(PATH + "/settings", json=altered).status_code == 409
    assert c.post(PATH + "/settings", json=body).status_code == 200
    assert c.get(PATH).json()["config"]["role_ids"]["founder"] == NEW_ROLE
    assert c.post(PATH + "/settings", json=body).status_code == 409


@pytest.mark.parametrize("flags", [{}, {"is_admin": True}])
def test_non_chiefs_cannot_read_or_modify_configuration(make_user, login_as, flags):
    uid = make_user(**flags)
    c = login_as(uid)
    assert c.get(PATH).status_code == 403
    assert c.post(PATH + "/preview", json={}).status_code == 403
    assert c.post(PATH + "/settings", json={}).status_code == 403


def test_technical_admin_has_same_editor_access(make_user, login_as):
    uid = make_user(is_admin=True, is_tech_admin=True)
    assert login_as(uid).get(PATH).status_code == 200


@pytest.mark.parametrize(
    "bad_id",
    [
        1448264873812164629,
        True,
        "1",
        "0" * 19,
        "18446744073709551616",
        "<@&1555555555555555555>",
    ],
)
def test_role_id_validation_preserves_saved_configuration(make_user, login_as, bad_id):
    c = chief(make_user, login_as)
    initial = c.get(PATH).json()
    config = initial["config"]
    config["role_ids"]["founder"] = bad_id
    assert (
        c.post(
            PATH + "/preview", json={"revision": initial["revision"], "config": config}
        ).status_code
        == 400
    )
    with SessionLocal() as db:
        assert db.get(AutomationSetting, CONFIG_KEY) is None


def test_missing_role_and_overflow_keep_old_text_and_settings(make_user, login_as):
    c = chief(make_user, login_as)
    seed_cache()
    initial = c.get(PATH).json()
    config = initial["config"]
    config["role_ids"]["founder"] = "1666666666666666666"
    assert (
        c.post(
            PATH + "/preview", json={"revision": initial["revision"], "config": config}
        ).status_code
        == 400
    )
    config = defaults()
    for key in config["descriptions"]:
        config["descriptions"][key] = "a" * 1100
    result = c.post(
        PATH + "/preview", json={"revision": initial["revision"], "config": config}
    )
    assert result.status_code == 400 and "не помещается" in result.json()["detail"]
    with SessionLocal() as db:
        assert db.get(AutomationSetting, CONFIG_KEY) is None


def test_changed_snapshot_invalidates_preview_but_timestamp_does_not(
    make_user, login_as
):
    c = chief(make_user, login_as)
    seed_cache()
    initial = c.get(PATH).json()
    preview = c.post(
        PATH + "/preview",
        json={"revision": initial["revision"], "config": initial["config"]},
    ).json()
    body = {
        "revision": initial["revision"],
        "config": initial["config"],
        "confirmation": preview["confirmation"],
    }
    with SessionLocal.begin() as db:
        row = db.get(AutomationSetting, CACHE_KEY)
        data = json.loads(row.value)
        data["members"][0]["nick"] = "Смена имени"
        write(db, CACHE_KEY, data)
    assert c.post(PATH + "/settings", json=body).status_code == 409
    new = c.post(
        PATH + "/preview",
        json={"revision": initial["revision"], "config": initial["config"]},
    ).json()
    body["confirmation"] = new["confirmation"]
    with SessionLocal.begin() as db:
        row = db.get(AutomationSetting, CACHE_KEY)
        data = json.loads(row.value)
        data["checked_at"] = "2026-10-30T12:00:00Z"
        data["applied_revision"] = "initial"
        write(db, CACHE_KEY, data)
    assert c.post(PATH + "/settings", json=body).status_code == 200


def test_csrf_unknown_fields_and_forged_confirmation_rejected(make_user, login_as):
    c = chief(make_user, login_as)
    initial = c.get(PATH).json()
    body = {"revision": initial["revision"], "config": initial["config"]}
    assert (
        c.post(
            PATH + "/preview", json=body, headers={"X-ST9-CSRF": "wrong"}
        ).status_code
        == 403
    )
    assert c.post(PATH + "/preview", json=body | {"channel_id": "1"}).status_code == 400
    assert (
        c.post(PATH + "/settings", json=body | {"confirmation": "forged"}).status_code
        == 409
    )


def test_publisher_reads_saved_config_reuses_messages_and_records_application():
    channel = Channel()
    instance, sends = worker(channel)
    asyncio.run(instance.sync_once())
    assert len(sends) == 2
    with SessionLocal.begin() as db:
        config = defaults()
        config["descriptions"]["founder"] = "Проверяемое описание"
        write(db, CONFIG_KEY, config)
    asyncio.run(instance.sync_once())
    assert len(sends) == 2 and any(
        "Проверяемое описание" in m.content for m in channel.messages.values()
    )
    with SessionLocal() as db:
        saved = json.loads(db.get(AutomationSetting, CACHE_KEY).value)
        assert saved["applied_revision"] and saved["roles"] and saved["members"]


def test_senior_can_edit_but_junior_cannot_see_tab(make_user, login_as):
    uid = make_user(is_admin=True, is_senior_admin=True)
    c = login_as(uid)
    initial = c.get(PATH).json()
    config = initial["config"]
    result = c.post(
        PATH + "/preview", json={"revision": initial["revision"], "config": config}
    )
    assert result.status_code == 200
    assert (
        c.post(
            PATH + "/settings",
            json={
                "revision": initial["revision"],
                "config": config,
                "confirmation": result.json()["confirmation"],
            },
        ).status_code
        == 200
    )
    assert 'id="tab-roster-settings"' in c.get("/admin").text
    make_user(102, is_admin=True)
    c.cookies.set("session", session_cookie(102))
    assert 'id="tab-roster-settings"' not in c.get("/admin").text


def test_preview_expires_and_existing_settings_remain(make_user, login_as, monkeypatch):
    import time
    from itsdangerous import TimestampSigner

    c = chief(make_user, login_as)
    initial = c.get(PATH).json()
    body = {"revision": initial["revision"], "config": initial["config"]}
    old = int(time.time()) - 301
    with monkeypatch.context() as patch:
        patch.setattr(
            TimestampSigner,
            "get_timestamp",
            lambda self: (
                old if self.salt == b"roster-publication-preview-v1" else old + 301
            ),
        )
        preview = c.post(PATH + "/preview", json=body).json()
    assert (
        c.post(
            PATH + "/settings", json=body | {"confirmation": preview["confirmation"]}
        ).status_code
        == 409
    )
    with SessionLocal() as db:
        assert db.get(AutomationSetting, CONFIG_KEY) is None
