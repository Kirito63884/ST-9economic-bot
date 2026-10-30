"""Exercise two real sea-battle sessions against an isolated local fixture server.

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
FIXTURE_SECRET = "synthetic-battleship-browser-only"
LAYOUT = [
    [0, 1, 2, 3],
    [5, 6, 7],
    [20, 21, 22],
    [24, 25],
    [40, 41],
    [43, 44],
    [60],
    [63],
    [66],
    [69],
]


def serve_fixture(database_path, port):
    from contextlib import asynccontextmanager

    path = database_path.resolve()
    if (
        not path.parent.name.startswith("st9-battle-browser-")
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

    BASE = base
    errors = []
    source = """import base64,json
from itsdangerous import TimestampSigner
result={}
for uid in (201,202,203):
 payload=base64.b64encode(json.dumps({'user_id':uid,'csrf_token':'synthetic-battle-csrf'}).encode())
 result[str(uid)]=TimestampSigner('synthetic-battleship-browser-only').sign(payload).decode()
print(json.dumps(result))
"""
    cookies = json.loads(subprocess.check_output([app_python, "-c", source], text=True))

    def cookie(uid):
        return cookies[str(uid)]

    def capture(page, name):
        if screenshot_dir:
            page.screenshot(path=str(screenshot_dir / name), full_page=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium, headless=True, args=["--no-sandbox"]
        )
        errors = []

        def context(uid, width=1440, touch=False):
            ctx = browser.new_context(
                viewport={"width": width, "height": 1050},
                has_touch=touch,
                timezone_id="America/New_York",
                locale="ru-RU",
            )

            def route(r):
                if r.request.url.startswith(BASE):
                    r.continue_()
                elif "bootstrap@5.3.0/dist/css/bootstrap.min.css" in r.request.url:
                    r.fulfill(
                        path=str(bootstrap_css), content_type="text/css"
                    ) if bootstrap_css else r.fulfill(
                        body="body{margin:0;font-family:system-ui,sans-serif}*{box-sizing:border-box}.mt-2{margin-top:.5rem}.mt-4{margin-top:1.5rem}",
                        content_type="text/css",
                    )
                else:
                    r.abort()

            ctx.route("**/*", route)
            ctx.add_cookies(
                [
                    {
                        "name": "session",
                        "value": cookie(uid),
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            )
            page = ctx.new_page()
            page.set_default_timeout(12000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            return ctx, page

        def state(page):
            return page.evaluate(
                "async()=>await (await fetch('/api/battleship/table/'+document.querySelector('#st-content').dataset.tableId+'/state')).json()"
            )

        def wait_idle(page):
            page.wait_for_function("!document.querySelector('#autoFleet').disabled")

        c1, a = context(201)
        a.goto(BASE + "/games")
        assert a.locator('a.st-game-card[href="/games/battleship/lobby"]').count() == 1
        a.goto(BASE + "/games/battleship/lobby")
        a.locator("#createButton").wait_for(state="visible")
        a.wait_for_function("!document.querySelector('#createButton').disabled")
        a.locator("#entry").fill("75")
        a.locator("#private").check()
        capture(a, "battleship-lobby.png")
        a.locator("#createButton").click()
        a.wait_for_url("**/games/battleship/table/**")
        a.locator("#game").wait_for(state="visible")
        url = a.url
        # Rotate, place, remove and place again; an invalid adjoining placement is rejected locally.
        a.locator("#rotate").click()
        a.locator('#ownGrid [data-cell="0"]').click()
        assert a.locator("#ownGrid .ship").count() == 4
        a.locator('#ownGrid [data-cell="0"]').click()
        assert a.locator("#ownGrid .ship").count() == 0
        a.locator("#rotate").click()
        a.locator('#ownGrid [data-cell="0"]').click()
        a.locator('#ownGrid [data-cell="4"]').click()
        assert a.locator("#ownGrid .ship").count() == 4
        c2, b = context(202)
        b.goto(url)
        b.locator("#join").wait_for(state="visible")
        b.locator("#join").click()
        b.locator("#game").wait_for(state="visible")
        b.locator("#autoFleet").click()
        b.wait_for_function(
            "document.querySelector('#ownGrid').querySelectorAll('.ship').length===20"
        )
        a.wait_for_timeout(1100)
        assert (
            a.locator("#ownGrid .ship").count() == 4
        )  # Draft survives join and opponent edits.
        assert state(a)["stake_limit_cents"] == 5000 and not state(a)["stake_valid"]
        a.locator("#entry").fill("50")
        a.locator("#applySettings").click()
        a.wait_for_function(
            "document.querySelector('#bank').textContent.includes('100')"
        )
        assert a.locator("#ownGrid .ship").count() == 4
        for ship in LAYOUT[1:]:
            a.locator('#ownGrid [data-cell="' + str(ship[0]) + '"]').click()
        assert (
            a.locator("#ownGrid .ship").count() == 20
            and a.locator("#ready").is_disabled()
        )
        a.locator("#saveFleet").click()
        a.wait_for_function("!document.querySelector('#ready').disabled")
        assert state(a)["boards"][0]["fleet"] == LAYOUT
        a.locator("#ready").click()
        b.locator("#ready").click()
        a.wait_for_function("!document.querySelector('#start').disabled")
        capture(a, "battleship-placement.png")
        a.locator("#start").click()
        a.wait_for_function("document.querySelector('#waiting').hidden")
        b.wait_for_function("document.querySelector('#waiting').hidden")
        assert (
            a.locator("#players img").count() == 0
            and "<img" in a.locator("#players").inner_text()
        )
        s = state(a)
        pages = {201: a, 202: b}
        actor = s["actor"]
        other = 202 if actor == 201 else 201
        assert s["match"]["bank_cents"] == 10000 and s["stake_limit_cents"] == 5000
        assert all("fleet" not in board for board in s["boards"] if board["id"] != 201)
        assert a.locator("#close").is_disabled()
        otherstate = state(pages[other])
        targetships = next(
            board["fleet"] for board in otherstate["boards"] if board["id"] == other
        )
        water = next(
            cell
            for cell in range(100)
            if cell not in [cell for ship in targetships for cell in ship]
        )
        active = pages[actor]
        before = state(active)["revision"]
        active.locator('#enemyGrid [data-cell="' + str(water) + '"]').click()
        assert state(active)["revision"] == before
        active.locator("#fire").click()
        active.wait_for_function(
            "!document.querySelector('#turn').textContent.includes('Ваш выстрел')"
        )
        winner = other
        loser = actor
        winpage = pages[winner]
        targetstate = state(pages[loser])
        ships = next(
            board["fleet"] for board in targetstate["boards"] if board["id"] == loser
        )
        winpage.wait_for_function(
            "document.querySelector('#turn').textContent==='Ваш выстрел'"
        )
        for cell in [cell for ship in ships for cell in ship]:
            button = winpage.locator('#enemyGrid [data-cell="' + str(cell) + '"]')
            button.wait_for(state="visible")
            button.click()
            assert winpage.locator("#fire").is_enabled()
            winpage.locator("#fire").click()
            winpage.wait_for_function("document.querySelector('#fire').disabled")
        winpage.locator("#result").wait_for(state="visible")
        a.locator("#result").wait_for(state="visible")
        b.locator("#result").wait_for(state="visible")
        done = state(winpage)
        assert done["match"]["settled"] and done["match"]["result"]["winner"] == winner
        assert done["match"]["result"]["payouts"] == {str(winner): 10000, str(loser): 0}
        assert state(a)["balance"] + state(b)["balance"] == 150
        capture(a, "battleship-desktop.png")
        winpage.reload()
        winpage.locator("#result").wait_for(state="visible")
        assert state(winpage)["balance"] == done["balance"]
        a.locator("#rematch").click()
        a.wait_for_function("!document.querySelector('#waiting').hidden")
        b.wait_for_function("!document.querySelector('#waiting').hidden")
        assert not state(a)["boards"][0]["fleet"] and all(
            not player["ready"] for player in state(a)["players"]
        )
        a.on("dialog", lambda dialog: dialog.accept())
        a.locator("#close").click()
        a.wait_for_url("**/games/battleship/lobby")
        b.wait_for_function(
            "document.querySelector('#error').textContent.includes('Стол закрыт')"
        )
        print(
            "PASS real two-session creation/join, private placement, draft preservation, lower-balance cap, readiness, miss/hit turn order, full victory, bank, reload/rematch/owner close",
            flush=True,
        )
        c1.close()
        c2.close()
        # Mobile free game, portrait/landscape, explicit confirmation and offline guard.
        cm, m = context(201, 390, True)
        cn, n = context(202, 390, True)
        m.goto(BASE + "/games/battleship/lobby")
        m.wait_for_function("!document.querySelector('#createButton').disabled")
        m.locator("#entry").fill("0")
        m.locator("#createButton").tap()
        m.wait_for_url("**/games/battleship/table/**")
        m.locator("#game").wait_for(state="visible")
        url = m.url
        n.goto(url)
        n.locator("#join").wait_for(state="visible")
        n.locator("#join").tap()
        n.locator("#game").wait_for(state="visible")
        for page in (m, n):
            page.bring_to_front()
            page.reload()
            page.locator("#game").wait_for(state="visible")
            page.locator("#autoFleet").tap()
            page.wait_for_function("!document.querySelector('#ready').disabled")
            page.locator("#ready").tap()
        m.wait_for_function("!document.querySelector('#start').disabled")
        m.locator("#start").tap()
        m.wait_for_function("document.querySelector('#waiting').hidden")
        n.wait_for_function("document.querySelector('#waiting').hidden")
        mobile = {201: m, 202: n}
        active = mobile[state(m)["actor"]]
        assert (
            active.locator("#ownPanel").is_hidden()
            and active.locator("#enemyPanel").is_visible()
        )
        rev = state(active)["revision"]
        active.locator('#enemyGrid [data-cell="0"]').tap()
        assert state(active)["revision"] == rev
        assert active.locator("#fire").is_enabled()
        assert active.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        capture(active, "battleship-mobile.png")
        active.locator("#tabOwn").tap()
        assert active.locator("#enemyPanel").is_hidden()
        active.locator("#tabEnemy").tap()
        active.set_viewport_size({"width": 844, "height": 390})
        assert active.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        capture(active, "battleship-landscape.png")
        active.set_viewport_size({"width": 390, "height": 1050})
        offline = {"enabled": True}

        def disconnect(route):
            if offline["enabled"]:
                route.fulfill(status=503, json={"detail": "Тестовая потеря связи"})
            else:
                route.continue_()

        active.route("**/api/battleship/table/*/state", disconnect)
        active.wait_for_timeout(1500)
        assert active.locator("#fire").is_disabled()
        offline["enabled"] = False
        active.wait_for_timeout(1500)
        assert active.locator("#fire").is_enabled()
        active.locator("#fire").tap()
        active.wait_for_timeout(150)
        assert state(active)["revision"] == rev + 1
        print(
            "PASS 390px real touch selection never fires, separate shot confirmation, mobile board tabs, portrait/landscape no overflow, offline guard and reconnect",
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
    with tempfile.TemporaryDirectory(prefix="st9-battle-browser-") as directory:
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
