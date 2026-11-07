"""Reusable event templates and final results; shared transactional operations."""

import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from automation_control import AutomationSetting
from roster_settings import write
import forum_events as events

TEMPLATES_KEY = "forum_event_templates:registry"
TEMPLATE_FIELDS = events.FIELDS - {"starts_at", "status"}


def templates(db):
    row = db.get(AutomationSetting, TEMPLATES_KEY)
    return json.loads(row.value) if row else {}


def template_config(data):
    if not isinstance(data, dict) or set(data) != TEMPLATE_FIELDS:
        raise events.EventError("Некорректные поля шаблона.")
    checked = events.config(
        {
            **data,
            "starts_at": (events.now() + timedelta(days=1)).isoformat(),
            "status": "open",
        }
    )
    return {key: checked[key] for key in TEMPLATE_FIELDS}


def save_template(db, tid, revision, name, data):
    if not isinstance(tid, str) or not re.fullmatch("[a-f0-9]{32}", tid):
        raise events.EventError("Некорректный ID шаблона.")
    name = events.text(name, 60, True)
    if "\n" in name:
        raise events.EventError("Название шаблона должно быть одной строкой.")
    checked = template_config(data)
    saved = templates(db)
    old = saved.get(tid)
    if revision is None and old and old["name"] == name and old["config"] == checked:
        return old
    if (old and (type(revision) is not int or revision != old["revision"])) or (
        not old and revision is not None
    ):
        raise events.EventError("Шаблон изменился. Обновите список.")
    if any(
        key != tid and row["name"].casefold() == name.casefold()
        for key, row in saved.items()
    ):
        raise events.EventError("Шаблон с таким названием уже существует.")
    if not old and len(saved) >= 100:
        raise events.EventError("Можно сохранить до 100 шаблонов.")
    row = {
        "id": tid,
        "name": name,
        "config": checked,
        "revision": (old["revision"] if old else 0) + 1,
    }
    saved[tid] = row
    write(db, TEMPLATES_KEY, saved)
    return row


def delete_template(db, tid, revision):
    saved = templates(db)
    old = saved.get(tid)
    if not old or type(revision) is not int or old["revision"] != revision:
        raise events.EventError("Шаблон изменился. Обновите список.")
    del saved[tid]
    write(db, TEMPLATES_KEY, saved)


def save_result(db, eid, revision, data):
    event = events.get(db, eid)
    if type(revision) is not int or event["revision"] != revision:
        raise events.EventError("Событие изменилось. Загрузите свежую версию.")
    if not isinstance(data, dict) or set(data) != {"summary", "score", "images"}:
        raise events.EventError("Некорректные поля итогов.")
    summary = events.text(data["summary"], 1000, True)
    score = events.text(data["score"], 80)
    images = data["images"]
    if not isinstance(images, list) or len(images) > 3:
        raise events.EventError("Можно приложить до трёх HTTPS-ссылок на скриншоты.")
    images = [events.text(image, 500, True) for image in images]
    for image in images:
        url = urlsplit(image)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise events.EventError("Для скриншотов нужны ссылки HTTPS.")
    if events.now() < datetime.fromisoformat(event["config"]["starts_at"]):
        raise events.EventError("Итоги можно публиковать после начала события.")
    event["result"] = {
        "summary": summary,
        "score": score,
        "images": images,
        "revision": event.get("result", {}).get("revision", 0) + 1,
    }
    event["config"]["status"] = "closed"
    event["revision"] += 1
    events.save(db, event)
    return event
