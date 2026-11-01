"""Exercise Protocol code entry on desktop and mobile against an isolated local fixture server.

The runner needs Playwright and Chromium; --app-python needs requirements-dev.txt.
Every account, cookie and wallet is synthetic. Dotenv is disabled, background
integrations are not started, and the browser blocks external requests.
No production services, accounts, database or GitHub credentials are used.
"""

import argparse
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
FIXTURE_SECRET = "synthetic-protocol-browser-only"


def serve_fixture(database_path, port):
    from contextlib import asynccontextmanager

    path = database_path.resolve()
    if (
        not path.parent.name.startswith("st9-protocol-browser-")
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
    import protocol_login_admin
    from contextlib import nullcontext

    protocol_login_admin.login_slot = nullcontext

    def fake_login(code, before_save):
        before_save()
        if code == "000000":
            raise protocol_login_admin.ClanUnavailable("test-only")
        return {"players": []}

    protocol_login_admin.login_with_code = fake_login
    protocol_login_admin.refresh_after_login = lambda document: True
    import uvicorn

    uvicorn.run(
        main.app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )


def run_checks(base, app_python, chromium, screenshot_dir, bootstrap_css):
    from playwright.sync_api import sync_playwright

    source = """import base64,json
from itsdangerous import TimestampSigner
print(TimestampSigner('synthetic-protocol-browser-only').sign(base64.b64encode(json.dumps({'user_id':203,'csrf_token':'synthetic-protocol-csrf'}).encode())).decode())
"""
    cookie = subprocess.check_output([app_python, "-c", source], text=True).strip()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium, headless=True, args=["--no-sandbox"]
        )
        for width in (1440, 390):
            context = browser.new_context(
                viewport={"width": width, "height": 1000}, has_touch=width == 390
            )
            context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": cookie,
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            )
            context.route(
                "**/*",
                lambda r: (
                    r.continue_() if r.request.url.startswith(base) else r.abort()
                ),
            )
            page = context.new_page()
            page.goto(base + "/admin")
            page.evaluate("switchTab('statistics')")
            field = page.locator("#protocolLoginCode")
            field.fill("000000")
            page.locator("#protocolLoginSubmit").click()
            page.wait_for_function(
                "document.querySelector('#protocolLoginStatus').textContent.includes('не подтвердил')"
            )
            assert field.input_value() == ""
            field.fill("123456")
            page.locator("#protocolLoginSubmit").click()
            page.wait_for_function(
                "document.querySelector('#protocolLoginStatus').textContent.includes('Вход выполнен')"
            )
            assert field.input_value() == ""
            assert "123456" not in page.locator("#protocolLoginStatus").inner_text()
            assert not page.locator("#protocolLoginSubmit").is_disabled()
            if screenshot_dir:
                page.screenshot(
                    path=str(screenshot_dir / f"protocol-login-{width}.png"),
                    full_page=True,
                )
            context.close()
        browser.close()
    print(
        "PASS desktop/mobile: code input, sanitized rejection, success, cleared secret, unchanged admin page"
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
    with tempfile.TemporaryDirectory(prefix="st9-protocol-browser-") as directory:
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
