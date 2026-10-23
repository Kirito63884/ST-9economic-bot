"""Verified Squad names for site members; no API calls or wallet writes."""

import math
import time

import squad_monitor as monitor

NAME_TTL = 30 * 86400
MAX_NAMES = 1500


def tracked_steam_ids():
    from database import SessionLocal, User

    with SessionLocal() as db:
        return {
            sid
            for (sid,) in db.query(User.steam_id)
            .filter(User.deleted_at.is_(None), User.steam_id.is_not(None))
            .all()
            if monitor.steam_id(sid)
        }


def valid_names(document, now, allowed):
    result = {}
    for sid, row in document.items() if isinstance(document, dict) else []:
        if sid not in allowed or not monitor.steam_id(sid) or not isinstance(row, dict):
            continue
        stamp = row.get("seen_at")
        name = monitor.text(row.get("name"), 100).strip()
        if (
            name
            and row.get("server_id") in monitor.SERVER_IDS
            and type(stamp) in (int, float)
            and math.isfinite(stamp)
            and 0 <= now - stamp <= NAME_TTL
        ):
            result[sid] = {
                "name": name,
                "server_id": row["server_id"],
                "seen_at": stamp,
            }
    return result


def remember_names(previous, servers, now, allowed):
    result = valid_names(previous, now, allowed)
    for server in servers:
        stamp = server.get("updated_at")
        if (
            server.get("status") != "ready"
            or server.get("stale") is True
            or server.get("id") not in monitor.SERVER_IDS
            or type(stamp) not in (int, float)
            or not math.isfinite(stamp)
            or not 0 <= now - stamp <= monitor.FRESH_SECONDS
        ):
            continue
        for player in server.get("roster", []):
            ids = player.get("steam_ids", [])
            name = monitor.text(player.get("name"), 100).strip()
            if (
                len(ids) != 1
                or not monitor.steam_id(ids[0])
                or ids[0] not in allowed
                or not name
            ):
                continue
            sid = ids[0]
            if sid not in result or stamp >= result[sid]["seen_at"]:
                result[sid] = {
                    "name": name,
                    "server_id": server["id"],
                    "seen_at": stamp,
                }
    return dict(
        sorted(result.items(), key=lambda item: item[1]["seen_at"], reverse=True)[
            :MAX_NAMES
        ]
    )


def admin_game_names(db, now=None):
    from database import User
    from squad_routes import load_snapshot

    now = time.time() if now is None else now
    users = db.query(User).filter(User.deleted_at.is_(None)).all()
    allowed = {u.steam_id for u in users if monitor.steam_id(u.steam_id)}
    try:
        raw = monitor.read_json(monitor.SNAPSHOT)
        previous = raw.get("known_names", {}) if raw.get("version") == 1 else {}
    except (OSError, ValueError, TypeError):
        previous = {}
    servers = load_snapshot(now)
    names = remember_names(previous, servers, now, allowed)
    live = {
        sid
        for s in servers
        if s["status"] == "ready" and not s["stale"]
        for player in s["roster"]
        if player["name"].strip()
        for sid in player["steam_ids"]
    }
    server_names = {s["id"]: s["name"] for s in servers}
    result = {}
    for u in users:
        known = names.get(u.steam_id)
        if known:
            result[u.id] = {
                **known,
                "status": "online" if u.steam_id in live else "last_seen",
                "server_name": server_names[known["server_id"]],
            }
        else:
            result[u.id] = {
                "name": None,
                "seen_at": None,
                "server_name": None,
                "status": "unknown" if u.steam_id else "no_steam",
            }
    from squad_play_history import presence_for
    from datetime import datetime, timezone

    moment = datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None)
    for user in users:
        if not user.steam_id:
            continue
        presence = presence_for(db, user.steam_id, servers, moment)
        current = presence.get("current") or {}
        if presence["online"] and current.get("source") == "SQSTAT":
            sighting = current
            result[user.id] = {
                "name": sighting.get("name") or result[user.id].get("name"),
                "server_id": sighting["server_id"],
                "server_name": sighting["server_name"],
                "seen_at": sighting["observed_at"],
                "status": "online",
                "source": "SQSTAT",
            }
        elif presence["online"]:
            result[user.id].update(
                status="online",
                source="BattleMetrics",
                server_id=current.get("server_id"),
                server_name=current.get("server_name"),
            )
        elif presence["online"] is False:
            result[user.id]["status"] = (
                "last_seen" if result[user.id].get("name") else "unknown"
            )
    return result
