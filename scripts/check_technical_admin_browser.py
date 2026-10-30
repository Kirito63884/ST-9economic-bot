"""Check owner-only role controls on an isolated local fixture, without integrations."""

import json
import subprocess
from pathlib import Path

import check_battleship_browser as fixture


base_fixture = fixture.serve_fixture


def serve_fixture(path, port):
    import uvicorn

    real_run = uvicorn.run

    def run(app, **options):
        import main
        from database import SessionLocal, User

        main.ADMIN_DISCORD_ID = "201"
        with SessionLocal.begin() as db:
            user = db.get(User, 201)
            user.is_admin = user.is_senior_admin = user.is_chief_admin = True
        return real_run(app, **options)

    uvicorn.run = run
    base_fixture(path, port)


def run_checks(base, app_python, chromium, screenshot_dir, bootstrap_css):
    from playwright.sync_api import sync_playwright

    script = """import base64,json
from itsdangerous import TimestampSigner
print(json.dumps({f'{uid}:{version}':TimestampSigner('synthetic-battleship-browser-only').sign(base64.b64encode(json.dumps({'user_id':uid,'csrf_token':'synthetic-battle-csrf','auth_version':version}).encode())).decode() for uid,version in [(201,0),(202,0),(202,1),(203,0)]}))
"""
    cookies = json.loads(subprocess.check_output([app_python, "-c", script], text=True))
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium, headless=True, args=["--no-sandbox"]
        )

        def authenticate(ctx, uid, version=0):
            ctx.add_cookies(
                [
                    {
                        "name": "session",
                        "value": cookies[f"{uid}:{version}"],
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            )

        def context(uid):
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 1000}, locale="ru-RU"
            )

            def route(r):
                if r.request.url.startswith(base):
                    r.continue_()
                elif bootstrap_css and "bootstrap.min.css" in r.request.url:
                    r.fulfill(path=str(bootstrap_css), content_type="text/css")
                else:
                    r.abort()

            ctx.route("**/*", route)
            authenticate(ctx, uid)
            page = ctx.new_page()
            page.set_default_timeout(12000)
            page.on("pageerror", lambda e: errors.append(str(e)))
            return ctx, page

        owner_ctx, owner = context(201)
        tech_ctx, tech = context(202)
        chief_ctx, chief = context(203)
        owner.goto(base + "/admin?player=202")
        owner.locator("#playerDrawer[open]").wait_for()
        owner.locator('#playerDrawer [data-action="make_tech"]').click()
        owner.locator("#modalConfirm").click()
        owner.locator('#playerDrawer [data-action="remove_tech"]').wait_for()
        tech.goto(base + "/admin")
        tech.wait_for_url("**/login")
        authenticate(tech_ctx, 202, 1)  # Fresh authentication after promotion.
        tech.goto(base + "/admin?player=201")
        tech.locator("#playerDrawer[open]").wait_for()
        assert tech.locator("#playerDrawer input#balance_201").is_enabled()
        assert not tech.locator('[data-action="make_tech"]').count()
        assert not tech.locator('[data-action="remove_tech"]').count()
        assert "Технический администратор" in tech.locator(".sidebar").inner_text()
        chief.goto(base + "/admin?player=202")
        assert not chief.locator('[data-action="make_tech"]').count()
        assert not chief.locator('[data-action="remove_tech"]').count()
        assert not chief.locator("#playerDrawer .btn-toggle").count()
        if screenshot_dir:
            owner.screenshot(
                path=str(screenshot_dir / "technical-admin-owner.png"), full_page=True
            )
        owner.locator('#playerDrawer [data-action="remove_tech"]').click()
        owner.locator("#modalConfirm").click()
        owner.locator('#playerDrawer [data-action="make_tech"]').wait_for()
        tech.reload()
        assert not tech.locator("#playerDrawer input#balance_201").is_enabled()
        assert not errors, errors
        browser.close()
    print(
        "PASS owner grants/removes tech via real drawer and confirmation, stale-session rejection, delegated owner controls and chief denial; zero browser errors",
        flush=True,
    )


if __name__ == "__main__":
    # Let the shared fixture harness launch this wrapper for its synthetic server.
    fixture.__file__ = str(Path(__file__).resolve())
    fixture.serve_fixture = serve_fixture
    fixture.run_checks = run_checks
    fixture.main()
