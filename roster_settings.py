"""Versioned publication settings; never modify Discord roles or site access."""

import hashlib
import json
import re

from automation_control import AutomationSetting, utcnow

CONFIG_KEY = "discord_roster:publication_config"
CACHE_KEY = "discord_roster:publication_snapshot"
LABELS = {
    "founder": "Основатель и куратор ST-9",
    "field": "Полевой командир ST-9",
    "security_head": "Руководитель СБ",
    "security": "Помощники СБ",
    "leader": "Командир группы",
    "subleader": "Командиры подгрупп",
    "recon": "Разведывательная группа",
    "assault": "Десантно-штурмовая группа",
    "armor": "Бронетанковая группа",
    "support": "Группа обеспечения",
    "chief_instructor": "Главный инструктор",
    "instructor": "Инструкторы",
}
TRAINING_DESCRIPTION = (
    "проведение тренировок;\nподготовка игроков к распределению в основные группы;"
)


def defaults():
    from discord_roster import ROLES, UNITS

    descriptions = {key: "" for key in ROLES}
    descriptions.update({key: "\n".join(tasks) for key, _, _, tasks in UNITS})
    return {
        "role_ids": dict(ROLES),
        "descriptions": descriptions,
        "training_description": TRAINING_DESCRIPTION,
        "extra_roles": [],
    }


def plain(value, maximum, *, required=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"Текст должен содержать не более {maximum} символов.")
    if any(
        (ord(c) < 32 and c not in "\n\t") or 0xD800 <= ord(c) <= 0xDFFF for c in value
    ) or any(c in value for c in "\u202a\u202b\u202d\u202e\u2066\u2067\u2068\u2069"):
        raise ValueError("В тексте есть недопустимые управляющие символы.")
    if required and (not value.strip() or "\n" in value):
        raise ValueError("Укажите название роли одной строкой.")
    return value


def snowflake(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[1-9][0-9]{16,19}", value)
        or int(value) >= 2**64
    ):
        raise ValueError(
            "ID роли должен быть строкой из 17–20 цифр. Скопируйте ID из Discord."
        )
    return value


def validate(config):
    base = defaults()
    if not isinstance(config, dict) or set(config) != set(base):
        raise ValueError("Некорректные поля настроек состава.")
    ids, descriptions = config["role_ids"], config["descriptions"]
    if not isinstance(ids, dict) or set(ids) != set(base["role_ids"]):
        raise ValueError("Нужно сохранить все существующие роли состава.")
    if not isinstance(descriptions, dict) or set(descriptions) != set(ids):
        raise ValueError("Некорректные описания ролей состава.")
    ids = {key: snowflake(value) for key, value in ids.items()}
    descriptions = {key: plain(value, 1200) for key, value in descriptions.items()}
    training = plain(config["training_description"], 1200)
    extra = config["extra_roles"]
    if not isinstance(extra, list) or len(extra) > 20:
        raise ValueError("Можно добавить не более 20 ролей.")
    normalized = []
    for row in extra:
        if not isinstance(row, dict) or set(row) != {
            "role_id",
            "title",
            "description",
            "kind",
            "message",
            "prefix",
        }:
            raise ValueError("Некорректные поля дополнительной роли.")
        if row["kind"] not in ("position", "group") or row["message"] not in (
            "first",
            "second",
        ):
            raise ValueError("Выберите вид блока и сообщение.")
        normalized.append(
            {
                "role_id": snowflake(row["role_id"]),
                "title": plain(row["title"], 80, required=True),
                "description": plain(row["description"], 1200),
                "prefix": plain(row["prefix"], 8, required=True),
                "kind": row["kind"],
                "message": row["message"],
            }
        )
    all_ids = list(ids.values()) + [r["role_id"] for r in normalized]
    if len(set(all_ids)) != len(all_ids):
        raise ValueError("Одна роль Discord указана несколько раз.")
    return {
        "role_ids": ids,
        "descriptions": descriptions,
        "training_description": training,
        "extra_roles": normalized,
    }


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def load(db):
    row = db.get(AutomationSetting, CONFIG_KEY)
    return validate(json.loads(row.value)) if row else defaults()


def version(db):
    row = db.get(AutomationSetting, CONFIG_KEY)
    return digest([row.value, row.updated_at.isoformat()]) if row else "initial"


def snapshot(db):
    row = db.get(AutomationSetting, CACHE_KEY)
    return json.loads(row.value) if row else None


def write(db, key, value, actor_id=None):
    row = db.get(AutomationSetting, key)
    if row is None:
        row = AutomationSetting(key=key)
        db.add(row)
    row.value = json.dumps(value, ensure_ascii=False)
    row.updated_at, row.actor_id = utcnow(), actor_id
    db.flush()


def required_roles(config):
    return list(config["role_ids"].values()) + [
        r["role_id"] for r in config["extra_roles"]
    ]


def preview(db, config):
    from discord_roster import render_roster

    saved = snapshot(db)
    warnings = []
    if saved:
        available = {r["id"] for r in saved["roles"]}
        missing = set(required_roles(config)) - available
        if missing:
            raise ValueError(
                "В последнем снимке Discord не найдены роли: "
                + ", ".join(sorted(missing))
                + ". Проверьте ID или дождитесь следующей синхронизации бота."
            )
        raw = saved["members"]
    else:
        # Formatting is still reviewable before the first cache has been captured.
        raw = [{"user": {"id": "1", "username": "Предпросмотр"}, "roles": []}]
        warnings.append(
            "Снимка участников ещё нет: показан только формат, без имён. ID проверит бот при следующем обновлении; при ошибке прежние сообщения сохранятся."
        )
    texts = render_roster(raw, config)
    return {
        "texts": texts,
        "warnings": warnings,
        "snapshot_at": saved.get("checked_at") if saved else None,
        "snapshot_revision": digest(
            {"members": saved["members"], "roles": saved["roles"]} if saved else None
        ),
    }
