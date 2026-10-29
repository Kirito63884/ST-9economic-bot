"""Read-only SQSTAT parser. No remote scripts are evaluated."""

import html
import fcntl
import os
from pathlib import Path
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar, Cookie
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (
    build_opener,
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
)
from medals import MOSCOW

BASE = "https://prot.sqstat.ru"
LIMIT = 8_000_000
REQUEST_INTERVAL = 60
RATE_LIMIT_PATH = Path(__file__).resolve().parent / "squad-data" / "sqstat-request.lock"


class SourceUnavailable(ValueError):
    pass


class BrokenMatchPage(SourceUnavailable):
    """Confirmed upstream PHP failure, explicitly excluded by roster policy."""


def check_broken_match(body):
    plain = html.unescape(re.sub(r"<[^>]*>", " ", body))
    if (
        "Fatal error" in plain
        and "fetch_assoc() on bool" in plain
        and "/engine/page/game.php" in plain
    ):
        raise BrokenMatchPage("SQSTAT PHP fetch_assoc failure; match excluded")


class SameOrigin(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Automatic redirects would create unmetered additional requests.
        raise SourceUnavailable("Unexpected SQSTAT redirect")


class RequestGate:
    """One shared request at a time, with 60 seconds after every response/error."""

    def __init__(self, path=RATE_LIMIT_PATH, clock=time.time, sleep=time.sleep):
        self.path = Path(path)
        self.clock = clock
        self.sleep = sleep
        self.cooldown_until = 0

    def defer(self, seconds):
        self.cooldown_until = max(
            self.cooldown_until, self.clock() + max(REQUEST_INTERVAL, float(seconds))
        )

    @contextmanager
    def request(self, wait=True):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
                except BlockingIOError:
                    raise SourceUnavailable("SQSTAT request slot busy") from None
                handle.seek(0)
                raw = handle.read().strip()
                if raw:
                    try:
                        last = float(raw)
                    except ValueError:
                        raise SourceUnavailable(
                            "Invalid SQSTAT rate limit state"
                        ) from None
                    if not 0 <= last < float("inf"):
                        raise SourceUnavailable("Invalid SQSTAT rate limit state")
                    delay = max(0, last + REQUEST_INTERVAL - self.clock())
                else:
                    delay = 0
                if not delay:
                    break
                fcntl.flock(handle, fcntl.LOCK_UN)
                if not wait:
                    raise SourceUnavailable("SQSTAT request slot busy")
                self.sleep(delay)
            try:
                yield
            finally:
                handle.seek(0)
                handle.truncate()
                handle.write(
                    str(max(self.clock(), self.cooldown_until - REQUEST_INTERVAL))
                )
                handle.flush()
                self.cooldown_until = 0
                fcntl.flock(handle, fcntl.LOCK_UN)


def defer_http_error(gate, error):
    if error.code not in (429, 503):
        return
    delay = 300.0
    value = error.headers.get("Retry-After", "") if error.headers else ""
    if value:
        try:
            delay = float(value)
            if not 0 <= delay < float("inf"):
                raise ValueError("invalid delay")
        except (TypeError, ValueError):
            try:
                delay = max(0, parsedate_to_datetime(value).timestamp() - gate.clock())
            except (TypeError, ValueError, OverflowError):
                delay = 300.0
    gate.defer(max(REQUEST_INTERVAL, min(86400, delay)))


class SQSTAT:
    def __init__(
        self,
        opener=None,
        clock=time.time,
        sleep=time.sleep,
        rate_limit_path=RATE_LIMIT_PATH,
    ):
        self.jar = CookieJar()
        self.opener = opener or build_opener(
            SameOrigin(), HTTPCookieProcessor(self.jar)
        )
        self.gate = RequestGate(rate_limit_path, clock, sleep)
        self.profile_cache = {}

    def get(self, path):
        if not re.fullmatch(
            r"/(?:|game/[0-9]+|player/7656[0-9]{13}\?season=all)", path
        ):
            raise SourceUnavailable("Unsupported SQSTAT path")
        for attempt in range(2):
            try:
                with self.gate.request():
                    try:
                        with self.opener.open(
                            Request(
                                BASE + path,
                                headers={"User-Agent": "StrikeTeam9-Medals/1.0"},
                            ),
                            timeout=25,
                        ) as response:
                            if not response.geturl().startswith(BASE + "/"):
                                raise SourceUnavailable("Unexpected SQSTAT origin")
                            raw = response.read(LIMIT + 1)
                    except HTTPError as exc:
                        defer_http_error(self.gate, exc)
                        raise
                if len(raw) > LIMIT:
                    raise SourceUnavailable("SQSTAT response too large")
                body = raw.decode("utf-8")
            except (OSError, TimeoutError, UnicodeError) as exc:
                if attempt == 0:
                    continue
                raise SourceUnavailable("SQSTAT unavailable") from exc
            cookie = re.search(r"document.cookie\s*=\s*'PHPSESID=([a-f0-9]{32})", body)
            if cookie and len(body) < 1000:
                self.jar.set_cookie(
                    Cookie(
                        0,
                        "PHPSESID",
                        cookie[1],
                        None,
                        False,
                        urlsplit(BASE).hostname,
                        True,
                        False,
                        "/",
                        True,
                        True,
                        None,
                        True,
                        None,
                        None,
                        {},
                    )
                )
                continue
            if re.fullmatch(r"/game/[0-9]+", path):
                check_broken_match(body)
            # Some match templates have a Russian title but no SQSTAT branding.
            game_page = bool(
                re.fullmatch(r"/game/[0-9]+", path)
                and re.search(
                    r"<title\b[^>]*>\s*Игра\s+на\s+[^<]+</title\s*>", body, re.I
                )
            )
            if "<html" not in body.lower() or not ("SQSTAT" in body or game_page):
                if attempt == 0:
                    continue
                raise SourceUnavailable("Invalid SQSTAT page")
            return body
        raise SourceUnavailable("SQSTAT session initialization failed")

    def player(self, sid):
        if sid not in self.profile_cache:
            self.profile_cache[sid] = self.get("/player/" + sid + "?season=all")
        return self.profile_cache[sid]


def text(value):
    return html.unescape(re.sub("<[^>]+>", "", value)).strip()[:200]


def identity(value):
    if re.fullmatch(r"7656[0-9]{13}", value):
        return value
    # Some public events record Steam account ID32; use its canonical ID64.
    if value.isdigit() and 0 < int(value) < 2**32:
        return str(76561197960265728 + int(value))
    raise SourceUnavailable("Invalid SQSTAT player identity")


def home(body):
    games = {}
    for match in re.finditer(r"/game/(\d+)'", body):
        section = body[max(0, match.start() - 2800) : match.start()]
        scores = re.findall(
            r'<span class="label label-(success|danger)">(\d+)</span>', section
        )
        if len(scores) < 2:
            continue
        a, b = scores[-2:]
        if a[0] == b[0]:
            winner = None
        else:
            winner = 1 if a[0] == "success" else 2
        games[int(match[1])] = winner
    if not games:
        raise SourceUnavailable("SQSTAT match list missing")
    return games


def profile(body, sid):
    options = re.findall(r"""<option\b(?:[^>"']|"[^"]*"|'[^']*')*>""", body, re.S)
    if (
        not any(
            'value="all"' in option and re.search(r"\bselected(?:\s|=|>)", option)
            for option in options
        )
        or f"/player/{sid}?season=all" not in body
    ):
        raise SourceUnavailable("SQSTAT all-time profile not confirmed")
    chart = re.search(
        r'labels:\s*\["Убийств",\s*"Смертей"\][\s\S]{0,450}?data:\s*\[(\d+),(\d+)\]',
        body,
    )
    hours = re.search(r'<h3 class="text-center">(\d+)ч\s*(\d+)м</h3>', body)
    if not chart or not hours or int(hours[2]) >= 60:
        raise SourceUnavailable("SQSTAT profile statistics missing")
    from distinctions_source import extend_profile

    return {
        "steam_id": sid,
        "season": "all",
        "verified": True,
        "minutes": int(hours[1]) * 60 + int(hours[2]),
        "kills": int(chart[1]),
        "deaths": int(chart[2]),
        "source": BASE + "/player/" + sid + "?season=all",
        "distinctions": extend_profile(body),
        "games": player_games(body),
    }


def player_games(body):
    """Membership comes from the player's match table, never from a live roster."""
    games = {}
    for row in re.findall(r"<tr\b.*?</tr>", body, re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)
        links = re.findall(r"href=[\"\']/game/([0-9]+)[\"\']", row)
        if len(links) != 1:
            continue
        ident = int(links[0])
        if ident <= 0:
            continue
        cell = next((c for c in cells if "/game/" + str(ident) in c), None)
        if cell is None:
            continue
        layer = text(cell)
        if not layer:
            continue
        result = (
            "win"
            if "label-success" in row and re.search(r">\s*Да\s*</span>", row)
            else "loss"
            if "label-danger" in row and re.search(r">\s*Нет\s*</span>", row)
            else "unknown"
        )
        games[ident] = {"match_id": ident, "layer": layer, "result": result}
    return sorted(games.values(), key=lambda g: g["match_id"], reverse=True)[:500]


def match(body, ident, winner=None):
    check_broken_match(body)
    dates = re.search(
        r"(\d\d:\d\d \d\d\.\d\d\.\d{4})\s*-\s*(\d\d:\d\d \d\d\.\d\d\.\d{4})", body
    )
    if not dates:
        if re.search(r"<title\b[^>]*>\s*Игра\s+на\s+[^<]+</title\s*>", body, re.I):
            raise SourceUnavailable(
                "SQSTAT match page has no completed-match statistics"
            )
        if 'id="game_server"' not in body:
            return None
        raise SourceUnavailable("SQSTAT match dates missing")
    end = datetime.strptime(dates[2], "%H:%M %d.%m.%Y").replace(tzinfo=MOSCOW)
    srv = re.search(r'id="game_server"[^>]*>(.*?)</h3>', body, re.S)
    players = {}
    teams = []
    for team in (1, 2):
        head = re.search(
            r'<p[^>]*>(-?\d+)</p>\s*<p id="team_t'
            + str(team)
            + r'-name"[^>]*>(.*?)</p>',
            body,
            re.S,
        )
        if not head:
            raise SourceUnavailable("SQSTAT team missing")
        table = re.search(r"<table\b.*?</table>", body[head.end() :], re.S)
        if not table:
            raise SourceUnavailable("SQSTAT player table missing")
        roster = []
        for row in re.findall(r"<tr\b.*?</tr>", table[0], re.S):
            sid = re.search(r'data-type="steam_id"[^>]*>(\d+)</span>', row)
            if not sid:
                continue
            sid = identity(sid[1])
            name = re.search(r'data-type="name"[^>]*>(.*?)</b>', row, re.S)
            roster.append(
                {
                    "steam_id": sid,
                    "name": text(name[1]),
                    "cmd": bool(re.search(r'<img[^>]*alt="CMD"', row)),
                    "values": {
                        typ: int(
                            re.search(r'data-type="' + typ + r'"[^>]*>(\d+)</td>', row)[
                                1
                            ]
                        )
                        for typ in ("kills", "revive", "damage", "vehicle")
                    },
                }
            )
            players.setdefault(
                sid,
                {
                    "name": text(name[1]),
                    "kills": 0,
                    "revive": 0,
                    "damage": 0,
                    "vehicle": 0,
                    "wins": 0,
                    "cmd": 0,
                },
            )
        teams.append({"tickets": int(head[1]), "roster": roster})
    if winner is None:
        if teams[0]["tickets"] <= 0 < teams[1]["tickets"]:
            winner = 2
        elif teams[1]["tickets"] <= 0 < teams[0]["tickets"]:
            winner = 1
        elif teams[0]["tickets"] <= 0 and teams[1]["tickets"] <= 0:
            winner = 0
    spans = {
        "kills": ("game_kills-block", "game_damage-block"),
        "damage": ("game_damage-block", "game_revive-block"),
        "revive": ("game_revive-block", "game_vehicle-block"),
        "vehicle": ("game_vehicle-block", "playerDetail"),
    }
    labels = {
        "kills": "Убийства",
        "damage": "Урон",
        "revive": "Поднятий",
        "vehicle": "Взорвано",
    }
    for typ, (start, stop) in spans.items():
        i = body.find(f'id="{start}"')
        if i < 0:
            raise SourceUnavailable("SQSTAT match event section missing: " + typ)
        j = body.find(f'id="{stop}"', i + len(start))
        seg = body[i:j] if j != -1 else body[i:]
        counter = Counter()
        for sid, event in re.findall(
            r'<div class="panel[^>]*data-steamid="(\d+)"[^>]*>(.*?)</div>', seg, re.S
        ):
            if typ == "kills" and "fa fa-fw fa-gun" not in event:
                continue
            if typ == "vehicle" and "fa-explosion text-danger" not in event:
                continue
            sid = identity(sid)
            nm = re.search(r'href="/player/\d+"[^>]*>(.*?)</a>', event, re.S)
            if not nm:
                raise SourceUnavailable("SQSTAT event identity missing")
            amount = (
                re.search(r'fa-explosion text-danger"></i>\s*(\d+)', event)
                if typ == "damage"
                else None
            )
            if typ == "damage" and not amount:
                raise SourceUnavailable("SQSTAT damage value missing")
            counter[sid] += int(amount[1]) if amount else 1
            players.setdefault(
                sid,
                {
                    "name": text(nm[1]),
                    "kills": 0,
                    "revive": 0,
                    "damage": 0,
                    "vehicle": 0,
                    "wins": 0,
                    "cmd": 0,
                },
            )
        stated = re.search(
            ">" + labels[typ] + r"</h4>\s*<h3[^>]*>(\d+)</h3>", body, re.S
        )
        if not stated or sum(counter.values()) != int(stated[1]):
            raise SourceUnavailable("SQSTAT event total mismatch: " + typ)
        for sid, v in counter.items():
            players[sid][typ] = v
        for t in teams:
            for p in t["roster"]:
                if counter[p["steam_id"]] != p["values"][typ]:
                    raise SourceUnavailable("SQSTAT player total mismatch: " + typ)
    if winner:
        for p in teams[winner - 1]["roster"]:
            players[p["steam_id"]]["wins"] = 1
            if p["cmd"]:
                players[p["steam_id"]]["cmd"] = 1
    started = datetime.strptime(dates[1], "%H:%M %d.%m.%Y").replace(tzinfo=MOSCOW)
    title = re.search(r"<title\b[^>]*>\s*Игра\s+на\s+([^<]+)</title\s*>", body, re.I)
    document = {
        "id": ident,
        "end": end.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
        "started_at": started.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
        "layer": text(title[1]) if title else "",
        "week": (end - timedelta(days=end.weekday())).date().isoformat(),
        "server": text(srv[1]) if srv else "",
        "winner": winner,
        "players": players,
        "teams": teams,
        "verified": True,
    }
    from distinctions_source import extend_match

    document["distinctions"] = extend_match(body, document)
    return document


def profile_result(body, ident):
    for row in re.findall(r"<tr\b.*?</tr>", body, re.S):
        if f'href="/game/{ident}"' not in row:
            continue
        if "label-success" in row and "> Да" in row:
            return True
        if "label-danger" in row and "> Нет" in row:
            return False
    return None


def resolve(client, document):
    if document["winner"] is not None:
        return document
    found = []
    for index, t in enumerate(document["teams"], 1):
        # Short visits tend to have fewer subsequent matches in their history.
        for p in t["roster"][-5:]:
            try:
                result = profile_result(client.player(p["steam_id"]), document["id"])
            except SourceUnavailable:
                continue
            if result is not None:
                found.append(index if result else 3 - index)
                break
    if not found or len(set(found)) != 1:
        return document
    document["winner"] = found[0]
    for p in document["teams"][found[0] - 1]["roster"]:
        document["players"][p["steam_id"]]["wins"] = 1
        if p["cmd"]:
            document["players"][p["steam_id"]]["cmd"] = 1
    return document
