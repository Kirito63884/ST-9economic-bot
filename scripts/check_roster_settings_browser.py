"""Exercise two real roster editor sessions against an isolated local fixture server.

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
FIXTURE_SECRET = "synthetic-roster-browser-only"


def serve_fixture(database_path, port):
    from contextlib import asynccontextmanager

    path = database_path.resolve()
    if (
        not path.parent.name.startswith("st9-roster-browser-")
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
                    is_admin=True,
                    is_senior_admin=uid == 201,
                    is_chief_admin=uid == 203,
                )
            )
        main.restore_runtime("{}")
        from roster_settings import CACHE_KEY, defaults, write

        config = defaults()
        write(
            db,
            CACHE_KEY,
            {
                "roles": [
                    {"id": rid, "name": key} for key, rid in config["role_ids"].items()
                ]
                + [
                    {"id": "1555555555555555555", "name": "Замена роли"},
                    {"id": "1666666666666666666", "name": "Резерв"},
                ],
                "members": [
                    {
                        "user": {"id": "201", "username": "Старший"},
                        "roles": [config["role_ids"]["founder"]],
                    },
                    {
                        "user": {"id": "203", "username": "Главный"},
                        "roles": ["1555555555555555555", "1666666666666666666"],
                    },
                ],
                "checked_at": "2026-10-30T12:00:00Z",
            },
        )
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    import uvicorn

    uvicorn.run(
        main.app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )


def run_checks(base, app_python, chromium, screenshot_dir, bootstrap_css):
    from playwright.sync_api import sync_playwright

    source = """import base64,json
from itsdangerous import TimestampSigner
print(json.dumps({str(uid):TimestampSigner('synthetic-roster-browser-only').sign(base64.b64encode(json.dumps({'user_id':uid,'csrf_token':'fixture-csrf'}).encode())).decode() for uid in (201,202,203)}))
"""
    cookies = json.loads(subprocess.check_output([app_python, "-c", source], text=True))
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium, headless=True, args=["--no-sandbox"]
        )

        def page_for(uid, width=1280, touch=False):
            context = browser.new_context(
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

            context.route("**/*", route)
            context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": cookies[str(uid)],
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            )
            page = context.new_page()
            page.set_default_timeout(12000)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())
            page.goto(base + "/admin?section=roster-settings")
            return page

        a = page_for(201)
        a.locator("#rosterEditor").wait_for(state="visible")
        assert a.locator("#rosterBaseRoles > details").count() == 12
        card = a.locator('[data-role-key="founder"]')
        card.locator("summary").click()
        card.locator('[data-field="role_id"]').fill("1555555555555555555")
        card.locator('[data-field="description"]').fill("Описание руководителя")
        a.locator("#rosterPreviewButton").click()
        a.wait_for_function("!document.querySelector('#rosterSaveButton').disabled")
        assert a.locator("#rosterPreview pre").count() == 2
        assert "Описание руководителя" in a.locator("#rosterPreview").inner_text()
        assert "ВЫСШИЙ КОМАНДНЫЙ СОСТАВ" in a.locator("#rosterPreview").inner_text()
        card.locator('[data-field="description"]').fill("Изменённое описание")
        assert a.locator("#rosterSaveButton").is_disabled()
        a.locator("#rosterPreviewButton").click()
        a.wait_for_function("!document.querySelector('#rosterSaveButton').disabled")
        a.locator("#rosterSaveButton").click()
        a.wait_for_function(
            "document.querySelector('#rosterStatus').textContent.includes('Настройки сохранены')"
        )
        a.locator("#rosterAddRole").click()
        extra = a.locator("#rosterExtraRoles > details").last
        extra.locator('[data-field="title"]').fill("Резерв")
        extra.locator('[data-field="role_id"]').fill("1666666666666666666")
        extra.locator('[data-field="description"]').fill("Поддержка подразделений")
        extra.locator('[data-field="kind"]').select_option("group")
        a.locator("#rosterPreviewButton").click()
        a.wait_for_function("!document.querySelector('#rosterSaveButton').disabled")
        assert "**Резерв**" in a.locator("#rosterPreview").inner_text()
        a.locator("#rosterSaveButton").click()
        a.wait_for_function(
            "document.querySelector('#rosterStatus').textContent.includes('Настройки сохранены')"
        )
        a.reload()
        a.locator("#rosterEditor").wait_for(state="visible")
        assert a.locator("#rosterExtraRoles > details").count() == 1
        if screenshot_dir:
            a.screenshot(
                path=str(screenshot_dir / "roster-editor-desktop.png"), full_page=True
            )
        b = page_for(203, 390, True)
        b.locator("#rosterEditor").wait_for(state="visible")
        b.locator("#rosterPreviewButton").tap()
        b.wait_for_function("!document.querySelector('#rosterSaveButton').disabled")
        assert b.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        if screenshot_dir:
            b.screenshot(
                path=str(screenshot_dir / "roster-editor-mobile.png"), full_page=True
            )
        junior = page_for(202)
        assert junior.locator("#tab-roster-settings").count() == 0
        assert (
            junior.evaluate(
                "async()=> (await fetch('/api/admin/roster-publication')).status"
            )
            == 403
        )
        assert not errors, errors
        browser.close()
        print(
            "PASS senior/chief editor, exact two-message preview, invalidation, role ID/description, new group, saved reload, mobile touch, junior denied; zero browser errors",
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
    with tempfile.TemporaryDirectory(prefix="st9-roster-browser-") as directory:
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
