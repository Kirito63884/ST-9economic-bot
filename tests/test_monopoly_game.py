"""Classic rules, pending obligations and hidden information; no site credentials."""

import json
import random

import pytest

from monopoly_board import BOARD, DECKS, GROUPS, PROPERTIES
from monopoly_game import MonopolyError, MonopolyTable


def game(n=3):
    table = MonopolyTable("classic", 1, "Игрок 1")
    for uid in range(2, n + 1):
        table.add_player(uid, f"Игрок {uid}")
    for p in table.players:
        p["ready"] = True
    table.start()
    table.players.sort(key=lambda p: p["id"])
    table.actor = 1
    return table


def roll(monkeypatch, table, dice):
    monkeypatch.setattr(MonopolyTable, "roll_dice", staticmethod(lambda: dice))
    table.action(table.actor, "roll")


def own(table, uid, *indices, levels=None):
    for index in indices:
        table.estate[str(index)]["owner"] = uid
        if levels is not None:
            table.estate[str(index)]["level"] = levels
    table.bank["houses"] = 32 - sum(
        s["level"] for s in table.estate.values() if s["level"] < 5
    )
    table.bank["hotels"] = 12 - sum(s["level"] == 5 for s in table.estate.values())
    table.validate()


def draw(table, uid, deck, effect, **match):
    key = next(
        i
        for i, card in enumerate(DECKS[deck])
        if card["effect"] == effect and all(card.get(k) == v for k, v in match.items())
    )
    table.decks[deck].remove(key)
    table.decks[deck].insert(0, key)
    table._prepend({"kind": "draw", "uid": uid, "deck": deck})
    table._drain()
    table.validate()


@pytest.mark.parametrize("n", range(2, 11))
def test_seats_first_roll_and_separate_money(n):
    t = game(n)
    assert len(t.match["participants"]) == n
    assert sum(p["cash"] for p in t.players) == n * 1500
    assert len({p["token"] for p in t.players}) == n
    assert t.match["entry_cents"] == 0
    assert len(BOARD) == 40 and len(PROPERTIES) == 28


def test_opening_tie_rolls_only_tied_players(monkeypatch):
    rolls = iter([[5, 5], [5, 5], [1, 2], [2, 2], [6, 6]])
    monkeypatch.setattr(MonopolyTable, "roll_dice", staticmethod(lambda: next(rolls)))
    t = game()
    assert t.opening_rolls == [
        {1: [5, 5], 2: [5, 5], 3: [1, 2]},
        {1: [2, 2], 2: [6, 6]},
    ]
    assert [p["id"] for p in t.match["participants"]] == [2, 3, 1]


def test_go_double_and_third_double_jail(monkeypatch):
    t = game()
    t.player(1)["pos"] = 38
    own(t, 1, 1, 6)
    # Land on an owned street, crossing Start once.
    roll(monkeypatch, t, [1, 2])
    assert t.player(1)["pos"] == 1 and t.player(1)["cash"] == 1700
    assert t.turn_action == "end"
    t.turn_action = "roll"
    t.doubles = 2
    before = t.player(1)["cash"]
    roll(monkeypatch, t, [6, 6])
    assert t.player(1)["jail"] and t.player(1)["pos"] == 10
    assert t.player(1)["cash"] == before and t.turn_action == "end"


def test_mandatory_auction_includes_decliner_and_jail(monkeypatch):
    t = game()
    t.player(2)["jail"] = True
    t.player(2)["pos"] = 10
    roll(monkeypatch, t, [1, 2])
    assert t.pending == {"type": "buy", "uid": 1, "property": 3}
    t.action(1, "decline")
    assert set(t.pending["eligible"]) == {1, 2, 3}
    t.action(1, "bid", {"amount": 1})
    t.action(2, "bid", {"amount": 12})
    t.action(3, "pass_bid")
    t.action(1, "pass_bid")
    assert t.pending is None and t.estate["3"]["owner"] == 2
    assert t.player(2)["cash"] == 1488
    assert t.actor == 1 and t.turn_action == "end"


def test_rent_all_types_mortgaged_full_set():
    t = game()
    own(t, 1, 1, 3, 5, 15, 12, 28)
    assert t.rent(1, 7) == 4
    t.action(1, "mortgage", {"property": 3})
    assert t.rent(1, 7) == 4 and t.rent(3, 7) == 0
    assert t.rent(5, 7) == 50 and t.rent(5, 7, 2) == 100
    assert t.rent(12, 7) == 70
    t.estate["28"]["owner"] = None
    assert t.rent(12, 7) == 28
    assert t.rent(12, 7, utility_multiplier=10) == 70


def test_build_sell_evenly_hotel_and_mortgage():
    t = game()
    own(t, 1, 1, 3)
    t.player(1)["cash"] = 5000
    t.action(1, "build", {"property": 1})
    before = t.to_dict()
    with pytest.raises(MonopolyError):
        t.action(1, "build", {"property": 1})
    assert t.to_dict() == before
    with pytest.raises(MonopolyError):
        t.action(1, "mortgage", {"property": 3})
    t.action(1, "build", {"property": 3})
    for _ in range(3):
        for i in (1, 3):
            t.action(1, "build", {"property": i})
    t.action(1, "build", {"property": 1})
    assert t.estate["1"]["level"] == 5 and t.bank == {"houses": 28, "hotels": 11}
    with pytest.raises(MonopolyError):
        t.action(1, "sell", {"property": 3})
    t.action(1, "sell", {"property": 1})
    assert t.bank == {"houses": 24, "hotels": 12}
    t.action(1, "sell_group", {"property": 1})
    assert t.bank == {"houses": 32, "hotels": 12}
    t.action(1, "mortgage", {"property": 1})
    before = t.player(1)["cash"]
    t.action(1, "unmortgage", {"property": 1})
    assert t.player(1)["cash"] == before - 33


def test_scarce_houses_auction_and_hotel_breakdown():
    t = game()
    own(t, 1, 1, 3, levels=5)
    for group in (GROUPS["blue"], GROUPS["pink"]):
        own(t, 2, *group, levels=3 if group == GROUPS["blue"] else 4)
    own(t, 3, *GROUPS["orange"], levels=3)
    # 21 + 9 = 30 houses: two remain, with two eligible builders.
    assert t.bank["houses"] == 2
    t.player(2)["cash"] = 5000
    t.player(3)["cash"] = 5000
    t.action(3, "build", {"property": 16})
    assert t.bank["houses"] == 1
    t.action(3, "build", {"property": 18})
    assert t.pending["kind"] == "houses"
    bidder = t.pending["bidder"]
    target = next(
        i
        for i in t.owned(bidder)
        if t.buildable(bidder, i) and t.building_kind(i) == "houses"
    )
    t.action(bidder, "bid", {"amount": 1, "property": target})
    while t.pending:
        t.action(t.pending["bidder"], "pass_bid")
    assert t.bank["houses"] == 0
    with pytest.raises(MonopolyError):
        t.action(1, "sell", {"property": 1})
    t.action(1, "sell_group", {"property": 1})
    assert t.bank["hotels"] == 12


def test_debt_waits_for_liquidation_and_continues():
    t = game()
    own(t, 1, 5, 15)
    t.player(1)["cash"] = 10
    t._prepend(
        {"kind": "pay", "uid": 1, "creditor": 2, "amount": 150, "reason": "тест"}
    )
    t._drain()
    t.action(1, "mortgage", {"property": 5})
    assert t.pending["type"] == "debt" and t.player(2)["cash"] == 1500
    with pytest.raises(MonopolyError):
        t.action(1, "bankrupt")
    t.action(1, "mortgage", {"property": 15})
    assert (
        t.pending is None and t.player(1)["cash"] == 60 and t.player(2)["cash"] == 1650
    )


@pytest.mark.parametrize("double", [True, False])
def test_jail_third_roll_and_double_has_no_bonus(monkeypatch, double):
    t = game()
    t._jail(1)
    t.turn_action = "roll"
    t.player(1)["jail_attempts"] = 2
    dice = [2, 2] if double else [2, 3]
    roll(monkeypatch, t, dice)
    assert not t.player(1)["jail"] and t.player(1)["pos"] == 10 + sum(dice)
    assert t.player(1)["cash"] == 1500 - (0 if double else 50)
    assert t.turn_action == "end"


def test_third_failed_jail_roll_debt_resumes_after_payment(monkeypatch):
    t = game()
    own(t, 1, 5)
    t.player(1)["cash"] = 0
    t._jail(1)
    t.turn_action = "roll"
    t.player(1)["jail_attempts"] = 2
    roll(monkeypatch, t, [1, 2])
    assert t.pending["type"] == "debt" and t.player(1)["pos"] == 10
    t.action(1, "mortgage", {"property": 5})
    assert (
        not t.player(1)["jail"]
        and t.player(1)["pos"] == 13
        and t.pending["type"] == "buy"
    )
    assert t.player(1)["cash"] == 50


def test_gooj_card_held_trade_return_and_restore():
    t = game()
    draw(t, 1, "chance", "jail_card")
    assert len(t.decks["chance"]) == 15 and t.player(1)["cards"] == ["chance"]
    give = {"cash": 0, "properties": [], "cards": ["chance"]}
    take = {"cash": 20, "properties": [], "cards": []}
    t.action(1, "trade_offer", {"to": 2, "give": give, "take": take})
    restored = MonopolyTable.from_dict(json.loads(json.dumps(t.to_dict())))
    restored.action(2, "trade_accept", {"offer_id": t.offers[0]["id"]})
    restored.actor = 2
    restored._jail(2)
    restored.turn_action = "roll"
    restored.action(2, "jail_card", {"deck": "chance"})
    assert len(restored.decks["chance"]) == 16 and not restored.player(2)["cards"]
    assert not restored.player(2)["jail"]


def test_card_back_three_chains_to_chest_and_no_go_reward():
    t = game()
    t.player(1)["pos"] = 36
    card = next(i for i, c in enumerate(DECKS["chest"]) if c.get("amount") == 100)
    t.decks["chest"].remove(card)
    t.decks["chest"].insert(0, card)
    draw(t, 1, "chance", "back")
    assert t.player(1)["pos"] == 33 and t.player(1)["cash"] == 1600


def test_nearest_utility_roll_and_station_double_rent(monkeypatch):
    t = game()
    own(t, 2, 12, 5)
    t.player(1)["pos"] = 7
    monkeypatch.setattr(MonopolyTable, "roll_dice", staticmethod(lambda: [5, 4]))
    draw(t, 1, "chance", "utility")
    assert t.player(1)["cash"] == 1410 and t.last_rent_dice == [5, 4]
    t.player(1)["pos"] = 36
    draw(t, 1, "chance", "rail")
    assert t.player(1)["pos"] == 5 and t.player(1)["cash"] == 1560


def test_bankruptcy_to_player_with_mortgaged_asset():
    t = game()
    own(t, 1, 5)
    t.action(1, "mortgage", {"property": 5})
    t.player(1)["cash"] = 4
    t._prepend(
        {"kind": "pay", "uid": 1, "creditor": 2, "amount": 999, "reason": "Рента"}
    )
    t._drain()
    t.action(1, "bankrupt")
    assert t.player(1)["bankrupt"] and t.estate["5"]["owner"] == 2
    assert t.player(2)["cash"] == 1504 and t.pending["type"] == "transfer"
    t.action(2, "transfer", {"redeem": False})
    assert t.player(2)["cash"] == 1494 and t.estate["5"]["mortgaged"]
    assert t.actor == 2


def test_bankruptcy_to_bank_auctions_and_last_player_wins():
    t = game()
    own(t, 1, 1, 5)
    t.player(1)["cash"] = 0
    t._prepend({"kind": "pay", "uid": 1, "amount": 999, "reason": "Налог"})
    t._drain()
    t.action(1, "bankrupt")
    assert t.pending["type"] == "auction"
    while t.pending:
        t.action(t.pending["bidder"], "pass_bid")
    assert t.actor == 2 and t.estate["5"]["owner"] is None
    t.leave(3)
    assert t.phase == "finished" and t.match["result"]["winners"] == [2]


@pytest.mark.parametrize("redeem", [False, True])
def test_trade_mortgage_interest_and_immediate_redemption(redeem):
    t = game()
    own(t, 1, 37)
    t.action(1, "mortgage", {"property": 37})
    before = t.player(2)["cash"]
    t.action(
        1,
        "trade_offer",
        {
            "to": 2,
            "give": {"cash": 0, "properties": [37], "cards": []},
            "take": {"cash": 100, "properties": [], "cards": []},
        },
    )
    assert t.offers and not t.public_state(3)["offers"]
    t.action(
        2,
        "trade_accept",
        {"offer_id": t.offers[0]["id"], "redeem": [37] if redeem else []},
    )
    assert t.player(2)["cash"] == before - 100 - 18 - (175 if redeem else 0)
    assert t.estate["37"]["mortgaged"] is (not redeem)


def test_trade_stale_properties_expiry_and_forgery(monkeypatch):
    t = game()
    own(t, 1, 1)
    payload = {
        "to": 2,
        "give": {"cash": 0, "properties": [1], "cards": []},
        "take": {"cash": 100, "properties": [], "cards": []},
    }
    t.action(1, "trade_offer", payload)
    offer = t.offers[0]["id"]
    t.action(1, "mortgage", {"property": 1})
    with pytest.raises(MonopolyError):
        t.action(2, "trade_accept", {"offer_id": offer})
    for bad in (
        {"cash": True, "properties": [], "cards": []},
        {"cash": 0, "properties": [True], "cards": []},
        {"cash": 0, "properties": [], "cards": [{}]},
    ):
        with pytest.raises(MonopolyError):
            t.action(1, "trade_offer", payload | {"give": bad})
    t.offers[0]["expires"] = 0
    with pytest.raises(MonopolyError):
        t.action(2, "trade_accept", {"offer_id": offer})
    assert "decks" not in t.public_state(1) and "tasks" not in t.public_state(1)
    with pytest.raises(MonopolyError):
        t.action(1, "roll", {"dice": [6, 6]})


def test_many_seeded_games_preserve_inventory_and_turns(monkeypatch):
    rng = random.Random(1283)
    monkeypatch.setattr(
        MonopolyTable,
        "roll_dice",
        staticmethod(lambda: [rng.randint(1, 6), rng.randint(1, 6)]),
    )
    for seats in (2, 3, 10):
        t = game(seats)
        for step in range(3000):
            if t.phase == "finished":
                break
            pending = t.pending
            if pending:
                kind = pending["type"]
                if kind == "buy":
                    uid = pending["uid"]
                    index = pending["property"]
                    t.action(
                        uid,
                        "buy"
                        if t.player(uid)["cash"] >= PROPERTIES[index]["price"] + 150
                        else "decline",
                    )
                elif kind == "auction":
                    uid = pending["bidder"]
                    if (
                        t.player(uid)["cash"] > pending["bid"] + 100
                        and rng.random() < 0.4
                    ):
                        t.action(uid, "bid", {"amount": pending["bid"] + 1})
                    else:
                        t.action(uid, "pass_bid")
                elif kind == "debt":
                    uid = pending["uid"]
                    candidates = [
                        i for i in t.owned(uid) if not t.estate[str(i)]["mortgaged"]
                    ]
                    if candidates:
                        t.action(uid, "mortgage", {"property": candidates[0]})
                    else:
                        t.action(uid, "bankrupt")
                elif kind == "transfer":
                    t.action(pending["uid"], "transfer", {"redeem": False})
            else:
                t.action(t.actor, "roll" if t.turn_action == "roll" else "end_turn")
            t.validate()
            if step % 100 == 0:
                t = MonopolyTable.from_dict(json.loads(json.dumps(t.to_dict())))
        assert t.turn_number > 10


@pytest.mark.parametrize(
    "deck,card_index", [(deck, i) for deck in DECKS for i in range(16)]
)
def test_every_card_effect_keeps_inventory_and_applies_basic_obligation(
    deck, card_index, monkeypatch
):
    """Every physical card is reachable and survives a snapshot roundtrip."""
    t = game()
    uid = 1
    t.player(uid)["pos"] = 7 if deck == "chance" else 2
    for p in t.players:
        p["cash"] = 5000
    card = DECKS[deck][card_index]
    t.decks[deck].remove(card_index)
    t.decks[deck].insert(0, card_index)
    t._prepend({"kind": "draw", "uid": uid, "deck": deck})
    t._drain()
    t.validate()
    effect = card["effect"]
    if effect == "cash":
        assert t.player(uid)["cash"] == 5000 + card["amount"]
    elif effect == "go":
        assert t.player(uid)["cash"] == 5200 and t.player(uid)["pos"] == 0
    elif effect == "jail":
        assert t.player(uid)["jail"] and t.player(uid)["pos"] == 10
    elif effect == "jail_card":
        assert t.player(uid)["cards"] == [deck] and len(t.decks[deck]) == 15
    elif effect == "advance":
        assert t.player(uid)["pos"] == card["to"]
    elif effect == "repairs":
        assert t.player(uid)["cash"] == 5000
    elif effect == "each":
        assert sum(p["cash"] for p in t.players) == 15000
        assert t.player(uid)["cash"] == 5000 + card["amount"] * 2
    restored = MonopolyTable.from_dict(json.loads(json.dumps(t.to_dict())))
    assert restored.public_state(uid)["players"] == t.public_state(uid)["players"]


def test_card_collect_from_each_resumes_after_debtor_mortgage():
    t = game()
    t.player(2)["cash"] = 0
    own(t, 2, 5)
    draw(t, 1, "chest", "each", amount=10)
    assert t.pending["type"] == "debt" and t.pending["uid"] == 2
    t.action(2, "mortgage", {"property": 5})
    assert t.pending is None and t.player(1)["cash"] == 1520
    assert t.player(2)["cash"] == 90 and t.player(3)["cash"] == 1490


def test_snapshot_rejects_corrupt_bank_or_unbalanced_buildings():
    t = game()
    own(t, 1, 1, 3)
    snapshot = t.to_dict()
    snapshot["match"]["bank_cents"] += 1
    with pytest.raises(MonopolyError):
        MonopolyTable.from_dict(snapshot)
    snapshot = t.to_dict()
    snapshot["estate"]["1"]["level"] = 2
    snapshot["bank"]["houses"] -= 2
    with pytest.raises(MonopolyError):
        MonopolyTable.from_dict(snapshot)


def test_doubles_keep_turn_but_third_is_jail_without_movement(monkeypatch):
    t = game()
    roll(monkeypatch, t, [2, 2])
    assert (
        t.player(1)["pos"] == 4
        and t.player(1)["cash"] == 1300
        and t.turn_action == "roll"
    )
    with pytest.raises(MonopolyError):
        t.action(1, "end_turn")
    roll(monkeypatch, t, [3, 3])
    assert (
        t.player(1)["pos"] == 10 and not t.player(1)["jail"] and t.turn_action == "roll"
    )
    roll(monkeypatch, t, [1, 1])
    assert t.player(1)["jail"] and t.player(1)["pos"] == 10 and t.turn_action == "end"
    t.action(1, "end_turn")
    assert t.actor == 2 and t.doubles == 0


def test_free_parking_and_jail_first_failure_have_no_windfall(monkeypatch):
    t = game()
    t.player(1)["pos"] = 17
    roll(monkeypatch, t, [1, 2])
    assert t.player(1)["pos"] == 20 and t.player(1)["cash"] == 1500
    t._jail(1)
    t.turn_action = "roll"
    roll(monkeypatch, t, [1, 2])
    assert t.player(1)["jail_attempts"] == 1 and t.turn_action == "end"
    assert t.player(1)["pos"] == 10 and t.player(1)["cash"] == 1500


@pytest.mark.parametrize(
    "rules",
    [
        {"max_players": 1},
        {"max_players": 11},
        {"max_players": True},
        {"entry_cents": -1},
        {"entry_cents": 0.1},
        {"private": "false"},
        {"winner": 1},
    ],
)
def test_invalid_rules_cannot_create_table(rules):
    with pytest.raises(MonopolyError):
        MonopolyTable("forged", 1, "Игрок", rules)
