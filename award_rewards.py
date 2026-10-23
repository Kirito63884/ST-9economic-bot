"""Atomic, permanent, once-per-achievement Bishopcoin rewards."""

import json
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Text,
    Boolean,
    UniqueConstraint,
)
from database import Base, User, GameHistory

MAX_BALANCE = Decimal("1000000000000")


class AwardRewardPolicy(Base):
    __tablename__ = "award_reward_policy"
    id = Column(Integer, primary_key=True)
    initialized_at = Column(DateTime, nullable=False)
    retroactive = Column(Boolean, nullable=False)
    prices = Column(Text, nullable=False)


class AwardRewardPayment(Base):
    __tablename__ = "award_reward_payments"
    __table_args__ = (UniqueConstraint("discord_id", "medal"),)
    award_id = Column(Integer, primary_key=True)
    discord_id = Column(String(20), nullable=False, index=True)
    medal = Column(String(40), nullable=False)
    amount = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False)
    paid_at = Column(DateTime, nullable=True)
    skipped = Column(Boolean, nullable=False, default=False)
    last_error = Column(String(60), nullable=True)


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def initialize_policy(db, prices, retroactive, now=None):
    """Explicit installer activation; imports never change a player's wallet."""
    from medals import CATALOG, MANUAL_CATALOG
    from distinctions_catalog import ROLE_IDS

    if set(prices) != set(CATALOG) | set(MANUAL_CATALOG) or any(
        type(v) is not int or not 0 < v <= 20000 for v in prices.values()
    ):
        raise ValueError("Invalid achievement reward policy")
    if any(prices[code] != 2000 for code in ROLE_IDS) or type(retroactive) is not bool:
        raise ValueError("Invalid distinction reward or retroactive setting")
    old = db.get(AwardRewardPolicy, 1)
    if old:
        current = json.loads(old.prices)
        if set(current) != set(prices) or any(
            type(v) is not int or not 0 <= v <= 100000 for v in current.values()
        ):
            raise ValueError("Existing reward policy is invalid")
        if old.retroactive != bool(retroactive):
            raise ValueError("Existing retroactive policy differs")
        return old
    row = AwardRewardPolicy(
        id=1,
        initialized_at=now or now_utc(),
        retroactive=bool(retroactive),
        prices=json.dumps(prices, sort_keys=True),
    )
    db.add(row)
    db.flush()
    return row


def read_policy(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        config.get("version") != 1
        or config.get("approved") is not True
        or config.get("retroactive") is not True
    ):
        raise ValueError("Medal reward scale must be confirmed before installation")
    return config["prices"], config["retroactive"]


def prices(db):
    row = db.get(AwardRewardPolicy, 1)
    return json.loads(row.prices) if row else {}


def add_medical_aid_price(db):
    """Extend an active policy without changing any old rate or payment."""
    row = db.get(AwardRewardPolicy, 1)
    if row is None:
        return False
    values = json.loads(row.prices)
    if "medical_aid" in values:
        if values["medical_aid"] != 2000:
            raise ValueError("Unexpected medical aid reward")
        return False
    values["medical_aid"] = 2000
    row.prices = json.dumps(values, sort_keys=True)
    return True


def pay(db, now=None, award_ids=None):
    """Caller holds the same runtime/database write lock used by games and admins."""
    from medals import MedalAward, CATALOG, MANUAL_CATALOG
    from award_role_names import named_catalog
    from award_revocation import active_awards

    now = now or now_utc()
    policy = db.get(AwardRewardPolicy, 1)
    if not policy:
        return
    values = json.loads(policy.prices)
    catalog = named_catalog(db, {**CATALOG, **MANUAL_CATALOG})
    known = {(r.discord_id, r.medal): r for r in db.query(AwardRewardPayment)}
    users = {u.discord_id: u for u in db.query(User).filter(User.deleted_at.is_(None))}
    for award in active_awards(db).order_by(MedalAward.id):
        if award_ids is not None and award.id not in award_ids:
            continue
        if award.medal not in values or award.medal not in catalog:
            continue
        key = (award.discord_id, award.medal)
        row = known.get(key)
        if row is None:
            skip = not policy.retroactive and award.earned_at < policy.initialized_at
            row = AwardRewardPayment(
                award_id=award.id,
                discord_id=award.discord_id,
                medal=award.medal,
                amount=0 if skip else values[award.medal],
                created_at=now,
                skipped=skip,
            )
            db.add(row)
            db.flush()
            known[key] = row
        if row.paid_at or row.skipped:
            continue
        user = users.get(award.discord_id)
        if not user:
            row.last_error = "recipient_inactive"
            continue
        old = Decimal(str(user.balance))
        if not old.is_finite() or not 0 <= old <= MAX_BALANCE:
            row.last_error = "wallet_invalid"
            continue
        new = old + Decimal(row.amount)
        if new > MAX_BALANCE:
            row.last_error = "balance_limit"
            continue
        user.balance = float(new)
        row.paid_at = now
        row.last_error = None
        db.add(
            GameHistory(
                user_id=user.id,
                game_type="medals",
                result="award_reward",
                amount=row.amount,
                details=f"{catalog[award.medal][0]}: +{row.amount} бишопкоинов. Баланс {old} → {new}. Основание: {award.reason}",
            )
        )


def payment_data(row):
    if not row:
        return None
    return {
        "amount": row.amount,
        "paid": row.paid_at is not None,
        "skipped": bool(row.skipped),
        "error": row.last_error,
        "paid_at": row.paid_at.isoformat() + "Z" if row.paid_at else None,
    }
