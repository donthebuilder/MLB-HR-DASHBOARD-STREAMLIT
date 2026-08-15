#!/usr/bin/env python3
"""nfl_picks.py — the weekly pick card.

2026-08-15, Donovan: "we dont even have any dedicated picjk style for football
we acan figure that out right nwo aswell."

WHY A LADDER AND NOT PER-GAME SLOTS. The MLB side designates one hitter per
category per game, and that shape is right for baseball: park and pitcher are
game-anchored effects, so "the best HR play in THIS game" is a real question.
Football props don't work that way. Nobody asks who the best play in Bills-Jets
is; they ask who the best TD plays of the week are, and they shop the whole
slate. Per-game slots would also mean 7 markets x 16 games = 112 designations,
almost all of them without conviction behind them.

So: one ranked ladder per market, DEPTH deep, across the whole slate. Five a
week per market is enough volume that a record actually accumulates over a
season, and shallow enough that #5 still means something.

Each rung is independently gradeable against that market's own bar, which is
what makes the ladder work as a head-to-head surface too: contest rung 3, and
your man is scored on the same bar against the man he replaced.

WHAT THE CARD SAYS ABOUT ITSELF. Every market carries its measured hit rate and
its edge over a form-only baseline, both out of sample, both measured AT THIS
DEPTH (see card_edges() in export_report.py — the edge at 5 is not the edge at
15). As of the 2024 out-of-sample season NOT ONE of the seven markets has an
edge distinguishable from zero: ninety picks a market, standard errors of 5-7
points, every z between -0.3 and +0.7. The card says so on every market rather
than quietly ranking them as though the differences were real.

The hit rates ARE real and are the useful number: the top five rush-attempt
plays cleared 12+ carries 86.7% of the time. That is worth knowing even though
"the model beats picking by recent form" is not yet demonstrated.
"""
from __future__ import annotations

from nfl_scoring import MODELS

DEPTH = 5

# Same ladder as lib/nfl/theme.js gradeFor(). Kept here so the published card
# carries the letter and the site never has to re-derive it from the number.
def _grade(score: float) -> str:
    s = float(score)
    if s >= 78: return "A+"
    if s >= 70: return "A"
    if s >= 62: return "A-"
    if s >= 54: return "B+"
    if s >= 46: return "B"
    return "C+"


def _rung(r: dict, key: str, rank: int) -> dict:
    return {
        "rank": rank,
        "player_id": r.get("player_id"),
        "name": r.get("name"),
        "team": r.get("team"),
        "opp": r.get("opp"),
        "position": r.get("position"),
        "score": round(float(r["scores"][key]), 1),
        "grade": _grade(r["scores"][key]),
        # Carried so the site can dim a rung rather than pretend it's solid.
        "low_sample": bool(r.get("low_sample")),
        "questionable": bool(r.get("questionable")),
        "carryover": bool(r.get("carryover")),
    }


def build(rows: list[dict], edges: dict | None = None, depth: int = DEPTH) -> dict:
    """{market_key: {label, bar, positions, edge, rungs:[...]}}

    `rows` are the finished payload rows — the SAME objects the Boards tab
    ranks. Building the card off the published rows rather than re-scoring is
    deliberate: the MLB side learned the hard way that two surfaces computing
    "the pick" separately will eventually name different players, and then
    nobody can tell which one is lying.
    """
    edges = edges or {}
    out: dict = {}
    for key, m in MODELS.items():
        pool = [r for r in rows
                if isinstance((r.get("scores") or {}).get(key), (int, float))]
        if not pool:
            continue

        # A headline card shouldn't be padded with rows the model itself flags
        # as unreliable — but in preseason there may not BE five solid ones, so
        # backfill rather than publish a short ladder, and let the flag ride so
        # the site can show what it is.
        solid = [r for r in pool if not r.get("low_sample")]
        use = solid if len(solid) >= depth else (
            solid + [r for r in pool if r.get("low_sample")])
        use = sorted(use, key=lambda r: -float(r["scores"][key]))[:depth]

        out[key] = {
            "key": key,
            "label": m["label"],
            "bar": m["bar"],
            "positions": m["pos"],
            "edge": edges.get(key),
            "rungs": [_rung(r, key, i + 1) for i, r in enumerate(use)],
        }
    return out


def summary(card: dict) -> str:
    """Terminal view, for the workflow log."""
    lines = []
    for key, blk in card.items():
        e = blk.get("edge") or {}
        tag = (f"hit {e['hit']:.0f}% · edge {e['edge']:+.1f} ±{e['se']:.1f} ({e['trust']})"
               if e else "no measured edge")
        lines.append(f"\n{blk['label']}  —  bar {blk['bar']} · {tag}")
        for r in blk["rungs"]:
            flags = "".join([" ?" if r["questionable"] else "",
                             " ~" if r["low_sample"] else "",
                             " CO" if r["carryover"] else ""])
            lines.append(f"  {r['rank']}. {r['score']:>5.1f} {r['grade']:<2} "
                         f"{r['name']:<24} {r['position']:<3} {r['team']} "
                         f"vs {r['opp'] or '?'}{flags}")
    return "\n".join(lines)
