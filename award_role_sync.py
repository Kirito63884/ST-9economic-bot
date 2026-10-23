"""Durable restoration of earned Discord roles; never repeats awards or payments."""

from datetime import timedelta
from sqlalchemy import Column, Integer, String, DateTime, or_
from database import Base, User

CHECK_SECONDS = 15 * 60
NETWORK_SECONDS = 60


class AwardRoleSync(Base):
    __tablename__ = "award_role_sync"
    discord_id = Column(String(20), primary_key=True)
    revision = Column(Integer, nullable=False, default=1)
    applied_revision = Column(Integer, nullable=False, default=0)
    requested_at = Column(DateTime, nullable=False)
    checked_at = Column(DateTime, nullable=True)
    retry_at = Column(DateTime, nullable=True)
    lease_until = Column(DateTime, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    restored = Column(Integer, nullable=False, default=0)
    last_error = Column(String(100), nullable=True)


def earned_roles(db, sid):
    from medals import MedalAward, ROLE_IDS, MANUAL_ROLES
    from award_revocation import active_awards

    mapping = {**ROLE_IDS, **MANUAL_ROLES}
    return {
        str(mapping[code])
        for (code,) in active_awards(db)
        .filter_by(discord_id=sid)
        .with_entities(MedalAward.medal)
        if code in mapping
    }


def request_sync(db, sid, now=None):
    from medals import utcnow

    now = now or utcnow()
    if (
        not db.query(User.id)
        .filter(User.discord_id == sid, User.deleted_at.is_(None))
        .first()
    ):
        return False
    if not earned_roles(db, sid):
        return False
    row = db.get(AwardRoleSync, sid)
    if row is None:
        db.add(AwardRoleSync(discord_id=sid, requested_at=now, retry_at=now))
    else:
        row.revision += 1
        row.requested_at = now
        row.retry_at = now
    db.flush()
    return True


def queue_all(db):
    from medals import MedalAward

    ids = [
        sid
        for (sid,) in db.query(User.discord_id)
        .join(MedalAward, User.discord_id == MedalAward.discord_id)
        .filter(User.deleted_at.is_(None))
        .distinct()
    ]
    return sum(request_sync(db, sid) for sid in ids)


def member_event(db, data):
    roles = data.get("roles")
    sid = str(data.get("user", {}).get("id", ""))
    if (
        not sid.isdigit()
        or not isinstance(roles, list)
        or any(not str(r).isdigit() for r in roles)
    ):
        return False
    from award_revocation import member_removed_roles_check

    member_removed_roles_check(db, sid, roles)
    if earned_roles(db, sid) - {str(r) for r in roles}:
        return request_sync(db, sid)
    return False


def claim(db, now):
    row = (
        db.query(AwardRoleSync)
        .join(User, User.discord_id == AwardRoleSync.discord_id)
        .filter(
            User.deleted_at.is_(None),
            or_(
                AwardRoleSync.revision > AwardRoleSync.applied_revision,
                AwardRoleSync.checked_at.is_(None),
                AwardRoleSync.checked_at <= now - timedelta(seconds=CHECK_SECONDS),
            ),
            or_(AwardRoleSync.retry_at.is_(None), AwardRoleSync.retry_at <= now),
            or_(AwardRoleSync.lease_until.is_(None), AwardRoleSync.lease_until <= now),
        )
        .order_by(AwardRoleSync.requested_at, AwardRoleSync.discord_id)
        .first()
    )
    if row is None:
        return None
    row.attempts += 1
    row.lease_until = now + timedelta(minutes=5)
    return {
        "sid": row.discord_id,
        "revision": row.revision,
        "attempts": row.attempts,
        "roles": earned_roles(db, row.discord_id),
    }


def finish(db, job, now, restored=0, error=None):
    row = db.get(AwardRoleSync, job["sid"])
    if row is None:
        return
    row.lease_until = None
    row.last_error = error
    row.restored += restored
    if error:
        if error in ("role_permissions", "discord_unavailable", "discord_timeout"):
            from automation_control import queue_alert

            queue_alert(
                db,
                "award_role_sync",
                "Discord",
                "Не удалось сверить роли наград: " + error + ". Бот повторит попытку.",
            )
        seconds = (
            3600
            if error
            in (
                "member_missing",
                "member_ineligible",
                "role_permissions",
                "role_or_member_missing",
            )
            else min(3600, 30 * 2 ** min(job["attempts"], 7))
        )
        row.retry_at = now + timedelta(seconds=seconds)
        # A failed recipient must not block other players.
        row.requested_at = now
    else:
        from automation_control import resolve_alerts

        resolve_alerts(db, "award_role_sync")
        row.checked_at = now
        row.applied_revision = job["revision"]
        row.attempts = 0
        row.retry_at = (
            now
            if row.revision != job["revision"]
            else now + timedelta(seconds=CHECK_SECONDS)
        )
        if row.revision == job["revision"]:
            row.requested_at = now


def status(db):
    rows = (
        db.query(AwardRoleSync)
        .join(User, User.discord_id == AwardRoleSync.discord_id)
        .filter(User.deleted_at.is_(None))
        .all()
    )
    return {
        "players": len(rows),
        "pending": sum(r.revision > r.applied_revision for r in rows),
        "failed": sum(bool(r.last_error) for r in rows),
        "restored": sum(r.restored for r in rows),
        "checked": sum(r.checked_at is not None for r in rows),
        "errors": sorted({r.last_error for r in rows if r.last_error}),
    }
