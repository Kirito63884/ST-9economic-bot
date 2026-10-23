"""Administrator revocation overrides permanent ownership and Discord role repair."""

from datetime import timedelta
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Text,
    UniqueConstraint,
    and_,
    exists,
    or_,
)
from database import Base, GameHistory


class AwardRevocation(Base):
    __tablename__ = "award_revocations"
    __table_args__ = (UniqueConstraint("discord_id", "medal"),)
    id = Column(Integer, primary_key=True)
    discord_id = Column(String(20), nullable=False, index=True)
    medal = Column(String(40), nullable=False)
    award_id = Column(Integer, nullable=False)
    actor_id = Column(String(20), nullable=False)
    actor_name = Column(String(100), nullable=False)
    reason = Column(Text, nullable=False)
    revoked_at = Column(DateTime, nullable=False)
    checked_at = Column(DateTime, nullable=True)
    removed_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    retry_at = Column(DateTime, nullable=True)
    lease_until = Column(DateTime, nullable=True)
    last_error = Column(String(100), nullable=True)


def active_awards(db):
    from medals import MedalAward

    return db.query(MedalAward).filter(
        ~exists().where(
            and_(
                AwardRevocation.discord_id == MedalAward.discord_id,
                AwardRevocation.medal == MedalAward.medal,
            )
        )
    )


def is_revoked(db, sid, code):
    return (
        db.query(AwardRevocation.id).filter_by(discord_id=sid, medal=code).first()
        is not None
    )


def revoke(db, user, actor, code, reason):
    from medals import MedalAward, utcnow
    from award_role_names import award_name

    old = (
        db.query(AwardRevocation)
        .filter_by(discord_id=user.discord_id, medal=code)
        .first()
    )
    if old:
        return old, False
    award = (
        db.query(MedalAward).filter_by(discord_id=user.discord_id, medal=code).first()
    )
    if award is None:
        return None, False
    row = AwardRevocation(
        discord_id=user.discord_id,
        medal=code,
        award_id=award.id,
        actor_id=actor.discord_id,
        actor_name=(actor.display_name or actor.username)[:100],
        reason=reason or "Снята администратором",
        revoked_at=utcnow(),
    )
    db.add(row)
    db.add(
        GameHistory(
            user_id=user.id,
            game_type="medals",
            result="medal_revoked",
            amount=0,
            details=f"{award_name(db, code)}: снята администратором {row.actor_name}. Причина: {row.reason}.",
        )
    )
    db.flush()
    return row, True


def member_removed_roles_check(db, sid, roles):
    if not isinstance(roles, list):
        return
    from medals import ROLE_IDS, MANUAL_ROLES, utcnow

    mapping = {**ROLE_IDS, **MANUAL_ROLES}
    present = {str(r) for r in roles}
    for row in db.query(AwardRevocation).filter_by(discord_id=sid):
        if str(mapping.get(row.medal, "")) in present:
            row.removed_at = None
            row.retry_at = utcnow()


def claim_removal(db, now):
    from medals import ROLE_IDS, MANUAL_ROLES

    row = (
        db.query(AwardRevocation)
        .filter(
            or_(
                AwardRevocation.checked_at.is_(None),
                AwardRevocation.removed_at.is_(None),
                AwardRevocation.checked_at <= now - timedelta(minutes=15),
            ),
            or_(AwardRevocation.retry_at.is_(None), AwardRevocation.retry_at <= now),
            or_(
                AwardRevocation.lease_until.is_(None),
                AwardRevocation.lease_until <= now,
            ),
        )
        .order_by(AwardRevocation.checked_at, AwardRevocation.id)
        .first()
    )
    if row is None:
        return None
    row.attempts += 1
    row.lease_until = now + timedelta(minutes=5)
    return {
        "id": row.id,
        "sid": row.discord_id,
        "code": row.medal,
        "role": {**ROLE_IDS, **MANUAL_ROLES}.get(row.medal),
        "attempts": row.attempts,
    }


def finish_removal(db, job, now, error=None):
    row = db.get(AwardRevocation, job["id"])
    if row is None:
        return
    row.lease_until = None
    row.checked_at = now
    row.last_error = error
    if error:
        if error in ("role_permissions", "discord_unavailable", "discord_timeout"):
            from automation_control import queue_alert

            queue_alert(
                db,
                "award_role_removal",
                "Discord",
                "Не удалось снять отозванную роль награды: "
                + error
                + ". Бот повторит попытку.",
            )
        row.retry_at = now + timedelta(
            seconds=min(3600, 30 * 2 ** min(job["attempts"], 7))
        )
    else:
        from automation_control import resolve_alerts

        resolve_alerts(db, "award_role_removal")
        row.removed_at = now
        row.retry_at = now + timedelta(minutes=15)
        row.attempts = 0


def revoked_data(db, sid):
    return {
        r.medal: {
            "reason": r.reason,
            "actor": r.actor_name,
            "date": r.revoked_at.isoformat() + "Z",
            "role_pending": r.removed_at is None,
            "error": r.last_error,
        }
        for r in db.query(AwardRevocation).filter_by(discord_id=sid)
    }


def removal_status(db):
    rows = db.query(AwardRevocation)
    return {
        "total": rows.count(),
        "pending": rows.filter(AwardRevocation.removed_at.is_(None)).count(),
        "failed": rows.filter(AwardRevocation.last_error.is_not(None)).count(),
    }
