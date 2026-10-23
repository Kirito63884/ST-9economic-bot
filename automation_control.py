"""Configuration, exceptions, identity history and durable administrator alerts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
)

from database import Base, SessionLocal, User


AUTOMATION_ALERT_CHANNEL_ID = "1553858549879148635"


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


DEFAULTS = {
    "source_protocol_enabled": True,
    "source_sqstat_enabled": True,
    "source_battlemetrics_enabled": True,
    "assembly_penalty": 800,
    "assembly_grace_hours": 12,
    "assembly_start": "19:00",
    "assembly_end": "22:00",
    "assembly_coverage_percent": 85,
    "medal_good_protocol_hours": 500,
    "medal_good_voice_hours": 500,
    "medal_combat_hours": 150,
    "medal_combat_i_kd": 3.0,
    "medal_combat_ii_kd": 2.0,
    "medal_combat_iii_kd": 1.5,
    "medal_purple_hours": 50,
    "medal_purple_kd_below": 0.5,
    "weekly_gold_rank": 1,
    "weekly_silver_rank": 2,
    "weekly_bronze_rank": 3,
    "weekly_cmd_rank": 3,
    "weekly_merit_kills_rank": 10,
    "weekly_merit_revive_rank": 8,
    "weekly_merit_wins_rank": 5,
    "weekly_merit_damage_rank": 5,
    "weekly_squad_leader_rank": 3,
    "weekly_anti_tank_rank": 3,
    "weekly_best_crew_rank": 5,
    "weekly_best_knife_rank": 3,
    "weekly_kit_rank": 3,
}


class AutomationSetting(Base):
    __tablename__ = "automation_settings"
    key = Column(String(80), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    actor_id = Column(String(20), nullable=True)


class AutomationException(Base):
    __tablename__ = "automation_exceptions"
    __table_args__ = (UniqueConstraint("user_id", "scope", "target", "event_date"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    scope = Column(String(20), nullable=False)  # award | assembly
    target = Column(String(50), nullable=False)
    event_date = Column(String(10), nullable=False, default="")
    reason = Column(Text, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    actor_id = Column(String(20), nullable=False)
    actor_name = Column(String(100), nullable=False)
    created_at = Column(DateTime, nullable=False)
    disabled_at = Column(DateTime, nullable=True)


class IdentityLinkHistory(Base):
    __tablename__ = "identity_link_history"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    kind = Column(String(12), nullable=False)  # discord | steam
    value = Column(String(50), nullable=False, index=True)
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    actor_id = Column(String(20), nullable=True)
    reason = Column(String(200), nullable=True)


class SystemAlert(Base):
    __tablename__ = "system_alerts"
    id = Column(Integer, primary_key=True)
    alert_key = Column(String(120), nullable=False, index=True)
    source = Column(String(40), nullable=False)
    level = Column(String(12), nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    retry_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    message_id = Column(String(20), nullable=True)
    last_error = Column(String(80), nullable=True)


class DiscordRoleRemoval(Base):
    __tablename__ = "discord_role_removals"
    __table_args__ = (UniqueConstraint("discord_id", "role_id"),)
    id = Column(Integer, primary_key=True)
    discord_id = Column(String(20), nullable=False, index=True)
    role_id = Column(String(20), nullable=False)
    reason = Column(String(200), nullable=False)
    requested_at = Column(DateTime, nullable=False)
    requested_by = Column(String(20), nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    retry_at = Column(DateTime, nullable=True)
    removed_at = Column(DateTime, nullable=True)
    last_error = Column(String(80), nullable=True)


def setting(db, key):
    default = DEFAULTS[key]
    row = db.get(AutomationSetting, key)
    if row is None:
        return default
    try:
        value = json.loads(row.value)
    except (TypeError, ValueError):
        return default
    valid = (
        (type(value) is bool)
        if type(default) is bool
        else (
            type(value) is int
            if type(default) is int
            else (
                type(value) in (int, float)
                if type(default) is float
                else isinstance(value, str)
            )
        )
    )
    return (
        float(value)
        if valid and type(default) is float
        else value
        if valid
        else default
    )


def settings(db):
    return {key: setting(db, key) for key in DEFAULTS}


def source_enabled(db, source):
    key = f"source_{source}_enabled"
    if key not in DEFAULTS:
        raise ValueError("unknown_source")
    return setting(db, key) is True


def update_settings(db, values, actor):
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError("unknown_setting")
    now = utcnow()
    for key, value in values.items():
        default = DEFAULTS[key]
        if type(default) is bool:
            if type(value) is not bool:
                raise ValueError(key)
        elif isinstance(default, int):
            if type(value) is not int or not 0 <= value <= 10000:
                raise ValueError(key)
        elif isinstance(default, float):
            if type(value) not in (int, float) or not 0 <= float(value) <= 1000:
                raise ValueError(key)
            value = float(value)
        elif not isinstance(value, str) or not value.strip() or len(value) > 30:
            raise ValueError(key)
        if key in ("assembly_start", "assembly_end"):
            try:
                hour, minute = map(int, value.split(":"))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except Exception as exc:
                raise ValueError(key) from exc
        row = db.get(AutomationSetting, key)
        if row is None:
            db.add(
                AutomationSetting(
                    key=key,
                    value=json.dumps(value),
                    updated_at=now,
                    actor_id=actor.discord_id,
                )
            )
        else:
            row.value = json.dumps(value)
            row.updated_at = now
            row.actor_id = actor.discord_id
    merged = settings(db)
    merged.update(values)
    if (
        len(
            {
                merged["weekly_gold_rank"],
                merged["weekly_silver_rank"],
                merged["weekly_bronze_rank"],
            }
        )
        != 3
    ):
        raise ValueError("weekly_star_ranks")
    if (
        not merged["medal_combat_i_kd"]
        >= merged["medal_combat_ii_kd"]
        >= merged["medal_combat_iii_kd"]
    ):
        raise ValueError("combat_kd_order")
    if merged["assembly_start"] == merged["assembly_end"]:
        raise ValueError("assembly_time")


def exception_for(db, user_id, scope, target, event_date=""):
    return (
        db.query(AutomationException)
        .filter_by(
            user_id=user_id,
            scope=scope,
            target=target,
            event_date=event_date or "",
            active=True,
        )
        .first()
    )


def add_exception(db, user, actor, scope, target, reason, event_date=""):
    if scope not in ("award", "assembly") or not reason.strip():
        raise ValueError("invalid_exception")
    if scope == "assembly":
        target = "absence_penalty"
    row = (
        db.query(AutomationException)
        .filter_by(
            user_id=user.id, scope=scope, target=target, event_date=event_date or ""
        )
        .first()
    )
    now = utcnow()
    values = dict(
        reason=reason.strip()[:500],
        active=True,
        actor_id=actor.discord_id,
        actor_name=(actor.display_name or actor.username)[:100],
        created_at=now,
        disabled_at=None,
    )
    if row is None:
        row = AutomationException(
            user_id=user.id,
            scope=scope,
            target=target,
            event_date=event_date or "",
            **values,
        )
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    return row


def queue_alert(db, key, source, message, level="error", cooldown=timedelta(hours=6)):
    now = utcnow()
    previous = (
        db.query(SystemAlert)
        .filter_by(alert_key=key, resolved_at=None)
        .order_by(SystemAlert.id.desc())
        .first()
    )
    if previous and now - previous.created_at < cooldown:
        previous.message = message[:1000]
        return previous
    row = SystemAlert(
        alert_key=key[:120],
        source=source[:40],
        level=level[:12],
        message=message[:1000],
        created_at=now,
        retry_at=now,
    )
    db.add(row)
    return row


def resolve_alerts(db, key):
    now = utcnow()
    for row in db.query(SystemAlert).filter_by(alert_key=key, resolved_at=None):
        row.resolved_at = now


@event.listens_for(SessionLocal, "before_flush")
def remember_identity_changes(db, flush_context, instances):
    now = utcnow()
    actor_id = db.info.get("identity_actor")
    reason = db.info.get("identity_reason")
    for user in [
        item for item in list(db.new) + list(db.dirty) if isinstance(item, User)
    ]:
        state = inspect(user)
        for kind, field in (("discord", "discord_id"), ("steam", "steam_id")):
            history = state.attrs[field].history
            if not history.has_changes():
                continue
            old_values = [str(value) for value in history.deleted if value]
            new_value = getattr(user, field)
            for old in old_values:
                row = (
                    db.query(IdentityLinkHistory)
                    .filter_by(user_id=user.id, kind=kind, value=old, valid_to=None)
                    .order_by(IdentityLinkHistory.id.desc())
                    .first()
                )
                if row:
                    row.valid_to = now
            if new_value:
                current = (
                    db.query(IdentityLinkHistory.id)
                    .filter_by(
                        user_id=user.id, kind=kind, value=str(new_value), valid_to=None
                    )
                    .first()
                )
                if current is None:
                    db.add(
                        IdentityLinkHistory(
                            user_id=user.id,
                            kind=kind,
                            value=str(new_value),
                            valid_from=now,
                            actor_id=actor_id,
                            reason=reason,
                        )
                    )


def backfill_identity_history(db):
    now = utcnow()
    for user in db.query(User).all():
        for kind, value in (("discord", user.discord_id), ("steam", user.steam_id)):
            if (
                value
                and not db.query(IdentityLinkHistory.id)
                .filter_by(user_id=user.id, kind=kind, value=str(value))
                .first()
            ):
                db.add(
                    IdentityLinkHistory(
                        user_id=user.id,
                        kind=kind,
                        value=str(value),
                        valid_from=now,
                        reason="Первичная фиксация текущей привязки",
                    )
                )
