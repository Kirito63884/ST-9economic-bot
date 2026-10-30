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

        def check_geometry(page, black=False):
            metrics = page.evaluate("""() => {
                const board = document.querySelector('#board').getBoundingClientRect();
                const cells = [...document.querySelectorAll('#board button')].map(e => {
                    const r = e.getBoundingClientRect();
                    return [r.width, r.height];
                });
                return {board: [board.width, board.height], cells,
                    overflow: document.documentElement.scrollWidth > innerWidth + 1,
                    first: document.querySelector('#board button').dataset.square,
                    files: document.querySelector('.chess-files').textContent,
                    ranks: document.querySelector('.chess-ranks').textContent};
            }""")
            assert len(metrics["cells"]) == 64
            assert abs(metrics["board"][0] - metrics["board"][1]) < 1, metrics
            for width, height in metrics["cells"]:
                assert abs(width - height) < 1, metrics
                assert abs(width * 8 - metrics["board"][0]) < 1, metrics
            assert not metrics["overflow"], metrics
            assert metrics["first"] == ("h1" if black else "a8")
            assert metrics["files"] == ("hgfedcba" if black else "abcdefgh")
            assert metrics["ranks"] == ("12345678" if black else "87654321")
            return metrics["board"]

        for width in (1280, 800, 761, 760, 390, 320):
            a.set_viewport_size({"width": width, "height": 950})
            check_geometry(a)
        a.set_viewport_size({"width": 1280, "height": 950})
        original = check_geometry(a)
        check_geometry(b, True)
        a.locator('[data-square="e2"]').click()
        assert (
            a.locator('[data-square="e4"]')
            .get_attribute("aria-label")
            .endswith("доступен ход")
        )
        assert a.locator(".chess-square.target").count() == 2
        assert check_geometry(a) == original
        # Selecting and polling update the same buttons, preserving keyboard focus.
        a.evaluate("window.savedChessCell = document.querySelector('[data-square=e2]')")
        a.wait_for_timeout(2200)
        assert a.evaluate("document.activeElement === window.savedChessCell")
        assert a.evaluate(
            "window.savedChessCell === document.querySelector('[data-square=e2]')"
        )
        if screenshot_dir:
            a.screenshot(
                path=str(screenshot_dir / "chess-desktop-targets.png"), full_page=True
            )

        # Hold an old state response, make a move, then deliver the stale poll.
        held = {}

        def hold_poll(route):
            if not held:
                held["data"] = route.fetch().json()
                held["route"] = route
            else:
                route.continue_()

        a.route("**/api/chess/table/*/state", hold_poll)
        for _ in range(100):
            if "route" in held:
                break
            a.wait_for_timeout(50)
        assert "route" in held, "No background poll observed"
        a.keyboard.press("ArrowUp")
        a.keyboard.press("ArrowUp")
        a.keyboard.press("Enter")
        a.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ход чёрных')"
        )
        held["route"].fulfill(json=held["data"])
        a.wait_for_timeout(150)
        assert "♙" in a.locator('[data-square="e4"]').inner_text()
        assert not a.locator(".selected").count()
        assert check_geometry(a) == original
        a.unroute("**/api/chess/table/*/state", hold_poll)
        b.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ход чёрных')"
        )
        b.locator('[data-square="d7"]').tap()
        assert b.locator(".chess-square.target").count() == 2
        check_geometry(b, True)
        if screenshot_dir:
            b.screenshot(
                path=str(screenshot_dir / "chess-mobile-targets.png"), full_page=True
            )
        b.locator('[data-square="d5"]').tap()
        a.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ход белых')"
        )
        a.locator('[data-square="e4"]').click()
        assert "target-capture" in a.locator('[data-square="d5"]').get_attribute(
            "class"
        )
        assert "доступно взятие" in a.locator('[data-square="d5"]').get_attribute(
            "aria-label"
        )
        a.locator('[data-square="d5"]').click()
        b.wait_for_function(
            "document.querySelector('#turn').textContent.includes('Ход чёрных')"
        )
        assert check_geometry(a) == original
        # Accidental exit is cancellable and does not lose the game or stake.
        a.once("dialog", lambda dialog: dialog.dismiss())
        a.locator("#leave").click()
        assert a.url == url
        a.reload()
        a.wait_for_function(
            "document.querySelector('[data-square=d5]')?.textContent.includes('♙')"
        )
        assert "♟" not in a.locator('[data-square="d5"]').inner_text()
        check_geometry(a)
        b.locator("#offer").tap()
        a.locator("#accept").click()
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
        "PASS chess two sessions; square geometry at 320–1280px, both coordinate orientations, visible move/capture markers, stable focus, stale poll after move, mouse/keyboard/touch, cancelled exit, reload, draw/refund and leave; zero browser errors",
        flush=True,
    )


if __name__ == "__main__":
    fixture.run_checks = run_checks
    fixture.main()
