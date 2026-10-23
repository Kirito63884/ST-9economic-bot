"""Separate read-only BattleMetrics worker. The web app never calls the API."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SERVER_IDS = ("35575126", "33285501", "34999551", "35223179", "35931471")
PROJECT = Path(__file__).resolve().parent
DATA_DIR = PROJECT / "squad-data"
SNAPSHOT = DATA_DIR / "online.json"
IDENTITIES = DATA_DIR / "identities.json"
TOKEN_FILE = PROJECT / ".env.squad"
INTERVAL = 30
FRESH_SECONDS = 90
IDENTITY_TTL = 86400 * 7
MAX_BYTES = 2_000_000
API = "https://api.battlemetrics.com"
logger = logging.getLogger("strike_team9.squad")


def steam_id(value):
    return isinstance(value, str) and re.fullmatch(r"7656[0-9]{13}", value) is not None


def number(value, default=0):
    return (
        value
        if isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 10000
        else default
    )


def text(value, limit=200):
    return value[:limit] if isinstance(value, str) else ""


def atomic_json(path, document):
    path = Path(path)
    if path.parent.is_symlink():
        raise OSError("Unexpected data directory symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".squad-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            json.dump(
                document,
                handle,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise OSError("Unexpected data symlink")
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Oversized data")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Invalid data")
    return value


class SourceError(Exception):
    def __init__(self, code, retry=0):
        super().__init__(code)
        self.code, self.retry = code, retry


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BattleMetrics:
    def __init__(self, token, opener=None, clock=time.monotonic, sleep=time.sleep):
        self.token = token
        self.opener = opener or build_opener(NoRedirect())
        self.clock, self.sleep = clock, sleep
        self.next_request = 0
        self.blocked_until = 0

    def _request(self, method, path, payload=None, allow_next=False):
        now = self.clock()
        if now < self.blocked_until:
            raise SourceError("rate_limited", self.blocked_until - now)
        self.sleep(max(0, self.next_request - now))
        self.next_request = self.clock() + 0.5
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.api+json",
            "User-Agent": "StrikeTeam9-Squad/1.0",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
                "utf-8"
            )
            headers["Content-Type"] = "application/vnd.api+json"
        request = Request(API + path, data=body, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise SourceError("invalid_response")
                result = json.loads(raw)
                if not isinstance(result, dict) or result.get("errors"):
                    raise SourceError("invalid_response")
                if (
                    not allow_next
                    and isinstance(result.get("links"), dict)
                    and result["links"].get("next")
                ):
                    raise SourceError("incomplete_response")
                return result
        except HTTPError as exc:
            if exc.code == 429:
                try:
                    delay = min(
                        3600, max(30, int(exc.headers.get("Retry-After", "60")))
                    )
                except (ValueError, TypeError):
                    delay = 60
                self.blocked_until = self.clock() + delay
                raise SourceError("rate_limited", delay) from None
            raise SourceError(
                {401: "authentication", 403: "permissions", 404: "not_found"}.get(
                    exc.code, "source_unavailable"
                )
            ) from None
        except (URLError, TimeoutError, OSError):
            raise SourceError("source_unavailable") from None
        except (ValueError, UnicodeError):
            raise SourceError("invalid_response") from None

    def get(self, path):
        if not re.fullmatch(
            r"/(?:servers|players)/[0-9]+\?include=(?:player|identifier|server)", path
        ):
            raise SourceError("invalid_request")
        return self._request("GET", path)

    def match_player(self, steam):
        if not steam_id(steam):
            raise SourceError("invalid_request")
        body = {
            "data": [
                {
                    "type": "identifier",
                    "attributes": {"type": "steamID", "identifier": steam},
                }
            ]
        }
        return self._request("POST", "/players/match", body)

    def player_server(self, pid, server_id):
        """Exact player/server information endpoint, including timePlayed."""
        if (
            not re.fullmatch(r"[0-9]{1,20}", str(pid))
            or str(server_id) not in SERVER_IDS
        ):
            raise SourceError("invalid_request")
        return self._request("GET", f"/players/{pid}/servers/{server_id}")

    def session_page(self, pid, next_url=None):
        if not re.fullmatch(r"[0-9]{1,20}", str(pid)):
            raise SourceError("invalid_request")
        if next_url:
            parsed = urlsplit(next_url)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "api.battlemetrics.com"
                or parsed.path != f"/players/{pid}/relationships/sessions"
            ):
                raise SourceError("invalid_response")
            query = parse_qs(parsed.query, keep_blank_values=True)
            if any(
                key not in {"page[size]", "page[key]", "page[rel]", "include", "sort"}
                for key in query
            ):
                raise SourceError("invalid_response")
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
        else:
            query = urlencode(
                {"page[size]": "100", "include": "server", "sort": "-start"}
            )
            path = f"/players/{pid}/relationships/sessions?" + query
        return self._request("GET", path, allow_next=True)


def parse_server(document, sid, now):
    data = document.get("data")
    if (
        not isinstance(data, dict)
        or data.get("type") != "server"
        or str(data.get("id")) != sid
    ):
        raise SourceError("invalid_response")
    attr = data.get("attributes")
    if not isinstance(attr, dict):
        raise SourceError("invalid_response")
    name = text(attr.get("name")) or "Сервер " + sid
    result = {
        "id": sid,
        "name": name,
        "status": "ready",
        "checked_at": now,
        "updated_at": now,
        "total_players": number(attr.get("players")),
        "max_players": number(attr.get("maxPlayers")),
        "map": text((attr.get("details") or {}).get("map"))
        if isinstance(attr.get("details"), dict)
        else "",
        "source_updated_at": text(attr.get("updatedAt"), 60),
        "roster": [],
        "pending": 0,
        "error": None,
    }
    from squad_server_details import mode_from_layer

    details = attr.get("details") if isinstance(attr.get("details"), dict) else {}
    result.update(
        mode=text(details.get("gameMode")) or mode_from_layer(result["map"]),
        queue=None,
        factions=[],
        source="BattleMetrics",
    )
    if attr.get("status") in ("offline", "dead"):
        result["status"] = "offline"
        return result
    if attr.get("status") != "online":
        raise SourceError("source_unavailable")
    included = document.get("included", [])
    if not isinstance(included, list) or len(included) > 2000:
        raise SourceError("invalid_response")
    seen = set()
    for player in included:
        if not isinstance(player, dict) or player.get("type") != "player":
            continue
        pid = str(player.get("id", ""))
        rel = player.get("relationships", {}).get("server", {}).get("data")
        # The live endpoint attaches exactly the queried server to each player.
        if (
            not re.fullmatch(r"[0-9]{1,20}", pid)
            or not isinstance(rel, dict)
            or rel.get("type") != "server"
            or str(rel.get("id")) != sid
        ):
            raise SourceError("invalid_roster")
        if pid not in seen:
            attributes = player.get("attributes", {})
            result["roster"].append(
                {
                    "id": pid,
                    "name": text(attributes.get("name"))
                    if isinstance(attributes, dict)
                    else "",
                    "steam_ids": [],
                }
            )
            seen.add(pid)
    # A positive population with no included roster must not become an empty list.
    if result["total_players"] > 0 and not result["roster"]:
        raise SourceError("incomplete_response")
    # BattleMetrics can return a complete live player relation while its RCON
    # connection is being renewed. The relation itself is the online source;
    # rejecting it solely by rconDisconnected made the website look offline.
    result["rcon_active"] = (
        attr.get("rconActive") is True and attr.get("rconDisconnected") is None
    )
    return result


def parse_identity(document, pid):
    data = document.get("data")
    if (
        not isinstance(data, dict)
        or data.get("type") != "player"
        or str(data.get("id")) != pid
    ):
        raise SourceError("invalid_response")
    result = set()
    included = document.get("included", [])
    if not isinstance(included, list):
        raise SourceError("invalid_response")
    for row in included:
        if not isinstance(row, dict) or row.get("type") != "identifier":
            continue
        attr = row.get("attributes")
        if not isinstance(attr, dict) or attr.get("type") != "steamID":
            continue
        owner = row.get("relationships", {}).get("player", {}).get("data")
        if (
            not isinstance(owner, dict)
            or owner.get("type") != "player"
            or str(owner.get("id")) != pid
        ):
            raise SourceError("invalid_identity")
        if not steam_id(attr.get("identifier")):
            raise SourceError("invalid_identity")
        result.add(attr["identifier"])
    if not result:
        raise SourceError("steam_unavailable")
    if len(result) != 1:
        raise SourceError("ambiguous_identity")
    return sorted(result)


def parse_matched_player(document, expected_steam):
    """Accept exactly one player returned by the exact Steam identifier match."""
    if not steam_id(expected_steam):
        raise SourceError("invalid_request")
    data = document.get("data")
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    included = document.get("included", [])
    if not isinstance(included, list) or len(rows) > 20 or len(included) > 100:
        raise SourceError("invalid_response")
    candidates = set()
    for row in rows + included:
        if not isinstance(row, dict):
            continue
        if row.get("type") == "player" and re.fullmatch(
            r"[0-9]{1,20}", str(row.get("id", ""))
        ):
            candidates.add(str(row["id"]))
        relation = row.get("relationships", {}).get("player", {}).get("data")
        if (
            isinstance(relation, dict)
            and relation.get("type") == "player"
            and re.fullmatch(r"[0-9]{1,20}", str(relation.get("id", "")))
        ):
            attr = row.get("attributes", {})
            if (
                not isinstance(attr, dict)
                or attr.get("type") != "steamID"
                or attr.get("identifier") != expected_steam
            ):
                continue
            candidates.add(str(relation["id"]))
    if not candidates:
        raise SourceError("not_found")
    if len(candidates) != 1:
        raise SourceError("ambiguous_identity")
    return candidates.pop()


def _iso_moment(value):
    if not isinstance(value, str) or len(value) > 40:
        raise SourceError("invalid_response")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        raise SourceError("invalid_response") from None


def parse_session_page(document, pid, known_names=None):
    data = document.get("data")
    included = document.get("included", [])
    if (
        not isinstance(data, list)
        or len(data) > 100
        or not isinstance(included, list)
        or len(included) > 2000
    ):
        raise SourceError("invalid_response")
    names = {
        str(key): text(value)
        for key, value in (known_names or {}).items()
        if re.fullmatch(r"[0-9]{1,20}", str(key))
    }
    squad_servers = set(SERVER_IDS)
    for row in included:
        if not isinstance(row, dict) or row.get("type") != "server":
            continue
        sid = str(row.get("id", ""))
        attr = row.get("attributes", {})
        game = row.get("relationships", {}).get("game", {}).get("data")
        if (
            isinstance(game, dict)
            and game.get("type") == "game"
            and game.get("id") == "squad"
        ):
            squad_servers.add(sid)
        if sid in squad_servers and isinstance(attr, dict) and text(attr.get("name")):
            names[sid] = text(attr["name"])
    sessions = []
    for row in data:
        if not isinstance(row, dict) or row.get("type") != "session":
            raise SourceError("invalid_response")
        relationships = row.get("relationships", {})
        player = (
            relationships.get("player", {}).get("data")
            if isinstance(relationships, dict)
            else None
        )
        server = (
            relationships.get("server", {}).get("data")
            if isinstance(relationships, dict)
            else None
        )
        if player is not None and (
            not isinstance(player, dict)
            or player.get("type") != "player"
            or str(player.get("id")) != str(pid)
        ):
            raise SourceError("invalid_response")
        sid = (
            str(server.get("id", ""))
            if isinstance(server, dict) and server.get("type") == "server"
            else ""
        )
        if sid not in squad_servers:
            continue
        attr = row.get("attributes", {})
        if not isinstance(attr, dict):
            raise SourceError("invalid_response")
        started = _iso_moment(attr.get("start"))
        ended = _iso_moment(attr.get("stop")) if attr.get("stop") else None
        if ended and ended < started:
            raise SourceError("invalid_response")
        sessions.append(
            {
                "server_id": sid,
                "server_name": names.get(sid, "Сервер " + sid),
                "started_at": started,
                "ended_at": ended,
            }
        )
    links = document.get("links", {})
    next_url = links.get("next") if isinstance(links, dict) else None
    if next_url is not None and (not isinstance(next_url, str) or len(next_url) > 2000):
        raise SourceError("invalid_response")
    return sessions, next_url


class Monitor:
    def __init__(self, client, directory=DATA_DIR, clock=time.time):
        self.client, self.directory, self.clock = client, Path(directory), clock
        self.servers, self.identities, self.retry_at = {}, {}, {}
        self.game_names = {}
        try:
            previous = read_json(self.directory / "online.json")
            if previous.get("version") == 1:
                self.game_names = previous.get("known_names", {})
        except (OSError, ValueError, TypeError):
            pass
        try:
            saved = read_json(self.directory / "identities.json")
            for pid, row in saved.get("players", {}).items():
                if (
                    re.fullmatch(r"[0-9]{1,20}", pid)
                    and isinstance(row, dict)
                    and isinstance(row.get("at"), (int, float))
                    and isinstance(row.get("steam_ids"), list)
                    and len(row["steam_ids"]) == 1
                    and all(steam_id(x) for x in row["steam_ids"])
                ):
                    self.identities[pid] = row
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def cached(self, pid, now):
        row = self.identities.get(pid)
        return row["steam_ids"] if row and 0 <= now - row["at"] <= IDENTITY_TTL else []

    def cycle(self):
        now = self.clock()
        for sid in SERVER_IDS:
            try:
                self.servers[sid] = parse_server(
                    self.client.get("/servers/" + sid + "?include=player"),
                    sid,
                    self.clock(),
                )
            except SourceError as exc:
                previous = self.servers.get(
                    sid,
                    {
                        "id": sid,
                        "name": "Сервер " + sid,
                        "roster": [],
                        "updated_at": None,
                        "pending": 0,
                    },
                )
                self.servers[sid] = {
                    **previous,
                    "status": "error",
                    "checked_at": self.clock(),
                    "error": exc.code,
                }
        missing = []
        for server in self.servers.values():
            if server["status"] != "ready":
                continue
            server["identity_error"] = None
            for player in server["roster"]:
                if (
                    not self.cached(player["id"], now)
                    and player["id"] not in missing
                    and self.retry_at.get(player["id"], 0) <= now
                ):
                    missing.append(player["id"])
        # Bounded warmup. Five roster queries + forty identities per 30 seconds.
        identity_started = time.monotonic()
        for pid in missing[:40]:
            if time.monotonic() - identity_started >= 15:
                break
            try:
                ids = parse_identity(
                    self.client.get("/players/" + pid + "?include=identifier"), pid
                )
                self.identities[pid] = {"at": self.clock(), "steam_ids": ids}
            except SourceError as exc:
                self.retry_at[pid] = self.clock() + (
                    max(30, exc.retry) if exc.code == "rate_limited" else 300
                )
                for server in self.servers.values():
                    if server["status"] == "ready" and any(
                        p["id"] == pid for p in server["roster"]
                    ):
                        server["identity_error"] = exc.code
                if exc.code in ("rate_limited", "authentication", "permissions"):
                    break
        now = self.clock()
        self.identities = {
            pid: row
            for pid, row in self.identities.items()
            if now - row["at"] <= IDENTITY_TTL * 2
        }
        if len(self.identities) > 8000:
            self.identities = dict(
                sorted(
                    self.identities.items(),
                    key=lambda item: item[1]["at"],
                    reverse=True,
                )[:8000]
            )
        self.retry_at = {pid: at for pid, at in self.retry_at.items() if at > now}
        for server in self.servers.values():
            if server["status"] == "ready":
                for player in server["roster"]:
                    player["steam_ids"] = self.cached(player["id"], now)
                server["pending"] = sum(not p["steam_ids"] for p in server["roster"])
        atomic_json(
            self.directory / "identities.json",
            {"version": 1, "players": self.identities},
        )
        # Keep exact evidence of any appearance during the Saturday clan assembly.
        # This is local database work; BattleMetrics credentials and payloads stay private.
        try:
            from assembly_attendance import observe_snapshot

            observe_snapshot(
                [self.servers[sid] for sid in SERVER_IDS],
                datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None),
            )
        except Exception:
            logger.warning(
                "Cannot record assembly attendance snapshot; absence fines will be disabled for incomplete coverage"
            )
        try:
            from squad_play_history import observe_sessions

            observe_sessions(
                [self.servers[sid] for sid in SERVER_IDS],
                datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None),
            )
        except Exception:
            logger.exception(
                "Cannot record Squad play history; live monitoring will continue"
            )
        from squad_names import remember_names, tracked_steam_ids

        try:
            allowed = tracked_steam_ids()
            self.game_names = remember_names(
                self.game_names, self.servers.values(), now, allowed
            )
        except Exception:
            # A temporary DB read failure must not interrupt live monitoring or grants.
            logger.warning("Cannot refresh registered Squad nickname cache; will retry")
        atomic_json(
            self.directory / "online.json",
            {
                "version": 1,
                "written_at": now,
                "servers": [self.servers[sid] for sid in SERVER_IDS],
                "known_names": self.game_names,
            },
        )
        try:
            from squad_play_history import sync_one_history

            names = {
                sid: self.servers[sid].get("name", "Сервер " + sid)
                for sid in SERVER_IDS
            }
            matches = {}
            for pid in self.identities:
                for steam in self.cached(pid, now):
                    matches.setdefault(steam, set()).add(pid)
            known = {
                steam: next(iter(pids))
                for steam, pids in matches.items()
                if len(pids) == 1
            }
            sync_one_history(
                self.client,
                names,
                datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None),
                known,
            )
        except Exception:
            logger.exception(
                "Cannot import BattleMetrics session history; live monitoring will continue"
            )

        try:
            from automation_control import resolve_alerts
            from database import SessionLocal

            with SessionLocal.begin() as db:
                resolve_alerts(db, "battlemetrics_monitor")
        except Exception:
            logger.warning(
                "Cannot resolve the previous BattleMetrics alert; will retry"
            )


def configure(project=PROJECT):
    if not sys.stdin.isatty():
        raise SystemExit("Run the installer in an interactive SSH terminal.")
    token = getpass.getpass(
        "BattleMetrics token (hidden; Enter keeps existing token): "
    ).strip()
    target = Path(project) / ".env.squad"
    if not token:
        if not target.is_file() or target.is_symlink():
            raise SystemExit("No existing token; rerun and enter your token.")
        from dotenv import dotenv_values

        token = dotenv_values(target).get("BM_SQUAD_TOKEN", "")
    if not isinstance(token, str) or not re.fullmatch(
        r"[A-Za-z0-9._~+/=-]{1,4096}", token
    ):
        raise SystemExit("Invalid token format; do not include Bearer.")
    client = BattleMetrics(token)
    sample = None
    for sid in SERVER_IDS:
        try:
            server = parse_server(
                client.get("/servers/" + sid + "?include=player"), sid, time.time()
            )
            if server["roster"] and sample is None:
                sample = server["roster"][0]["id"]
            print("Server " + sid + ": access verified")
        except SourceError as exc:
            raise SystemExit(
                "Server " + sid + ": " + exc.code + "; site files not changed."
            ) from None
    if sample:
        try:
            parse_identity(
                client.get("/players/" + sample + "?include=identifier"), sample
            )
        except SourceError as exc:
            raise SystemExit(
                "Steam identifier access: " + exc.code + "; site files not changed."
            ) from None
    elif not target.exists():
        raise SystemExit(
            "All servers are empty; Steam identifier permissions cannot yet be verified."
        )
    fd, name = tempfile.mkstemp(prefix=".squad-token-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o640)
            handle.write("BM_SQUAD_TOKEN=" + token + "\n")
        import grp

        os.chown(name, 0, grp.getgrnam("www-data").gr_gid)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print("Token saved in protected .env.squad; it will not be exposed to the browser.")


def run():
    from dotenv import dotenv_values

    token = os.getenv("BM_SQUAD_TOKEN") or dotenv_values(TOKEN_FILE).get(
        "BM_SQUAD_TOKEN"
    )
    if not token or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,4096}", token):
        raise SystemExit("Configure the BattleMetrics token first.")
    monitor = Monitor(BattleMetrics(token))
    while True:
        started = time.monotonic()
        try:
            from automation_control import resolve_alerts, source_enabled
            from database import SessionLocal

            with SessionLocal.begin() as db:
                enabled = source_enabled(db, "battlemetrics")
                sqstat_enabled = source_enabled(db, "sqstat")
            if sqstat_enabled:
                try:
                    from sqstat_online import refresh

                    refresh()
                except Exception:
                    logger.warning(
                        "SQSTAT live online unavailable; BattleMetrics monitoring continues"
                    )
            if enabled:
                monitor.cycle()
            else:
                with SessionLocal.begin() as db:
                    resolve_alerts(db, "battlemetrics_monitor")
        except Exception:
            # Never log API responses, request objects, credentials or identities.
            logger.error("Squad monitor could not publish a snapshot; will retry.")
            try:
                from database import SessionLocal
                from automation_control import queue_alert

                with SessionLocal.begin() as db:
                    queue_alert(
                        db,
                        "battlemetrics_monitor",
                        "BattleMetrics",
                        "BattleMetrics не обновил онлайн Protocol; посещение сбора не будет штрафоваться при неполных данных.",
                    )
            except Exception:
                pass
        time.sleep(max(1, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "configure"))
    parser.add_argument("--app-dir", type=Path, default=PROJECT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        configure(args.app_dir) if args.action == "configure" else run()
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(130)
