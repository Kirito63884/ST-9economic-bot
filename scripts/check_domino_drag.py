"""Standalone checks of rendered domino pointer controls with local API fixtures.

Run with a Python that has Playwright installed, for example:
  python scripts/check_domino_drag.py --app-python venv/bin/python

--app-python needs the normal application dependencies (Jinja2); --chromium can
select an installed browser. All requests are fulfilled by fixtures. No database,
site credentials, real account actions or external network calls are used.
"""

import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def render_fixture():
    from types import SimpleNamespace
    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

    sys.path.insert(0, str(ROOT))
    from domino import DominoTable, TILES

    request = SimpleNamespace(state=SimpleNamespace(csp_nonce="synthetic-browser-only"))
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader(
                    {
                        "_site_nav.html": "",
                        "_site_ui.html": "<style>body{margin:0;background:#101a27}*{box-sizing:border-box}</style>",
                    }
                ),
                FileSystemLoader(str(ROOT / "templates")),
            ]
        )
    )
    t = DominoTable("fixture", 101, "Первый", {"entry_cents": 0, "turn_seconds": 120})
    t.add_player(102, "Второй")
    for p in t.players:
        p["ready"] = True
    t.start()
    t.chain = [
        {"id": "2-5", "left": 2, "right": 5, "by": 102},
        {"id": "4-5", "left": 5, "right": 4, "by": 102},
    ]
    t.player(101)["hand"] = ["1-2", "3-4", "2-4", "0-0"]
    t.player(102)["hand"] = ["0-1", "4-6"]
    used = [b["id"] for b in t.chain] + [x for p in t.players for x in p["hand"]]
    t.stock = [x for x in TILES if x not in used]
    t.actor = 101
    t.deadline = time.time() + 120
    state = t.public_state(101) | {
        "balance": 1000,
        "can_close_table": True,
        "can_close": False,
        "close_version": "synthetic",
    }
    return {
        "html": env.get_template("domino_table.html").render(
            request=request, table_id=t.id
        ),
        "state": state,
    }


def check_browser(FIX, chromium, screenshot_dir):
    from playwright.sync_api import sync_playwright

    ORIGIN = "http://domino.test"
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium, headless=True, args=["--no-sandbox"]
        )

        def room(width=1280, touch=False, opening=False):
            state = copy.deepcopy(FIX["state"])
            state["deadline"] = time.time() + 120
            if opening:
                state["chain"] = []
                state["hand"][2] = "6-6"
                state["legal"] = {"6-6": ["right"]}
            calls = []
            context = browser.new_context(
                viewport={"width": width, "height": 1100}, has_touch=touch
            )
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))

            def route(r):
                if r.request.url == ORIGIN + "/games/domino/table/fixture":
                    r.fulfill(body=FIX["html"], content_type="text/html")
                elif r.request.url == ORIGIN + "/api/domino/table/fixture/state":
                    if state.get("offline"):
                        r.fulfill(status=503, json={"detail": "Связь потеряна"})
                    else:
                        r.fulfill(json=state | {"server_time": time.time()})
                elif r.request.url == ORIGIN + "/api/domino/table/fixture/action":
                    calls.append(r.request.post_data_json)
                    state.update(revision=state["revision"] + 1, actor=102, legal={})
                    r.fulfill(json=state | {"server_time": time.time()})
                else:
                    r.fulfill(body="", content_type="text/css")

            context.route("**/*", route)
            page.goto(ORIGIN + "/games/domino/table/fixture")
            page.locator("#game").wait_for(state="visible")
            return context, page, state, calls

        def center(locator):
            b = locator.bounding_box()
            assert b, b
            return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2

        def pick(page, bone="1-2"):
            source = page.locator('#hand button[data-tile="' + bone + '"]')
            source.scroll_into_view_if_needed()
            x, y = center(source)
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 12, y - 15, steps=3)
            page.locator("#dropTargets").wait_for(state="visible")

        def clean(page):
            assert page.locator(".dm-drag-ghost").count() == 0
            assert page.locator("#dropTargets").is_hidden()

        # Ordinary click stays a selection. Keyboard alternative still works.
        c, page, s, calls = room()
        page.locator('[data-tile="1-2"]').click()
        assert not calls
        assert page.locator('[data-tile="1-2"]').get_attribute("aria-pressed") == "true"
        page.locator("#playLeft").focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(100)
        assert (
            len(calls) == 1 and calls[0]["tile"] == "1-2" and calls[0]["side"] == "left"
        )
        c.close()
        print("PASS click selects, keyboard end button plays once", flush=True)
        c, page, s, calls = room()
        source = page.locator('[data-tile="1-2"]')
        x, y = center(source)
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 2, y - 2)
        assert page.locator("#dropTargets").is_hidden()
        page.mouse.up()
        assert source.get_attribute("aria-pressed") == "true" and not calls
        # An unplayable bone cannot start a drag or dispatch a move.
        invalid = page.locator('[data-tile="0-0"]')
        x, y = center(invalid)
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 20, y - 40)
        page.mouse.up()
        clean(page)
        assert not calls
        c.close()
        print(
            "PASS small pointer jitter selects; unplayable bone cannot drag", flush=True
        )
        for bone, side, opening in [
            ("1-2", "left", False),
            ("3-4", "right", False),
            ("2-4", "left", False),
            ("2-4", "right", False),
            ("6-6", "right", True),
        ]:
            c, page, s, calls = room(opening=opening)
            rev = s["revision"]
            pick(page, bone)
            if opening:
                assert page.locator("#dropLeft").is_hidden()
            else:
                assert page.locator("#drop" + side.title()).inner_text()
                assert page.locator(".dm-drag-ghost").count() == 1
            # Polling an unchanged state must not detach capture or ghost.
            page.wait_for_timeout(1200)
            assert page.locator(".dm-drag-ghost").count() == 1
            x, y = center(page.locator("#drop" + side.title()))
            page.mouse.move(x, y, steps=6)
            assert "over" in page.locator("#drop" + side.title()).get_attribute("class")
            page.mouse.up()
            page.wait_for_timeout(100)
            assert calls == [
                {"revision": rev, "kind": "play", "tile": bone, "side": side}
            ], calls
            clean(page)
            c.close()
        print(
            "PASS left/right/both-ended/opening mouse drops, unchanged poll, one request",
            flush=True,
        )
        for mode in [
            "outside",
            "wrong-end",
            "escape",
            "blur",
            "cancel",
            "lost-capture",
            "scroll",
            "turn-change",
            "timeout",
            "offline",
        ]:
            c, page, s, calls = room()
            pick(page)
            if mode == "outside":
                page.mouse.move(10, 10)
            elif mode == "wrong-end":
                x, y = center(page.locator("#dropRight"))
                page.mouse.move(x, y)
                assert "over" not in page.locator("#dropRight").get_attribute("class")
            elif mode == "escape":
                page.keyboard.press("Escape")
            elif mode == "blur":
                page.evaluate('window.dispatchEvent(new Event("blur"))')
            elif mode in ("cancel", "lost-capture"):
                page.evaluate(
                    """kind=>{const h=document.querySelector('#hand');h.dispatchEvent(new PointerEvent(kind,{pointerId:1,bubbles:true}))}""",
                    "pointercancel" if mode == "cancel" else "lostpointercapture",
                )
            elif mode == "scroll":
                page.evaluate('window.dispatchEvent(new Event("scroll"))')
            elif mode == "turn-change":
                s.update(actor=102, revision=s["revision"] + 1, legal={})
                page.wait_for_timeout(1200)
            elif mode == "timeout":
                s["deadline"] = time.time() - 1
                page.wait_for_timeout(1200)
            elif mode == "offline":
                s["offline"] = True
                page.wait_for_timeout(1200)
            if mode not in ("outside", "wrong-end"):
                clean(page)
                x, y = center(page.locator("#board"))
                page.mouse.move(x, y)
            page.mouse.up()
            page.wait_for_timeout(100)
            assert not calls, (mode, calls)
            clean(page)
            c.close()
        print(
            "PASS invalid drop, Escape, lost capture/focus, scroll, turn/timeout/offline cancellation",
            flush=True,
        )
        # Real touch events via CDP, including cancellation, not synthetic pointer-only drags.
        for canceled in (False, True):
            c, page, s, calls = room(390, True)
            source = page.locator('[data-tile="1-2"]')
            source.scroll_into_view_if_needed()
            x, y = center(source)
            cdp = c.new_cdp_session(page)

            def touch(kind, x=None, y=None):
                cdp.send(
                    "Input.dispatchTouchEvent",
                    {
                        "type": kind,
                        "touchPoints": [] if x is None else [{"x": x, "y": y, "id": 1}],
                    },
                )

            touch("touchStart", x, y)
            touch("touchEnd")
            page.wait_for_timeout(80)
            assert not calls and source.get_attribute("aria-pressed") == "true"
            touch("touchStart", x, y)
            touch("touchMove", x + 12, y - 15)
            page.locator("#dropTargets").wait_for(state="visible")
            dx, dy = center(page.locator("#dropLeft"))
            for step in range(1, 8):
                touch("touchMove", x + (dx - x) * step / 7, y + (dy - y) * step / 7)
            assert "over" in page.locator("#dropLeft").get_attribute("class")
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
            if not canceled and screenshot_dir:
                page.screenshot(
                    path=str(screenshot_dir / "domino-drag-mobile.png"), full_page=True
                )
            touch("touchCancel" if canceled else "touchEnd")
            page.wait_for_timeout(100)
            assert len(calls) == (0 if canceled else 1), calls
            if calls:
                assert calls[0]["side"] == "left" and calls[0]["tile"] == "1-2"
            clean(page)
            c.close()
        print(
            "PASS real 390px touch: tap selects, drag drop plays once, cancellation never plays, no overflow",
            flush=True,
        )
        assert not errors, errors
        browser.close()
        print("PASS zero browser errors", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-python", default=sys.executable)
    parser.add_argument("--chromium", default=shutil.which("chromium"))
    parser.add_argument("--screenshot-dir", type=Path)
    parser.add_argument("--render-fixture", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.render_fixture:
        print(json.dumps(render_fixture()))
        return
    if not args.chromium:
        parser.error("Specify an installed browser with --chromium")
    if args.screenshot_dir:
        args.screenshot_dir.mkdir(parents=True, exist_ok=True)
    fixture = json.loads(
        subprocess.check_output(
            [args.app_python, str(Path(__file__).resolve()), "--render-fixture"],
            text=True,
        )
    )
    check_browser(fixture, args.chromium, args.screenshot_dir)


if __name__ == "__main__":
    main()
