"""Builds the Top Plays content object — a leaderboard of the night's best
actual performances (real hits/HR from the graded sheet), not model scores.
Complements Player Spotlight (one player, full treatment) with a wider,
numbers-first list: "here's the top 10 lines of the night."

Same rule as every other builder here: every row comes straight from
graded_results_<date>.json's actual_* fields — nothing recomputed, nothing
estimated.
"""

from __future__ import annotations

import datetime as dt
from typing import Any


def build(*, date_str: str | None = None, graded: dict[str, Any], top_n: int = 10) -> dict[str, Any] | None:
    """Returns a Top Plays `data` dict from an already-fetched, already-
    complete `graded` payload (pass night_recap.fetch_graded()'s result —
    this module does not fetch anything itself, it's always run alongside
    the Night Recap / Player Spotlight in the same script, same run)."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    slots = (graded or {}).get("graded_slots") or []
    best: dict[Any, dict[str, Any]] = {}
    for r in slots:
        if int(r.get("actual_ab") or 0) == 0:
            continue
        pid = r.get("player_id")
        if pid is None:
            continue
        hr = int(r.get("actual_hr") or 0)
        hits = int(r.get("actual_hits") or 0)
        cur = best.get(pid)
        if cur is None or (hr, hits) > (int(cur.get("actual_hr") or 0), int(cur.get("actual_hits") or 0)):
            best[pid] = r

    ranked = sorted(best.values(), key=lambda r: (-int(r.get("actual_hr") or 0), -int(r.get("actual_hits") or 0)))
    ranked = [r for r in ranked if int(r.get("actual_hr") or 0) or int(r.get("actual_hits") or 0)][:top_n]
    if not ranked:
        return None

    plays: list[dict[str, Any]] = []
    for r in ranked:
        plays.append({
            "name": r.get("name"),
            "team": r.get("team"),
            "hr": int(r.get("actual_hr") or 0),
            "hits": int(r.get("actual_hits") or 0),
            "ab": int(r.get("actual_ab") or 0),
            "tb": int(r.get("actual_tb") or 0),
        })

    data: dict[str, Any] = {"date": date_str, "count": len(plays), "plays": plays}
    return {k: v for k, v in data.items() if v not in (None, [], "")}
