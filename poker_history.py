"""Finished hands are stored atomically with their wallet settlement."""

import copy
import json
from datetime import datetime

from database import PokerHand, PokerHandPlayer, User


def archive_hand(db, table):
    if not table.hand_payout_done or getattr(table, "hand_archived", False):
        return
    if table.phase != "showdown" and not getattr(table, "hand_players", []):
        return
    existing = (
        db.query(PokerHand)
        .filter_by(table_id=table.id, hand_number=table.hand_number)
        .first()
    )
    if existing:
        table.hand_archived = True
        return
    original = {p["user_id"]: p for p in getattr(table, "hand_players", [])}
    live_count = len([p for p in table.players if not p["folded"]])
    players = []
    for p in table.players:
        if not p.get("participated"):
            continue
        snapshot = original.get(p["user_id"], {})
        players.append(
            {
                "user_id": p["user_id"],
                "display_name": p["display_name"],
                "cards": copy.deepcopy(snapshot.get("cards", p["cards"])),
                "revealed": live_count > 1 and not p["folded"],
                "folded": p["folded"],
                "invested": round(p["invested"], 2),
                "returned": round(p.get("returned", 0), 2),
                "net": round(p.get("returned", 0) - p["invested"], 2),
            }
        )
        payouts = [
            x for x in getattr(table, "payouts", []) if x["user_id"] == p["user_id"]
        ]
        players[-1]["won_pot"] = bool(payouts)
        players[-1]["hand"] = ", ".join(
            sorted({x.get("hand", "") for x in payouts if x.get("hand")})
        )
        if db.get(User, p["user_id"]) is None:
            players[-1].update(user_id=None, display_name="Удалённый игрок", cards=[])
    payload = {
        "table_id": table.id,
        "hand_number": table.hand_number,
        "started_at": getattr(table, "hand_started_at", None),
        "finished_at": datetime.now().isoformat(),
        "pot": round(table.last_pot, 2),
        "community": copy.deepcopy(table.community),
        "players": players,
        "actions": copy.deepcopy(getattr(table, "hand_actions", [])),
        "partial": not bool(original),
    }
    row = PokerHand(
        table_id=table.id,
        hand_number=table.hand_number,
        payload=json.dumps(payload, ensure_ascii=False, allow_nan=False),
    )
    db.add(row)
    db.flush()
    for p in players:
        if p["user_id"] is not None:
            db.add(PokerHandPlayer(hand_id=row.id, user_id=p["user_id"]))
    missing = {p["user_id"] for p in table.players} - {p["user_id"] for p in players}
    if missing:
        for action in payload["actions"]:
            if action.get("user_id") in missing:
                action["user_id"] = None
        row.payload = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    table.hand_archived = True


def visible_hand(row, viewer):
    data = json.loads(row.payload)
    if not viewer.is_admin:
        for p in data["players"]:
            if p["user_id"] != viewer.id and not p["revealed"]:
                p["cards"] = [{"value": "?", "suit": "?"}] * len(p["cards"])
    data["id"] = row.id
    data["created_at"] = datetime.fromisoformat(data["finished_at"])
    return data


def anonymize_player(db, uid):
    rows = (
        db.query(PokerHand)
        .join(PokerHandPlayer, PokerHand.id == PokerHandPlayer.hand_id)
        .filter(PokerHandPlayer.user_id == uid)
        .all()
    )
    for row in rows:
        data = json.loads(row.payload)
        for p in data["players"]:
            if p["user_id"] == uid:
                p.update(user_id=None, display_name="Удалённый игрок", cards=[])
        for action in data["actions"]:
            if action.get("user_id") == uid:
                action["user_id"] = None
        row.payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
    db.query(PokerHandPlayer).filter_by(user_id=uid).delete(synchronize_session=False)
