"""Read-only public SQSTAT live API; positive sightings on five Protocol servers."""

import json
import math
import re
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener
from urllib.parse import urlencode

from medals_source import (
    LIMIT,
    RequestGate,
    SameOrigin,
    SourceUnavailable,
    defer_http_error,
)

API_BASE = "https://prot.sqstat.ru/api/server/stat.php"
SERVERS = {
    "10": ("INVASION", "INVASION"),
    "12": ("AAS", "AAS"),
    "5": ("ВС РФ ПРОТИВ ВСУ", "ВС РФ против ВСУ"),
    "11": ("Classic", "Classic"),
    "6": ("Тренировки", "Тренировочный"),
}
SNAPSHOT = Path(__file__).resolve().parent / "squad-data" / "sqstat-online.json"
UPSTREAM_FRESH_SECONDS = 90
FRESH_SECONDS = 390  # Five one-minute slots plus network/scheduling allowance.


def parse_online(document, now, server_id="10"):
    short_name, display_name = SERVERS[server_id]
    if not isinstance(document, dict) or document.get("status") != "ok":
        raise SourceUnavailable("SQSTAT API unavailable")
    data = document.get("data")
    if (
        not isinstance(data, dict)
        or data.get("name") != short_name
        or type(data.get("enabled")) is not bool
    ):
        raise SourceUnavailable("SQSTAT live server not confirmed")
    players = data.get("players")
    if not isinstance(players, list) or len(players) > 200:
        raise SourceUnavailable("Invalid SQSTAT live roster")
    if not data["enabled"] and players:
        raise SourceUnavailable("SQSTAT disabled server has players")
    roster = []
    seen = set()
    for player in players:
        if not isinstance(player, dict):
            raise SourceUnavailable("Invalid SQSTAT live player")
        sid = player.get("steam_id")
        playtime = player.get("playtime")
        if (
            not isinstance(sid, str)
            or not re.fullmatch(r"7656\d{13}", sid)
            or sid in seen
        ):
            raise SourceUnavailable("Invalid SQSTAT live identity")
        if not isinstance(playtime, dict):
            raise SourceUnavailable("Missing SQSTAT observation time")
        last = playtime.get("last_seen")
        if isinstance(last, bool) or not isinstance(last, (int, float, str)):
            raise SourceUnavailable("Invalid SQSTAT observation time")
        try:
            last = float(last) / 1000
        except ValueError:
            raise SourceUnavailable("Invalid SQSTAT observation time") from None
        if not math.isfinite(last) or not -15 <= now - last <= UPSTREAM_FRESH_SECONDS:
            raise SourceUnavailable("SQSTAT live roster is stale")
        name = player.get("name")
        name = name[:200] if isinstance(name, str) else ""
        seen.add(sid)
        roster.append({"steam_id": sid, "name": name, "observed_at": min(now, last)})
    from squad_server_details import sqstat_metadata

    return {
        "version": 1,
        "source": "SQSTAT",
        "server_id": "sqstat:" + server_id,
        "server_name": "Protocol · " + display_name,
        "fetched_at": now,
        "players": roster,
        "enabled": data["enabled"],
        "metadata": sqstat_metadata(data),
    }


def _state(snapshot):
    try:
        if Path(snapshot).stat().st_size > LIMIT:
            raise ValueError("cache too large")
        value = json.loads(Path(snapshot).read_text())
        if value.get("version") == 2 and isinstance(value.get("servers"), dict):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return {"version": 2, "next_index": 0, "servers": {}}


def refresh(opener=None, gate=None, clock=time.time, snapshot=SNAPSHOT):
    """Rotate five servers; busy budget never blocks the BattleMetrics loop."""
    from squad_monitor import atomic_json

    gate = gate or RequestGate()
    try:
        with gate.request(wait=False):
            state = _state(snapshot)
            index = int(state.get("next_index", 0)) % len(SERVERS)
            server_id = list(SERVERS)[index]
            state["next_index"] = (index + 1) % len(SERVERS)
            state["attempted_at"] = clock()
            state["attempted_server"] = server_id
            url = API_BASE + "?" + urlencode({"server": SERVERS[server_id][0]})
            try:
                client = opener or build_opener(SameOrigin())
                try:
                    with client.open(
                        Request(
                            url,
                            headers={
                                "User-Agent": "StrikeTeam9-Online/1.0",
                                "Accept": "application/json",
                            },
                        ),
                        timeout=20,
                    ) as response:
                        if response.geturl() != url:
                            raise SourceUnavailable("Unexpected SQSTAT API redirect")
                        raw = response.read(LIMIT + 1)
                        if len(raw) > LIMIT:
                            raise SourceUnavailable("SQSTAT response too large")
                except HTTPError as exc:
                    defer_http_error(gate, exc)
                    raise
                state["servers"][server_id] = parse_online(
                    json.loads(raw), clock(), server_id
                )
                state["last_error"] = None
            except (OSError, ValueError, TimeoutError):
                # Advance even after a failure so one unavailable server cannot stop the rest.
                state["last_error"] = "source_unavailable"
                atomic_json(snapshot, state)
                raise
            atomic_json(snapshot, state)
        return True
    except SourceUnavailable as exc:
        if str(exc) == "SQSTAT request slot busy":
            return False
        raise


def live_presence(steam_id, now=None, snapshot=SNAPSHOT):
    """Use dated positive sightings; absence/failed requests never mean offline."""
    now = time.time() if now is None else now
    found = []
    for server_id, document in _state(snapshot)["servers"].items():
        if server_id not in SERVERS or not isinstance(document, dict):
            continue
        fetched = document.get("fetched_at")
        if type(fetched) not in (int, float) or not 0 <= now - fetched <= FRESH_SECONDS:
            continue
        players = document.get("players")
        if not isinstance(players, list) or len(players) > 200:
            continue
        for player in players:
            if not isinstance(player, dict) or player.get("steam_id") != steam_id:
                continue
            at = player.get("observed_at")
            if type(at) in (int, float) and 0 <= now - at <= FRESH_SECONDS:
                found.append(
                    {
                        "server_id": "sqstat:" + server_id,
                        "server_name": "Protocol · " + SERVERS[server_id][1],
                        "source": "SQSTAT",
                        "observed_at": at,
                        "name": str(player.get("name") or "")[:200],
                    }
                )
    return max(found, key=lambda item: item["observed_at"]) if found else None


def server_key(name):
    """Match explicit configured server labels, never positional server IDs."""
    value = str(name or "").casefold()
    if "всу" in value and ("вс рф" in value or "vs" in value):
        return "5"
    if "трен" in value or "training" in value:
        return "6"
    if re.search(r"\baas\b", value):
        return "12"
    if "classic" in value or "классик" in value:
        return "11"
    if "invasion" in value:
        return "10"
    return None


def display_servers(servers, now, snapshot=SNAPSHOT):
    """Public display only. Does not replace evidence used for wallet actions."""
    from squad_server_details import same_layer

    documents = _state(snapshot)["servers"]
    result = []
    for original in servers:
        row = {**original, "source": "BattleMetrics", "metadata_sources": {}}
        for field in ("map", "mode", "queue", "factions"):
            if row.get(field) not in (None, "", []):
                row["metadata_sources"][field] = "BattleMetrics"
        key = server_key(row.get("name"))
        doc = documents.get(key, {})
        fetched = doc.get("fetched_at") if isinstance(doc, dict) else None
        fresh = type(fetched) in (int, float) and 0 <= now - fetched <= FRESH_SECONDS
        if not fresh:
            result.append(row)
            continue
        meta = doc.get("metadata", {})
        if not isinstance(meta, dict):
            meta = {}
        primary = not row.get("stale") and row.get("status") in ("ready", "offline")
        if not primary and doc.get("enabled") is True:
            players = doc.get("players", [])
            if isinstance(players, list) and all(
                isinstance(p, dict)
                and type(p.get("observed_at")) in (int, float)
                and 0 <= now - p["observed_at"] <= FRESH_SECONDS
                and re.fullmatch(r"7656\d{13}", str(p.get("steam_id", "")))
                for p in players
            ):
                row.update(
                    status="ready",
                    stale=False,
                    updated_at=fetched,
                    source="SQSTAT",
                    roster=[
                        {
                            "name": str(p.get("name") or "")[:200],
                            "steam_ids": [p["steam_id"]],
                        }
                        for p in players
                    ],
                    total_players=len(players),
                    max_players=0,
                    pending=0,
                    can_grant=False,
                    error=None,
                    map="",
                    mode="",
                    queue=None,
                    factions=[],
                    metadata_sources={},
                )
        if row.get("status") == "offline" and primary:
            result.append(row)
            continue
        for field in ("map", "mode", "queue", "factions"):
            if row.get(field) not in (None, "", []):
                continue
            value = meta.get(field)
            if value in (None, "", []):
                continue
            if (
                field == "factions"
                and row.get("map")
                and not same_layer(row["map"], meta.get("map"))
            ):
                continue
            if (
                field == "mode"
                and row.get("map")
                and not same_layer(row["map"], meta.get("map"))
            ):
                continue
            row[field] = value
            row["metadata_sources"][field] = "SQSTAT"
        row["metadata_checked_at"] = fetched
        result.append(row)
    return result
