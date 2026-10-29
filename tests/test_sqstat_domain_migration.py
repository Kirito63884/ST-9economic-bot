"""Canonical SQSTAT requests, challenge cookies, pacing and archive continuity."""

import json
from datetime import datetime
from urllib.request import Request

import pytest

from database import SessionLocal
from medals import MedalWeek, closed_sqstat_weeks
from medals_monitor import weekly
from medals_source import BASE, SQSTAT, SameOrigin, SourceUnavailable


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.delays = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds


class Page:
    def __init__(self, url, body):
        self.url, self.body = url, body.encode()

    def geturl(self):
        return self.url

    def read(self, size):
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Opener:
    def __init__(self, clock, bodies):
        self.clock, self.bodies = clock, iter(bodies)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.full_url, self.clock.now))
        return Page(request.full_url, next(self.bodies))


def make_client(tmp_path, bodies):
    clock = Clock()
    opener = Opener(clock, bodies)
    client = SQSTAT(
        opener=opener,
        clock=clock.time,
        sleep=clock.sleep,
        rate_limit_path=tmp_path / "sqstat.lock",
    )
    return client, opener, clock


def test_requests_use_canonical_origin_and_preserve_sixty_second_limit(tmp_path):
    body = "<html><title>SQSTAT</title></html>"
    client, opener, clock = make_client(tmp_path, [body, body])
    assert client.get("/") == body
    assert client.get("/game/123") == body
    assert [url for url, _ in opener.requests] == [
        "https://prot.sqstat.ru/",
        "https://prot.sqstat.ru/game/123",
    ]
    assert opener.requests[1][1] - opener.requests[0][1] == 60
    assert clock.delays == [60]


def test_session_challenge_cookie_belongs_to_new_host_and_retry_is_metered(tmp_path):
    challenge = "<script>document.cookie = 'PHPSESID=" + "a" * 32 + "';</script>"
    body = "<html>SQSTAT</html>"
    client, opener, clock = make_client(tmp_path, [challenge, body])
    assert client.get("/") == body
    cookies = list(client.jar)
    assert len(cookies) == 1 and cookies[0].domain == "prot.sqstat.ru"
    assert cookies[0].name == "PHPSESID" and cookies[0].secure
    request = Request(BASE + "/")
    client.jar.add_cookie_header(request)
    assert request.get_header("Cookie") == "PHPSESID=" + "a" * 32
    assert clock.delays == [60] and len(opener.requests) == 2


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/",
        "http://prot.sqstat.ru/",
        "https://prot.sqstat.ru/login",
        "https://prot.p.sqstat.ru/",
    ],
)
def test_unexpected_redirects_are_still_rejected(target):
    with pytest.raises(SourceUnavailable, match="Unexpected SQSTAT redirect"):
        SameOrigin().redirect_request(
            Request(BASE + "/"), None, 301, "Moved", {}, target
        )


def test_response_from_other_origin_is_rejected(tmp_path):
    client, opener, clock = make_client(tmp_path, [])
    opener.open = lambda request, timeout: Page(
        "https://evil.example/", "<html>SQSTAT</html>"
    )
    with pytest.raises(SourceUnavailable, match="Unexpected SQSTAT origin"):
        client.get("/")


def test_new_weekly_aggregates_use_canonical_source():
    assert weekly([], "2026-10-19")["source"] == "https://prot.sqstat.ru/"


@pytest.mark.parametrize(
    "source,accepted",
    [
        ("https://prot.p.sqstat.ru/", True),
        ("https://prot.sqstat.ru/", True),
        ("https://prot.sqstat.ru.evil.example/", False),
        ("http://prot.sqstat.ru/", False),
    ],
)
def test_new_and_legacy_verified_archives_are_accepted_only_from_exact_hosts(
    source, accepted
):
    with SessionLocal.begin() as db:
        db.query(MedalWeek).delete()
        db.add(
            MedalWeek(
                week="2026-10-19",
                finalized_at=datetime(2026, 10, 26),
                payload=json.dumps(
                    {
                        "week": "2026-10-19",
                        "complete": True,
                        "match_count": 1,
                        "ranks": {},
                        "source": source,
                    }
                ),
            )
        )
    try:
        with SessionLocal() as db:
            rows = closed_sqstat_weeks(db, datetime(2026, 10, 29))
            assert bool(rows) is accepted
    finally:
        with SessionLocal.begin() as db:
            db.query(MedalWeek).delete()
