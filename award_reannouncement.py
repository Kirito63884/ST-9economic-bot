"""One requested replay of old congratulations, without changing award/payment history."""

from datetime import timedelta
from sqlalchemy import Column, Integer, String, DateTime, UniqueConstraint
from database import Base, User

CAMPAIGN = "all-earned-20261007-v1"


class AwardReplayBatch(Base):
    __tablename__ = "award_replay_batches"
    campaign = Column(String(60), primary_key=True)
    created_at = Column(DateTime, nullable=False)
    count = Column(Integer, nullable=False, default=0)


class AwardReplay(Base):
    __tablename__ = "award_replays"
    __table_args__ = (UniqueConstraint("campaign", "award_id"),)
    id = Column(Integer, primary_key=True)
    campaign = Column(String(60), nullable=False)
    award_id = Column(Integer, nullable=False, index=True)
    requested_at = Column(DateTime, nullable=False)
    sent_at = Column(DateTime, nullable=True)
    message_id = Column(String(20), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    retry_at = Column(DateTime, nullable=True)
    lease_until = Column(DateTime, nullable=True)
    last_error = Column(String(100), nullable=True)


def queue_replays(db):
    from medals import MedalAward, CATALOG, MANUAL_CATALOG, utcnow

    batch = db.get(AwardReplayBatch, CAMPAIGN)
    if batch is not None:
        return {"created": False, "awards": batch.count}
    now = utcnow()
    codes = set(CATALOG) | set(MANUAL_CATALOG)
    from award_revocation import active_awards

    awards = (
        active_awards(db)
        .join(User, User.discord_id == MedalAward.discord_id)
        .filter(
            User.deleted_at.is_(None),
            MedalAward.sent_at.is_not(None),
            MedalAward.medal.in_(codes),
        )
        .all()
    )
    db.add(AwardReplayBatch(campaign=CAMPAIGN, created_at=now, count=len(awards)))
    for award in awards:
        db.add(AwardReplay(campaign=CAMPAIGN, award_id=award.id, requested_at=now))
    db.flush()
    return {"created": True, "awards": len(awards)}


def claim_replay(db, now):
    from medals import MedalAward
    from award_role_names import award_name
    from award_revocation import active_awards

    row = (
        db.query(AwardReplay)
        .join(MedalAward, MedalAward.id == AwardReplay.award_id)
        .join(User, User.discord_id == MedalAward.discord_id)
        .filter(
            User.deleted_at.is_(None),
            MedalAward.id.in_(active_awards(db).with_entities(MedalAward.id)),
            AwardReplay.sent_at.is_(None),
            AwardReplay.retry_at.is_(None) | (AwardReplay.retry_at <= now),
            AwardReplay.lease_until.is_(None) | (AwardReplay.lease_until <= now),
        )
        .order_by(AwardReplay.id)
        .first()
    )
    if row is None:
        return None
    award = db.get(MedalAward, row.award_id)
    row.lease_until = now + timedelta(minutes=5)
    row.attempts += 1
    return {
        "id": award.id,
        "replay_id": row.id,
        "discord_id": award.discord_id,
        "medal": award.medal,
        "name": award_name(db, award.medal),
        "reason": award.reason,
        "attempts": row.attempts,
        "reward": None,
        "repeat": True,
        "requested_at": row.requested_at,
        "marker": f"ST9-REPLAY-{row.id}",
        "nonce": "r" + str(row.id),
    }


def finish_replay(db, job, now, message=None, error=None):
    row = db.get(AwardReplay, job["replay_id"])
    if row is None or row.sent_at is not None:
        return
    row.lease_until = None
    row.last_error = error
    if message:
        row.sent_at = now
        row.message_id = message
        row.retry_at = None
    else:
        row.retry_at = now + timedelta(
            seconds=min(3600, 30 * 2 ** min(job["attempts"], 7))
        )


def replay_status(db):
    from medals import MedalAward
    from award_revocation import active_awards

    rows = (
        db.query(AwardReplay)
        .join(MedalAward, MedalAward.id == AwardReplay.award_id)
        .join(User, User.discord_id == MedalAward.discord_id)
        .filter(
            User.deleted_at.is_(None),
            MedalAward.id.in_(active_awards(db).with_entities(MedalAward.id)),
        )
    )
    return {
        "total": rows.count(),
        "sent": rows.filter(AwardReplay.sent_at.is_not(None)).count(),
        "pending": rows.filter(AwardReplay.sent_at.is_(None)).count(),
        "failed": rows.filter(
            AwardReplay.sent_at.is_(None), AwardReplay.last_error.is_not(None)
        ).count(),
    }
