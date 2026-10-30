"""Real two-session draughts UI against a new local database; no integrations."""

import argparse
from contextlib import asynccontextmanager
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
SECRET = "synthetic-russian-draughts-browser-only"


def serve(path, port):
    if path.exists() or not path.parent.name.startswith("st9-draughts-browser-"):
        raise SystemExit("Only a new temporary fixture database is allowed")
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        DATABASE_URL="sqlite:///" + str(path),
        SECRET_KEY=SECRET,
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
    from draughts_game import DraughtsTable
    import uvicorn

    @asynccontextmanager
    async def quiet(app):
        yield

    main.app.router.lifespan_context = quiet
    with SessionLocal.begin() as db:
        for uid in (201, 202, 301, 302, 303, 304):
            db.add(
                User(
                    id=uid,
                    discord_id=str(uid),
                    username="Игрок " + str(uid),
                    balance=100 if uid % 2 else 50,
                )
            )
        main.restore_runtime("{}")
        for tid, uid, position in (
            ("promotion", 301, {"b6": "w", "c7": "b", "f6": "b", "h2": "b"}),
            ("victory", 303, {"a1": "w", "b2": "b"}),
        ):
            table = DraughtsTable(tid, uid, "Белые")
            table.add_player(uid + 1, "Чёрные")
            for player in table.players:
                player["ready"] = True
            table.start()
            table.position = position
            main.draughts_service.tables[tid] = table
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    uvicorn.run(
        main.app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )


def checks(base, chromium, screens, app_python):
    cookies = json.loads(
        subprocess.check_output(
            [
                app_python,
                "-c",
                "import base64,json; from itsdangerous import TimestampSigner; print(json.dumps({str(uid):TimestampSigner('"
                + SECRET
                + "').sign(base64.b64encode(json.dumps({'user_id':uid,'csrf_token':'synthetic-csrf'}).encode())).decode() for uid in (201,202,301,302,303,304)}))",
            ],
            text=True,
        )
    )
    from playwright.sync_api import sync_playwright

    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium, headless=True, args=["--no-sandbox"]
        )

        def room(uid, touch=False):
            ctx = browser.new_context(
                viewport={"width": 390 if touch else 1280, "height": 1050},
                has_touch=touch,
                is_mobile=touch,
            )
            value = cookies[str(uid)]
            ctx.add_cookies(
                [
                    {
                        "name": "session",
                        "value": value,
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            )
            ctx.route(
                "**/*",
                lambda r: (
                    r.continue_() if r.request.url.startswith(base) else r.abort()
                ),
            )
            page = ctx.new_page()
            page.set_default_timeout(12000)
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            return ctx, page

        def ready(page):
            page.locator("#boardFrame").wait_for(state="visible")

        def geometry(page, black=False):
            data = page.evaluate("""() => {const r=document.querySelector('#board').getBoundingClientRect();
                return {board:[r.width,r.height], cells:[...document.querySelectorAll('#board button')].map(n=>{const b=n.getBoundingClientRect();return [b.width,b.height]}), first:document.querySelector('#board button').dataset.square, overflow:document.documentElement.scrollWidth>innerWidth+1};}""")
            assert data["first"] == ("h1" if black else "a8")
            assert (
                not data["overflow"] and abs(data["board"][0] - data["board"][1]) < 1
            ), data
            assert len(data["cells"]) == 64 and all(
                abs(w - h) < 1 for w, h in data["cells"]
            ), data

        ac, a = room(201)
        bc, b = room(202, True)
        a.goto(base + "/games/draughts/lobby")
        a.locator("#entry").fill("10")
        a.locator("#private").select_option("true")
        a.locator("#create").click()
        a.wait_for_url("**/games/draughts/table/**")
        ready(a)
        url = a.url
        b.goto(url)
        b.locator("#joinButton").tap()
        ready(b)
        b.locator("#ready").tap()
        a.locator("#ready").click()
        a.locator("#start").click()
        a.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ход белых')"
        )
        for width in (320, 390, 800, 1280):
            a.set_viewport_size({"width": width, "height": 1050})
            geometry(a)
        geometry(b, True)
        a.locator('[data-square="a3"]').click()
        a.locator('[data-square="b4"]').click()
        b.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ход чёрных')"
        )
        b.locator('[data-square="b6"]').tap()
        b.locator('[data-square="a5"]').tap()
        a.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ход белых')"
        )
        a.locator("#offer").click()
        b.locator("#accept").tap()
        a.locator("#rematch").wait_for(state="visible")
        a.locator("#rematch").click()
        a.wait_for_function("!document.querySelector('#waiting').hidden")
        b.wait_for_function("!document.querySelector('#waiting').hidden")
        geometry(a, True)
        geometry(b)
        assert "100.00" in a.locator("[data-st-balance]").first.inner_text()
        assert "50.00" in b.locator("[data-st-balance]").first.inner_text()
        a.once("dialog", lambda d: d.accept())
        a.locator("#close").click()
        a.wait_for_url("**/games/draughts/lobby")
        ac.close()
        bc.close()
        pc, page = room(301)
        oc, opponent = room(302, True)
        page.goto(base + "/games/draughts/table/promotion")
        opponent.goto(page.url)
        ready(page)
        ready(opponent)
        page.locator('[data-square="b6"]').click()
        page.locator('[data-square="d8"]').click()
        page.wait_for_function(
            "document.querySelector('[data-square=d8] .draughts-piece').classList.contains('king')"
        )
        assert page.locator('[data-square="c7"] .taken').count() == 1
        assert page.locator(".draughts-square.target").count() == 2
        page.reload()
        ready(page)
        assert (
            page.locator('[data-square="d8"]').get_attribute("aria-pressed") == "true"
        )
        page.locator('[data-square="h4"]').click()
        page.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ход чёрных')"
        )
        assert page.locator(".taken").count() == 0
        assert page.locator('[data-square="h4"] .king svg').count() == 1
        if screens:
            page.screenshot(path=str(screens / "draughts-desktop.png"), full_page=True)
            opponent.wait_for_function(
                "document.querySelector('[data-square=h4] .king') !== null"
            )
            opponent.screenshot(
                path=str(screens / "draughts-mobile.png"), full_page=True
            )
        pc.close()
        oc.close()
        vc, v = room(303, True)
        v.goto(base + "/games/draughts/table/victory")
        ready(v)
        v.locator('[data-square="a1"]').tap()
        v.locator('[data-square="c3"]').tap()
        v.wait_for_function(
            "document.querySelector('#turn').textContent.includes('У соперника')"
        )
        assert v.locator("#rematch").is_visible()
        assert not errors, errors
        vc.close()
        browser.close()
    print(
        "PASS two sessions, equal stake/refund, fixed 320–1280px board, inverted colours, mouse/touch, mandatory capture, mid-series reload, immediate flying promotion, victory, rematch and close; zero browser errors",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-python", default=sys.executable)
    parser.add_argument("--chromium", default=shutil.which("chromium"))
    parser.add_argument("--screenshot-dir", type=Path)
    parser.add_argument("--serve-fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--database", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_fixture:
        serve(args.database, args.port)
        return
    if args.screenshot_dir:
        args.screenshot_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="st9-draughts-browser-") as directory:
        stage = Path(directory)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
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
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(60):
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(log.read()[-4000:])
                    try:
                        if opener.open(base + "/", timeout=0.5).status == 200:
                            break
                    except OSError:
                        time.sleep(0.2)
                else:
                    raise RuntimeError("Fixture did not start")
                checks(base, args.chromium, args.screenshot_dir, args.app_python)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
