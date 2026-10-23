"""Screenshot criteria: first crew place grants DRIVER, first pilot grants PILOT."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from admin_hub import AwardWeekControl
from database import SessionLocal, User
from medals import MedalAward, progress
from protocol_award_exports import (
    ClanUnavailable,
    ProtocolAwardExport,
    award_exports,
    flight_records,
    parse_page,
)

NOW = datetime(2026, 10, 23, 12)
FIXTURE = Path(__file__).parent / "fixtures" / "protocol_exports.html"
SID = "76561198000000000"


def node(tag, children, **props):
    return ["$", tag, None, {"children": children, **props}]


def page():
    # Preserve referenced RSC records from the real fixture. New rows are synthetic.
    records = flight_records(FIXTURE.read_text())
    encoded = json.dumps(records, ensure_ascii=False)
    encoded = encoded.replace("12.10–18.10", "19.10–25.10").replace(
        "05.10–11.10", "12.10–18.10"
    )
    records = json.loads(encoded)
    section = records["1"][3]["children"][1]
    import re

    clan = re.search(r"clan=([a-z0-9]{15,40})", FIXTURE.read_text()).group(1)
    for kind, title in [
        ("pilot", "Пилот — топ-3 недели (пилот и командир вертолёта)"),
        ("crew", "Экипаж — топ-3 недели (экипаж и командир экипажа)"),
    ]:
        blocks = [node("h3", title)]
        for scope, period in [
            ("cur", "эта неделя · 19.10–25.10"),
            ("prev", "прошлая неделя · 12.10–18.10"),
        ]:
            rows = [
                [
                    "$",
                    "li",
                    str(int(SID) + i),
                    {"children": ["Игрок — ", node("b", f"{18 - i},0 ч")]},
                ]
                for i in range(3)
            ]
            link = f"/api/clans/dump?clan={clan}&kind={kind}&week={scope}"
            blocks.append(
                node(
                    "div",
                    [
                        node("p", period),
                        node("a", "⬇ .txt", href=link),
                        node("ol", rows),
                    ],
                )
            )
        section[3]["children"].append(node("div", blocks))
    stream = "".join(
        key + ":" + json.dumps(value, ensure_ascii=False) + "\n"
        for key, value in records.items()
    )
    return (
        "<script>self.__next_f.push("
        + json.dumps([1, stream], ensure_ascii=False)
        + ")</script>"
    )


@pytest.fixture(autouse=True)
def clean_awards():
    with SessionLocal.begin() as db:
        db.query(ProtocolAwardExport).delete()
        db.query(MedalAward).delete()
        db.query(AwardWeekControl).delete()
    yield
    with SessionLocal.begin() as db:
        db.query(ProtocolAwardExport).delete()
        db.query(MedalAward).delete()
        db.query(AwardWeekControl).delete()


def store(doc, checked_at=NOW):
    with SessionLocal.begin() as db:
        db.add(
            ProtocolAwardExport(
                key=doc["week"] + ":" + doc["kind"],
                week=doc["week"],
                kind=doc["kind"],
                checked_at=checked_at,
                payload=json.dumps({**doc, "file_verified": True}),
            )
        )
        if not db.get(AwardWeekControl, doc["week"]):
            db.add(
                AwardWeekControl(week=doc["week"], status="approved", created_at=NOW)
            )


@pytest.mark.parametrize(
    "kind,code", [("pilot", "best_pilot"), ("crew", "best_driver")]
)
def test_publisher_top3_is_parsed_but_only_first_place_is_awarded(
    make_user, kind, code
):
    documents = parse_page(page(), NOW)
    doc = documents["2026-10-12:" + kind]
    assert doc["weekly_kit_award"] == code
    assert doc["unit"] == "hours" and doc["final"]
    assert len(doc["rows"]) == 3
    for i in range(3):
        make_user(101 + i, steam_id=str(int(SID) + i))
    store(doc)
    with SessionLocal.begin() as db:
        users = db.query(User).all()
        award_exports(db, users, NOW)
        awards = db.query(MedalAward).all()
        assert [(a.discord_id, a.medal) for a in awards] == [("101", code)]
        proof = json.loads(awards[0].evidence)
        assert proof["published"]["rank"] == 1
        assert proof["week"] == "2026-10-12"
        assert not db.query(MedalAward).filter_by(medal="best_crew").first()
        award_exports(db, users, NOW)
        assert db.query(MedalAward).count() == 1
        card = next(
            card
            for card in progress(db, users[0], NOW)["cards"]
            if card["code"] == code
        )
        assert card["values"][0]["target_rank"] == 1


@pytest.mark.parametrize("kind", ["pilot", "crew"])
def test_current_week_never_grants(make_user, kind):
    doc = parse_page(page(), NOW)["2026-10-19:" + kind]
    uid = make_user(steam_id=SID)
    store(doc)
    with SessionLocal.begin() as db:
        award_exports(db, [db.get(User, uid)], NOW)
        assert db.query(MedalAward).count() == 0


def test_wrong_units_titles_and_periods_fail_closed():
    body = page()
    for changed in [
        body.replace("командир вертолёта", "неизвестный кит"),
        body.replace("18,0 ч", "18,0"),
        body.replace("12.10–18.10", "11.10–17.10"),
    ]:
        with pytest.raises(ClanUnavailable):
            parse_page(changed, NOW)
