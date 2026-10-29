"""The embedded audit API retains administrator permissions and private-note policy."""

from datetime import datetime

import pytest

from database import GameHistory, SessionLocal


@pytest.mark.parametrize("role", ["anonymous", "player", "junior", "senior"])
def test_embedded_audit_role_policy_and_private_notes(
    make_user, login_as, client, role
):
    if role != "anonymous":
        client = login_as(
            make_user(
                101,
                is_admin=role in ("junior", "senior"),
                is_senior_admin=role == "senior",
            )
        )
    with SessionLocal.begin() as db:
        db.add(
            GameHistory(
                user_id=101,
                game_type="admin",
                result="note_updated",
                amount=0,
                details="PRIVATE_NOTE_ONLY_SENIOR",
                created_at=datetime(2026, 10, 29, 12),
            )
        )
    response = client.get("/api/admin/audit")
    assert response.status_code == (200 if role in ("junior", "senior") else 403)
    if response.status_code == 200:
        assert "no-store" in response.headers["cache-control"]
        assert ("PRIVATE_NOTE_ONLY_SENIOR" in response.text) == (role == "senior")
        search = client.get("/api/admin/audit?q=PRIVATE_NOTE_ONLY_SENIOR").json()
        assert search["total_records"] == (1 if role == "senior" else 0)
        assert response.json()["summary"]["financial_count"] == 0


def test_embedded_audit_player_filters_pagination_and_bad_ids(make_user, login_as):
    admin = make_user(101, is_admin=True, is_senior_admin=True)
    make_user(102)
    with SessionLocal.begin() as db:
        for i in range(105):
            db.add(
                GameHistory(
                    user_id=102,
                    game_type="admin",
                    result="admin_adjustment",
                    amount=10,
                    details=f"Начисление {i}",
                    created_at=datetime(2026, 10, 23, 21),
                )
            )
        db.add(
            GameHistory(
                user_id=101,
                game_type="admin",
                result="admin_adjustment",
                amount=-5,
                details="Another player",
                created_at=datetime(2026, 10, 24),
            )
        )
    client = login_as(admin)
    query = {
        "player_id": "102",
        "outcome": "positive",
        "date_from": "2026-10-24",
        "date_to": "2026-10-24",
    }
    first = client.get("/api/admin/audit", params=query).json()
    assert len(first["entries"]) == 100 and first["total_records"] == 105
    assert first["summary"]["positive"] == 1050
    second = client.get(
        "/api/admin/audit", params={**query, "before": first["next_before"]}
    ).json()
    assert len(second["entries"]) == 5 and second["next_before"] is None
    assert not {r["id"] for r in first["entries"]} & {
        r["id"] for r in second["entries"]
    }
    assert client.get("/api/admin/audit?player_id=not-a-number").status_code == 400
    assert client.get("/api/admin/audit?before=" + "9" * 100).status_code == 400
