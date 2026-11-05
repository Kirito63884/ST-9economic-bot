"""Desktop/mobile event editor using the existing isolated fixture server."""

import json
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
            page.goto(base + "/admin?section=forum-events")
            return page

        page = page_for(201)
        page.wait_for_function(
            "document.querySelector('#eventSave') && !document.querySelector('#eventSave').disabled"
        )
        page.locator("#eventTitle").fill("Матч <img src=x onerror=alert(1)>")
        page.locator("#eventStarts").fill("2030-11-14T19:30")
        page.locator("#eventTeams").fill("ST-9 / Fixture")
        page.locator("#eventRules").fill("Тестовые правила")
        assert "<img" in page.locator("#eventPreview").inner_text()
        assert page.locator("#eventPreview img").count() == 0
        page.locator("#eventSave").click()
        page.wait_for_function(
            "document.querySelector('#eventNotice').textContent.includes('Сохранено.')"
        )
        assert page.locator("#eventPicker option").count() == 2
        page.reload()
        page.wait_for_function(
            "document.querySelector('#eventPicker').options.length === 2"
        )
        eid = page.locator("#eventPicker option").nth(1).get_attribute("value")
        page.locator("#eventPicker").select_option(eid)
        assert page.locator("#eventStarts").input_value() == "2030-11-14T19:30"
        mobile = page_for(203, True)
        mobile.wait_for_function(
            "document.querySelector('#eventPicker').options.length === 2"
        )
        mobile.locator("#eventPicker").select_option(eid)
        mobile.locator("#eventTitle").fill("Изменённый матч")
        mobile.locator("#eventStatus").select_option("closed")
        mobile.locator("#eventSave").tap()
        mobile.wait_for_function(
            "document.querySelector('#eventNotice').textContent.includes('Сохранено.')"
        )
        assert mobile.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        if screenshot_dir:
            page.screenshot(
                path=str(screenshot_dir / "events-desktop.png"), full_page=True
            )
            mobile.screenshot(
                path=str(screenshot_dir / "events-mobile.png"), full_page=True
            )
        junior = page_for(202)
        assert junior.locator("#tab-forum-events").count() == 0
        assert (
            junior.evaluate("async()=> (await fetch('/api/admin/forum-events')).status")
            == 403
        )
        assert not errors, errors
        browser.close()
        print(
            "PASS event create/edit/reload, Moscow time, hostile title as text, mobile touch, junior denied; no browser errors"
        )


if __name__ == "__main__":
    fixture.run_checks = run_checks
    fixture.main()
