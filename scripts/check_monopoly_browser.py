"""Exercise two real Monopoly sessions against an isolated local fixture server.

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
FIXTURE_SECRET = "synthetic-monopoly-browser-only"


def serve_fixture(database_path, port):
    from contextlib import asynccontextmanager

    path = database_path.resolve()
    if (
        not path.parent.name.startswith("st9-monopoly-browser-")
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
            (201, "Адмирал Восток", 100),
            (202, "Игрок <img src=x onerror=alert(1)>", 50),
            (203, "Проверяющий", 0),
            (301, "Владелец города", 100),
            (302, "Покупатель", 100),
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
        from monopoly_game import MonopolyTable

        estate = MonopolyTable("fixture-estate", 301, "Владелец города")
        estate.add_player(302, "Покупатель")
        for p in estate.players:
            p["ready"] = True
        estate.start()
        estate.actor = 301
        estate.turn_action = "end"
        for i in (1, 3):
            estate.estate[str(i)]["owner"] = 301
        estate.estate["37"].update(owner=302, mortgaged=True)
        estate.pending = {"type": "buy", "uid": 301, "property": 5}
        estate.validate()
        main.monopoly_service.tables[estate.id] = estate
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    import uvicorn

    uvicorn.run(
        main.app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )


def run_checks(base, app_python, chromium, screenshot_dir, bootstrap_css):
    from playwright.sync_api import sync_playwright

    source = """import base64,json
from itsdangerous import TimestampSigner
print(json.dumps({str(uid):TimestampSigner('synthetic-monopoly-browser-only').sign(base64.b64encode(json.dumps({'user_id':uid,'csrf_token':'fixture-csrf'}).encode())).decode() for uid in (201,202,203,301,302)}))
"""
    cookies = json.loads(subprocess.check_output([app_python, "-c", source], text=True))
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium, headless=True, args=["--no-sandbox"]
        )

        def context(uid, width=1280, touch=False):
            ctx = browser.new_context(
                viewport={"width": width, "height": 1000},
                has_touch=touch,
                locale="ru-RU",
            )

            def route(r):
                if r.request.url.startswith(base):
                    r.continue_()
                elif "bootstrap@5.3.0/dist/css/bootstrap.min.css" in r.request.url:
                    if bootstrap_css:
                        r.fulfill(path=str(bootstrap_css), content_type="text/css")
                    else:
                        r.fulfill(
                            body="body{margin:0;font-family:system-ui,sans-serif}",
                            content_type="text/css",
                        )
                else:
                    r.abort()

            ctx.route("**/*", route)
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
            page = ctx.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            return ctx, page

        def capture(page, name):
            if screenshot_dir:
                page.screenshot(path=str(screenshot_dir / name), full_page=True)

        def wait_text(page, selector, text):
            page.wait_for_function(
                "([selector,text])=>document.querySelector(selector).textContent.includes(text)",
                arg=[selector, text],
            )

        c1, a = context(201)
        a.goto(base + "/games/monopoly/lobby")
        a.locator("#entry").fill("10")
        a.locator("#capacity").select_option("10")
        capture(a, "monopoly-lobby.png")
        a.locator("#create").click()
        a.wait_for_url("**/games/monopoly/table/**")
        a.locator("#game").wait_for(state="visible")
        url = a.url
        c2, b = context(202, 390, True)
        b.goto(url)
        b.locator("#joinButton").tap()
        b.locator("#game").wait_for(state="visible")
        a.wait_for_function(
            "document.querySelectorAll('#players .mono-player').length===2"
        )
        a.locator("#ready").click()
        b.wait_for_function(
            "[...document.querySelectorAll('#players .mono-player')].find(p=>p.textContent.includes('Адмирал')).textContent.includes('Готов')"
        )
        b.locator("#ready").tap()
        a.wait_for_function("!document.querySelector('#start').disabled")
        a.locator("#start").click()
        a.wait_for_function("document.querySelector('#waiting').hidden")
        b.wait_for_function("document.querySelector('#waiting').hidden")
        assert (
            a.locator(".mono-cell").count() == 40
            and b.locator(".mono-cell").count() == 40
        )
        current = a.evaluate(
            "async()=>await(await fetch('/api/monopoly/table/'+location.pathname.split('/').pop()+'/state')).json()"
        )
        active = a if current["actor"] == 201 else b
        active.locator('#centerActions [data-action="roll"]').click()
        active.wait_for_function(
            "document.querySelectorAll('#dice .mono-die').length===2"
        )
        assert a.locator("#players img").count() == 0
        assert "<img" in a.locator("#players").inner_text()
        for width in (320, 390, 768, 1280):
            b.set_viewport_size({"width": width, "height": 1000})
            b.locator("#cell-37").tap()
            wait_text(b, "#deed", "Парк")
            assert b.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
            box = b.locator("#board").bounding_box()
            assert abs(box["height"] - box["width"]) <= 2
            assert box["width"] <= width
        b.set_viewport_size({"width": 390, "height": 1000})
        capture(a, "monopoly-desktop.png")
        capture(b, "monopoly-phone.png")
        b.locator("#leave").tap()
        b.wait_for_url("**/games/monopoly/lobby")
        wait_text(a, "#turn", "Победитель")
        assert "20 БК" in a.locator("#pot").inner_text()
        print(
            "PASS create/ready/stake/start, hostile names, 40 cells at 320–1280 px, touch and winner",
            flush=True,
        )
        d1, d = context(301)
        d2, e = context(302, 390, True)
        for page in (d, e):
            page.goto(base + "/games/monopoly/table/fixture-estate")
            page.locator("#game").wait_for(state="visible")
        d.locator('[data-action="decline"]').click()
        d.locator("#bidAmount").fill("10")
        d.locator("#bidForm button[type=submit]").click()
        e.locator('[data-action="pass_bid"]').wait_for()
        e.locator('[data-action="pass_bid"]').tap()
        d.wait_for_function("document.querySelector('#pending').hidden")
        d.locator("#cell-1").click()
        d.locator('[data-action="build"]').click()
        wait_text(d, "#assets", "1 д.")
        d.locator('[data-action="sell"]').click()
        d.locator('[data-action="mortgage"]').click()
        wait_text(d, "#deed", "в залоге")
        d.locator('[data-action="unmortgage"]').click()
        d.locator("#tradeDetails summary").click()
        d.locator('input[name="giveProperty"][value="1"]').check()
        d.locator("#takeCash").fill("90")
        d.locator("#tradeForm button[type=submit]").click()
        e.locator("[data-accept-offer]").wait_for()
        e.locator("[data-accept-offer]").tap()
        wait_text(e, "#assets", "#1")
        d.wait_for_function(
            "!document.querySelector('#assets').textContent.includes('#1 ')"
        )
        print(
            "PASS mandatory auction, bid/pass, even build/sell, mortgage/redeem and accepted trade across two sessions",
            flush=True,
        )
        assert not errors, errors
        browser.close()
        print("PASS zero browser errors", flush=True)


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
    with tempfile.TemporaryDirectory(prefix="st9-monopoly-browser-") as directory:
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
