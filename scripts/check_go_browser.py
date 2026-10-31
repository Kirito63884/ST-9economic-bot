"""Exercise two real Go sessions against an isolated local fixture server.

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
FIXTURE_SECRET = "synthetic-go-browser-only"


def serve_fixture(database_path, port):
    from contextlib import asynccontextmanager

    path = database_path.resolve()
    if (
        not path.parent.name.startswith("st9-go-browser-")
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
print(json.dumps({str(uid):TimestampSigner('synthetic-go-browser-only').sign(base64.b64encode(json.dumps({'user_id':uid,'csrf_token':'fixture-csrf'}).encode())).decode() for uid in (201,202,203)}))
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
                elif (
                    "bootstrap" in r.request.url
                    and ".css" in r.request.url
                    and bootstrap_css
                ):
                    r.fulfill(path=str(bootstrap_css), content_type="text/css")
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
            page.set_default_timeout(12000)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())
            return page

        def state(page):
            return page.evaluate(
                "async()=>await(await fetch('/api/go/table/'+location.pathname.split('/').pop()+'/state')).json()"
            )

        def capture(page, name):
            if screenshot_dir:
                page.screenshot(path=str(screenshot_dir / name), full_page=True)

        def wait_for(page, expr):
            page.wait_for_function(expr)

        a = context(201)
        a.goto(base + "/games/go/lobby")
        a.locator("#size").select_option("19")
        a.locator("#entry").fill("10")
        capture(a, "go-lobby.png")
        a.locator("#create").click()
        a.wait_for_url("**/games/go/table/**")
        a.locator("#game").wait_for(state="visible")
        url = a.url
        b = context(202, 390, True)
        b.goto(url)
        b.locator("#joinButton").tap()
        b.locator("#game").wait_for(state="visible")
        wait_for(a, "document.querySelectorAll('#players .go-player').length===2")
        for size in (9, 13, 19):
            a.locator("#settings summary").click() if not a.locator(
                "#settings"
            ).evaluate("e=>e.open") else None
            a.locator("#size").select_option(str(size))
            a.locator("#settingsForm button[type=submit]").click()
            wait_for(
                a, f"document.querySelectorAll('.go-point').length==={size * size}"
            )
        a.locator("#settings summary").click()
        a.locator("#ready").click()
        wait_for(
            b,
            "[...document.querySelectorAll('#players .go-player')].find(e=>e.textContent.includes('Адмирал')).textContent.includes('Готов')",
        )
        b.locator("#ready").tap()
        wait_for(a, "!document.querySelector('#start').disabled")
        a.locator("#start").click()
        wait_for(a, "document.querySelector('#waiting').hidden")
        wait_for(b, "document.querySelector('#waiting').hidden")
        s = state(a)
        pages = {201: a, 202: b}
        black = pages[s["actor"]]
        white = pages[
            next(p["id"] for p in s["match"]["participants"] if p["color"] == "white")
        ]
        assert (
            a.locator("#players img").count() == 0
            and "<img" in a.locator("#players").inner_text()
        )
        assert a.locator(".st-delete-table").is_disabled()
        for width in (320, 390, 768, 1280):
            b.set_viewport_size({"width": width, "height": 1000})
            assert b.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
            box = b.locator("#board").bounding_box()
            assert abs(box["height"] - box["width"]) < 2
        b.set_viewport_size({"width": 390, "height": 1000})
        b.locator("#zoom").select_option("3")
        assert b.locator("#boardFrame").bounding_box()["width"] > 390
        black.locator("#point-0").click()
        assert state(black)["board"][0] == 0
        black.locator("#place").click()
        wait_for(black, "document.querySelector('#point-0 .go-stone.black')!==null")
        wait_for(white, "!document.querySelector('#pass').disabled")
        white.locator("#point-360").click()
        white.locator("#place").click()
        wait_for(white, "document.querySelector('#point-360 .go-stone.white')!==null")
        assert len(state(white)["moves"]) == 2
        # A native touch scroll on the enlarged board never submits a move.
        revision = state(b)["revision"]
        b.locator("#boardScroll").evaluate("e=>{e.scrollLeft=100;e.scrollTop=100}")
        assert state(b)["revision"] == revision
        b.locator("#zoom").select_option("1")
        capture(a, "go-desktop.png")
        capture(b, "go-mobile.png")

        def double_pass():
            wait_for(black, "!document.querySelector('#pass').disabled")
            black.locator("#pass").click()
            wait_for(white, "!document.querySelector('#pass').disabled")
            white.locator("#pass").click()
            for page in (black, white):
                page.locator("#scoring").wait_for(state="visible")

        double_pass()
        white.locator("#point-0").click()
        white.locator("#markDead").click()
        wait_for(black, "document.querySelector('#point-0 .go-stone.dead')!==null")
        black.locator("#confirmScore").click()
        wait_for(black, "document.querySelector('#confirmScore').disabled")
        assert (
            state(black)["phase"] == "scoring"
            and state(black)["match"]["result"] is None
        )
        wait_for(white, "!document.querySelector('#accepts').textContent.includes('пока никто')")
        white.locator("#resume").click()
        wait_for(white, "document.querySelector('#scoring').hidden")
        assert state(white)["board"][0] == 1 and state(white)["dead"] == []
        double_pass()
        white.locator("#point-0").click()
        white.locator("#markDead").click()
        wait_for(black, "document.querySelector('#point-0 .go-stone.dead')!==null")
        black.locator("#confirmScore").click()
        wait_for(
            white,
            "!document.querySelector('#accepts').textContent.includes('пока никто')",
        )
        white.locator("#confirmScore").click()
        wait_for(white, "!document.querySelector('#finished').hidden")
        wait_for(black, "!document.querySelector('#finished').hidden")
        final = state(white)
        assert final["match"]["settled"] and final["match"]["bank_cents"] == 2000
        assert final["match"]["result"]["score"]["dead"] == [0]
        capture(white, "go-agreed-result.png")
        a.locator("#rematch").click()
        wait_for(a, "!document.querySelector('#waiting').hidden")
        assert state(a)["board"] == [0] * 361 and not any(
            p["ready"] for p in state(a)["players"]
        )
        outsider = context(203)
        outsider.goto(url)
        assert (
            outsider.evaluate(
                "async()=> (await fetch('/api/go/table/'+location.pathname.split('/').pop()+'/state')).status"
            )
            == 403
        )
        assert not errors, errors
        browser.close()
        print(
            "PASS all sizes, random colors, equal stake, explicit placement, mobile zoom/scroll, double pass, group marks, consent, dispute resume, final payout, rematch and outsider denial; zero browser errors",
            flush=True,
        )


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
    with tempfile.TemporaryDirectory(prefix="st9-go-browser-") as directory:
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
