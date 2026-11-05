import json

from database import GameHistory, SessionLocal, User
from horse_racing import HORSE_BY_ID, MAX_CENTS, HorseRacingService


class FirstHorseWins:
    @staticmethod
    def sample(population, count):
        return list(population)[:count]


def test_restore_pauses_the_open_betting_window():
    service = HorseRacingService(clock=lambda: 100)
    race = service.ensure(100)
    snapshot = service.to_dict()

    restored = HorseRacingService(clock=lambda: 200)
    restored.restore(snapshot, pause=60)

    assert restored.current["created_at"] == race["created_at"] + 60
    assert restored.current["betting_ends_at"] == race["betting_ends_at"] + 60


def test_settlement_defers_overflow_then_pays_when_headroom_returns(make_user):
    make_user(101, balance=100)
    service = HorseRacingService(clock=lambda: 0, random_source=FirstHorseWins())

    with SessionLocal() as db:
        user = db.get(User, 101)
        race = service.ensure(0)
        service.place_bet(user, 1, 1_000, db, race["number"], now=0)
        user.balance = MAX_CENTS / 100
        db.commit()

        service.advance(db, now=18)
        result = service.advance(db, now=30)
        state = service.state(user, db, now=30)

        assert result["phase"] == "finished"
        assert result["result"]["winner_id"] == HORSE_BY_ID[1]["id"]
        assert state["my_result"]["pending_payout_cents"] == 1_000
        assert state["my_result"]["paid_payout_cents"] == 0
        assert state["my_pending_payout_cents"] == 1_000

        restored = HorseRacingService(clock=lambda: 31)
        restored.restore(json.loads(json.dumps(service.to_dict())))
        assert restored.pending_payouts == service.pending_payouts

        user.balance = (MAX_CENTS - 1_000) / 100
        restored.pay_pending(db)
        db.commit()

        assert db.get(User, 101).balance == MAX_CENTS / 100
        assert restored.pending_payouts == []
        deferred = (
            db.query(GameHistory)
            .filter_by(user_id=101, game_type="horse_racing", result="deferred_payout")
            .one()
        )
        assert deferred.amount == 10


def test_horse_racing_errors_return_their_detail(login_as, make_user):
    make_user(101)
    client = login_as(101)
    state = client.get("/api/horse-racing/state").json()

    response = client.post(
        "/api/horse-racing/bet",
        json={
            "race_number": state["race_number"],
            "horse_id": 99,
            "amount_cents": 1_000,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Выберите лошадь из списка"


def test_http_mixed_winners_loser_exact_payout_restart_and_no_double_credit(
    make_user, login_as, monkeypatch
):
    import main
    from conftest import session_cookie, saved_state, wallet

    clock = [100.0]
    monkeypatch.setattr(main.horse_racing_service, "clock", lambda: clock[0])
    monkeypatch.setattr(main.horse_racing_service, "random", FirstHorseWins())
    for uid in (101, 102, 103):
        make_user(uid, balance=100)
    client = login_as(101)
    number = client.get("/api/horse-racing/state").json()["race_number"]
    for uid, horse, amount in ((101, 1, 1000), (102, 1, 2000), (103, 2, 1001)):
        client.cookies.set("session", session_cookie(uid))
        data = {"race_number": number, "horse_id": horse, "amount_cents": amount}
        assert client.post("/api/horse-racing/bet", json=data).status_code == 200
        assert client.post("/api/horse-racing/bet", json=data).json()["duplicate"]
    assert [wallet(uid) for uid in (101, 102, 103)] == [90, 80, 89.99]
    clock[0] = 118
    assert client.get("/api/horse-racing/state").json()["phase"] == "running"
    main.restore_runtime(json.dumps(saved_state()))
    clock[0] = 127
    result = client.get("/api/horse-racing/state")
    assert result.status_code == 200, result.text
    assert result.json()["my_result"]["payout_cents"] == 0
    assert (
        "payouts" not in result.json()["result"]
        and "ticket_results" not in result.json()["result"]
    )
    assert [wallet(uid) for uid in (101, 102, 103)] == [103.34, 106.67, 89.99]
    main.restore_runtime(json.dumps(saved_state()))
    for _ in range(3):
        assert client.get("/api/horse-racing/state").status_code == 200
    assert [wallet(uid) for uid in (101, 102, 103)] == [103.34, 106.67, 89.99]
    with SessionLocal() as db:
        rows = db.query(GameHistory).filter_by(game_type="horse_racing").all()
        assert len(rows) == 3
        assert {r.result for r in rows} == {"won", "lost"}
        assert sum(round(r.amount * 100) for r in rows) == 0
