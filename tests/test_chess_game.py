import pytest
import chess

from chess_game import (
    ChessError,
    ChessTable,
    automatic_result,
    claim_draw_reason,
)


def ready_table():
    table = ChessTable("table", 1, "Белые", {"entry_cents": 0})
    table.add_player(2, "Чёрные")
    table.player(1)["ready"] = True
    table.player(2)["ready"] = True
    table.start()
    return table


def play(table, uid, uci):
    table.action(uid, "move", uci)


def test_server_rejects_illegal_move_without_changing_position():
    table = ready_table()
    before = table.to_dict()
    with pytest.raises(ChessError):
        play(table, 1, "e2e5")
    assert table.to_dict() == before
    play(table, 1, "e2e4")
    assert table.actor == 2


def test_en_passant_castling_and_restore_move_history():
    table = ready_table()
    for uid, move in [(1, "e2e4"), (2, "a7a6"), (1, "e4e5"), (2, "d7d5")]:
        play(table, uid, move)
    assert "e5d6" in table.public_state(1)["legal_moves"]
    restored = ChessTable.from_dict(table.to_dict())
    assert restored.board.is_en_passant(restored.board.parse_uci("e5d6"))
    play(restored, 1, "e5d6")

    table = ready_table()
    for uid, move in [
        (1, "e2e4"),
        (2, "e7e5"),
        (1, "g1f3"),
        (2, "b8c6"),
        (1, "f1e2"),
        (2, "g8f6"),
    ]:
        play(table, uid, move)
    assert "e1g1" in table.public_state(1)["legal_moves"]
    play(table, 1, "e1g1")


def test_checkmate_and_threefold_draw_claim():
    table = ready_table()
    for uid, move in [(1, "f2f3"), (2, "e7e5"), (1, "g2g4"), (2, "d8h4")]:
        play(table, uid, move)
    assert table.phase == "finished"
    assert table.match["result"]["winners"] == [2]
    assert table.match["result"]["reason"] == "Мат"

    table = ready_table()
    cycle = [(1, "g1f3"), (2, "g8f6"), (1, "f3g1"), (2, "f6g8")]
    for _ in range(2):
        for uid, move in cycle:
            play(table, uid, move)
    assert table.phase == "playing"  # threefold is claimable, not automatic
    assert table.public_state(1)["can_claim_draw"]
    table.action(1, "claim_draw")
    assert table.match["result"]["reason"].startswith("Ничья: трёхкратное")


def test_stale_revision_and_repeated_settlement(make_user):
    from chess_routes import ChessService, revision
    from conftest import wallet
    from database import GameHistory, SessionLocal, User

    make_user(101, balance=100)
    make_user(102, balance=100)
    table = ChessTable("table", 101, "Белые", {"entry_cents": 250})
    table.add_player(102, "Чёрные")
    for player in table.players:
        player["ready"] = True
    table.start()
    start_revision = table.revision
    with pytest.raises(ChessError) as stale:
        revision(table, {"revision": start_revision - 1})
    assert stale.value.status == 409

    play(table, 101, "f2f3")
    with pytest.raises(ChessError) as repeated:
        revision(table, {"revision": start_revision})
    assert repeated.value.status == 409
    with pytest.raises(ChessError):
        play(table, 101, "e2e4")  # White cannot move again

    # The black player may resign out of turn; the winner gets the existing pot.
    table.action(102, "resign")
    service = ChessService()
    service.tables[table.id] = table
    with SessionLocal.begin() as db:
        for player in table.match["participants"]:
            user = db.get(User, player["id"])
            user.balance -= 2.5
        service.settle(table, db)
        service.settle(table, db)
    assert wallet(101) == 102.5
    assert wallet(102) == 97.5
    with SessionLocal() as db:
        assert (
            db.query(GameHistory).filter(GameHistory.game_type == "chess").count() == 2
        )


def test_runtime_restore_polling_does_not_extend_idle_and_refunds(make_user):
    import time
    from types import SimpleNamespace

    import main
    from conftest import wallet
    from database import SessionLocal, User
    from table_lifecycle import request_activity

    make_user(101, balance=100)
    make_user(102, balance=100)
    table = ChessTable("table", 101, "Белые", {"entry_cents": 250})
    table.add_player(102, "Чёрные")
    for player in table.players:
        player["ready"] = True
    table.start()
    for uid in (101, 102):
        with SessionLocal.begin() as db:
            db.get(User, uid).balance -= 2.5
    main.chess_service.tables[table.id] = table
    payload = main.serialize_runtime()
    main.restore_runtime(payload)
    table = main.chess_service.tables[table.id]
    assert table.board.fen() == table.fen

    table.last_player_activity = time.time() - 8 * 60 * 60 - 1
    before_poll = table.last_player_activity
    empty = SimpleNamespace(tables={})
    request_activity(
        "GET",
        "/api/chess/table/table/state",
        101,
        {},
        empty,
        empty,
        chess=main.chess_service,
    )
    assert table.last_player_activity == before_poll
    with SessionLocal.begin() as db:
        main.chess_service.tick_table(table.id, db)
    assert table.id not in main.chess_service.tables
    assert wallet(101) == 100
    assert wallet(102) == 100


def test_promotion_and_automatic_fivefold_repetition():
    table = ready_table()
    sequence = [
        (1, "a2a4"),
        (2, "h7h5"),
        (1, "a4a5"),
        (2, "h5h4"),
        (1, "a5a6"),
        (2, "h4h3"),
        (1, "a6b7"),
        (2, "g7g6"),
    ]
    for uid, move in sequence:
        play(table, uid, move)
    assert "b7a8q" in table.public_state(1)["legal_moves"]
    play(table, 1, "b7a8n")
    assert table.board.piece_at(56).symbol() == "N"

    table = ready_table()
    cycle = [(1, "g1f3"), (2, "g8f6"), (1, "f3g1"), (2, "f6g8")]
    for _ in range(4):
        for uid, move in cycle:
            if table.phase == "finished":
                break
            play(table, uid, move)
    assert table.phase == "finished"
    assert table.match["result"]["reason"].startswith("Ничья: пятикратное")


def test_50_move_claim_requires_100_halfmoves_and_75_moves_are_automatic():
    position = chess.STARTING_FEN.split()
    position[4] = "99"
    at_99 = chess.Board(" ".join(position))
    assert at_99.can_claim_fifty_moves()  # only by naming a qualifying next move
    assert claim_draw_reason(at_99) is None  # the API has no announced-move field

    position[4] = "100"
    at_100 = chess.Board(" ".join(position))
    assert claim_draw_reason(at_100) == "Ничья: правило 50 ходов"

    position[4] = "150"
    at_150 = chess.Board(" ".join(position))
    assert automatic_result(at_150, 1) == ([], "Ничья: правило 75 ходов")


def test_stalemate_checkmate_and_insufficient_material_results():
    checkmate = chess.Board("7k/6Q1/6K1/8/8/8/8/8 b - - 0 1")
    assert automatic_result(checkmate, 101) == ([101], "Мат")

    stalemate = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    assert automatic_result(stalemate, 101) == ([], "Пат")

    bare_kings = chess.Board("8/8/8/8/8/6k1/8/7K w - - 0 1")
    assert automatic_result(bare_kings, 101) == (
        [],
        "Ничья: недостаточно материала для мата",
    )


def test_board_orientation_is_stable_from_waiting_to_playing():
    table = ChessTable("orientation", 1, "Белые")
    assert table.public_state(1)["your_side"] == "white"
    table.add_player(2, "Чёрные")
    for uid, side in ((1, "white"), (2, "black")):
        assert table.public_state(uid)["your_side"] == side
        table.player(uid)["ready"] = True
    table.start()
    assert table.public_state(1)["your_side"] == "white"
    assert table.public_state(2)["your_side"] == "black"
