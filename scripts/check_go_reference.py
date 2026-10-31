"""Independent offline differential check against sgfmill 1.1.1.

Install sgfmill in a separate developer venv; the server does not depend on it.
No app import, database, credentials, network, wallet or background worker.
"""

import random
import sys
from importlib.metadata import version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go_engine import GoError, GoPosition, area_score, fingerprint


def main():
    try:
        from sgfmill.boards import Board
    except ImportError:
        raise SystemExit("Install sgfmill==1.1.1 in a separate development venv.")
    rng = random.Random(71329)
    attempts = legal_count = 0
    for size in (9, 13, 19):
        for _ in range(12):
            ours, reference = GoPosition(size), Board(size)
            for _ in range(size * size * 4):
                point = rng.randrange(size * size)
                before = ours.to_dict()
                candidate = reference.copy()
                row, col = divmod(point, size)
                expected = None
                try:
                    candidate.play(row, col, "b" if ours.to_move == 1 else "w")
                    flat = [
                        {None: 0, "b": 1, "w": 2}[candidate.get(r, c)]
                        for r in range(size)
                        for c in range(size)
                    ]
                    # sgfmill allows self-capture and does not enforce ko. Apply
                    # this ruleset's explicit restrictions to its resulting board.
                    if flat[point] and fingerprint(flat) not in ours.seen:
                        expected = flat
                except ValueError:
                    pass
                attempts += 1
                try:
                    ours.play(point)
                except GoError:
                    assert expected is None, (size, point, "unexpected rejection")
                    assert ours.to_dict() == before
                    continue
                assert expected == ours.board, (size, point, "different capture result")
                legal_count += 1
                reference = candidate
                score = area_score(ours.board)
                assert (
                    score["black_half"] - score["white_half"] + 15
                ) / 2 == reference.area_score()
            assert GoPosition.from_dict(ours.to_dict()).to_dict() == ours.to_dict()
    print(
        f"PASS sgfmill {version('sgfmill')}: {attempts} attempted moves, {legal_count} legal positions; captures, suicide, repetition filter, area difference and replay agree"
    )


if __name__ == "__main__":
    main()
