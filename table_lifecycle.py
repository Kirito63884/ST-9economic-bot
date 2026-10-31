"""Eight-hour inactivity, independent of polling and automatic game timers.

All functions run within the existing durable wallet/runtime transaction.
Unfinished expired bets are returned exactly once before their table is removed.
"""

import time
from decimal import Decimal
from datetime import datetime
from database import GameHistory, User

IDLE_SECONDS = 8 * 60 * 60
MAX_CENTS = 100_000_000_000_000


def initialise(table):
    if hasattr(table, "last_player_activity"):
        return
    stamp = getattr(table, "updated_at", None) or getattr(
        table, "last_action_time", None
    )
    if stamp is None:
        stamp = getattr(table, "created_at", time.time())
        if isinstance(stamp, datetime):
            stamp = stamp.timestamp()
    table.last_player_activity = float(stamp)


def expired(table, now=None):
    initialise(table)
    return (
        time.time() if now is None else now
    ) - table.last_player_activity >= IDLE_SECONDS


def request_activity(
    method, path, uid, poker, durak, billiards, domino=None, chess=None,
    battleship=None, draughts=None, monopoly=None
):
    if method != "POST" or not uid:
        return
    parts = path.strip("/").split("/")
    game = tid = None
    if (
        len(parts) >= 5
        and parts[:1] == ["api"]
        and parts[1] in ("poker", "durak", "billiards", "domino", "chess", "battleship", "draughts", "monopoly")
        and parts[2] == "table"
    ):
        game, tid = parts[1], parts[3]
    elif len(parts) == 4 and parts[:3] == ["api", "poker", "join-table"]:
        game, tid = "poker", parts[3]
    if not game:
        return
    games = {"poker": poker, "durak": durak.tables, "billiards": billiards.tables}
    if domino is not None:
        games["domino"] = domino.tables
    if chess is not None:
        games["chess"] = chess.tables
    if battleship is not None:
        games["battleship"] = battleship.tables
    if draughts is not None:
        games["draughts"] = draughts.tables
    if monopoly is not None:
        games["monopoly"] = monopoly.tables
    if game not in games:
        return
    tables = games[game]
    table = tables.get(tid)
    if table and any(
        p.get("user_id" if game == "poker" else "id") == uid for p in table.players
    ):
        table.last_player_activity = time.time()


def cents(value):
    amount = Decimal(str(value)) * 100
    if not amount.is_finite() or amount != amount.to_integral_value() or amount < 0:
        raise ValueError("Некорректная сумма незавершённой ставки")
    return int(amount)


def refund(db, game, table_id, contributions):
    changes = []
    for uid, amount in contributions:
        if type(amount) is not int or amount < 0:
            raise ValueError("Некорректный взнос")
        if not amount:
            continue
        user = db.get(User, uid)
        if not user:
            raise ValueError(
                "Для возврата ставки требуется восстановить профиль участника"
            )
        balance = Decimal(str(user.balance)) * 100
        if not balance.is_finite() or balance + amount > MAX_CENTS:
            raise ValueError("Недостаточно места на балансе для возврата ставки")
        changes.append((user, amount))
    if len({user.id for user, _ in changes}) != len(changes):
        raise ValueError("Повторяющийся участник")
    for user, amount in changes:
        user.balance = float((Decimal(str(user.balance)) * 100 + amount) / 100)
        db.add(
            GameHistory(
                user_id=user.id,
                game_type=game,
                result="push",
                amount=0,
                details=f"Стол {table_id} закрыт после 8 часов без действий. Возвращён взнос {amount / 100:.2f} БК; партия отменена.",
            )
        )


def expire_match(service, tid, db, game, now=None):
    table = service.tables.get(tid)
    if not table or not expired(table, now):
        return False
    match = table.match
    if match and match.get("result"):
        service.settle(table, db)
    elif match and not match.get("settled"):
        participants = match["participants"]
        entry = match["entry_cents"]
        if (
            type(entry) is not int
            or entry < 0
            or match["bank_cents"] != entry * len(participants)
        ):
            raise ValueError("Банк стола не совпадает со взносами")
        refund(db, game, tid, [(p["id"], entry) for p in participants])
        match["settled"] = True
    service.tables.pop(tid, None)
    return True


def expire_poker(tables, requests, tid, db, sync_balances, now=None):
    table = tables.get(tid)
    if not table or not expired(table, now):
        return False
    # Sync any pending delta first. A completed hand retains its actual payouts
    # and archive; an unfinished hand returns only the chips still in its pot.
    sync_balances(db, table)
    if not table.hand_payout_done:
        contributions = [
            (p["user_id"], cents(p.get("invested", 0))) for p in table.players
        ]
        if sum(amount for _, amount in contributions) != cents(table.pot):
            raise ValueError("Банк покера не совпадает со взносами")
        refund(db, "poker", tid, contributions)
    tables.pop(tid, None)
    requests.pop(tid, None)
    return True
