"""Protocol clan source, exact K/D, seed hours, access and safe deployment."""

import json
from datetime import timedelta

import httpx
import main
import pytest
from database import SessionLocal, User
from medals import MedalAward, ProtocolProfile, award_totals, progress, utcnow
from protocol_clan_source import (
    CLAN,
    URL,
    ClanAuthRequired,
    ClanUnavailable,
    ProtocolClanStats,
    clan_profile,
    collect_clan,
    parse_clan,
    request_page,
)
from protocol_login import login_form

SID = "76561198126266582"


def raw_player(**changes):
    return {
        "steamId": SID,
        "name": "BISHOP",
        "role": "owner",
        "hours": 483.7,
        "kills": 121,
        "deaths": 59,
        "rating": 0,
        "seedH": 159.5,
        **changes,
    }


def page(players=None, count=None, binding="ClanRosterStats", split=False):
    players = players if players is not None else [raw_player()]
    record = ["$", "$L69", None, {"rows": players}]
    stream = "69:I" + json.dumps([276827, ["/chunk.js"], binding]) + "\n"
    stream += (
        "6:"
        + json.dumps(["$", "div", None, {"children": record}], ensure_ascii=False)
        + "\n"
    )
    chunks = (
        [stream[: len(stream) // 2], stream[len(stream) // 2 :]] if split else [stream]
    )
    return (
        '<html><a href="/clans/STRIKE%20TEAM%209">Страница клана</a><h2>👥 Состав клана <b>'
        + str(len(players) if count is None else count)
        + "</b></h2>"
        + "".join(
            "<script>self.__next_f.push("
            + json.dumps([1, chunk], ensure_ascii=False)
            + ")</script>"
            for chunk in chunks
        )
        + "</html>"
    )


@pytest.fixture(autouse=True)
def clean_clan(monkeypatch):
    main.request_guard.reset()
    with SessionLocal.begin() as db:
        db.query(ProtocolClanStats).delete()
        db.query(ProtocolProfile).delete()
        db.query(MedalAward).delete()
    from automation_control import AutomationSetting

    with SessionLocal.begin() as db:
        db.query(AutomationSetting).delete()
    yield
    with SessionLocal.begin() as db:
        db.query(ProtocolClanStats).delete()


def save(document, moment=None):
    with SessionLocal.begin() as db:
        db.add(
            ProtocolClanStats(
                id=1,
                payload=json.dumps(document),
                checked_at=moment or utcnow(),
                attempted_at=moment or utcnow(),
                status="ready",
            )
        )


def test_parses_real_protocol_values_and_streamed_chunks():
    # Real source values observed after the user signed in; no session material.
    rows = [
        raw_player(),
        raw_player(
            steamId="76561198116283526", hours=198.1, kills=663, deaths=829, seedH=10.1
        ),
        raw_player(
            steamId="76561198169780015", hours=284.5, kills=2000, deaths=612, seedH=20.3
        ),
    ]
    data = parse_clan(page(rows, split=True))
    assert data["clan"] == CLAN
    player = data["players"][SID]
    assert player["seed_hours"] == 159.5
    assert player["minutes"] == 29022
    assert player["kd"] == 121 / 59
    assert player["kd"] != 2.05  # Rounded visible table must not determine eligibility.


@pytest.mark.parametrize(
    "changes",
    [
        {"hours": -1},
        {"seedH": float("nan")},
        {"kills": 1.5},
        {"deaths": True},
        {"steamId": "not steam"},
        {"seedH": None},
        {"hours": "483.7"},
    ],
)
def test_invalid_statistics_are_rejected(changes):
    with pytest.raises(ClanUnavailable):
        parse_clan(page([raw_player(**changes)]))


def test_wrong_component_partial_roster_and_duplicate_ids_fail():
    for body in [
        page(count=115),
        page(binding="DifferentTable"),
        page([raw_player(), raw_player()]),
        page().replace("/clans/STRIKE%20TEAM%209", "/clans/OTHER"),
        "<html>Loading…</html>",
    ]:
        with pytest.raises(ClanUnavailable):
            parse_clan(body)


def test_scripts_are_never_evaluated():
    body = page() + '<script>throw new Error("must not run");</script>'
    assert parse_clan(body)["players"][SID]["kills"] == 121


def test_zero_deaths_matches_published_table():
    assert parse_clan(page([raw_player(kills=5, deaths=0)]))["players"][SID]["kd"] == 5
    assert parse_clan(page([raw_player(kills=0, deaths=0)]))["players"][SID]["kd"] == 0


def test_source_failure_retains_last_good_snapshot_and_reports_login():
    now = utcnow()
    doc = parse_clan(page())
    assert collect_clan(fetch=lambda: doc, now=now)

    def blocked():
        raise ClanAuthRequired("Требуется вход в Protocol.")

    assert not collect_clan(fetch=blocked, now=now + timedelta(hours=24))
    with SessionLocal() as db:
        row = db.get(ProtocolClanStats, 1)
        assert json.loads(row.payload) == doc
        assert row.checked_at == now
        assert row.status == "auth_required"
        assert clan_profile(db, SID, now + timedelta(hours=25)) is None
        assert clan_profile(db, SID, now + timedelta(hours=25), allow_stale=True)[
            "stale"
        ]
    assert not collect_clan(fetch=lambda: {"schema": 0}, now=now + timedelta(hours=48))
    with SessionLocal() as db:
        assert json.loads(db.get(ProtocolClanStats, 1).payload) == doc


def test_fetch_redirect_and_size_guards():
    for status, location in [
        (302, "/login?next=%2Fclan"),
        (302, "https://foreign.example/"),
        (403, None),
    ]:
        transport = httpx.MockTransport(
            lambda request, s=status, l=location: httpx.Response(
                s, headers={"Location": l} if l else {}
            )
        )
        with (
            httpx.Client(transport=transport) as client,
            pytest.raises(ClanUnavailable),
        ):
            request_page(client, URL)
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"x" * 3000001)
    )
    with httpx.Client(transport=transport) as client, pytest.raises(ClanUnavailable):
        request_page(client, URL)


def test_progress_prioritizes_clan_over_sqstat_and_preserves_seed(make_user):
    uid = make_user(steam_id=SID)
    save(parse_clan(page()))
    with SessionLocal.begin() as db:
        db.add(
            ProtocolProfile(
                steam_id=SID,
                payload=json.dumps({"minutes": 600, "kills": 1, "deaths": 100}),
                checked_at=utcnow(),
            )
        )
    with SessionLocal() as db:
        data = progress(db, db.get(User, uid))
        assert data["seed_hours"] == 159.5 and data["minutes"] == 29022
        assert data["kd"] == round(121 / 59, 3)
        assert "статистика состава" in data["kd_source"]
        iron = next(c for c in data["cards"] if c["code"] == "iron_cross")
        assert iron["values"][1]["value"] == 159.5 and not iron["blocked"]


def test_exact_kd_controls_grant_not_rounded_table(make_user):
    uid = make_user(steam_id=SID)
    # Published K/D rounds to 3.00; actual ratio is below 3, so no I degree.
    save(parse_clan(page([raw_player(hours=150, kills=2999, deaths=1000, seedH=0)])))
    with SessionLocal.begin() as db:
        award_totals(db, db.get(User, uid))
        codes = {r.medal for r in db.query(MedalAward)}
        assert "infantry_blue" not in codes
        assert {"infantry_red", "infantry_green"} <= codes


@pytest.mark.parametrize(
    "hours,seed,earned",
    [(1000, 1000, True), (999.9, 1000, False), (1000, 999.9, False)],
)
def test_order_needs_both_protocol_and_seed_hours(make_user, hours, seed, earned):
    uid = make_user(steam_id=SID)
    save(parse_clan(page([raw_player(hours=hours, seedH=seed)])))
    with SessionLocal.begin() as db:
        award_totals(db, db.get(User, uid))
        assert (
            bool(db.query(MedalAward).filter_by(medal="iron_cross").first()) == earned
        )


def test_stale_seed_cannot_issue_order_and_progress_keeps_it(make_user):
    uid = make_user(steam_id=SID)
    save(
        parse_clan(page([raw_player(hours=2000, seedH=2000)])),
        utcnow() - timedelta(hours=25),
    )
    with SessionLocal.begin() as db:
        award_totals(db, db.get(User, uid))
        assert not db.query(MedalAward).first()
        data = progress(db, db.get(User, uid))
        assert data["seed_hours"] == 2000 and data["clan_stats_stale"]
        assert next(c for c in data["cards"] if c["code"] == "iron_cross")["blocked"]


@pytest.mark.parametrize("length", [40, 42])
def test_login_uses_current_form_action_and_does_not_need_steam_password(length):
    token = "a" * length
    form = (
        '<form method="POST"><input name="$ACTION_ID_'
        + token
        + '" type="hidden"><input type="hidden" name="next" value="/clan"><input name="code"></form>'
    )
    fields = login_form(form)
    assert set(fields) == {"next", "$ACTION_ID_" + token}
    with pytest.raises(ClanUnavailable):
        login_form(form.replace(token, "bad"))


def test_server_login_stores_only_verified_secure_cookie(tmp_path, monkeypatch):
    import protocol_login

    target = tmp_path / "session.cookies"
    monkeypatch.setattr(protocol_login, "cookie_path", lambda: target)
    monkeypatch.setattr(protocol_login.getpass, "getpass", lambda _: "123456")
    token = "4087511fbd23e72351e70dba32e82af1454256fbc0"
    body = (
        '<form method="POST"><input name="$ACTION_ID_'
        + token
        + '" type="hidden"><input name="code"></form>'
    )
    posted = []

    def handle(request):
        if request.method == "POST":
            posted.append(request)
            return httpx.Response(
                303,
                headers={
                    "location": "/clan",
                    "set-cookie": "session=TEST-ONLY; Path=/; Secure; HttpOnly",
                },
            )
        return httpx.Response(200, text=page() if request.url.path == "/clan" else body)

    real_client = httpx.Client
    monkeypatch.setattr(
        protocol_login.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    protocol_login.main()
    assert target.stat().st_mode & 0o777 == 0o600
    assert "123456" not in target.read_text()
    assert "TEST-ONLY" in target.read_text()
    assert posted[0].headers["origin"] == "https://protocol-squad.ru"
    assert ("$ACTION_ID_" + token).encode() in posted[0].content
    with SessionLocal() as db:
        assert clan_profile(db, SID)["seed_hours"] == 159.5


def test_failed_login_preserves_old_session(tmp_path, monkeypatch):
    import protocol_login

    target = tmp_path / "session.cookies"
    target.write_text("existing-session")
    monkeypatch.setattr(protocol_login, "cookie_path", lambda: target)
    monkeypatch.setattr(protocol_login.getpass, "getpass", lambda _: "123456")
    token = "b" * 40
    body = (
        '<form method="POST"><input name="$ACTION_ID_'
        + token
        + '" type="hidden"><input name="code"></form>'
    )

    def handle(request):
        return httpx.Response(200, text=body)

    real_client = httpx.Client
    monkeypatch.setattr(
        protocol_login.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(ClanAuthRequired):
        protocol_login.main()
    assert target.read_text() == "existing-session"


def test_cli_explains_form_error_instead_of_blame_on_code(monkeypatch):
    import protocol_login

    def changed_form():
        raise ClanUnavailable("Не найдена форма входа по коду игры.")

    monkeypatch.setattr(protocol_login, "main", changed_form)
    with pytest.raises(SystemExit, match="Не найдена форма входа"):
        protocol_login.run_cli()


def test_hourly_schedule_survives_sessions_and_throttles_failed_attempts():
    now = utcnow()
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) > 1:
            raise ClanUnavailable("temporary")
        return parse_clan(page())

    collect_clan(fetch=fetch, now=now)
    for hours in (0.1, 0.5, 0.99):
        collect_clan(fetch=fetch, now=now + timedelta(hours=hours))
    assert len(calls) == 1
    collect_clan(fetch=fetch, now=now + timedelta(hours=1))
    collect_clan(fetch=fetch, now=now + timedelta(hours=1.1))
    collect_clan(fetch=fetch, now=now + timedelta(hours=1.99))
    assert len(calls) == 2
    collect_clan(fetch=fetch, now=now + timedelta(hours=2))
    assert len(calls) == 3
