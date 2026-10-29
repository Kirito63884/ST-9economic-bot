"""Durable per-player Squad sessions observed through BattleMetrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import json

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint, text

from database import Base, SessionLocal

SESSION_GAP = timedelta(minutes=5)
HISTORY_REFRESH = timedelta(hours=24)
HISTORY_RETRY = timedelta(minutes=15)
HISTORY_MAX_PAGES = 3


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SquadPlaySession(Base):
    __tablename__ = "squad_play_sessions"
    __table_args__ = (UniqueConstraint("steam_id", "server_id", "started_at"),)

    id = Column(Integer, primary_key=True)
    steam_id = Column(String(20), nullable=False, index=True)
    server_id = Column(String(32), nullable=False, index=True)
    server_name = Column(String(200), nullable=False)
    started_at = Column(DateTime, nullable=False, index=True)
    last_seen_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=True, index=True)
    sample_count = Column(Integer, nullable=False, default=1)


class BattleMetricsHistorySync(Base):
    """Import status for one linked Steam account.

    The BattleMetrics token remains inside the worker.  The web application only
    reads this status and the already validated local session rows.
    """

    __tablename__ = "battlemetrics_history_sync"

    steam_id = Column(String(20), primary_key=True)
    player_id = Column(String(20), nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    last_checked_at = Column(DateTime, nullable=True)
    next_attempt_at = Column(DateTime, nullable=False)
    imported_sessions = Column(Integer, nullable=False, default=0)
    last_error = Column(String(80), nullable=True)


class SquadPresence(Base):
    """Latest complete per-player/server online check; contains no credentials."""

    __tablename__ = "squad_player_presence"
    steam_id = Column(String(20), primary_key=True)
    payload = Column(Text, nullable=True)
    checked_at = Column(DateTime, nullable=True)
    last_error = Column(String(80), nullable=True)


def queue_history_refresh(db, steam_id, moment=None):
    from squad_monitor import steam_id as valid_steam
    from automation_control import source_enabled

    moment = moment or utcnow()
    if not valid_steam(steam_id):
        return {"queued": False, "reason": "no_steam"}
    if not source_enabled(db, "battlemetrics"):
        return {"queued": False, "reason": "disabled"}
    row = db.get(BattleMetricsHistorySync, steam_id)
    if row and (
        row.status == "queued"
        or (row.status == "syncing" and row.next_attempt_at > moment)
    ):
        return {"queued": True, "reason": "already_queued"}
    if (
        row
        and row.last_checked_at
        and timedelta(0) <= moment - row.last_checked_at < timedelta(seconds=30)
    ):
        return {"queued": False, "reason": "fresh"}
    if row is None:
        row = BattleMetricsHistorySync(steam_id=steam_id, imported_sessions=0)
        db.add(row)
    row.status, row.next_attempt_at = "queued", moment
    db.flush()
    return {"queued": True}


def observe_player_presence(db, steam_id, servers, moment):
    """Only the queried player's rows can be closed by a private online check."""
    from squad_monitor import SERVER_IDS

    live = [s for s in servers if s["online"]]
    rows = db.query(SquadPlaySession).filter_by(steam_id=steam_id, ended_at=None).all()
    by_server = {row.server_id: row for row in rows}
    for item in live:
        row = by_server.get(item["server_id"])
        if row is None:
            row = SquadPlaySession(
                steam_id=steam_id,
                server_id=item["server_id"],
                server_name=item["server_name"],
                started_at=moment,
                last_seen_at=moment,
                sample_count=1,
            )
            db.add(row)
        else:
            row.last_seen_at, row.server_name = moment, item["server_name"]
    offline_ids = {item["server_id"] for item in servers if item["online"] is False}
    for row in rows:
        if row.server_id in SERVER_IDS and row.server_id in offline_ids:
            row.ended_at = row.last_seen_at
    cached = db.get(SquadPresence, steam_id)
    if cached is None:
        cached = SquadPresence(steam_id=steam_id)
        db.add(cached)
    cached.payload, cached.checked_at, cached.last_error = (
        json.dumps(servers),
        moment,
        None,
    )


def refresh_player_presence(client, steam_id, player_id, names=None):
    from squad_monitor import SERVER_IDS, SourceError

    servers = []
    try:
        for sid in SERVER_IDS:
            try:
                document = client.player_server(player_id, sid)
            except SourceError as exc:
                if exc.code != "not_found":
                    raise
                servers.append(
                    {
                        "server_id": sid,
                        "server_name": (names or {}).get(sid, "Сервер " + sid),
                        "online": False,
                    }
                )
                continue
            data = document.get("data") if isinstance(document, dict) else None
            attr = data.get("attributes") if isinstance(data, dict) else None
            if not isinstance(attr, dict) or type(attr.get("online")) is not bool:
                raise SourceError("presence_unavailable")
            servers.append(
                {
                    "server_id": sid,
                    "server_name": (names or {}).get(sid, "Сервер " + sid),
                    "online": attr["online"],
                }
            )
        with SessionLocal.begin() as db:
            observe_player_presence(db, steam_id, servers, utcnow())
        return None
    except SourceError as exc:
        with SessionLocal.begin() as db:
            row = db.get(SquadPresence, steam_id)
            if row is None:
                row = SquadPresence(steam_id=steam_id)
                db.add(row)
            row.last_error = exc.code
        return exc.code


_NOT_LOADED = object()


def presence_for(
    db,
    steam_id,
    servers=None,
    moment=None,
    *,
    cached_presence=_NOT_LOADED,
    recent_session=_NOT_LOADED,
):
    """Fresh BattleMetrics is primary; SQSTAT is a positive-only reserve."""
    from squad_routes import load_snapshot
    from squad_monitor import SERVER_IDS

    moment = moment or utcnow()
    if not steam_id:
        return {
            "online": None,
            "presence_status": "unknown",
            "current": None,
            "presence_checked_at": None,
            "presence_error": "no_steam",
        }
    stamp = moment.replace(tzinfo=timezone.utc).timestamp()
    servers = load_snapshot(stamp) if servers is None else servers
    observations = []
    live = []
    complete = {s.get("id") for s in servers} == set(SERVER_IDS)
    stamps = []
    for server in servers:
        fresh = (
            not server.get("stale")
            and server.get("status") in ("ready", "offline")
            and type(server.get("updated_at")) in (int, float)
            and 0 <= stamp - server["updated_at"] <= 90
        )
        if not fresh:
            complete = False
            continue
        stamps.append(server["updated_at"])
        if server.get("status") == "ready":
            if server.get("pending") or any(
                len(p.get("steam_ids", [])) != 1 for p in server.get("roster", [])
            ):
                complete = False
            for player in server.get("roster", []):
                if steam_id in player.get("steam_ids", []):
                    live.append(
                        {
                            "server_id": server["id"],
                            "server_name": server["name"],
                            "observed_at": server["updated_at"],
                            "source": "BattleMetrics",
                        }
                    )
    if live:
        item = max(live, key=lambda item: item["observed_at"])
        observations.append((item["observed_at"], True, item))
    elif complete and stamps:
        observations.append((min(stamps), False, None))
    cached = (
        db.get(SquadPresence, steam_id)
        if cached_presence is _NOT_LOADED
        else cached_presence
    )
    if (
        cached
        and cached.payload
        and cached.checked_at
        and timedelta(0) <= moment - cached.checked_at <= timedelta(seconds=90)
    ):
        try:
            value = json.loads(cached.payload)
            items = [s for s in value if s.get("online") is True]
            if items or {s.get("server_id") for s in value} == set(SERVER_IDS):
                observations.append(
                    (
                        cached.checked_at.replace(tzinfo=timezone.utc).timestamp(),
                        bool(items),
                        {**items[0], "source": "BattleMetrics"} if items else None,
                    )
                )
        except (ValueError, TypeError, AttributeError):
            pass
    if not observations and steam_id:
        row = (
            recent_session
            if recent_session is not _NOT_LOADED
            else db.query(SquadPlaySession)
            .filter(
                SquadPlaySession.steam_id == steam_id,
                SquadPlaySession.ended_at.is_(None),
                SquadPlaySession.last_seen_at >= moment - timedelta(seconds=90),
                SquadPlaySession.last_seen_at <= moment,
            )
            .order_by(SquadPlaySession.last_seen_at.desc())
            .first()
        )
        if row:
            observations.append(
                (
                    row.last_seen_at.replace(tzinfo=timezone.utc).timestamp(),
                    True,
                    {
                        "server_id": row.server_id,
                        "server_name": row.server_name,
                        "source": "BattleMetrics",
                    },
                )
            )
    if not observations:
        from sqstat_online import live_presence

        sqstat = live_presence(steam_id, stamp)
        if sqstat:
            observations.append((sqstat["observed_at"], True, sqstat))
    if not observations:
        return {
            "online": None,
            "presence_status": "unknown",
            "current": None,
            "presence_checked_at": None,
            "presence_error": cached.last_error if cached else None,
        }
    checked, online, current = max(observations, key=lambda item: item[0])
    return {
        "online": online,
        "presence_status": "online" if online else "offline",
        "current": current,
        "presence_source": (current or {}).get("source", "BattleMetrics"),
        "presence_checked_at": datetime.fromtimestamp(checked, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "presence_error": cached.last_error if cached else None,
    }


def profile_history(db, steam_id, servers=None, moment=None):
    moment = moment or utcnow()
    rows = sessions_for(db, steam_id) if steam_id else []
    presence = presence_for(db, steam_id, servers, moment)
    current = presence["current"]
    active = set()
    if current:
        row = next(
            (
                r
                for r in rows
                if r.server_id == current["server_id"] and r.ended_at is None
            ),
            None,
        )
        if row:
            active.add(row.id)
        current = {
            **current,
            "started_at": row.started_at.isoformat() + "Z" if row else None,
            "last_seen_at": presence["presence_checked_at"],
        }

    def end_value(row):
        return (
            None
            if row.id in active
            else (row.ended_at or row.last_seen_at).isoformat() + "Z"
        )

    sync = history_status(db, steam_id)
    from automation_control import source_enabled

    reserve = (
        not rows or sync["status"] != "ready" or not source_enabled(db, "battlemetrics")
    )
    if sync.get("checked_at"):
        reserve = (
            reserve
            or moment - datetime.fromisoformat(sync["checked_at"].replace("Z", ""))
            > HISTORY_REFRESH
        )
    return {
        **presence,
        "current": current,
        "steam_id": steam_id,
        "source": (current or {}).get("source", "BattleMetrics"),
        "sync": sync,
        "history_source": "SQSTAT" if reserve else "BattleMetrics",
        "matches": sqstat_games_for(db, steam_id)
        if reserve and source_enabled(db, "sqstat")
        else [],
        "history_started": rows[-1].started_at.isoformat() + "Z" if rows else None,
        "sessions": [
            {
                "server_id": row.server_id,
                "server_name": row.server_name,
                "started_at": row.started_at.isoformat() + "Z",
                "ended_at": end_value(row),
                "last_seen_at": row.last_seen_at.isoformat() + "Z",
                "active": row.id in active,
                "duration_seconds": duration_seconds(
                    row, moment if row.id in active else row.last_seen_at
                ),
            }
            for row in rows
        ],
    }


def sqstat_games_for(db, steam_id, limit=100):
    """Read cached participation; match bounds are not player connection times."""
    from squad_monitor import steam_id as valid_steam
    from medals import ProtocolMatch, ProtocolProfile

    if not valid_steam(steam_id):
        return []
    result = {}
    profile = db.get(ProtocolProfile, steam_id)
    if profile:
        try:
            p = json.loads(profile.payload)
            if (
                p.get("verified")
                and p.get("steam_id") == steam_id
                and p.get("season") == "all"
            ):
                for item in p.get("games", []):
                    ident = item.get("match_id")
                    if type(ident) is not int or ident <= 0:
                        continue
                    result[ident] = {
                        "match_id": ident,
                        "layer": str(item.get("layer") or "")[:200],
                        "result": item.get("result")
                        if item.get("result") in ("win", "loss")
                        else "unknown",
                        "started_at": None,
                        "ended_at": None,
                        "server_name": None,
                        "source": "SQSTAT",
                    }
        except (ValueError, TypeError, AttributeError):
            pass
    records = (
        db.query(ProtocolMatch)
        .filter(ProtocolMatch.payload.contains('"' + steam_id + '"'))
        .order_by(ProtocolMatch.ended_at.desc())
        .limit(limit)
        .all()
    )
    for row in records:
        try:
            doc = json.loads(row.payload)
            if (
                not doc.get("verified")
                or doc.get("id") != row.match_id
                or steam_id not in doc.get("players", {})
            ):
                continue
            ident = row.match_id
            old = result.get(ident, {})
            win = doc["winner"]
            outcome = old.get("result", "unknown")
            if win in (1, 2):
                team = next(
                    (
                        i
                        for i, t in enumerate(doc.get("teams", []), 1)
                        if any(
                            p.get("steam_id") == steam_id for p in t.get("roster", [])
                        )
                    ),
                    None,
                )
                if team is not None:
                    outcome = "win" if team == win else "loss"
            start = doc.get("started_at")
            start = datetime.fromisoformat(start).isoformat() + "Z" if start else None
            result[ident] = {
                "match_id": ident,
                "layer": str(doc.get("layer") or old.get("layer") or "")[:200],
                "result": outcome,
                "started_at": start,
                "ended_at": row.ended_at.isoformat() + "Z",
                "server_name": str(doc.get("server") or "")[:200],
                "source": "SQSTAT",
            }
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return [
        {**g, "url": "https://prot.sqstat.ru/game/" + str(g["match_id"])}
        for g in sorted(result.values(), key=lambda g: g["match_id"], reverse=True)[
            :limit
        ]
    ]


def _steam_ids(server):
    result = set()
    for player in server.get("roster", []):
        for value in player.get("steam_ids", []):
            sid = str(value)
            if sid.isdigit() and len(sid) == 17:
                result.add(sid)
    return result


def observe_sessions(servers, moment=None):
    """Record complete ready snapshots; source errors never create false exits."""
    moment = moment or utcnow()
    ready = {
        str(row.get("id")): row
        for row in servers
        if isinstance(row, dict) and row.get("status") in {"ready", "offline"}
    }
    seen = {}
    for server_id, server in ready.items():
        if server.get("status") != "ready":
            continue
        name = str(server.get("name") or ("Сервер " + server_id))[:200]
        for sid in _steam_ids(server):
            seen[sid] = (server_id, name)

    with SessionLocal.begin() as db:
        open_rows = (
            db.query(SquadPlaySession).filter(SquadPlaySession.ended_at.is_(None)).all()
        )
        by_steam = {row.steam_id: row for row in open_rows}
        for sid, (server_id, server_name) in seen.items():
            row = by_steam.get(sid)
            if (
                row
                and row.server_id == server_id
                and moment - row.last_seen_at <= SESSION_GAP
            ):
                row.last_seen_at = moment
                row.server_name = server_name
                row.sample_count = (row.sample_count or 0) + 1
                continue
            if row:
                row.ended_at = row.last_seen_at
            db.add(
                SquadPlaySession(
                    steam_id=sid,
                    server_id=server_id,
                    server_name=server_name,
                    started_at=moment,
                    last_seen_at=moment,
                    sample_count=1,
                )
            )

        complete_ids = {
            sid
            for sid, server in ready.items()
            if server.get("status") == "offline"
            or (
                not server.get("pending")
                and all(
                    len(p.get("steam_ids", [])) == 1 for p in server.get("roster", [])
                )
            )
        }
        for row in open_rows:
            if row.steam_id in seen or row.server_id not in complete_ids:
                continue
            # A ready/offline server has a complete roster. Keep the last observed
            # moment as the conservative end time instead of inventing play time.
            row.ended_at = row.last_seen_at


def sessions_for(db, steam_id, limit=100):
    return (
        db.query(SquadPlaySession)
        .filter_by(steam_id=str(steam_id))
        .order_by(SquadPlaySession.started_at.desc())
        .limit(limit)
        .all()
    )


def history_status(db, steam_id):
    row = db.get(BattleMetricsHistorySync, str(steam_id)) if steam_id else None
    if row is None:
        return {
            "status": "pending",
            "checked_at": None,
            "imported_sessions": 0,
            "error": None,
        }
    return {
        "status": row.status,
        "checked_at": row.last_checked_at.isoformat() + "Z"
        if row.last_checked_at
        else None,
        "imported_sessions": row.imported_sessions or 0,
        "error": row.last_error,
    }


def _claim_history_sync(moment):
    """Claim one active linked account without holding a DB lock during HTTP."""
    from database import User

    with SessionLocal.begin() as db:
        if db.bind.dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        steam_ids = [
            str(value)
            for (value,) in db.query(User.steam_id)
            .filter(User.deleted_at.is_(None), User.steam_id.is_not(None))
            .order_by(User.id)
            .all()
            if isinstance(value, str) and value.isdigit() and len(value) == 17
        ]
        states = (
            {
                row.steam_id: row
                for row in db.query(BattleMetricsHistorySync)
                .filter(BattleMetricsHistorySync.steam_id.in_(steam_ids))
                .all()
            }
            if steam_ids
            else {}
        )
        steam_ids.sort(
            key=lambda sid: (
                0 if states.get(sid) and states[sid].status == "queued" else 1,
                states[sid].last_checked_at
                if states.get(sid) and states[sid].last_checked_at
                else datetime.min,
                sid,
            )
        )
        for sid in steam_ids:
            row = states.get(sid)
            if row is not None and row.next_attempt_at > moment:
                continue
            priority = bool(row and row.status == "queued")
            if row is None:
                row = BattleMetricsHistorySync(
                    steam_id=sid,
                    status="syncing",
                    next_attempt_at=moment + timedelta(minutes=5),
                    imported_sessions=0,
                )
                db.add(row)
            else:
                row.status = "syncing"
                row.next_attempt_at = moment + timedelta(minutes=5)
            return {"steam_id": sid, "player_id": row.player_id, "priority": priority}
    return None


def _finish_history_sync(job, moment, player_id=None, imported=0, error=None, retry=0):
    with SessionLocal.begin() as db:
        row = db.get(BattleMetricsHistorySync, job["steam_id"])
        if row is None:
            return
        row.last_checked_at = moment
        if player_id:
            row.player_id = str(player_id)
        if error:
            row.status = "error"
            row.last_error = str(error)[:80]
            row.next_attempt_at = (
                moment + timedelta(seconds=max(60, retry))
                if retry
                else moment + HISTORY_RETRY
            )
        else:
            row.status = "ready"
            row.last_error = None
            row.imported_sessions = max(row.imported_sessions or 0, int(imported))
            row.next_attempt_at = moment + HISTORY_REFRESH


def _merge_historical_sessions(steam_id, sessions):
    """Merge validated API sessions into the live history without duplicates."""
    with SessionLocal.begin() as db:
        for item in sessions:
            row = (
                db.query(SquadPlaySession)
                .filter_by(
                    steam_id=steam_id,
                    server_id=item["server_id"],
                    started_at=item["started_at"],
                )
                .first()
            )
            if row is None and item["ended_at"] is None:
                row = (
                    db.query(SquadPlaySession)
                    .filter(
                        SquadPlaySession.steam_id == steam_id,
                        SquadPlaySession.server_id == item["server_id"],
                        SquadPlaySession.ended_at.is_(None),
                        SquadPlaySession.started_at >= item["started_at"],
                        SquadPlaySession.last_seen_at
                        >= utcnow() - timedelta(seconds=90),
                    )
                    .order_by(SquadPlaySession.started_at.desc())
                    .first()
                )
                if row:
                    row.started_at = item["started_at"]
            if row is None:
                db.add(
                    SquadPlaySession(
                        steam_id=steam_id,
                        server_id=item["server_id"],
                        server_name=item["server_name"],
                        started_at=item["started_at"],
                        last_seen_at=item["ended_at"] or item["started_at"],
                        ended_at=item["ended_at"],
                        sample_count=1,
                    )
                )
                continue
            row.server_name = item["server_name"]
            if item["ended_at"] and (
                row.ended_at is None or item["ended_at"] > row.ended_at
            ):
                row.ended_at = item["ended_at"]
                row.last_seen_at = item["ended_at"]
        return db.query(SquadPlaySession).filter_by(steam_id=steam_id).count()


def sync_one_history(client, server_names=None, moment=None, known_players=None):
    """Import up to three recent BattleMetrics history pages for one user."""
    moment = moment or utcnow()
    if not hasattr(client, "match_player") or not hasattr(client, "session_page"):
        return False
    job = _claim_history_sync(moment)
    if not job:
        return False
    try:
        from squad_monitor import parse_matched_player, parse_session_page, SourceError

        player_id = job.get("player_id") or (known_players or {}).get(job["steam_id"])
        if not player_id:
            player_id = parse_matched_player(
                client.match_player(job["steam_id"]), job["steam_id"]
            )
        if job.get("priority") and hasattr(client, "player_server"):
            refresh_player_presence(client, job["steam_id"], player_id, server_names)
        sessions = []
        next_url = None
        for _ in range(HISTORY_MAX_PAGES):
            document = client.session_page(player_id, next_url)
            page, next_url = parse_session_page(document, player_id, server_names or {})
            sessions.extend(page)
            if not next_url:
                break
        imported = _merge_historical_sessions(job["steam_id"], sessions)
        _finish_history_sync(job, utcnow(), player_id=player_id, imported=imported)
        return True
    except SourceError as exc:
        _finish_history_sync(
            job,
            utcnow(),
            player_id=locals().get("player_id") or job.get("player_id"),
            error=exc.code,
            retry=max(0, int(exc.retry or 0)),
        )
        return False
    except Exception:
        _finish_history_sync(
            job, moment, player_id=job.get("player_id"), error="history_unavailable"
        )
        return False


def duration_seconds(row, now=None):
    end = row.ended_at or now or row.last_seen_at or utcnow()
    return max(0, int((end - row.started_at).total_seconds()))
