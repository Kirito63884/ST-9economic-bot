"""Protocol achievement-page evidence joined to the publisher's SteamID dumps."""

import json
import math
import re
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from urllib.parse import parse_qs, urlsplit

import httpx
from database import Base, SessionLocal
from protocol_clan_source import BASE, ClanAuthRequired, ClanUnavailable, request_page
from protocol_week_source import Tree, previous_key
from sqlalchemy import Column, DateTime, Integer, String, Text

URL = BASE + "/clan/achievements?clan=STRIKE%20TEAM%209"
KIT_START = "2026-10-12"
WEAPON_START = "2026-10-16"
EXTRA_KINDS = {
    "veh": (3, "Взорванная техника"),
    "fobweek": (10, "Недельные FOB"),
    "knife": (3, "Ножевые убийства"),
    "handat": (3, "Техника с труб и тандемов"),
    "pilot": (3, "Пилот и командир вертолёта"),
    "crew": (3, "Экипаж и командир экипажа"),
    "rifleman": (5, "Убийства за матч на Rifleman"),
    "atgm15": (5000, "15+ техники с ПТУР или СПГ за матч"),
}
WEAPON_KINDS = frozenset(("knife", "handat", "rifleman", "atgm15"))
KINDS = {
    "kills": (10, "Убийства"),
    "revives": (8, "Поднятия"),
    "wins": (5, "Победы"),
    "cmdwins": (3, "Победы командиром стороны"),
    "slwins": (3, "Победы сквадным"),
    "mg": (3, "Пулемётчик"),
    "sniper": (3, "Снайпер"),
    "sapper": (3, "Сапёр"),
    "medic": (3, "Медик"),
}
KIT_AWARDS = {
    "mg": "best_machine_gunner",
    "sniper": "best_sniper",
    "sapper": "best_engineer",
    "medic": "best_medic",
}


class ProtocolAwardExport(Base):
    __tablename__ = "protocol_award_exports"
    key = Column(String(40), primary_key=True)
    week = Column(String(10), nullable=True, index=True)
    kind = Column(String(20), nullable=False)
    payload = Column(Text, nullable=False)
    checked_at = Column(DateTime, nullable=False)


class ProtocolExportHealth(Base):
    __tablename__ = "protocol_export_health"
    id = Column(Integer, primary_key=True)
    attempted_at = Column(DateTime, nullable=False)
    status = Column(String(30), nullable=False)
    last_error = Column(String(300), nullable=True)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def flight_records(body):
    """Decode JSON transport only; never evaluate publisher JavaScript."""
    if len(body.encode("utf-8")) > 3_000_000:
        raise ClanUnavailable("Страница выгрузок слишком большая.")
    tree = Tree()
    tree.feed(body)
    stack, stream = [tree.root], ""
    while stack:
        node = stack.pop()
        stack.extend(reversed([c for c in node.children if hasattr(c, "tag")]))
        if node.tag != "script" or node.attrs.get("src"):
            continue
        script = "".join(c for c in node.children if isinstance(c, str))
        match = re.fullmatch(
            r"\s*self\.__next_f\.push\((\[[\s\S]*\])\)\s*;?\s*", script
        )
        if match:
            try:
                chunk = json.loads(match[1])
            except (ValueError, RecursionError) as exc:
                raise ClanUnavailable("Повреждён JSON страницы выгрузок.") from exc
            if (
                isinstance(chunk, list)
                and len(chunk) == 2
                and chunk[0] == 1
                and isinstance(chunk[1], str)
            ):
                stream += chunk[1]
    records = {}
    for line in stream.splitlines():
        key, sep, value = line.partition(":")
        if sep and re.fullmatch(r"[0-9a-f]+", key):
            try:
                records[key] = json.loads(value)
            except (ValueError, RecursionError):
                pass
    return records


def elements(value, records=None, depth=0):
    if depth > 100:
        raise ClanUnavailable("Слишком сложная структура выгрузок.")
    if isinstance(value, str) and records and re.fullmatch(r"\$(?:L)?[0-9a-f]+", value):
        yield from elements(
            records.get(value[2:] if value.startswith("$L") else value[1:]),
            records,
            depth + 1,
        )
    elif isinstance(value, list):
        if len(value) == 4 and value[0] == "$" and isinstance(value[3], dict):
            yield value
        for child in value:
            yield from elements(child, records, depth + 1)
    elif isinstance(value, dict):
        yield from elements(value.get("children"), records, depth + 1)


def text(value):
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, dict):
        return text(value.get("children"))
    if isinstance(value, list):
        if len(value) == 4 and value[0] == "$" and isinstance(value[3], dict):
            return text(value[3])
        return " ".join(text(c) for c in value)
    return ""


def weekly_kit_award(kind, title, unit):
    """Resolve the exact published top-3, never infer an award from names alone."""
    normalized = title.casefold().replace("ё", "е").replace("—", "-").replace("–", "-")
    if kind not in ("pilot", "crew") and not any(
        word in normalized for word in ("пилот", "экипаж")
    ):
        return None
    if unit != "hours" or not re.search(r"топ\s*-?\s*3\b", normalized):
        raise ClanUnavailable("Не подтверждён недельный топ-3 по часам.")
    if "пилот" in normalized and "командир вертолета" in normalized:
        return "best_pilot"
    if "экипаж" in normalized and "командир экипажа" in normalized:
        return "best_driver"
    raise ClanUnavailable("Не подтверждён состав китов недельного рейтинга.")


def parse_page(body, now=None):
    now = now or utcnow()
    records = flight_records(body)
    nodes = [n for value in records.values() for n in elements(value, records)]
    headings = [text(n[3]) for n in nodes if n[1] == "h1"]
    if not any("Достижения бойцов" in h and "STRIKE TEAM 9" in h for h in headings):
        raise ClanUnavailable("Не подтверждена страница достижений STRIKE TEAM 9.")
    sections = [
        n
        for n in nodes
        if n[1] == "section"
        and any(
            c[1] == "h2" and "Выгрузки SteamID (.txt)" in text(c[3])
            for c in elements(n[3], records)
        )
    ]
    # Referenced RSC subtrees can be reached more than once; use structural IDs.
    sections = {json.dumps(n, sort_keys=True, ensure_ascii=False): n for n in sections}
    if len(sections) != 1:
        raise ClanUnavailable("Не найдена однозначная секция выгрузок SteamID.")
    section = next(iter(sections.values()))
    basis = text(section)
    if "Все игроки Протокола" not in basis or "Бои без сида" not in basis:
        raise ClanUnavailable("Не подтверждён общий рейтинг Protocol без сида.")
    parents = {}
    for node in elements(section, records):
        for child in elements(node[3], records):
            if child is not node:
                # Deep iteration overwrites ancestors with the nearest enclosing node.
                parents[id(child)] = node
    previous = previous_key(now)
    current = (datetime.fromisoformat(previous) + timedelta(days=7)).date().isoformat()
    output, clan_ids = {}, set()
    for anchor in elements(section, records):
        if "href" not in anchor[3]:
            continue
        href = anchor[3].get("href", "")
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc or parsed.path != "/api/clans/dump":
            continue
        query = parse_qs(parsed.query)
        if set(query) - {"clan", "kind", "week"} or any(
            len(v) != 1 for v in query.values()
        ):
            raise ClanUnavailable("Некорректная ссылка выгрузки.")
        clan_id = query.get("clan", [""])[0]
        if not re.fullmatch(r"[a-z0-9]{15,40}", clan_id):
            raise ClanUnavailable("Не подтверждён идентификатор клана в выгрузке.")
        clan_ids.add(clan_id)
        kind = query.get("kind", [""])[0]
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,19}", kind):
            raise ClanUnavailable("Не подтверждено имя выгрузки.")
        scope = query.get("week", [None])[0]
        if (
            (kind == "fob" and scope is not None)
            or (kind in KINDS or kind in EXTRA_KINDS)
            and scope not in ("cur", "prev")
            or scope not in (None, "cur", "prev")
        ):
            raise ClanUnavailable("Не подтверждён период выгрузки.")
        container = parents.get(id(anchor))
        lists = []
        links = []
        empty = False
        for _ in range(5):
            if container is None:
                break
            lists = [n for n in elements(container[3], records) if n[1] == "ol"]
            links = [
                n
                for n in elements(container[3], records)
                if n[1] == "a" and "/api/clans/dump?" in n[3].get("href", "")
            ]
            if len(links) != 1:
                break
            empty = any(
                text(n[3]).strip() == "—" for n in elements(container[3], records)
            )
            if lists or empty:
                break
            container = parents.get(id(container))
        if (
            container is None
            or len(lists) > 1
            or (not lists and not empty)
            or len(links) != 1
        ):
            raise ClanUnavailable("Не найдена таблица значений выгрузки.")
        week = None if scope is None else previous if scope == "prev" else current
        heading = container
        title = ""
        for _ in range(4):
            if heading is None:
                break
            title = (
                text(heading).split("эта неделя")[0].split("прошлая неделя")[0].strip()
            )
            if title and title not in ("⬇ .txt", "—"):
                break
            heading = parents.get(id(heading))
        title = re.sub(r"\$L?[a-f0-9]+\b", "", title.split("⬇")[0])
        title = (
            re.sub(r"\s+", " ", title).strip()[:250]
            or ({**KINDS, **EXTRA_KINDS}.get(kind, (0, kind))[1])
        )
        if week:
            start = datetime.fromisoformat(week)
            period = (
                start.strftime("%d.%m")
                + "–"
                + (start + timedelta(days=6)).strftime("%d.%m")
            )
            label = "прошлая неделя" if scope == "prev" else "эта неделя"
            value = text(container)
            if label not in value or period not in value:
                raise ClanUnavailable(
                    "Даты рейтинга не соответствуют указанной неделе."
                )
        rows = []
        units = set()
        for li in elements(lists[0][3], records) if lists else []:
            if li[1] != "li":
                continue
            sid = li[2]
            if not isinstance(sid, str) or not re.fullmatch(r"7656\d{13}", sid):
                raise ClanUnavailable("В рейтинге нет проверяемого Steam ID.")
            values = [
                text(n[3]).strip() for n in elements(li[3], records) if n[1] == "b"
            ]
            if len(values) != 1:
                raise ClanUnavailable("Не подтверждено значение рейтинга.")
            unit = "hours" if values[0].strip().endswith("ч") else "count"
            units.add(unit)
            raw = (
                values[0]
                .replace("\u00a0", "")
                .replace(" ", "")
                .replace(",", ".")
                .removesuffix("ч")
                .strip()
            )
            try:
                numeric = float(raw)
            except ValueError as exc:
                raise ClanUnavailable("Некорректное число в рейтинге.") from exc
            if not math.isfinite(numeric) or not 0 < numeric < 1_000_000_000:
                raise ClanUnavailable("Некорректный диапазон рейтинга.")
            if unit == "count" and not numeric.is_integer():
                raise ClanUnavailable("Счётчик событий должен быть целым.")
            raw_name = text(li[3]).rsplit("—", 1)[0].strip()
            rows.append(
                {
                    "steam_id": sid,
                    "name": raw_name[:200],
                    "value": numeric if unit == "hours" else int(numeric),
                }
            )
        if len(units) > 1 or (kind in KIT_AWARDS and rows and units != {"hours"}):
            raise ClanUnavailable("Единицы измерения рейтинга не совпадают.")
        unit = next(
            iter(units),
            "hours"
            if kind in KIT_AWARDS
            or kind in ("pilot", "crew")
            or any(word in title.casefold() for word in ("пилот", "экипаж"))
            else "count",
        )
        weekly_award = weekly_kit_award(kind, title, unit)
        if weekly_award and len(rows) > 3:
            raise ClanUnavailable(
                "Недельный топ пилота или экипажа содержит больше трёх мест."
            )
        candidate_award = None
        # Driver is awarded only from the verified «Экипаж» weekly top.
        if len({r["steam_id"] for r in rows}) != len(rows) or any(
            a["value"] < b["value"] for a, b in pairwise(rows)
        ):
            raise ClanUnavailable("Порядок или Steam ID рейтинга повреждены.")
        if (
            kind in {**KINDS, **EXTRA_KINDS}
            and len(rows) > {**KINDS, **EXTRA_KINDS}[kind][0]
        ):
            raise ClanUnavailable("Размер рейтинга не соответствует выгрузке.")
        for index, row in enumerate(rows):
            row["rank"] = (
                index + 1
                if unit == "hours"
                else 1
                + sum(1 for other in rows[:index] if other["value"] > row["value"])
            )
        key = (week or "all") + ":" + kind
        if key in output:
            raise ClanUnavailable("Повторяющаяся выгрузка на странице.")
        output[key] = {
            "schema": 1,
            "week": week,
            "kind": kind,
            "rows": rows,
            "file": BASE + href,
            "source": URL,
            "title": title,
            "unit": unit,
            "candidate_award": candidate_award,
            "weekly_kit_award": weekly_award,
            "exact_weapons": bool(week and week >= WEAPON_START),
            "handat_criteria_verified": bool(
                kind == "handat"
                and all(
                    v in title.casefold()
                    for v in ("труб", "тандем", "без мин", "птур", "спг", "техники")
                )
            ),
            "sl_scope": "infantry_only" if kind == "slwins" else None,
            "final": scope == "prev",
            "exact_kits": bool(week and week >= KIT_START),
            "basis": "Все игроки Protocol; бои без сида. Кит при победе — в конце матча.",
            "coverage": "FOB учитываются с 07.10.2026; показаны первые 20 счётчиков."
            if kind == "fob"
            else "Учёт оружия опубликован с 16.10.2026; более ранние недели неполные."
            if kind in WEAPON_KINDS
            else None,
        }
    expected = {"all:fob"} | {w + ":" + k for w in (current, previous) for k in KINDS}
    if not expected.issubset(output) or len(clan_ids) != 1:
        raise ClanUnavailable("Неполный набор выгрузок или смешаны разные кланы.")
    return output


def parse_ids(body):
    if len(body) > 200_000:
        raise ClanUnavailable("Выгрузка SteamID слишком большая.")
    ids = [line.strip() for line in body.lstrip("\ufeff").splitlines() if line.strip()]
    if (
        len(ids) > 5000
        or any(not re.fullmatch(r"7656\d{13}", sid) for sid in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ClanUnavailable(
            "Выгрузка должна содержать уникальные SteamID, по одному в строке."
        )
    return ids


def fetch_exports(now):
    from protocol_clan_source import make_client

    accepted, errors = {}, []
    with make_client() as client:
        documents = parse_page(request_page(client, URL), now)
        for key, document in documents.items():
            try:
                ids = parse_ids(request_page(client, document["file"]))
                expected = [r["steam_id"] for r in document["rows"]]
                if (document["kind"] == "fob" and ids[: len(expected)] != expected) or (
                    document["kind"] != "fob" and ids != expected
                ):
                    raise ClanUnavailable(
                        "Страница и TXT расходятся. Проверка будет повторена."
                    )
                document["file_verified"] = True
                document["file_count"] = len(ids)
                document["file_ids"] = ids
                accepted[key] = document
            except ClanAuthRequired:
                raise
            except (ClanUnavailable, httpx.HTTPError, OSError):
                errors.append(key)
    return accepted, errors


def collect_exports(now=None, fetch=fetch_exports, force=False):
    moment = now or utcnow()
    from automation_control import queue_alert, resolve_alerts, source_enabled

    with SessionLocal() as db:
        if not source_enabled(db, "protocol"):
            return False
        old = db.get(ProtocolExportHealth, 1)
        if (
            not force
            and old
            and timedelta(0) <= moment - old.attempted_at < timedelta(hours=1)
        ):
            return old.status == "ready"
    documents, error, status = {}, None, "ready"
    try:
        documents, failed = fetch(moment)
        if failed:
            status, error = (
                "partial",
                "Часть выгрузок не подтверждена: "
                + ", ".join(failed)[:180]
                + ". Предыдущие данные сохранены.",
            )
    except ClanAuthRequired:
        status, error = (
            "auth_required",
            "Для выгрузок требуется вход Protocol: выполните protocol_login.py.",
        )
    except (ClanUnavailable, httpx.HTTPError, OSError, UnicodeError):
        status, error = (
            "unavailable",
            "Выгрузки Protocol не подтверждены; сохранены предыдущие данные.",
        )
    with SessionLocal.begin() as db:
        for key, document in documents.items():
            row = db.get(ProtocolAwardExport, key)
            if row is None:
                row = ProtocolAwardExport(
                    key=key, week=document["week"], kind=document["kind"]
                )
                db.add(row)
            row.payload, row.checked_at = (
                json.dumps(document, ensure_ascii=False, allow_nan=False),
                moment,
            )
        health = db.get(ProtocolExportHealth, 1)
        if health is None:
            health = ProtocolExportHealth(id=1)
            db.add(health)
        health.attempted_at, health.status, health.last_error = moment, status, error
        if error:
            queue_alert(db, "protocol_award_exports", "Protocol", error)
        else:
            resolve_alerts(db, "protocol_award_exports")
    return bool(documents)


def verified_documents(db, now=None):
    now = now or utcnow()
    for row in db.query(ProtocolAwardExport):
        try:
            data = json.loads(row.payload)
            if (
                data.get("schema") != 1
                or data.get("source") != URL
                or not data.get("file_verified")
                or data.get("week") != row.week
                or data.get("kind") != row.kind
            ):
                continue
            # Current-week and all-time values must be recent; a verified closed
            # week remains historical evidence and never becomes next week's data.
            if not data.get("final") and not timedelta(
                0
            ) <= now - row.checked_at <= timedelta(hours=24):
                continue
            yield row, data
        except (ValueError, TypeError):
            continue


def knife_final_supported(data):
    """Use the publisher's explicit final top, including its first tracked week.

    This is a published ranking, not a claim of seven days of weapon coverage.
    The first partial period is disclosed in evidence and the chief preview.
    """
    return bool(
        data.get("kind") == "knife"
        and data.get("final")
        and data.get("week", "") >= "2026-10-12"
        and data.get("unit", "count") == "count"
    )


def award_exports(db, users, now, only_week=None, verified_keys=None):
    from automation_control import setting
    from medals import grant, week_label

    covered = set()
    for snapshot, data in verified_documents(db, now):
        if only_week is not None and snapshot.week != only_week:
            continue
        if verified_keys is not None and snapshot.key not in verified_keys:
            continue
        kind, week = snapshot.kind, snapshot.week
        if week:
            if not data.get("final") or week > previous_key(now):
                continue
            if kind == "slwins":
                # The publisher labels this dump infantry-only. It cannot prove
                # a place in the site's overall SL/SLCrewman/SLPilot ranking.
                continue
            if (kind in KIT_AWARDS or data.get("weekly_kit_award")) and not data.get(
                "exact_kits"
            ):
                continue
            if (
                kind in WEAPON_KINDS
                and not data.get("exact_weapons")
                and not knife_final_supported(data)
            ):
                continue
            if kind == "handat" and not data.get("handat_criteria_verified"):
                continue
            if data.get("candidate_award") == "best_driver" and not data.get(
                "exact_kits"
            ):
                continue
            covered.add((week, kind))
            if data.get("candidate_award") in ("best_driver",):
                covered.add((week, "__" + data["candidate_award"]))
        rows = {r["steam_id"]: r for r in data["rows"]}
        for user in users:
            item = rows.get(user.steam_id)
            if not item:
                continue
            codes = []
            if kind == "fob" and item["value"] >= 50:
                codes.append("best_demolition")
            elif kind == "kills":
                star = {1: "gold_star", 2: "silver_star", 3: "bronze_star"}.get(
                    item["rank"]
                )
                if star:
                    codes.append(star)
                if item["rank"] <= setting(db, "weekly_merit_kills_rank"):
                    codes.append("merit")
            elif kind == "revives" and item["rank"] <= setting(
                db, "weekly_merit_revive_rank"
            ):
                codes.append("medical_aid")
            elif kind == "wins" and item["rank"] <= setting(
                db, "weekly_merit_wins_rank"
            ):
                codes.append("merit")
            elif kind in ("cmdwins", "slwins"):
                limit = setting(
                    db,
                    "weekly_cmd_rank"
                    if kind == "cmdwins"
                    else "weekly_squad_leader_rank",
                )
                if item["rank"] <= limit:
                    codes.append(
                        "outstanding_service" if kind == "cmdwins" else "squad_leader"
                    )
            elif data.get("weekly_kit_award") and item["rank"] == 1:
                codes.append(data["weekly_kit_award"])
            elif kind in KIT_AWARDS and item["rank"] <= 3:
                codes.append(KIT_AWARDS[kind])
            elif kind == "knife" and item["rank"] <= setting(
                db, "weekly_best_knife_rank"
            ):
                codes.append("best_knife")
            elif kind == "handat" and item["rank"] <= setting(
                db, "weekly_anti_tank_rank"
            ):
                codes.append("anti_tank")

            for code in codes:
                proof = {
                    **data,
                    "rows": None,
                    "file_ids": None,
                    "published": item,
                    "checked_at": snapshot.checked_at.isoformat() + "Z",
                }
                label = (
                    week_label(week)
                    if week
                    else "С 07.10.2026 (подтверждённый минимум за всё время)"
                )
                metric = (
                    "уничтоженных вражеских FOB"
                    if kind == "fob"
                    else {**KINDS, **EXTRA_KINDS}.get(
                        kind, (0, data.get("title", kind))
                    )[1]
                )
                reason = (
                    f"Protocol, выгрузки SteamID. {label}: {metric}, {item['value']:g}"
                    + (
                        " ч"
                        if data.get("unit") == "hours" or kind in KIT_AWARDS
                        else ""
                    )
                )
                if week:
                    reason += f", место {item['rank']} среди всех игроков. Завершённая неделя."
                if kind == "knife":
                    proof["tracking_from"] = max(week, WEAPON_START)
                    proof["partial_tracking"] = week < WEAPON_START
                    if proof["partial_tracking"]:
                        reason += " Опубликованный итог Protocol; учёт ножевых убийств только с 16.10.2026 (неполная первая неделя)."
                grant(db, user, code, reason, proof, now)
    return covered


def export_status(db):
    health = db.get(ProtocolExportHealth, 1)
    return {
        "status": health.status if health else "pending",
        "error": health.last_error if health else None,
        "attempted_at": health.attempted_at.isoformat() + "Z" if health else None,
        "snapshots": db.query(ProtocolAwardExport).count(),
        "exact_kits_from": KIT_START,
    }


def player_export_progress(db, steam_id, now=None):
    result = {}
    for snapshot, data in verified_documents(db, now):
        item = next((r for r in data["rows"] if r["steam_id"] == steam_id), None)
        result[snapshot.key] = {
            "week": snapshot.week,
            "kind": snapshot.kind,
            "player": item,
            "final": data["final"],
            "exact_kits": data["exact_kits"],
            "coverage": data["coverage"],
            "exact_weapons": data.get("exact_weapons", False),
            "candidate_award": data.get("candidate_award"),
            "title": data.get("title", snapshot.kind),
            "unit": data.get("unit"),
            "weekly_kit_award": data.get("weekly_kit_award"),
            "checked_at": snapshot.checked_at.isoformat() + "Z",
            "source": URL,
        }
    return result
