"""Authenticated, read-only collection of Protocol's ClanRosterStats data."""

import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from database import PROJECT_DIR, Base, SessionLocal
from protocol_week_source import Tree
from sqlalchemy import Column, DateTime, Integer, String, Text

BASE = "https://protocol-squad.ru"
URL = BASE + "/clan?clan=STRIKE%20TEAM%209"
CLAN = "STRIKE TEAM 9"
MAX_BYTES = 3_000_000
FRESH_FOR = timedelta(hours=24)


class ClanUnavailable(ValueError):
    pass


class ClanAuthRequired(ClanUnavailable):
    pass


class ProtocolClanStats(Base):
    __tablename__ = "protocol_clan_stats"
    id = Column(Integer, primary_key=True)
    payload = Column(Text, nullable=True)
    checked_at = Column(DateTime, nullable=True)
    attempted_at = Column(DateTime, nullable=False)
    status = Column(String(30), nullable=False)
    last_error = Column(String(200), nullable=True)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cookie_path():
    configured = Path(
        os.getenv("PROTOCOL_COOKIE_FILE", ".protocol-session.cookies")
    ).expanduser()
    return configured if configured.is_absolute() else PROJECT_DIR / configured


def bounded_number(value, maximum):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= maximum
    ):
        raise ClanUnavailable("Некорректные значения статистики Protocol.")
    return value


def parse_clan(body):
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise ClanUnavailable("Ответ Protocol слишком большой.")
    tree = Tree()
    tree.feed(body)
    tree.close()
    nodes, stack = [], [tree.root]
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(
            reversed([child for child in node.children if hasattr(child, "tag")])
        )
    if any(n.tag == "input" and n.attrs.get("name") == "code" for n in nodes):
        raise ClanAuthRequired("Требуется вход в Protocol на сервере.")
    if not any(
        n.tag == "a" and n.attrs.get("href") == "/clans/STRIKE%20TEAM%209"
        for n in nodes
    ):
        raise ClanUnavailable("Не подтверждена страница клана STRIKE TEAM 9.")
    counts = set()
    for node in nodes:
        if node.tag == "h2":
            match = re.search(r"Состав клана\s*(\d+)", node.text())
            if match:
                counts.add(int(match[1]))
    if len(counts) != 1:
        raise ClanUnavailable("Не подтверждён полный состав клана.")
    stream = ""
    for node in nodes:
        if node.tag != "script" or node.attrs.get("src"):
            continue
        script = "".join(x for x in node.children if isinstance(x, str))
        match = re.fullmatch(
            r"\s*self\.__next_f\.push\((\[[\s\S]*\])\)\s*;?\s*", script
        )
        if not match:
            continue
        try:
            chunk = json.loads(match[1])
        except (ValueError, RecursionError) as exc:
            raise ClanUnavailable("Повреждён ответ Protocol.") from exc
        if (
            isinstance(chunk, list)
            and len(chunk) == 2
            and chunk[0] == 1
            and isinstance(chunk[1], str)
        ):
            stream += chunk[1]
    bindings = set()
    records = []
    for line in stream.splitlines():
        ident, sep, value = line.partition(":")
        if not sep:
            continue
        if value.startswith("I"):
            try:
                item = json.loads(value[1:])
            except ValueError:
                continue
            if isinstance(item, list) and item[-1:] == ["ClanRosterStats"]:
                bindings.add("$L" + ident)
        else:
            try:
                records.append(json.loads(value))
            except (ValueError, RecursionError):
                continue
    groups = []

    def visit(value, depth=0):
        if depth > 100:
            raise ClanUnavailable("Слишком сложная структура Protocol.")
        if isinstance(value, list):
            if (
                len(value) == 4
                and value[0] == "$"
                and isinstance(value[1], str)
                and value[1] in bindings
            ):
                if not isinstance(value[3], dict) or not isinstance(
                    value[3].get("rows"), list
                ):
                    raise ClanUnavailable("Не найдена таблица статистики состава.")
                groups.append(value[3]["rows"])
                return
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item, depth + 1)

    for record in records:
        visit(record)
    if (
        len(groups) != 1
        or not 0 < len(groups[0]) <= 2000
        or len(groups[0]) != next(iter(counts))
    ):
        raise ClanUnavailable("Получена неполная или неоднозначная статистика состава.")
    players = {}
    for row in groups[0]:
        if not isinstance(row, dict) or not re.fullmatch(
            r"7656\d{13}", str(row.get("steamId", ""))
        ):
            raise ClanUnavailable("Не подтверждён Steam ID игрока.")
        sid = row["steamId"]
        if sid in players:
            raise ClanUnavailable("Повторяющийся Steam ID в таблице Protocol.")
        hours = bounded_number(row.get("hours"), 10_000_000)
        seed = bounded_number(row.get("seedH"), 10_000_000)
        kills = bounded_number(row.get("kills"), 1_000_000_000)
        deaths = bounded_number(row.get("deaths"), 1_000_000_000)
        if type(kills) is not int or type(deaths) is not int:
            raise ClanUnavailable("Убийства и смерти должны быть целыми числами.")
        # No conversion of Steam-total playtime, clan totals or rounded K/D.
        players[sid] = {
            "steam_id": sid,
            "scope": "clan_roster",
            "verified": True,
            "minutes": int(Decimal(str(hours)) * 60),
            "hours": hours,
            "seed_hours": seed,
            "kills": kills,
            "deaths": deaths,
            "kd": kills / deaths if deaths else kills,
            "source": URL,
            "name": str(row.get("name") or sid)[:100],
        }
    return {"schema": 1, "clan": CLAN, "source": URL, "players": players}


def safe_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "protocol-squad.ru"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise ClanUnavailable("Protocol перенаправил запрос на другой сайт.")
    return value


def request_page(client, url):
    for _ in range(5):
        safe_url(url)
        with client.stream("GET", url) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = safe_url(urljoin(url, response.headers.get("location", "")))
                if urlsplit(url).path.startswith(("/login", "/api/auth/steam")):
                    raise ClanAuthRequired(
                        "Сессия Protocol истекла. Выполните protocol_login.py."
                    )
                continue
            if response.status_code in (401, 403):
                raise ClanAuthRequired("Требуется повторный вход в Protocol.")
            if response.status_code != 200:
                raise ClanUnavailable(
                    "Protocol не ответил: HTTP " + str(response.status_code)
                )
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise ClanUnavailable("Ответ Protocol слишком большой.")
            body = raw.decode("utf-8")
            if "NEXT_REDIRECT" in body and "/login?" in body:
                raise ClanAuthRequired(
                    "Сессия Protocol истекла. Выполните protocol_login.py."
                )
            return body
    raise ClanUnavailable("Слишком много перенаправлений Protocol.")


def make_client():
    jar = MozillaCookieJar(str(cookie_path()))
    try:
        jar.load(ignore_discard=True)
    except (OSError, ValueError) as exc:
        raise ClanAuthRequired(
            "Выполните protocol_login.py на сервере для входа в Protocol."
        ) from exc
    if not any(
        c.domain.lstrip(".") == "protocol-squad.ru" and c.secure and not c.is_expired()
        for c in jar
    ):
        raise ClanAuthRequired(
            "Нет действующей сессии Protocol. Выполните protocol_login.py."
        )
    return httpx.Client(
        cookies=jar,
        timeout=25,
        follow_redirects=False,
        headers={"User-Agent": "StrikeTeam9-ClanStats/1.0"},
    )


def fetch_clan():
    with make_client() as client:
        return parse_clan(request_page(client, URL))


def collect_clan(fetch=fetch_clan, now=None):
    moment = now or utcnow()
    with SessionLocal() as db:
        from automation_control import source_enabled

        if not source_enabled(db, "protocol"):
            return False
        old = db.get(ProtocolClanStats, 1)
        if old and timedelta(0) <= moment - old.attempted_at < timedelta(hours=1):
            return old.status == "ready"
    document, error, status = None, None, "ready"
    try:
        document = fetch()
        if (
            document.get("schema") != 1
            or document.get("clan") != CLAN
            or not document.get("players")
        ):
            raise ClanUnavailable("Неполная статистика состава.")
    except ClanAuthRequired as exc:
        document = None
        error, status = str(exc), "auth_required"
    except (ClanUnavailable, httpx.HTTPError, OSError, UnicodeError):
        document = None
        error, status = (
            "Protocol временно недоступен; предыдущие данные сохранены.",
            "unavailable",
        )
    with SessionLocal.begin() as db:
        row = db.get(ProtocolClanStats, 1)
        if row is None:
            row = ProtocolClanStats(id=1)
            db.add(row)
        row.status, row.attempted_at, row.last_error = status, moment, error
        from automation_control import queue_alert, resolve_alerts

        if document is not None:
            row.payload = json.dumps(document, ensure_ascii=False, allow_nan=False)
            row.checked_at = moment
            resolve_alerts(db, "protocol_clan_stats")
        else:
            queue_alert(db, "protocol_clan_stats", "Protocol", error)
    return document is not None


def clan_profile(db, steam_id, now=None, allow_stale=False):
    row = db.get(ProtocolClanStats, 1) if steam_id else None
    if not row or not row.payload or not row.checked_at:
        return None
    moment = now or utcnow()
    fresh = timedelta(0) <= moment - row.checked_at <= FRESH_FOR
    if not fresh and not allow_stale:
        return None
    try:
        document = json.loads(row.payload)
        player = document["players"].get(steam_id)
        if (
            not player
            or player.get("steam_id") != steam_id
            or not player.get("verified")
        ):
            return None
        return {
            **player,
            "checked_at": row.checked_at.isoformat() + "Z",
            "stale": not fresh,
        }
    except (KeyError, ValueError, TypeError):
        return None


def clan_status(db):
    row = db.get(ProtocolClanStats, 1)
    if not row:
        return {
            "status": "not_configured",
            "players": 0,
            "checked_at": None,
            "error": "Для загрузки статистики выполните protocol_login.py на сервере.",
        }
    try:
        count = len(json.loads(row.payload)["players"]) if row.payload else 0
    except (ValueError, KeyError, TypeError):
        count = 0
    return {
        "status": row.status,
        "players": count,
        "checked_at": row.checked_at.isoformat() + "Z" if row.checked_at else None,
        "error": row.last_error,
    }
