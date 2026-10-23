"""Read the published previous-week Protocol standings without evaluating scripts."""

import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.request import build_opener, HTTPRedirectHandler, Request
from sqlalchemy import Column, Integer, String, Text, DateTime
from database import Base, SessionLocal

BASE = "https://protocol-squad.ru"
URL = BASE + "/week?w=prev"
MOSCOW = timezone(timedelta(hours=3))
LABELS = {
    "cmd": "Победы CMD",
    "sl": "Победы сквадным",
    "revive": "Поднятия",
    "vehicles": "Уничтоженная техника",
    "rifle_kills": "Убийства стрелками",
    "crew_kills": "Убийства с техники",
}
SECTIONS = {
    "ЦМД недели": "cmd",
    "Сквадные недели": "sl",
    "Медик недели": "revive",
    "Анти-техника недели": "vehicles",
    "Стрелок недели": "rifle_kills",
    "Технарь недели": "crew_kills",
}
EXTRA_SECTIONS = {"Мехвод недели": "driver_hours", "Пистолетчик недели": "pistol_kills"}
EXTRA_LABELS = {"driver_hours": "Часы мехвода", "pistol_kills": "Убийства с пистолета"}
EMPTY_SECTION_TEXT = "Пока нет данных за неделю"
EMPTY_RECHECK = timedelta(hours=24)
MAX_EMPTY_RECHECKS = 3


class PublicWeekUnavailable(ValueError):
    pass


class ProtocolPublishedWeek(Base):
    __tablename__ = "protocol_published_weeks"
    week = Column(String(10), primary_key=True)
    payload = Column(Text, nullable=False)
    checked_at = Column(DateTime, nullable=False)


class ProtocolPublishedHealth(Base):
    __tablename__ = "protocol_published_health"
    id = Column(Integer, primary_key=True)
    status = Column(String(30), nullable=False)
    checked_at = Column(DateTime, nullable=False)
    last_error = Column(String(150), nullable=True)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def previous_key(now=None):
    local = (
        (now or datetime.now(timezone.utc))
        .replace(tzinfo=timezone.utc)
        .astimezone(MOSCOW)
        if not (now and now.tzinfo)
        else now.astimezone(MOSCOW)
    )
    return (local.date() - timedelta(days=local.weekday() + 7)).isoformat()


class Node:
    def __init__(self, tag, attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def walk(self, slots=None, depth=0):
        if depth > 120:
            raise PublicWeekUnavailable("HTML depth exceeded")
        if self.tag in ("script", "style"):
            return
        if self.tag == "template" and slots:
            ident = self.attrs.get("id", "")
            suffix = ident.partition(":")[2]
            if ident.startswith(("P:", "B:")) and "S:" + suffix in slots:
                yield from slots["S:" + suffix].walk(slots, depth + 1)
                return
            raise PublicWeekUnavailable("Incomplete streamed standings")
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk(slots, depth + 1)

    def text(self, slots=None, depth=0):
        if depth > 120:
            raise PublicWeekUnavailable("HTML depth exceeded")
        if self.tag in ("script", "style"):
            return ""
        if self.tag == "template" and slots:
            ident = self.attrs.get("id", "")
            target = slots.get("S:" + ident.partition(":")[2])
            if target:
                return target.text(slots, depth + 1)
            raise PublicWeekUnavailable("Incomplete streamed standings")
        return " ".join(
            child.text(slots, depth + 1) if isinstance(child, Node) else child
            for child in self.children
        ).strip()


class Tree(HTMLParser):
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if len(self.stack) > 120:
            raise PublicWeekUnavailable("HTML depth exceeded")
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_previous(body, now=None):
    if len(body) > 3_000_000:
        raise PublicWeekUnavailable("Response too large")
    tree = Tree()
    tree.feed(body)
    tree.close()
    slots = {
        n.attrs["id"]: n
        for n in tree.root.walk()
        if n.attrs.get("id", "").startswith("S:")
    }
    all_text = tree.root.text(slots)
    period = re.search(
        r"Итог завершённой недели\s*\(\s*(\d{2}\.\d{2})\s*—\s*(\d{2}\.\d{2})\s*\)",
        all_text,
    )
    key = previous_key(now)
    monday = datetime.strptime(key, "%Y-%m-%d").date()
    if not period or period.groups() != (
        (monday - timedelta(days=1)).strftime("%d.%m"),
        (monday + timedelta(days=6)).strftime("%d.%m"),
    ):
        raise PublicWeekUnavailable("Previous-week dates not confirmed")
    if "Награды выданы в воскресенье в 23:00 МСК" not in all_text:
        raise PublicWeekUnavailable("Final standings not confirmed")
    categories = {}
    seen_sections = set()
    for section in tree.root.walk(slots):
        if section.tag != "section" or id(section) in seen_sections:
            continue
        seen_sections.add(id(section))
        headings = [n.text(slots).strip() for n in section.walk(slots) if n.tag == "h2"]
        if len(headings) != 1 or headings[0] not in {**SECTIONS, **EXTRA_SECTIONS}:
            continue
        code = {**SECTIONS, **EXTRA_SECTIONS}[headings[0]]
        if code in categories:
            raise PublicWeekUnavailable("Duplicate category")
        rows = []
        seen = set()
        for anchor in section.walk(slots):
            if anchor.tag != "a":
                continue
            identity = re.fullmatch(
                r"/player/(7656\d{13})", anchor.attrs.get("href", "")
            )
            if not identity:
                continue
            sid = identity[1]
            if sid in seen:
                raise PublicWeekUnavailable("Duplicate player")
            children = list(anchor.walk(slots))
            text = anchor.text(slots)
            names = [
                n.text(slots).strip()
                for n in children
                if "truncate" in n.attrs.get("class", "").split()
            ]
            if len(names) != 1 or not names[0] or len(names[0]) > 200:
                raise PublicWeekUnavailable("Player name missing")
            if code in ("cmd", "sl"):
                found = re.search(
                    r"(\d+)\s*(?:побед|W)\s*·\s*(\d+)\s*(?:матчей|игр)", text
                )
                if not found:
                    raise PublicWeekUnavailable("Win statistics missing")
                value = int(found[1])
                matches = int(found[2])
                if value > matches:
                    raise PublicWeekUnavailable("Invalid win statistics")
            elif code == "driver_hours":
                found = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ч\b|час)", text)
                if not found:
                    raise PublicWeekUnavailable("Driver hours missing")
                value = float(found[1].replace(",", "."))
                matches = None
            else:
                unit = {
                    "revive": "поднятий",
                    "vehicles": "техники",
                    "rifle_kills": "убийств",
                    "crew_kills": "убийств",
                    "pistol_kills": "убийств",
                }[code]
                numbers = re.findall(r"(\d[\d\s\u00a0]*)\s+" + unit, text)
                if not numbers:
                    raise PublicWeekUnavailable("Category statistics missing")
                value = int(re.sub(r"\s", "", numbers[-1]))
                matches = None
            positions = [
                n.text(slots).strip()
                for n in children
                if "text-center" in n.attrs.get("class", "").split() and n.tag == "span"
            ]
            medal = re.search("[🥇🥈🥉]", text)
            rank = (
                {"🥇": 1, "🥈": 2, "🥉": 3}[medal[0]]
                if medal
                else int(positions[0])
                if positions and positions[0].isdigit()
                else 0
            )
            if rank != len(rows) + 1 or value <= 0:
                raise PublicWeekUnavailable("Invalid published rank")
            rows.append(
                {
                    "steam_id": sid,
                    "name": names[0],
                    "rank": rank,
                    "value": value,
                    "matches": matches,
                }
            )
            seen.add(sid)
        expected = 10 if code in ("cmd", "sl") else 5
        section_text = section.text(slots)
        # Protocol legitimately publishes an empty final category when nobody
        # qualified. Accept only its explicit empty state; partial rows still
        # mean that the streamed response was truncated.
        if code in EXTRA_LABELS:
            if not 1 <= len(rows) <= 10 and not (
                not rows and EMPTY_SECTION_TEXT in section_text
            ):
                raise PublicWeekUnavailable("Incomplete optional category")
        elif len(rows) != expected and not (
            not rows and EMPTY_SECTION_TEXT in section_text
        ):
            raise PublicWeekUnavailable("Incomplete category")
        categories[code] = {
            "name": {**LABELS, **EXTRA_LABELS}[code],
            "rows": rows,
            "published_count": len(rows),
        }
        if code in EXTRA_LABELS:
            categories[code]["criterion"] = code
    if not set(LABELS).issubset(categories):
        raise PublicWeekUnavailable("Categories missing")
    empty = [code for code, value in categories.items() if not value["rows"]]
    return {
        "week": key,
        "period": period[1] + " — " + period[2],
        "source": URL,
        "final": True,
        "basis": "Опубликованный итог Protocol; закрытие в воскресенье в 23:00 МСК",
        "categories": categories,
        "empty_categories": empty,
        "empty_rechecks": {code: 0 for code in empty},
        "award_compatible": False,
    }


def empty_categories(document):
    categories = document.get("categories", {}) if isinstance(document, dict) else {}
    return [
        code
        for code in LABELS
        if code in categories and not categories[code].get("rows")
    ]


def empty_rechecks(document):
    values = document.get("empty_rechecks", {}) if isinstance(document, dict) else {}
    return {
        code: min(MAX_EMPTY_RECHECKS, max(0, int(values.get(code, 0))))
        for code in empty_categories(document)
    }


class SameOrigin(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(BASE + "/"):
            raise PublicWeekUnavailable("Unexpected redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_previous(opener=None, now=None):
    opener = opener or build_opener(SameOrigin())
    for attempt in range(2):
        try:
            with opener.open(
                Request(URL, headers={"User-Agent": "StrikeTeam9-Weekly/1.0"}),
                timeout=20,
            ) as response:
                if not response.geturl().startswith(BASE + "/"):
                    raise PublicWeekUnavailable("Unexpected origin")
                raw = response.read(3_000_001)
            if len(raw) > 3_000_000:
                raise PublicWeekUnavailable("Response too large")
            return parse_previous(raw.decode("utf-8"), now)
        except (OSError, UnicodeError, PublicWeekUnavailable) as exc:
            if attempt:
                raise PublicWeekUnavailable(
                    "Protocol previous week unavailable"
                ) from exc


def collect_previous(fetch=fetch_previous, now=None, empty_recheck=False):
    moment = now or utcnow()
    try:
        document = fetch(now=moment)
        if document.get("week") != previous_key(moment):
            raise PublicWeekUnavailable("Unexpected week")
        with SessionLocal.begin() as db:
            row = db.get(ProtocolPublishedWeek, document["week"])
            if row is not None:
                try:
                    previous = json.loads(row.payload)
                except (TypeError, ValueError):
                    previous = {}
                old_empty = set(empty_categories(previous))
                old_counts = empty_rechecks(previous)
                document["empty_rechecks"] = {
                    code: (
                        min(
                            MAX_EMPTY_RECHECKS,
                            old_counts.get(code, 0) + (1 if empty_recheck else 0),
                        )
                        if code in old_empty
                        else 0
                    )
                    for code in empty_categories(document)
                }
                if empty_recheck:
                    document["empty_recheck_last_at"] = moment.isoformat() + "Z"
                elif previous.get("empty_recheck_last_at"):
                    document["empty_recheck_last_at"] = previous[
                        "empty_recheck_last_at"
                    ]
            payload = json.dumps(document, ensure_ascii=False, allow_nan=False)
            if row is None:
                db.add(
                    ProtocolPublishedWeek(
                        week=document["week"], payload=payload, checked_at=moment
                    )
                )
            else:
                row.payload = payload
                row.checked_at = moment
            health = db.get(ProtocolPublishedHealth, 1)
            if health is None:
                health = ProtocolPublishedHealth(id=1)
                db.add(health)
            health.status = "ready"
            health.checked_at = moment
            health.last_error = None
            from automation_control import resolve_alerts

            resolve_alerts(db, "protocol_previous_week")
        return True
    except (PublicWeekUnavailable, OSError):
        with SessionLocal.begin() as db:
            if empty_recheck:
                row = db.get(ProtocolPublishedWeek, previous_key(moment))
                if row is not None:
                    try:
                        document = json.loads(row.payload)
                    except (TypeError, ValueError):
                        document = {}
                    counts = empty_rechecks(document)
                    document["empty_rechecks"] = {
                        code: min(MAX_EMPTY_RECHECKS, count + 1)
                        for code, count in counts.items()
                    }
                    document["empty_recheck_last_at"] = moment.isoformat() + "Z"
                    row.payload = json.dumps(
                        document, ensure_ascii=False, allow_nan=False
                    )
            health = db.get(ProtocolPublishedHealth, 1)
            if health is None:
                health = ProtocolPublishedHealth(id=1)
                db.add(health)
            health.status = "unavailable"
            health.checked_at = moment
            health.last_error = (
                "Не удалось проверить итог прошлой недели Protocol; повторим запрос."
            )
            from automation_control import queue_alert

            queue_alert(db, "protocol_previous_week", "Protocol", health.last_error)
        return False


def published_data(db, sid=None, now=None):
    key = previous_key(now)
    row = db.get(ProtocolPublishedWeek, key)
    if row is None:
        row = (
            db.query(ProtocolPublishedWeek)
            .order_by(ProtocolPublishedWeek.week.desc())
            .first()
        )
    health = db.get(ProtocolPublishedHealth, 1)
    status = health.status if health else "warming"
    if row is None:
        return {"available": False, "status": status, "source": URL, "categories": {}}
    doc = json.loads(row.payload)
    if sid is not None:
        doc["categories"] = {
            k: {
                "name": v["name"],
                "published_count": v["published_count"],
                "player": next((r for r in v["rows"] if r["steam_id"] == sid), None),
            }
            for k, v in doc["categories"].items()
        }
    return {
        **doc,
        "available": True,
        "stale": row.week != key,
        "status": status,
        "checked_at": row.checked_at.isoformat() + "Z",
        "error": health.last_error if health else None,
    }


def seed_previous(db, document):
    """Install a verified public snapshot without replacing live data or award evidence."""
    if (
        document.get("source") != URL
        or document.get("award_compatible") is not False
        or not document.get("final")
    ):
        raise PublicWeekUnavailable("Unrecognized snapshot")
    stamp = (
        datetime.fromisoformat(document["captured_at"].replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    if document["week"] != previous_key(stamp) or set(document["categories"]) != set(
        LABELS
    ):
        raise PublicWeekUnavailable("Snapshot period invalid")
    if db.get(ProtocolPublishedWeek, document["week"]) is None:
        db.add(
            ProtocolPublishedWeek(
                week=document["week"],
                payload=json.dumps(document, ensure_ascii=False, allow_nan=False),
                checked_at=stamp,
            )
        )


def collect_due_previous(now=None, fetch=fetch_previous):
    """One successful check per closed week, starting Monday Moscow; retry failures."""
    moment = now or utcnow()
    with SessionLocal() as db:
        from automation_control import source_enabled, resolve_alerts

        if not source_enabled(db, "protocol"):
            resolve_alerts(db, "protocol_previous_week")
            db.commit()
            return True
        row = db.get(ProtocolPublishedWeek, previous_key(moment))
        health = db.get(ProtocolPublishedHealth, 1)
        # Persisted attempts also throttle failures across worker restarts.
        if (
            health
            and health.checked_at
            and timedelta(0) <= moment - health.checked_at < timedelta(hours=1)
        ):
            return health.status == "ready"
        empty_recheck = False
        if row:
            try:
                document = json.loads(row.payload)
                empty = empty_categories(document)
                counts = empty_rechecks(document)
            except (TypeError, ValueError):
                empty = []
                counts = {}
            raw_last = (
                document.get("empty_recheck_last_at")
                if isinstance(document, dict)
                else None
            )
            try:
                last_attempt = (
                    datetime.fromisoformat(raw_last.replace("Z", "+00:00"))
                    .astimezone(timezone.utc)
                    .replace(tzinfo=None)
                    if raw_last
                    else row.checked_at
                )
            except (TypeError, ValueError):
                last_attempt = row.checked_at
            if not empty or all(
                counts.get(code, 0) >= MAX_EMPTY_RECHECKS for code in empty
            ):
                return True
            if moment - last_attempt < EMPTY_RECHECK:
                return True
            empty_recheck = True
    return collect_previous(fetch=fetch, now=moment, empty_recheck=empty_recheck)


def award_published(db, users, now, only_week=None):
    """Award criteria that are explicitly published in the final Protocol table."""
    from medals import grant, FIRST_WEEK
    from automation_control import setting

    rows = (
        db.query(ProtocolPublishedWeek)
        .filter(
            ProtocolPublishedWeek.week >= FIRST_WEEK,
            ProtocolPublishedWeek.week <= previous_key(now),
        )
        .order_by(ProtocolPublishedWeek.week)
        .all()
    )
    # A failed health check is not an authoritative standings document.
    primary = set()
    for snapshot in rows:
        if only_week is not None and snapshot.week != only_week:
            continue
        try:
            doc = json.loads(snapshot.payload)
        except (ValueError, TypeError):
            continue
        if (
            not isinstance(doc, dict)
            or doc.get("source") != URL
            or not doc.get("final")
            or doc.get("week") != snapshot.week
        ):
            continue
        categories = doc.get("categories")
        if not isinstance(categories, dict) or any(
            not isinstance(categories.get(key), dict)
            or not isinstance(categories[key].get("rows"), list)
            for key in ("cmd", "sl", "revive")
        ):
            continue
        primary.add(snapshot.week)
        for user in users:
            if not user.steam_id:
                continue
            for metric, code, minimum in (
                ("driver_hours", "best_driver", "2026-10-12"),
            ):
                category = categories.get(metric, {})
                if (
                    snapshot.week < minimum
                    or category.get("criterion") != metric
                    or (snapshot.week, "__" + code)
                    in db.info.get("protocol_export_primary", set())
                ):
                    continue
                item = next(
                    (
                        r
                        for r in category.get("rows", [])
                        if r["steam_id"] == user.steam_id
                    ),
                    None,
                )
                if item and item.get("rank") == 1 and item.get("value", 0) > 0:
                    grant(
                        db,
                        user,
                        code,
                        f"Итог Protocol {doc['period']}: 1-е место, {item['value']} "
                        + (
                            "ч на ките мехвода."
                            if metric == "driver_hours"
                            else "убийств с пистолета."
                        ),
                        {
                            "source": URL,
                            "week": snapshot.week,
                            "metric": metric,
                            "published": item,
                            "final": True,
                        },
                        now,
                    )
            for metric, code, limit, unit in (
                (
                    "cmd",
                    "outstanding_service",
                    setting(db, "weekly_cmd_rank"),
                    "побед командиром стороны",
                ),
                (
                    "sl",
                    "squad_leader",
                    setting(db, "weekly_squad_leader_rank"),
                    "побед сквадным",
                ),
                (
                    "revive",
                    "medical_aid",
                    setting(db, "weekly_merit_revive_rank"),
                    "поднятий",
                ),
            ):
                if (
                    snapshot.week,
                    {"cmd": "cmdwins", "sl": "slwins", "revive": "revives"}[metric],
                ) in db.info.get("protocol_export_primary", set()):
                    continue
                item = next(
                    (
                        r
                        for r in doc["categories"][metric]["rows"]
                        if r["steam_id"] == user.steam_id
                    ),
                    None,
                )
                if item and 0 < item["rank"] <= limit:
                    proof = {
                        "source": URL,
                        "week": snapshot.week,
                        "period": doc["period"],
                        "metric": metric,
                        "published": item,
                        "basis": doc["basis"],
                        "final": True,
                    }
                    grant(
                        db,
                        user,
                        code,
                        f"Итог Protocol {doc['period']}: {item['rank']}-е место, {item['value']} {unit}. "
                        + (
                            f"Матчей: {item['matches']}. "
                            if item.get("matches")
                            else ""
                        )
                        + "Опубликованная завершённая неделя, закрытие в воскресенье в 23:00 МСК.",
                        proof,
                        now,
                    )
    return primary
