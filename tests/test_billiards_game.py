"""Pool rules, authoritative shot timing and transaction-safe wallet outcomes."""

import copy
import math

import pytest

import billiards
import main
from billiards import BilliardsError, BilliardsTable, fresh_balls, simulate
from conftest import wallet
from database import GameHistory, SessionLocal


def table(mode="eight"):
    game = BilliardsTable("test", 101, "Alice", {"mode": mode, "entry_cents": 0})
    if mode != "practice":
        game.add_player(102, "Bob")
    for player in game.players:
        player["ready"] = True
    game.start()
    return game


def outcome(monkeypatch, first, sunk=(), rail=True, pocket=0, rails=(1, 2, 3, 4)):
    def run(balls, angle, power):
        before = billiards._frame(balls)
        for ball in balls:
            if ball["id"] in sunk:
                ball["pocketed"] = True
        return {
            "first_contact": first,
            "pocketed": list(sunk),
            "scratch": 0 in sunk,
            "rail_after_contact": rail,
            "frames": [before, billiards._frame(balls)],
            "frame_ms": 33.33,
            "object_rails": list(rails),
            "pocket_map": {str(number): pocket for number in sunk},
        }

    monkeypatch.setattr(billiards, "simulate", run)


def assigned(game, clear=False):
    game.groups = {"101": "solids", "102": "stripes"}
    game.players[0]["shots"] = 1  # This is not the break.
    if clear:
        for ball in game.balls:
            if 1 <= ball["id"] <= 7:
                ball["pocketed"] = True


def test_legal_eight_finishes_after_group_cleared(monkeypatch):
    game = table()
    assigned(game, clear=True)
    outcome(monkeypatch, 8, (8,), pocket=4)
    game.shoot(101, 0, 0.4, called_pocket=4)
    assert game.phase == "finished"
    assert game.match["result"]["winners"] == [101]
    assert game.last_shot["foul"] is None


@pytest.mark.parametrize("foul", ["wrong-pocket", "scratch", "early", "same-shot"])
def test_eight_loses_for_wrong_pocket_scratch_or_early_finish(monkeypatch, foul):
    game = table()
    assigned(game, clear=foul not in ("early", "same-shot"))
    if foul == "same-shot":
        for ball in game.balls:
            if 2 <= ball["id"] <= 7:
                ball["pocketed"] = True
    sunk = (8, 0) if foul == "scratch" else (1, 8) if foul == "same-shot" else (8,)
    outcome(monkeypatch, 1 if foul == "same-shot" else 8, sunk, pocket=3)
    game.shoot(101, 0, 0.4, called_pocket=4 if foul == "wrong-pocket" else 3)
    assert game.match["result"]["winners"] == [102]


def test_call_required_only_in_new_rules_and_before_mutation(monkeypatch):
    game = table()
    assigned(game, clear=True)
    before = game.to_dict()
    with pytest.raises(BilliardsError, match="выберите лузу"):
        game.shoot(101, 0, 0.4)
    assert game.to_dict() == before
    game.match.pop("rules_version")
    outcome(monkeypatch, 8, (8,))
    game.shoot(101, 0, 0.4)
    assert game.match["result"]["winners"] == [101]


def test_break_is_open_and_eight_is_respot_not_loss(monkeypatch):
    game = table()
    outcome(monkeypatch, 1, (1, 8))
    game.shoot(101, 0, 0.8)
    assert game.phase == "playing" and game.actor == 101 and game.groups == {}
    eight = next(b for b in game.balls if b["id"] == 8)
    assert not eight["pocketed"]
    assert all(
        b["id"] == 8
        or b["pocketed"]
        or math.hypot(b["x"] - eight["x"], b["y"] - eight["y"]) >= 28.7
        for b in game.balls
    )
    game.last_shot["available_at"] = 0
    outcome(monkeypatch, 9, (9,))
    game.shoot(101, 0, 0.4)
    assert game.groups == {"101": "stripes", "102": "solids"}


def test_soft_break_foul_and_animation_does_not_consume_turn(monkeypatch):
    game = table()
    outcome(monkeypatch, 1, rails=(1,))
    game.shoot(101, 0, 0.05)
    assert game.actor == 102 and game.ball_in_hand
    assert "четыре" in game.last_shot["foul"]
    assert (
        game.deadline
        >= game.last_shot["available_at"] + game.rules["turn_seconds"] - 0.01
    )
    with pytest.raises(BilliardsError, match="остановки"):
        game.shoot(102, 0, 0.5, 245, 250)
    assert game.public_state(102)["server_time"] > 0


@pytest.mark.parametrize(
    "values",
    [
        {"angle": float("nan")},
        {"power": float("inf")},
        {"power": True},
        {"called_pocket": True},
        {"called_pocket": 6},
    ],
)
def test_invalid_shot_rejected_without_state_change(values):
    game = table()
    before = game.to_dict()
    with pytest.raises(BilliardsError):
        game.shoot(101, **({"angle": 0, "power": 0.5} | values))
    assert game.to_dict() == before


def test_physics_deterministic_cushions_and_finite_stopped_positions():
    balls = fresh_balls("eight")
    other = copy.deepcopy(balls)
    first = simulate(balls, 0, 0.85)
    second = simulate(other, 0, 0.85)
    assert first == second and balls == other
    assert first["first_contact"] == 1
    assert first["frames"][0][0][1:3] == [245.0, 250.0]
    assert len(first["frames"]) <= 271
    for ball in balls:
        assert math.isfinite(ball["x"]) and math.isfinite(ball["y"])
        if not ball["pocketed"]:
            assert 0 <= ball["x"] <= 1000 and 0 <= ball["y"] <= 500
            assert math.hypot(ball["vx"], ball["vy"]) <= billiards.STOP_SPEED


def test_expired_turn_rejects_shot_before_timeout_worker_runs():
    game = table()
    game.deadline = 1
    before = game.to_dict()
    with pytest.raises(BilliardsError, match="Время хода истекло"):
        game.shoot(101, 0, 0.5)
    assert game.to_dict() == before


def test_http_wallet_settlement_once_and_no_client_positions(
    make_user, login_as, monkeypatch
):
    make_user(101, balance=100)
    make_user(102, balance=100)
    client = login_as(101)
    created = client.post(
        "/api/billiards/tables", json={"rules": {"mode": "eight", "entry_cents": 1000}}
    ).json()
    tid = created["table_id"]
    path = f"/api/billiards/table/{tid}"
    assert wallet(101) == 100
    client = login_as(102)
    assert client.post(path + "/join").status_code == 200
    state = client.get(path + "/state").json()
    client.post(path + "/ready", json={"revision": state["revision"], "ready": True})
    client = login_as(101)
    state = client.get(path + "/state").json()
    state = client.post(
        path + "/ready", json={"revision": state["revision"], "ready": True}
    ).json()
    state = client.post(path + "/start", json={"revision": state["revision"]}).json()
    assert wallet(101) == 90 and wallet(102) == 90
    # Mutating the durable runtime is only fixture preparation for a final eight.
    game = main.billiards_service.table(tid)
    assigned(game, clear=True)
    with SessionLocal.begin() as db:
        from database import AppRuntime

        db.get(AppRuntime, 1).payload = main.serialize_runtime()
    outcome(monkeypatch, 8, (8,), pocket=2)
    state = client.get(path + "/state").json()
    payload = {
        "revision": state["revision"],
        "angle": 0,
        "power": 0.5,
        "called_pocket": 2,
    }
    bad = client.post(path + "/shoot", json=payload | {"balls": []})
    assert bad.status_code == 400
    result = client.post(path + "/shoot", json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["match"]["result"]["winners"] == [101]
    assert wallet(101) == 110 and wallet(102) == 90
    assert client.post(path + "/shoot", json=payload).status_code == 409
    for _ in range(2):
        assert client.get(path + "/state").status_code == 200
    assert wallet(101) == 110 and wallet(102) == 90
    with SessionLocal() as db:
        assert db.query(GameHistory).filter_by(game_type="billiards").count() == 2


def test_private_membership_and_csrf(make_user, login_as, client):
    make_user(101)
    make_user(102)
    player = login_as(101)
    tid = player.post(
        "/api/billiards/tables", json={"rules": {"mode": "practice"}}
    ).json()["table_id"]
    path = f"/api/billiards/table/{tid}"
    denied = player.post(
        path + "/ready",
        headers={"X-ST9-CSRF": "wrong"},
        json={"revision": 1, "ready": True},
    )
    assert denied.status_code == 403
    player = login_as(102)
    assert player.get(path + "/state").status_code == 403
    assert player.post(path + "/join").status_code == 403
    assert all(
        t["id"] != tid for t in player.get("/api/billiards/tables").json()["tables"]
    )


def test_table_template_and_scripts_parse(make_user, login_as, tmp_path):
    import subprocess
    from test_frontend_scripts import Scripts

    make_user(101, display_name="Alice <img src=x onerror=alert(1)>")
    client = login_as(101)
    tid = client.post(
        "/api/billiards/tables", json={"rules": {"mode": "practice"}}
    ).json()["table_id"]
    page = client.get("/games/billiards/table/" + tid)
    assert page.status_code == 200
    parser = Scripts()
    parser.feed(page.text)
    for block in parser.blocks:
        checked = subprocess.run(
            ["node", "--check"], input=block, text=True, capture_output=True
        )
        assert checked.returncode == 0, checked.stderr
    assert "calledPocket" in page.text and "PoolRenderer" in page.text


def test_poll_omits_only_known_frames_without_mutating_saved_shot(make_user, login_as):
    make_user(101)
    client = login_as(101)
    tid = client.post(
        "/api/billiards/tables", json={"rules": {"mode": "practice"}}
    ).json()["table_id"]
    path = f"/api/billiards/table/{tid}"
    state = client.get(path + "/state").json()
    state = client.post(
        path + "/ready", json={"revision": state["revision"], "ready": True}
    ).json()
    state = client.post(path + "/start", json={"revision": state["revision"]}).json()
    shot = client.post(
        path + "/shoot", json={"revision": state["revision"], "angle": 0, "power": 0.6}
    ).json()["last_shot"]
    assert len(shot["frames"]) > 20
    abbreviated = client.get(path + "/state", params={"known_shot": shot["id"]}).json()[
        "last_shot"
    ]
    assert abbreviated["frames"] is None and abbreviated["id"] == shot["id"]
    full = client.get(path + "/state").json()["last_shot"]
    assert full["frames"] == shot["frames"]
    assert client.get(path + "/state", params={"known_shot": -1}).status_code == 422
