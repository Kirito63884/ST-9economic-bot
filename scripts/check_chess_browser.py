"""Two signed sessions and a touch client against the isolated game fixture.

Uses the naval runner's local fixture lifecycle only; no production credentials,
workers or outbound browser requests are used. Playwright is a dev-only tool.
"""

import json
import subprocess

import check_battleship_browser as fixture


def run_checks(base, app_python, chromium, screenshot_dir, bootstrap_css):
    from playwright.sync_api import sync_playwright

    cookies = json.loads(
        subprocess.check_output(
            [
                app_python,
                "-c",
                """
import base64,json
from itsdangerous import TimestampSigner
print(json.dumps({str(uid):TimestampSigner('synthetic-battleship-browser-only').sign(base64.b64encode(json.dumps({'user_id':uid,'csrf_token':'synthetic-battle-csrf'}).encode())).decode() for uid in (201,202)}))
""",
            ],
            text=True,
        )
    )
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium, headless=True, args=["--no-sandbox"]
        )

        def context(uid, touch=False):
            ctx = browser.new_context(
                viewport={"width": 390 if touch else 1280, "height": 950},
                has_touch=touch,
                is_mobile=touch,
                locale="ru-RU",
            )

            def route(r):
                if r.request.url.startswith(base):
                    r.continue_()
                elif bootstrap_css and "bootstrap.min.css" in r.request.url:
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
            return ctx, page

        a_ctx, a = context(201)
        b_ctx, b = context(202, True)
        a.goto(base + "/games/chess/lobby")
        a.locator("#entry").fill("10")
        a.locator("#private").select_option("true")
        a.locator("#create").click()
        a.wait_for_url("**/games/chess/table/**")
        url = a.url
        tid = url.rsplit("/", 1)[1]
        b.goto(url)
        b.locator("#joinButton").tap()
        b.locator("#ready").tap()
        b.wait_for_function(
            "document.querySelector('#ready').textContent.includes('✓')"
        )
        a.wait_for_function(
            "document.querySelector('#players').textContent.includes('готов')"
        )
        a.locator("#ready").click()
        a.locator("#start").click()
        a.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ход белых')"
        )
        a.locator('[data-square="e2"]').click()
        a.locator('[data-square="e4"]').click()
        b.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ход чёрных')"
        )
        b.locator('[data-square="e7"]').tap()
        b.locator('[data-square="e5"]').tap()
        a.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ход белых')"
        )
        assert (
            a.locator("#board button").count()
            == b.locator("#board button").count()
            == 64
        )
        assert b.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if screenshot_dir:
            a.screenshot(path=str(screenshot_dir / "chess-desktop.png"), full_page=True)
            b.screenshot(path=str(screenshot_dir / "chess-mobile.png"), full_page=True)
        a.reload()
        a.wait_for_function(
            "document.querySelector('[data-square=e4]')?.textContent.includes('♙')"
        )
        assert "♟" in a.locator('[data-square="e5"]').inner_text()
        a.locator("#offer").click()
        b.locator("#accept").tap()
        a.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ничья')"
        )
        for page, balance in [(a, 100), (b, 50)]:
            state = page.evaluate(
                "async path => (await fetch(path)).json()",
                "/api/chess/table/" + tid + "/state",
            )
            assert state["match"]["settled"] and state["balance"] == balance
            assert not state["match"]["result"]["winners"]
        b.locator("#leaveFinished").tap()
        b.wait_for_url("**/games/chess/lobby")
        a.locator("#leaveFinished").click()
        a.wait_for_url("**/games/chess/lobby")
        assert not errors, errors
        a_ctx.close()
        b_ctx.close()
        browser.close()
    print(
        "PASS chess two-session create/join/ready/start, mouse and real touch moves, mobile layout, reload, draw/refund and leave; zero browser errors",
        flush=True,
    )


if __name__ == "__main__":
    fixture.run_checks = run_checks
    fixture.main()
