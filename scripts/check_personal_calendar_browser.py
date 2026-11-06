"""Exercise the real personal calendar with isolated users and fixture events."""

import json
import os
import subprocess

import check_roster_settings_browser as fixture


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

        def page_for(uid, mobile=False):
            context = browser.new_context(
                viewport={"width": 390 if mobile else 1280, "height": 1000},
                has_touch=mobile,
                timezone_id="America/Los_Angeles",
                locale="ru-RU",
            )
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if route.request.url.startswith(base)
                    else route.abort()
                ),
            )
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
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/calendar")
            page.wait_for_function(
                "document.querySelector('#calNotice').textContent.includes('Событий')"
            )
            return page

        page = page_for(201)
        assert "Мой матч <img" in page.locator("#calList").inner_text()
        assert page.locator("#calList img").count() == 0
        assert "Событие другого игрока" not in page.locator("#calList").inner_text()
        assert "19:30 МСК" in page.locator("#calList").inner_text()
        assert "В игровом составе" in page.locator("#calList").inner_text()
        page.locator("#calMaybe").uncheck()
        assert "Возможно тренировка" not in page.locator("#calList").inner_text()
        page.locator("#calMaybe").check()
        assert "Возможно тренировка" in page.locator("#calList").inner_text()
        page.locator(".cal-day[data-today=true]").click()
        assert "Мой матч" in page.locator("#calList").inner_text()
        assert "Будущий матч" not in page.locator("#calList").inner_text()
        page.locator("#calClearDay").click()
        original = page.locator("#calMonthTitle").inner_text()
        with page.expect_response(lambda response: "/api/calendar?" in response.url):
            page.locator("#calNext").click()
        page.wait_for_function(
            '(old)=>document.querySelector("#calMonthTitle").textContent!==old',
            arg=original,
        )
        with page.expect_response(lambda response: "/api/calendar?" in response.url):
            page.locator("#calPrev").click()
        page.wait_for_function(
            '(old)=>document.querySelector("#calMonthTitle").textContent===old',
            arg=original,
        )
        page.locator("#calUpcomingMode").click()
        page.wait_for_function(
            'document.querySelector("#calUpcomingMode").getAttribute("aria-pressed")==="true"'
        )
        assert "Будущий матч" in page.locator("#calList").inner_text()
        mobile = page_for(201, True)
        mobile.locator(".cal-day[data-today=true]").tap()
        assert "Мой матч" in mobile.locator("#calList").inner_text()
        assert mobile.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        other = page_for(202)
        assert "Событие другого игрока" in other.locator("#calList").inner_text()
        assert "Мой матч" not in other.locator("#calList").inner_text()
        empty = page_for(203)
        assert empty.locator(".cal-empty").is_visible()
        if screenshot_dir:
            page.screenshot(
                path=str(screenshot_dir / "calendar-desktop.png"), full_page=True
            )
            mobile.screenshot(
                path=str(screenshot_dir / "calendar-mobile.png"), full_page=True
            )
        assert not errors, errors
        browser.close()
        print(
            "PASS private calendars, Moscow time in another browser timezone, month/day navigation, maybe filter, upcoming, empty state, mobile touch; no JS errors"
        )


if __name__ == "__main__":
    os.environ["ST9_BROWSER_CALENDAR_FIXTURE"] = "1"
    fixture.run_checks = run_checks
    fixture.main()
