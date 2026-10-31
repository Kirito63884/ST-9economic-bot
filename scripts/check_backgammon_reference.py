"""Optional independent short-backgammon comparison; not a server dependency.

Download https://raw.githubusercontent.com/dellalibera/gym-backgammon/master/gym_backgammon/envs/backgammon.py
and pass its local path. This runner never imports the website or any credentials.
"""

import sys, importlib.util, random
from pathlib import Path
import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("reference", type=Path)
args = parser.parse_args()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backgammon_engine import Position, Round, legal_moves, moved

spec = importlib.util.spec_from_file_location("reference", args.reference)
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


def finals(pos, side, dice):
    choices = legal_moves(pos, side, dice, "short")
    if not choices:
        return {pos}
    result = set()
    for move in choices:
        rest = list(dice)
        rest.remove(move[2])
        after = moved(pos, side, move)
        if after.off[side] == 15:
            result.add(after)
        else:
            result.update(finals(after, side, rest))
    return result


def ref(pos, side, dice):
    g = reference.Backgammon()
    g.board = [(abs(v), 0 if v > 0 else 1 if v < 0 else None) for v in pos.board]
    g.bar = list(pos.bar)
    g.off = list(pos.off)
    g.players_positions = g.get_players_positions()
    initial = g.save_state()
    plays = g.get_valid_plays(side, tuple(-v if side == 0 else v for v in dice))
    result = set()
    for play in plays:
        g.restore_state(initial)
        g.execute_play(side, play)
        result.add(
            Position(
                tuple(
                    n * (1 if owner == 0 else -1) if owner is not None else 0
                    for n, owner in g.board
                ),
                tuple(g.bar),
                tuple(g.off),
            )
        )
    return result or {pos}


rng = random.Random(4951)
checked = 0
for gameid in range(4):
    game = Round("short", [6, 1])
    while game.winner is None:
        if not game.dice:
            game.roll([rng.randint(1, 6), rng.randint(1, 6)])
            continue
        if len(game.dice) == 4 or (
            len(game.dice) == 2 and game.dice[0] != game.dice[1]
        ):
            pair = (game.dice[0], game.dice[1])
            ours = finals(game.pos, game.turn, game.dice)
            theirs = ref(game.pos, game.turn, pair)
            if ours != theirs:
                print(
                    "MISMATCH",
                    gameid,
                    checked,
                    game.turn,
                    game.dice,
                    game.pos,
                    "ours",
                    len(ours),
                    "ref",
                    len(theirs),
                )
                print("ours only", ours - theirs)
                print("ref only", theirs - ours)
                sys.exit(1)
            checked += 1
        game.move(rng.choice(game.legal()))
print("Compared full legal-turn resulting positions:", checked)
