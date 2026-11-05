"""Published Protocol tabs must refer to the same completed week."""

from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import pytest

from protocol_week_source import (
    BASE,
    URL,
    LABELS,
    SECTIONS,
    PublicWeekUnavailable,
    discover_commanders_url,
    fetch_previous,
)

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
GENERAL = set(LABELS) - {"cmd", "sl"}


def page(codes, link="", period="25.10 — 01.11"):
    titles = {code: title for title, code in SECTIONS.items()}
    sections = "".join(
        f"<section><h2>{titles[code]}</h2>Пока нет данных за неделю</section>"
        for code in sorted(codes)
    )
    return f"<p>Итог завершённой недели ({period})</p><p>Награды выданы в воскресенье в 23:00 МСК</p>{link}{sections}"


class Response(BytesIO):
    def __init__(self, url, body):
        super().__init__(body.encode())
        self.url = url

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, general, commanders=""):
        self.general, self.commanders = general, commanders
        self.urls = []

    def open(self, request, timeout):
        url = request.full_url
        self.urls.append(url)
        return Response(url, self.general if url == URL else self.commanders)


def test_complete_page_needs_no_commander_link():
    opener = Opener(page(set(LABELS)))
    result = fetch_previous(opener, NOW)
    assert set(result["categories"]) == set(LABELS)
    assert opener.urls == [URL]


@pytest.mark.parametrize(
    "href",
    [
        "?tab=commanders",
        "/week?tab=commanders&w=cur",
        BASE + "/week?tab=commanders&w=prev",
    ],
)
def test_published_tab_keeps_previous_week_and_merges_categories(href):
    opener = Opener(
        page(GENERAL, f'<a href="{href}">ЦМД недели</a>'), page({"cmd", "sl"})
    )
    result = fetch_previous(opener, NOW)
    assert len(opener.urls) == 2
    assert parse_qs(urlsplit(opener.urls[1]).query) == {
        "tab": ["commanders"],
        "w": ["prev"],
    }
    assert set(result["categories"]) == set(LABELS)
    assert result["week"] == "2026-10-26"
    assert result["tab_sources"]["commanders"] == opener.urls[1]


@pytest.mark.parametrize(
    "href",
    [
        "//other.example/week",
        "https://other.example/week",
        "/player/123",
        "javascript:alert(1)",
    ],
)
def test_commander_link_must_be_protocol_week_page(href):
    with pytest.raises(PublicWeekUnavailable):
        discover_commanders_url(f'<a href="{href}">ЦМД недели</a>')


def test_current_week_cannot_replace_previous_week_standings():
    opener = Opener(
        page(GENERAL, '<a href="?view=cmd">ЦМД недели</a>'),
        page({"cmd", "sl"}, period="01.11 — 08.11"),
    )
    with pytest.raises(PublicWeekUnavailable):
        fetch_previous(opener, NOW)


def test_missing_commander_tab_fails_without_returning_partial_document():
    with pytest.raises(PublicWeekUnavailable):
        fetch_previous(Opener(page(GENERAL)), NOW)
