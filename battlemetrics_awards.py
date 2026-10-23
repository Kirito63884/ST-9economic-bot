"""Verified play time fallback; no inferred kills, kits, wins or seeding hours."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
import time

from sqlalchemy import Column, String, Text, DateTime
from database import Base, SessionLocal, User
from squad_monitor import (
    BattleMetrics,
    SourceError,
    SERVER_IDS,
    TOKEN_FILE,
    IDENTITIES,
    read_json,
    IDENTITY_TTL,
    parse_matched_player,
    steam_id,
)


class BattleMetricsPlaytime(Base):
    __tablename__ = "battlemetrics_award_playtime"
    steam_id = Column(String(20), primary_key=True)
    payload = Column(Text, nullable=True)
    checked_at = Column(DateTime, nullable=True)
    attempted_at = Column(DateTime, nullable=False)
    last_error = Column(String(80), nullable=True)


ERRORS = {
    "authentication": (
        "Токен не принят (HTTP 401)",
        "Обновите BM_SQUAD_TOKEN в защищённом файле .env.squad.",
    ),
    "permissions": (
        "Нет доступа (HTTP 403)",
        "Проверьте права токена и доступ его владельца к идентификаторам и игровому времени.",
    ),
    "rate_limited": (
        "Лимит запросов (HTTP 429)",
        "Сборщик повторит проверку с задержкой; частые ручные повторы не нужны.",
    ),
    "not_found": (
        "Профиль по Steam ID не найден",
        "Проверьте Steam ID и доступ токена к этому игроку. Новая попытка будет выполнена автоматически.",
    ),
    "ambiguous_identity": (
        "Steam ID связан с несколькими профилями",
        "Проверьте связь профилей в BattleMetrics. Система не выбирает игрока по нику.",
    ),
    "playtime_unavailable": (
        "Ответ не содержит проверяемого игрового времени",
        "Проверьте доступ к игровому времени и наличие данных игрока на серверах Protocol.",
    ),
    "no_server_history": (
        "Нет игрового времени на отслеживаемых серверах",
        "Проверьте, играл ли участник на отслеживаемых серверах Protocol.",
    ),
    "source_unavailable": (
        "BattleMetrics не ответил или вернул ошибку",
        "Проверка повторится автоматически; при повторении проверьте доступ сервера к API.",
    ),
    "invalid_response": (
        "Неожиданный формат ответа BattleMetrics",
        "Сохранённые часы не заменены. Для уточнения запустите диагностику.",
    ),
}


def parse_playtime(document, pid, sid):
    player = document.get("data")
    if (
        not isinstance(player, dict)
        or player.get("type") != "player"
        or str(player.get("id")) != pid
    ):
        raise SourceError("invalid_response")
    relationships = player.get("relationships")
    relation = relationships.get("servers") if isinstance(relationships, dict) else None
    if relation is None and isinstance(relationships, dict):
        relation = relationships.get("server")
    servers = relation.get("data") if isinstance(relation, dict) else None
    if not isinstance(servers, list):
        raise SourceError("playtime_unavailable")
    times = {}
    for server in servers:
        if not isinstance(server, dict):
            raise SourceError("invalid_response")
        ident = str(server.get("id", ""))
        if ident not in SERVER_IDS:
            continue
        metadata = server.get("meta")
        value = metadata.get("timePlayed") if isinstance(metadata, dict) else None
        if (
            server.get("type") != "server"
            or ident in times
            or type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= 2_000_000_000
        ):
            raise SourceError("playtime_unavailable")
        # Fractional seconds are rounded down; no unverified time is credited.
        times[ident] = int(value)
    if not times:
        raise SourceError("no_server_history")
    return {
        "steam_id": sid,
        "player_id": pid,
        "source": "BattleMetrics",
        "server_seconds": times,
        "minutes": sum(times.values()) // 60,
        "lower_bound": True,
        "verified": True,
    }


def parse_server_playtime(document):
    """Parse attributes.timePlayed from the requested player/server resource.

    Identity and server scope come from BattleMetrics.player_server's validated
    HTTPS path, not from names or undocumented compound resource IDs.
    """
    data = document.get("data") if isinstance(document, dict) else None
    attributes = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(attributes, dict):
        raise SourceError("invalid_response")
    value = attributes.get("timePlayed")
    if (
        type(value) not in (int, float)
        or not 0 <= value <= 2_000_000_000
        or not math.isfinite(value)
    ):
        raise SourceError("playtime_unavailable")
    return int(value)


def collect_player_playtime(client, pid, steam):
    """Five explicit server requests; unexpected errors retain the prior data."""
    if not re.fullmatch(r"[0-9]{1,20}", str(pid)) or not steam_id(steam):
        raise SourceError("invalid_request")
    times = {}
    for server_id in SERVER_IDS:
        try:
            times[server_id] = parse_server_playtime(
                client.player_server(pid, server_id)
            )
        except SourceError as exc:
            if exc.code == "not_found":
                continue  # No player/server record, not an invented zero-hour record.
            raise
    if not times:
        raise SourceError("no_server_history")
    return {
        "steam_id": steam,
        "player_id": str(pid),
        "source": "BattleMetrics",
        "server_seconds": times,
        "minutes": sum(times.values()) // 60,
        "lower_bound": True,
        "verified": True,
    }


def fresh_profile(profile, now):
    return bool(
        profile
        and profile.checked_at
        and timedelta(0) <= now - profile.checked_at <= timedelta(hours=24)
    )


def cached_mapping(identities):
    mapping = {}
    if not isinstance(identities, dict):
        return mapping
    wall_now = time.time()
    for pid, item in identities.items():
        if not re.fullmatch(r"[0-9]{1,20}", str(pid)) or not isinstance(item, dict):
            continue
        ids, stamp = item.get("steam_ids"), item.get("at")
        if (
            type(stamp) in (float, int)
            and math.isfinite(stamp)
            and 0 <= wall_now - stamp <= IDENTITY_TTL
            and isinstance(ids, list)
            and len(ids) == 1
            and steam_id(ids[0])
        ):
            mapping.setdefault(ids[0], set()).add(str(pid))
    return mapping


def collect_fallback(now=None, client=None, identities=None):
    from medals import ProtocolProfile
    from squad_play_history import BattleMetricsHistorySync
    from dotenv import dotenv_values

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        from automation_control import source_enabled

        if not source_enabled(db, "battlemetrics"):
            return {"attempted": 0, "reason": "disabled"}
    if client is None:
        token = os.getenv("BM_SQUAD_TOKEN") or dotenv_values(TOKEN_FILE).get(
            "BM_SQUAD_TOKEN"
        )
        if not token:
            return {"attempted": 0, "reason": "token_missing"}
        client = BattleMetrics(token)
    if identities is None:
        try:
            identities = read_json(IDENTITIES).get("players", {})
        except (OSError, ValueError):
            identities = {}
    mapping = cached_mapping(identities)
    with SessionLocal() as db:
        targets = []
        for (sid,) in (
            db.query(User.steam_id)
            .filter(User.deleted_at.is_(None), User.steam_id.is_not(None))
            .distinct()
        ):
            if not steam_id(sid):
                continue
            p, b = db.get(ProtocolProfile, sid), db.get(BattleMetricsPlaytime, sid)
            if fresh_profile(p, now):
                continue
            retry_minutes = 5 if b and b.last_error else 60
            if b and timedelta(0) <= now - b.attempted_at < timedelta(
                minutes=retry_minutes
            ):
                continue
            pids = mapping.get(sid, set())
            history = db.get(BattleMetricsHistorySync, sid)
            known = next(iter(pids)) if len(pids) == 1 else None
            if (
                not pids
                and history
                and history.player_id
                and re.fullmatch(r"[0-9]{1,20}", history.player_id)
            ):
                known = history.player_id
            if not pids and not known and b and b.payload:
                try:
                    previous = json.loads(b.payload)
                    if previous.get("steam_id") == sid and re.fullmatch(
                        r"[0-9]{1,20}", str(previous.get("player_id", ""))
                    ):
                        known = str(previous["player_id"])
                except (ValueError, TypeError, AttributeError):
                    pass
            targets.append(
                (b.attempted_at if b else datetime.min, sid, known, len(pids) > 1)
            )
    # New accounts first, then the oldest attempt. Failed early accounts cannot
    # monopolize the fixed-size batch and starve newly registered players.
    targets.sort(key=lambda target: (target[0], target[1]))
    attempted = 0
    for _, sid, pid, ambiguous in targets[:5]:
        result, error = None, None
        try:
            if ambiguous:
                raise SourceError("ambiguous_identity")
            if not pid:
                pid = parse_matched_player(client.match_player(sid), sid)
            result = collect_player_playtime(client, pid, sid)
        except SourceError as exc:
            error = exc.code
        with SessionLocal.begin() as db:
            row = db.get(BattleMetricsPlaytime, sid)
            if row is None:
                row = BattleMetricsPlaytime(steam_id=sid)
                db.add(row)
            row.attempted_at, row.last_error = now, error
            if result:
                row.payload, row.checked_at = json.dumps(result), now
        attempted += 1
        if error in ("authentication", "permissions", "rate_limited"):
            break
    return {"attempted": attempted, "queued": max(0, len(targets) - attempted)}


def playtime(db, sid, now):
    row = db.get(BattleMetricsPlaytime, sid) if sid else None
    if (
        not row
        or not row.payload
        or not row.checked_at
        or not timedelta(0) <= now - row.checked_at <= timedelta(hours=24)
    ):
        return None
    return json.loads(row.payload)


def status(db, now=None):
    from medals import ProtocolProfile
    from automation_control import source_enabled

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    users = {
        u.steam_id: u
        for u in db.query(User)
        .filter(User.deleted_at.is_(None), User.steam_id.is_not(None))
        .all()
        if steam_id(u.steam_id)
    }
    rows = {row.steam_id: row for row in db.query(BattleMetricsPlaytime).all()}
    profiles = (
        {
            p.steam_id: p
            for p in db.query(ProtocolProfile)
            .filter(ProtocolProfile.steam_id.in_(users))
            .all()
        }
        if users
        else {}
    )
    needed = {sid for sid in users if not fresh_profile(profiles.get(sid), now)}
    failures = [rows[sid] for sid in needed if sid in rows and rows[sid].last_error]
    counts = Counter(row.last_error for row in failures)
    stamp = lambda value: value.isoformat() + "Z" if value else None
    return {
        "enabled": source_enabled(db, "battlemetrics"),
        "total": len(users),
        "needed": len(needed),
        "checked": sum(sid in rows for sid in needed),
        "failed": len(failures),
        "ready": sum(bool(playtime(db, sid, now)) for sid in needed),
        "pending": sum(
            (sid not in rows or not rows[sid].last_error) and not playtime(db, sid, now)
            for sid in needed
        ),
        "ignored_errors": sum(
            bool(row.last_error) for sid, row in rows.items() if sid not in needed
        ),
        "errors": [
            {
                "code": code,
                "count": count,
                "message": ERRORS.get(
                    code, ("Ошибка проверки", "Запустите диагностику.")
                )[0],
                "action": ERRORS.get(
                    code, ("Ошибка проверки", "Запустите диагностику.")
                )[1],
            }
            for code, count in sorted(counts.items())
        ],
        "players": [
            {
                "user_id": users[row.steam_id].id,
                "name": users[row.steam_id].display_name
                or users[row.steam_id].username,
                "error": row.last_error,
                "attempted_at": stamp(row.attempted_at),
                "checked_at": stamp(row.checked_at),
            }
            for row in failures[:100]
        ],
    }
