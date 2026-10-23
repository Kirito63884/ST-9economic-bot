"""Display award names from Discord roles; achievement identity remains immutable."""

from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime
from database import Base
from award_design import AwardDesign


class AwardRoleName(Base):
    __tablename__ = "award_role_names"
    role_id = Column(String(20), primary_key=True)
    name = Column(String(100), nullable=False)
    updated_at = Column(DateTime, nullable=False)


def role_ids():
    from medals import ROLE_IDS, MANUAL_ROLES

    return {code: str(role) for code, role in {**ROLE_IDS, **MANUAL_ROLES}.items()}


def sync_names(db, roles):
    """Only trusted guild role payloads reach here; missing roles retain their last name."""
    if not isinstance(roles, list):
        return
    allowed = set(role_ids().values())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for role in roles:
        if not isinstance(role, dict):
            continue
        rid = str(role.get("id", ""))
        name = role.get("name")
        if (
            rid not in allowed
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 100
        ):
            continue
        row = db.get(AwardRoleName, rid)
        if row is None:
            db.add(AwardRoleName(role_id=rid, name=name, updated_at=now))
        else:
            row.name = name
            row.updated_at = now


def names(db):
    ids = role_ids()
    cached = {
        r.role_id: r.name
        for r in db.query(AwardRoleName).filter(
            AwardRoleName.role_id.in_(set(ids.values()))
        )
    }
    result = {code: cached[rid] for code, rid in ids.items() if rid in cached}
    result.update(
        {
            r.code: r.name
            for r in db.query(AwardDesign).filter(AwardDesign.name.is_not(None))
        }
    )
    return result


def name_revision(db):
    """A manual website refresh must also supersede an older in-flight poll."""
    return tuple(
        (r.role_id, r.name, r.updated_at)
        for r in db.query(AwardRoleName).order_by(AwardRoleName.role_id)
    )


def award_name(db, code):
    from medals import CATALOG, MANUAL_CATALOG

    custom = db.get(AwardDesign, code)
    if custom and custom.name:
        return custom.name
    rid = role_ids().get(code)
    row = db.get(AwardRoleName, rid) if rid else None
    return row.name if row else {**CATALOG, **MANUAL_CATALOG}[code][0]


def named_catalog(db, catalog):
    titles = names(db)
    return {
        code: (titles.get(code, item[0]), *item[1:]) for code, item in catalog.items()
    }
