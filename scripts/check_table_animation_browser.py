"""Check pool timing and poker card slots on an isolated local fixture.

All wallets/sessions are synthetic. Pool animation responses are controlled to
exercise delayed clock samples. Poker presentation uses legal-shaped fixtures;
this script does not claim to test its server-side betting rules.
"""

import copy
import json
import subprocess
import time

import check_battleship_browser as fixture


def run_checks(base, app_python, chromium, screenshot_dir, bootstrap_css):
    from playwright.sync_api import sync_playwright

    source = """
import base64,json
from itsdangerous import TimestampSigner
print(json.dumps({str(uid):TimestampSigner('synthetic-battleship-browser-only').sign(base64.b64encode(json.dumps({'user_id':uid,'csrf_token':'synthetic-battle-csrf'}).encode())).decode() for uid in (201,202)}))
"""
    cookies = json.loads(subprocess.check_output([app_python, "-c", source], text=True))
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium, headless=True, args=["--no-sandbox"]
        )

        def room(uid, width=1280):
            ctx = browser.new_context(
                viewport={"width": width, "height": 1050}, has_touch=width < 600
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

        ctx, page = room(201)
        page.goto(base + "/games/billiards/lobby")
        page.locator("#quickPractice").click()
        page.wait_for_url("**/games/billiards/table/**")
        page.wait_for_function(
            "document.querySelector('#clock span')?.textContent !== '—'"
        )
        tid = page.url.rsplit("/", 1)[1]
        data = page.evaluate(
            "async path => (await fetch(path)).json()",
            "/api/billiards/table/" + tid + "/state",
        )
        data["revision"] += 10
        start = time.time()
        frames = []
        for i in range(121):
            rows = copy.deepcopy(data["balls"])
            rows[0][1] = 245 + 555 * i / 120
            frames.append(rows)
        data["balls"] = frames[-1]
        data["last_shot"] = {
            "id": 100,
            "by": 201,
            "frames": frames,
            "frame_ms": 25,
            "started_at": start,
            "available_at": start + 3,
            "power": 0.5,
            "pocketed": [],
            "foul": None,
        }
        data["deadline"] = start + 60
        calls = []

        def pool_poll(route):
            packet = copy.deepcopy(data)
            # The second sample is deliberately late; old code rewound balls.
            lag = -0.45 if len(calls) == 1 else 0.05
            packet["server_time"] = time.time() + lag
            calls.append(lag)
            if "known_shot=100" in route.request.url:
                packet["last_shot"]["frames"] = None
            route.fulfill(json=packet)

        page.route("**/api/billiards/table/*/state*", pool_poll)
        page.evaluate("""() => {
            window.poolFrames = [];
            window.originalClock = document.querySelector('#clock');
            const draw = PoolRenderer.prototype.draw;
            PoolRenderer.prototype.draw = function(rows, model) {
                const cue = rows.find(r => r[0] === 0);
                window.poolFrames.push([performance.now(),cue[1]]);
                return draw.call(this, rows, model);
            };
        }""")
        page.wait_for_timeout(1200)
        # A device clock adjustment must not finish/restart the shot.
        page.evaluate(
            """() => { const real = Date.now; Date.now = () => real() + 30000; }"""
        )
        page.wait_for_timeout(3000)
        page.wait_for_function("window.poolFrames?.at(-1)?.[1] === 800")
        samples = page.evaluate("window.poolFrames")
        moving_samples = [(t, x) for t, x in samples if x > 245.1]
        assert len(moving_samples) > 10, moving_samples
        for (t0, x0), (t1, x1) in zip(moving_samples, moving_samples[1:]):
            assert x1 >= x0 - 0.05, (x0, x1)
            assert x1 - x0 <= (t1 - t0) * 0.185 + 12, (t1 - t0, x1 - x0)
        assert abs(moving_samples[-1][1] - 800) < 0.1, (
            moving_samples[-5:],
            calls,
            page.locator("#clock").inner_text(),
        )
        assert page.evaluate(
            "document.querySelector('#clock') === window.originalClock"
        )
        assert page.locator("#clock span").inner_text() not in ("—", "···")
        assert len(calls) >= 2
        if screenshot_dir:
            page.screenshot(path=str(screenshot_dir / "pool-timer.png"), full_page=True)
        ctx.close()
        print(
            "PASS pool stable clock DOM, no rewind/jump under delayed polls and +30s device clock change; final frame matches server",
            flush=True,
        )

        for width in (1280, 390):
            ctx, page = room(201, width)
            page.goto(base + "/games/poker/table/presentation-fixture")
            # Create a normal public table and join a second synthetic account.
            made = page.evaluate("""async () => {
                const form = new FormData();
                for (const [k,v] of Object.entries({max_players:'6',small_blind:'1',big_blind:'2'})) form.append(k,v);
                return (await fetch('/api/poker/create-table',{method:'POST',body:form})).json();
            }""")
            assert made.get("ok"), made
            poker_id = made["table_id"]
            other_ctx, other = room(202, width)
            other.goto(base + "/games/poker/table/" + poker_id)
            other.evaluate(
                "async path => fetch(path,{method:'POST'})",
                "/api/poker/join-table/" + poker_id,
            )
            page.goto(base + "/games/poker/table/" + poker_id)
            packet = page.evaluate(
                "async path => (await fetch(path)).json()",
                "/api/poker/table/" + poker_id,
            )
            assert len(packet["players"]) == 2
            packet.update(
                hand_number=1,
                action_revision=10,
                phase="preflop",
                deck_count=48,
                turn_remaining=50,
            )
            for i, player in enumerate(packet["players"]):
                player["cards"] = (
                    [{"value": "A", "suit": "♠"}, {"value": "K", "suit": "♥"}]
                    if i == 0
                    else [{"value": "?", "suit": "?"}] * 2
                )

            def poker_poll(route):
                route.fulfill(json=packet)

            page.route("**/api/poker/table/" + poker_id, poker_poll)
            page.evaluate("s => renderState(s)", packet)
            page.wait_for_function(
                "document.querySelectorAll('.st-card-flight').length > 0"
            )
            assert page.evaluate(
                "[...document.querySelectorAll('.st-card-flight')].every(n => getComputedStyle(n).display === 'flex')"
            )
            page.evaluate(
                "window.pokerGhosts = [...document.querySelectorAll('.st-card-flight')]"
            )
            page.evaluate("s => renderState(s)", packet)
            assert page.evaluate("window.pokerGhosts.every(n => n.isConnected)")
            page.wait_for_timeout(1100)
            flop = [
                {"value": "2", "suit": "♠"},
                {"value": "3", "suit": "♥"},
                {"value": "4", "suit": "♣"},
            ]
            packet.update(phase="flop", community=flop, action_revision=11)
            page.evaluate("s => renderState(s)", packet)
            page.wait_for_timeout(1100)
            page.evaluate("""() => { window.flopNodes=[...document.querySelector('#communityCards').children];
                window.flopRects=window.flopNodes.map(n=>{const r=n.getBoundingClientRect();return [r.left,r.top]});
                window.pokerBefore=['centerArea','communityCards','tableInner'].map(id=>{const n=document.getElementById(id),r=n.getBoundingClientRect();return [id,r.left,r.top,r.width,r.height,getComputedStyle(n).transform,getComputedStyle(n).display]}); }""")
            for i, phase in enumerate(("turn", "river"), start=12):
                packet.update(
                    phase=phase,
                    community=packet["community"]
                    + [{"value": str(i - 7), "suit": "♦"}],
                    action_revision=i,
                )
                page.evaluate("s => renderState(s)", packet)
                assert page.evaluate("""() => window.flopNodes.every((n,i)=> {
                    const r=n.getBoundingClientRect(),old=window.flopRects[i];
                    return n.isConnected && Math.abs(r.left-old[0])<1 && Math.abs(r.top-old[1])<1 && n.getAnimations().length===0;
                })"""), page.evaluate(
                    """() => ({width:innerWidth,layoutBefore:window.pokerBefore,layoutAfter:['centerArea','communityCards','tableInner'].map(id=>{const n=document.getElementById(id),r=n.getBoundingClientRect();return [id,r.left,r.top,r.width,r.height,getComputedStyle(n).transform,getComputedStyle(n).display]}), before:window.flopRects, after:window.flopNodes.map(n=>{const r=n.getBoundingClientRect();return [r.left,r.top,n.getAnimations().length,n.isConnected,getComputedStyle(n).transform]})})"""
                )
                page.wait_for_timeout(700)
            assert page.locator("#communityCards .community-card").count() == 5
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
            if screenshot_dir:
                page.screenshot(
                    path=str(screenshot_dir / f"poker-slots-{width}.png"),
                    full_page=True,
                )
            # Leave the synthetic waiting table through the API (UI fixtures did
            # not start a real hand) before the next viewport iteration.
            page.unroute("**/api/poker/table/" + poker_id, poker_poll)
            page.evaluate(
                "async path => fetch(path,{method:'POST'})",
                "/api/poker/table/" + poker_id + "/leave",
            )
            other.evaluate(
                "async path => fetch(path,{method:'POST'})",
                "/api/poker/table/" + poker_id + "/leave",
            )
            other_ctx.close()
            ctx.close()
        assert not errors, errors
        browser.close()
    print(
        "PASS poker deal ghosts keep flex geometry, unchanged poll keeps flights, flop slots stay fixed through turn/river on desktop/mobile; zero browser errors",
        flush=True,
    )


if __name__ == "__main__":
    fixture.run_checks = run_checks
    fixture.main()
