"""Exercise two real backgammon sessions against an isolated local fixture server.

The runner needs Playwright and Chromium; --app-python needs requirements-dev.txt.
Every account, cookie and wallet is synthetic. Dotenv is disabled, background
integrations are not started, and the browser blocks external requests.
No production services, accounts, database or GitHub credentials are used.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SECRET = "synthetic-backgammon-browser-only"


def serve_fixture(database_path, port):
    from contextlib import asynccontextmanager

    path = database_path.resolve()
    if (
        not path.parent.name.startswith("st9-bg-browser-")
        or path.name != "fixture.sqlite3"
        or path.exists()
    ):
        raise SystemExit(
            "Use only a new temporary fixture database created by this runner."
        )
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        DATABASE_URL="sqlite:///" + str(path),
        SECRET_KEY=FIXTURE_SECRET,
        REQUEST_GUARD_ENABLED="false",
    )
    for key in (
        "REQUIRED_GUILD_ID",
        "REQUIRED_ROLE_ID",
        "DISCORD_BOT_TOKEN",
        "ADMIN_DISCORD_ID",
        "SITE_URL",
        "DISCORD_REDIRECT_URI",
    ):
        os.environ.pop(key, None)
    sys.path.insert(0, str(ROOT))
    import main
    from database import SessionLocal, User, AppRuntime

    @asynccontextmanager
    async def quiet(app):
        yield

    main.app.router.lifespan_context = quiet
    with SessionLocal.begin() as db:
        for uid, name, balance in (
            (201, "Белый игрок", 100),
            (202, "Игрок <img src=x onerror=alert(1)>", 50),
            (203, "Проверяющий", 0),
        ):
            db.add(
                User(
                    id=uid,
                    discord_id=str(uid),
                    username=name,
                    balance=balance,
                    is_admin=uid == 203,
                    is_chief_admin=uid == 203,
                )
            )
        main.restore_runtime("{}")
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    import uvicorn

    uvicorn.run(
        main.app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )


def run_checks(base, app_python, chromium, screenshot_dir, bootstrap_css):
    from playwright.sync_api import sync_playwright

    source = """import base64,json
from itsdangerous import TimestampSigner
print(json.dumps({str(uid):TimestampSigner('synthetic-backgammon-browser-only').sign(base64.b64encode(json.dumps({'user_id':uid,'csrf_token':'synthetic-bg-csrf'}).encode())).decode() for uid in (201,202,203)}))
"""
    cookies = json.loads(subprocess.check_output([app_python, "-c", source], text=True))
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium, headless=True, args=["--no-sandbox"]
        )

        def context(uid, width, touch):
            ctx = browser.new_context(
                viewport={"width": width, "height": 1050},
                has_touch=touch,
                locale="ru-RU",
            )
            ctx.add_cookies(
                [
                    {
                        "name": "session",
                        "value": cookies[str(uid)],
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            )

            def block_external(route):
                if route.request.url.startswith(base):
                    route.continue_()
                elif (
                    bootstrap_css
                    and "bootstrap" in route.request.url
                    and route.request.url.endswith(".css")
                ):
                    route.fulfill(path=str(bootstrap_css), content_type="text/css")
                else:
                    route.abort()

            ctx.route("**/*", block_external)
            page = ctx.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            return ctx, page

        def read(page):
            return page.evaluate(
                "async()=>await (await fetch('/api/backgammon/table/'+BACKGAMMON_TABLE_ID+'/state')).json()"
            )

        def shot(page, name):
            if screenshot_dir:
                page.screenshot(path=str(screenshot_dir / name), full_page=True)

        balances = {201: 100, 202: 50}
        for variant, width, touch in (("long", 1440, False), ("short", 390, True)):
            c1, a = context(201, width, touch)
            c2, b = context(202, width, touch)
            pages = {201: a, 202: b}
            a.goto(base + "/games")
            assert (
                a.locator('a.st-game-card[href="/games/backgammon/lobby"]').count() == 1
            )
            a.goto(base + "/games/backgammon/lobby")
            a.locator("#variant").select_option(variant)
            a.locator("#entry").fill("2.50")
            a.locator("#private").select_option("true")
            a.locator("#create").click()
            a.wait_for_url("**/games/backgammon/table/**")
            a.locator("#game").wait_for(state="visible")
            b.goto(a.url)
            b.locator("#joinButton").click()
            b.locator("#game").wait_for(state="visible")
            for page in (a, b):
                page.reload()
                page.locator("#game").wait_for(state="visible")
                page.wait_for_function(
                    "document.querySelectorAll('.bg-player').length === 2"
                )
                page.locator("#ready").click()
                page.wait_for_function(
                    "document.querySelector('#ready').textContent.includes('Отменить')"
                )
            a.wait_for_function("!document.querySelector('#start').disabled")
            a.locator("#start").click()
            a.wait_for_function("document.querySelector('#waiting').hidden")
            b.wait_for_function("document.querySelector('#waiting').hidden")
            assert (
                read(a)["balance"] == balances[201] - 2.5
                and read(b)["balance"] == balances[202] - 2.5
            )
            for _ in range(6):
                active = pages[read(a)["actor"]]
                active.bring_to_front()
                active.wait_for_timeout(2600)
                data = read(active)
                if not data["round"]["dice"]:
                    active.locator("#mobileRoll" if touch else "#roll").click()
                    active.wait_for_function("document.querySelector('#roll').hidden")
                    data = read(active)
                    if data["actor"] != (201 if active == a else 202):
                        continue
                moves = data["round"]["legal"]
                assert moves
                source, target, die = moves[0]
                first = active.locator(
                    "#bar" if source == -1 else f'[data-point="{source}"]'
                )
                first.scroll_into_view_if_needed()
                first.tap() if touch else first.click()
                active.wait_for_function(
                    "document.querySelector('.bg-point.selected') !== null || document.querySelector('#bar').getAttribute('aria-pressed') === 'true'"
                )
                # Polling must preserve selection and the actual button node.
                active.evaluate(
                    "window.savedPoint = document.querySelector('.bg-point.selected')"
                )
                active.wait_for_timeout(2750)
                assert active.evaluate(
                    "window.savedPoint === document.querySelector('.bg-point.selected')"
                )
                destination = active.locator(
                    "#off" if target == 24 else f'[data-point="{target}"]'
                )
                if target != 24:
                    assert "target" in destination.get_attribute("class")
                destination.tap() if touch else destination.click()
                if len([m for m in moves if m[0] == source and m[1] == target]) > 1:
                    active.locator("#choices button").first.click()
                active.wait_for_function(
                    "!document.querySelector('.bg-point.selected') && document.querySelector('#bar').getAttribute('aria-pressed') !== 'true'"
                )
                after = read(active)
                assert after["revision"] == data["revision"] + 1
                assert len(after["round"]["history"]) > 0
                if touch:
                    assert active.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1"
                    )
            for page in (a, b):
                page.reload()
                page.locator("#game").wait_for(state="visible")
                assert page.locator(".bg-point").count() == 24
                assert page.locator("img[src=x]").count() == 0
            shot(a, f"backgammon-{variant}.png")
            if touch:
                a.set_viewport_size({"width": 844, "height": 390})
                assert a.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth + 1"
                )
                shot(a, "backgammon-landscape.png")
                a.set_viewport_size({"width": 390, "height": 1050})
            a.locator("#resign").click()
            a.wait_for_function("!document.querySelector('#rematch').hidden")
            assert (
                read(a)["balance"] == balances[201] - 2.5
                and read(b)["balance"] == balances[202] + 2.5
            )
            a.reload()
            a.locator("#rematch").wait_for(state="visible")
            assert read(a)["match"]["settled"]
            a.locator("#rematch").click()
            a.locator("#ready").wait_for(state="visible")
            assert not any(player["ready"] for player in read(a)["players"])
            a.locator(".st-delete-table").click()
            a.locator("[data-confirm]").click()
            a.wait_for_function(
                "document.querySelector('#error').textContent.includes('закрыт')"
            )
            c1.close()
            c2.close()
            print(
                f"PASS {variant}: real two-session create/join, equal stakes, dice/moves, stable polling, reload, resignation, single payout, rematch, close; touch={touch}",
                flush=True,
            )
            balances[201] -= 2.5
            balances[202] += 2.5
        assert not errors, errors
        browser.close()
        print("PASS zero browser JavaScript errors", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-python", default=sys.executable)
    parser.add_argument(
        "--chromium",
        default=shutil.which("chromium") or shutil.which("chromium-browser"),
    )
    parser.add_argument("--screenshot-dir", type=Path)
    parser.add_argument("--bootstrap-css", type=Path)
    parser.add_argument("--serve-fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--database", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_fixture:
        if not args.database or not 1 <= args.port <= 65535:
            parser.error("Fixture database and local port are required")
        serve_fixture(args.database, args.port)
        return
    if not args.chromium:
        parser.error("Specify an installed Chromium with --chromium")
    if args.bootstrap_css and not args.bootstrap_css.is_file():
        parser.error("The specified Bootstrap CSS file does not exist")
    if args.screenshot_dir:
        args.screenshot_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="st9-bg-browser-") as directory:
        stage = Path(directory)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with (stage / "server.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    args.app_python,
                    str(Path(__file__).resolve()),
                    "--serve-fixture",
                    "--database",
                    str(stage / "fixture.sqlite3"),
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                # Health requests are explicitly loopback; they do not use an HTTP proxy.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(60):
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(
                            "Fixture server exited: " + log.read()[-4000:]
                        )
                    try:
                        with opener.open(base + "/", timeout=0.5) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.2)
                else:
                    raise RuntimeError("Local fixture server did not become ready")
                run_checks(
                    base,
                    args.app_python,
                    args.chromium,
                    args.screenshot_dir,
                    args.bootstrap_css,
                )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
