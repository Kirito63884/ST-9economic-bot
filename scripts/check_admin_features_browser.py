"""Check admin birthdays/import/export and event rewards with synthetic users."""

from datetime import datetime, timedelta, timezone
import json
import subprocess

import check_roster_settings_browser as fixture


def run_checks(base, app_python, chromium, screenshot_dir, bootstrap_css):
    from playwright.sync_api import sync_playwright, expect

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

        def open_page(uid, width=1280, touch=False):
            context = browser.new_context(
                viewport={"width": width, "height": 1000},
                has_touch=touch,
                locale="ru-RU",
                accept_downloads=True,
            )
            context.route(
                "**/*",
                lambda r: (
                    r.continue_() if r.request.url.startswith(base) else r.abort()
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
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(base + "/admin?section=birthdays")
            expect(page.locator("#bdNotice")).to_contain_text("загружен")
            return page

        junior = open_page(202, 390, True)
        junior.locator("#bdId").fill("123456789012345678")
        junior.locator("#bdName").fill("Fixture <img src=x onerror=alert(1)>")
        junior.locator("#bdDate").fill("15.06.1814")
        junior.locator("#bdSave").tap()
        expect(junior.locator("#bdNotice")).to_contain_text("сохранена")
        assert junior.locator("#bdList img").count() == 0
        junior.locator("#bdList button").click()
        assert junior.locator("#bdId").evaluate("el=>el.readOnly")
        junior.locator("#bdDate").fill("16.06.1814")
        junior.locator("#bdSave").click()
        expect(junior.locator("#bdList")).to_contain_text("16.06.1814")
        records = {
            "123456789012345678": {"name": "Fixture updated", "date": "15.06.1814"},
            "223456789012345678": {"name": "Second", "date": "20.10.2000"},
        }
        junior.locator("#bdFile").set_input_files(
            {
                "name": "birthdays (1).json",
                "mimeType": "application/json",
                "buffer": json.dumps(records).encode(),
            }
        )
        junior.locator("#bdPreview").click()
        expect(junior.locator("#bdPlan")).to_contain_text("Изменится: 1")
        expect(junior.locator("#bdList")).to_contain_text("16.06.1814")
        junior.locator("#bdApply").tap()
        expect(junior.locator("#bdNotice")).to_contain_text("Импорт завершён")
        with junior.expect_download() as download_info:
            junior.locator("#bdExport").click()
        download = download_info.value
        assert json.loads(open(download.path()).read()) == records
        assert junior.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        if screenshot_dir:
            junior.screenshot(
                path=str(screenshot_dir / "birthdays-mobile.png"), full_page=True
            )
        senior = open_page(201)
        assert "Second" in senior.locator("#bdList").inner_text()
        eid = "b" * 32
        response = senior.request.post(
            base + "/api/admin/forum-events",
            headers={"X-ST9-CSRF": "fixture-csrf"},
            data={
                "id": eid,
                "revision": None,
                "lineup": [],
                "config": {
                    "title": "Browser reward fixture",
                    "starts_at": (
                        datetime.now(timezone.utc) + timedelta(days=2)
                    ).isoformat(),
                    "capacity": 20,
                    "teams": "",
                    "map": "",
                    "server": "",
                    "rules": "",
                    "image_url": "",
                    "status": "open",
                },
            },
        )
        assert response.status == 200, response.text()
        senior.goto(base + "/admin?section=forum-events")
        senior.locator("#eventPicker").select_option(eid)
        senior.locator("#rewardLoad").click()
        expect(senior.locator("#rewardNotice")).to_contain_text("загружены")
        card = senior.locator(".reward-row").filter(has_text="Игрок <img")
        card.locator("input[type=checkbox]").check()
        card.locator("input[type=text]").fill("3,25")
        senior.locator("#rewardPreview").click()
        expect(senior.locator("#rewardPlan")).to_contain_text("3,25 БК")
        senior.locator("#rewardApply").click()
        expect(senior.locator("#rewardNotice")).to_contain_text("Начислено")
        expect(card.locator("input[type=checkbox]")).to_be_disabled()
        paid = senior.request.get(
            base + f"/api/admin/forum-events/{eid}/rewards"
        ).json()["paid"]
        assert paid["202"]["cents"] == 325
        assert (
            junior.request.get(base + f"/api/admin/forum-events/{eid}/rewards").status
            == 403
        )
        senior.set_viewport_size({"width": 390, "height": 1000})
        assert senior.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        if screenshot_dir:
            senior.screenshot(
                path=str(screenshot_dir / "event-rewards-mobile.png"), full_page=True
            )
        assert not errors, errors
        browser.close()
        print(
            "PASS junior birthday editing, import preview/apply/export round-trip, mobile touch, senior event payout, duplicate UI prevention, junior payout denial; no JS errors"
        )


if __name__ == "__main__":
    fixture.run_checks = run_checks
    fixture.main()
