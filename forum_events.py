"""Durable forum events shared by the site and Discord (no network here)."""

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from automation_control import AutomationSetting
from roster_settings import write

FORUM_ID = 1568007339343093951
PREFIX = "forum_event:"
ZONE = ZoneInfo("Europe/Moscow")
FIELDS = {
    "title",
    "starts_at",
    "capacity",
    "teams",
    "map",
    "server",
    "rules",
    "image_url",
    "status",
}


class EventError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc)


def text(value, limit, required=False):
    if (
        not isinstance(value, str)
        or len(value) > limit
        or any(ord(c) < 32 and c != "\n" for c in value)
    ):
        raise EventError("Некорректный текст или превышена длина поля.")
    value = value.strip()
    if required and not value:
        raise EventError("Заполните название события.")
    return value


def config(data, creating=False):
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise EventError("Некорректные поля события.")
    result = {
        key: text(data[key], limit, key == "title")
        for key, limit in {
            "title": 70,
            "teams": 100,
            "map": 100,
            "server": 150,
            "rules": 500,
            "image_url": 500,
        }.items()
    }
    if "\n" in result["title"]:
        raise EventError("Название должно быть одной строкой.")
    if type(data["capacity"]) is not int or not 1 <= data["capacity"] <= 100:
        raise EventError("Количество мест: от 1 до 100.")
    if data["status"] not in {"open", "closed", "cancelled"}:
        raise EventError("Неизвестный статус.")
    try:
        if not isinstance(data["starts_at"], str):
            raise ValueError()
        start = datetime.fromisoformat(data["starts_at"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZONE)
        start = start.astimezone(timezone.utc)
    except ValueError:
        raise EventError("Укажите дату и время события по Москве.") from None
    if (creating or data["status"] == "open") and start <= now():
        raise EventError("Открытое событие должно начинаться в будущем.")
    if result["image_url"]:
        url = urlsplit(result["image_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise EventError("Для изображения нужна ссылка HTTPS.")
    result.update(
        starts_at=start.isoformat(), capacity=data["capacity"], status=data["status"]
    )
    return result


def get(db, eid):
    if not isinstance(eid, str) or not re.fullmatch("[a-f0-9]{32}", eid):
        raise EventError("Некорректный ID события.")
    row = db.get(AutomationSetting, PREFIX + eid)
    if not row:
        raise EventError("Событие не найдено.")
    return json.loads(row.value)


def all_events(db):
    rows = (
        db.query(AutomationSetting)
        .filter(AutomationSetting.key.like(PREFIX + "%"))
        .all()
    )
    return sorted(
        (json.loads(row.value) for row in rows),
        key=lambda e: e["config"]["starts_at"],
        reverse=True,
    )


def save(db, event):
    write(db, PREFIX + event["id"], event)


def create(db, eid, actor, data):
    if not re.fullmatch("[a-f0-9]{32}", eid):
        raise EventError("Некорректный ключ создания.")
    normalized = config(data, creating=True)
    fingerprint = hashlib.sha256(
        json.dumps([str(actor), normalized], sort_keys=True).encode()
    ).hexdigest()
    if db.get(AutomationSetting, PREFIX + eid):
        event = get(db, eid)
        if event["creation_hash"] != fingerprint:
            raise EventError("Ключ создания уже использован. Обновите форму.")
        return event
    counter = db.get(AutomationSetting, "forum_events:counter")
    number = int(counter.value) + 1 if counter else 1
    write(db, "forum_events:counter", number)
    event = {
        "id": eid,
        "number": number,
        "config": normalized,
        "creator": str(actor),
        "created_at": now().isoformat(),
        "creation_hash": fingerprint,
        "revision": 1,
        "responses": {},
        "lineup": [],
        "thread_id": None,
        "published_revision": 0,
        "delivery": {},
        "error": "",
    }
    save(db, event)
    return event


def update(db, eid, revision, data, lineup):
    event = get(db, eid)
    if type(revision) is not int or event["revision"] != revision:
        raise EventError("Событие изменилось. Загрузите свежую версию.")
    normalized = config(data)
    if event.get("result") and normalized["status"] == "open":
        raise EventError(
            "У события уже есть итоги; создайте новое событие для следующего матча."
        )
    if (
        not isinstance(lineup, list)
        or any(not isinstance(uid, str) for uid in lineup)
        or len(set(lineup)) != len(lineup)
    ):
        raise EventError("Некорректный игровой состав.")
    if len(lineup) > normalized["capacity"] or any(
        event["responses"].get(uid, {}).get("choice") != "yes" for uid in lineup
    ):
        raise EventError(
            "В составе могут быть только записавшиеся; не превышайте число мест."
        )
    event.update(
        config=normalized, lineup=lineup, revision=event["revision"] + 1, error=""
    )
    save(db, event)
    return event


def is_open(event):
    return (
        event["config"]["status"] == "open"
        and datetime.fromisoformat(event["config"]["starts_at"]) > now()
    )


def respond(db, eid, uid, name, choice, thread_id):
    event = get(db, eid)
    if str(event["thread_id"]) != str(thread_id) or choice not in {
        "yes",
        "maybe",
        "no",
    }:
        raise EventError("Эта кнопка не относится к опубликованному событию.")
    if not is_open(event):
        raise EventError("Регистрация закрыта.")
    uid = str(uid)
    previous = event["responses"].get(uid)
    if previous and previous["choice"] == choice:
        return event
    if not previous and len(event["responses"]) >= 1000:
        raise EventError("Достигнут лимит участников события.")
    event["responses"][uid] = {"name": text(name, 100), "choice": choice}
    if choice != "yes" and uid in event["lineup"]:
        event["lineup"].remove(uid)
    event["revision"] += 1
    save(db, event)
    return event
