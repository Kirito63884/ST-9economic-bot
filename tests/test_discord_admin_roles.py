import pytest

import main
from automation_control import AutomationSetting
from database import SessionLocal, User
from discord_admin_roles import (
    JUNIOR_ROLE,
    SENIOR_ROLE,
    manual_role_change,
    sync_admin_roles,
)


@pytest.fixture(autouse=True)
def clear_grants():
    with SessionLocal.begin() as db:
        db.query(AutomationSetting).filter(
            AutomationSetting.key.like("discord_admin_grant:%")
        ).delete()


def test_role_grant_upgrade_downgrade_and_revoke(make_user):
    make_user(101)
    with SessionLocal.begin() as db:
        user = db.get(User, 101)
        sync_admin_roles(db, user, [JUNIOR_ROLE])
        assert user.is_admin and not user.is_senior_admin
        sync_admin_roles(db, user, [JUNIOR_ROLE, SENIOR_ROLE])
        assert user.is_senior_admin
        sync_admin_roles(db, user, [JUNIOR_ROLE])
        assert user.is_admin and not user.is_senior_admin
        sync_admin_roles(db, user, [])
        assert not user.is_admin and not user.is_senior_admin


def test_manual_baseline_and_explicit_same_level_are_preserved(make_user):
    make_user(101, is_admin=True)
    with SessionLocal.begin() as db:
        user = db.get(User, 101)
        sync_admin_roles(db, user, [SENIOR_ROLE])
        sync_admin_roles(db, user, [])
        assert user.is_admin and not user.is_senior_admin
        sync_admin_roles(db, user, [SENIOR_ROLE])
        manual_role_change(db, user)
        sync_admin_roles(db, user, [])
        assert user.is_admin and user.is_senior_admin


@pytest.mark.parametrize("protected", ["chief", "tech", "owner"])
def test_protected_roles_and_invalid_evidence(make_user, monkeypatch, protected):
    make_user(
        101,
        is_admin=True,
        is_senior_admin=True,
        is_chief_admin=protected == "chief",
        is_tech_admin=protected == "tech",
    )
    if protected == "owner":
        monkeypatch.setenv("ADMIN_DISCORD_ID", "101")
    with SessionLocal.begin() as db:
        user = db.get(User, 101)
        sync_admin_roles(db, user, [])
        assert user.is_senior_admin and user.is_admin
    make_user(102)
    with SessionLocal.begin() as db:
        user = db.get(User, 102)
        sync_admin_roles(db, user, [SENIOR_ROLE])
        for evidence in (None, "", ["bad"]):
            sync_admin_roles(db, user, evidence)
            assert user.is_senior_admin


def test_verified_membership_path_grants_and_removes_auto_rights(
    make_user, monkeypatch
):
    monkeypatch.setattr(main, "REQUIRED_GUILD_ID", "123")
    make_user(101)
    with SessionLocal.begin() as db:
        user = db.get(User, 101)
        main.apply_discord_membership(
            db, user, {"allowed": True, "roles": [SENIOR_ROLE]}
        )
        assert user.is_senior_admin
        main.apply_discord_membership(db, user, {"allowed": True, "roles": []})
        assert not user.is_admin
