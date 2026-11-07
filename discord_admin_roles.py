"""Reconcile only role-derived junior/senior grants, preserving manual grants."""

import json
import os

from automation_control import AutomationSetting
from database import GameHistory
from roster_settings import write

JUNIOR_ROLE = "1458519514470682654"
SENIOR_ROLE = "1447307527846101222"
PREFIX = "discord_admin_grant:"


def level(user):
    return 2 if user.is_admin and user.is_senior_admin else 1 if user.is_admin else 0


def protected(user):
    return (
        user.is_chief_admin
        or user.is_tech_admin
        or str(user.discord_id) == os.getenv("ADMIN_DISCORD_ID")
    )


def manual_role_change(db, user):
    # Explicitly assigning the same level makes it manual too.
    write(
        db,
        PREFIX + str(user.discord_id),
        {"baseline": level(user), "applied": level(user)},
    )


def sync_admin_roles(db, user, roles):
    if not isinstance(roles, (list, set, tuple)) or any(
        not str(role).isdigit() for role in roles
    ):
        return  # Missing/invalid evidence must not remove or grant privileges.
    key = PREFIX + str(user.discord_id)
    row = db.get(AutomationSetting, key)
    if protected(user):
        if row:
            db.delete(row)
        return
    current = level(user)
    state = json.loads(row.value) if row else {"baseline": current, "applied": current}
    baseline = state["baseline"] if current == state["applied"] else current
    values = {str(role) for role in roles}
    desired = (
        0
        if user.deleted_at
        else 2
        if SENIOR_ROLE in values
        else 1
        if JUNIOR_ROLE in values
        else 0
    )
    target = max(baseline, desired)
    if target != current:
        user.is_admin = target >= 1
        user.is_senior_admin = target >= 2
        db.add(
            GameHistory(
                user_id=user.id,
                game_type="admin",
                result="role_change",
                amount=0,
                details=f"Синхронизация прав Discord: уровень {current} → {target}; ручной уровень {baseline} сохранён.",
            )
        )
    updated = {"baseline": baseline, "applied": target}
    if target != baseline or row:
        if not row or state != updated:
            write(db, key, updated)
