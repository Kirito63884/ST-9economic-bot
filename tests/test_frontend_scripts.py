"""Check actual rendered JavaScript, including templates with hostile names."""

import shutil
import subprocess
from html.parser import HTMLParser

import pytest


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and not dict(attrs).get("src"):
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.blocks.append("".join(self.current))
            self.current = None


def test_rendered_scripts_parse(make_user, login_as):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to check browser JavaScript syntax")
    uid = make_user(
        is_admin=True,
        is_senior_admin=True,
        is_chief_admin=True,
        display_name="O'Brien <script>alert(1)</script>",
    )
    client = login_as(uid)
    routes = [
        "/",
        "/profile",
        "/wallet",
        "/statistics",
        "/admin",
        "/admin/logs",
        "/games",
        "/games/poker/lobby",
        "/games/poker/history",
        "/games/blackjack",
        "/games/roulette",
        "/games/crash",
        "/games/durak/lobby",
        "/games/billiards/lobby",
        "/games/domino/lobby",
        "/medals",
        f"/medals/progress/{uid}",
        "/squad",
        "/appeals",
        "/rule-tests",
        "/logout",
        "/auth/link",
    ]
    count = 0
    for route in routes:
        response = client.get(route)
        assert response.status_code == 200, (route, response.text)
        parser = Scripts()
        parser.feed(response.text)
        for block in parser.blocks:
            result = subprocess.run(
                [node, "--check"], input=block, text=True, capture_output=True
            )
            assert result.returncode == 0, (route, result.stderr)
            count += 1
    assert count > 50
