#!/usr/bin/env python3
"""
Tests for bots/hr_v3_shadow.py.

Runnable BOTH as a pytest module and as a plain script — every other file in
tests/ is a script and pytest is not installed everywhere this repo runs.

    python3 tests/test_hr_v3_shadow.py
    pytest  tests/test_hr_v3_shadow.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))
import hr_v3_shadow as V  # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    if cond:
        return
    FAILS.append(msg)
    print("  RED  " + msg)


def prof(**kw):
    base = {"season_bbe": 200, "season_avg_ev": 88.7, "season_max_distance": 419.0,
            "season_hard_hit_rate": 0.402, "season_avg_la": 13.0}
    base.update(kw)
    return base


def test_weights():
    print("weights")
    total = sum(w for _, w, _, _ in V.TERMS)
    check(abs(total - 1.0) < 1e-9, f"the four term weights must sum to 1.00, sum to {total}")
    fields = [f for f, _, _, _ in V.TERMS]
    check(len(set(fields)) == len(fields), "no term appears twice")
    for f, w, lo, hi in V.TERMS:
        check(hi > lo, f"{f}: league_high must exceed league_low")
        check(0 < w < 1, f"{f}: weight must be a proper fraction")
    check(fields[0] == "season_avg_ev", "exit velocity is the anchor term and carries the top weight")
    check(max(V.TERMS, key=lambda t: t[1])[0] == "season_avg_ev",
          "no term may outweigh season_avg_ev — it is the strongest measured signal")


def test_barrel_is_deliberately_absent():
    """The load-bearing decision in the file. Barrel looks strong alone
    (z=+14.90) and adds +2.81pp once EV is known, less than max distance
    (+3.80) and FB rate (+3.34); in a five-term fit it goes negative. A
    barrel-led variant was the only tested weighting that is clearly worse
    (17.12% vs 19.23% at ten picks a night). If someone adds it back, this
    test should be what makes them argue for it."""
    print("barrel is absent on purpose")
    fields = [f for f, _, _, _ in V.TERMS]
    check("season_barrel_rate" not in fields,
          "season_barrel_rate is back in the blend — see the docstring before keeping it")
    check(not any("l20_" in f or "l25_" in f for f in fields),
          "a recent window is back in the blend — L20 EV adds +1.85pp over season, "
          "L20 barrel +0.31pp, and the recent fields are the leak surface")


def test_scoring_range_and_monotonicity():
    print("range and monotonicity")
    lo, _ = V.score_row(prof(season_avg_ev=80.0, season_max_distance=340.0,
                             season_hard_hit_rate=0.15, season_avg_la=2.0))
    hi, _ = V.score_row(prof(season_avg_ev=96.0, season_max_distance=470.0,
                             season_hard_hit_rate=0.70, season_avg_la=24.0))
    check(0.0 <= lo <= 100.0 and 0.0 <= hi <= 100.0, "the score stays inside 0-100")
    check(lo < 5.0, f"a bat below every league floor should score near 0, scored {lo}")
    check(hi > 95.0, f"a bat above every league ceiling should score near 100, scored {hi}")
    prev = -1.0
    for ev in (82.0, 85.0, 88.0, 91.0, 94.0):
        s, _ = V.score_row(prof(season_avg_ev=ev))
        check(s > prev, f"the score must rise with exit velocity (ev={ev} gave {s})")
        prev = s


def test_low_sample_refuses():
    print("low sample refuses rather than guessing")
    s, st = V.score_row(prof(season_bbe=10, season_avg_ev=99.0))
    check(s == V.NEUTRAL, f"a 10-BBE bat must publish neutral, published {s}")
    check(st.startswith("low_sample"), f"status must say why, said {st}")
    check(V.MIN_BBE >= 25, "the minimum sample must stay at 25+ batted balls")
    s2, st2 = V.score_row(prof(season_bbe=25))
    check(st2 == "ok", "exactly 25 batted balls is scoreable")


def test_missing_terms():
    print("missing terms dilute toward neutral, never toward zero")
    p = prof(); p.pop("season_avg_la")
    s, st = V.score_row(p)
    check(st == "ok", "one missing term out of four is still scoreable")
    full, _ = V.score_row(prof(season_avg_la=13.0))
    check(abs(s - full) < 25.0, "dropping one term must not swing the score wildly")
    p2 = prof(); p2.pop("season_avg_ev"); p2.pop("season_max_distance")
    s2, st2 = V.score_row(p2)
    check(s2 == V.NEUTRAL and st2 == "insufficient_terms",
          "losing 65% of the weight must publish neutral, not a confident number")
    p3 = prof(season_avg_ev=None, season_max_distance="--")
    s3, st3 = V.score_row(p3)
    check(s3 == V.NEUTRAL, "None and '--' are missing values, not zeros")


def test_neutral_is_not_zero():
    """A bat that cannot be scored must not sort to the bottom of the board as
    though it had been measured and found wanting."""
    print("unscoreable is not the same as bad")
    check(V.NEUTRAL == 50.0, "an unscoreable bat publishes 50, mid-board")


def test_grade_ledger_is_idempotent():
    print("grading the same night twice does not double-count it")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "public" / "data" / "current" / "bbe_history").mkdir(parents=True)
        old_data, old_bbe = V.DATA, V.BBE
        try:
            V.DATA = root / "public" / "data" / "current"
            V.BBE = V.DATA / "bbe_history"
            rows = [{"player_id": i, "name": f"B{i}", "team": "X", "game_pk": 1,
                     "hr_score_v3": 100 - i, "hr_score_v3_status": "ok",
                     "hr_score_live": 50.0, "game_pick_role": "TOP" if i == 0 else "",
                     "season_bbe": 100} for i in range(20)]
            (V.DATA / "hr_v3_2026-08-01.json").write_text(json.dumps(
                {"date": "2026-08-01", "n": 20, "rows": rows}))
            with (V.BBE / "bbe_2026-08-01.jsonl").open("w") as fh:
                for i in range(20):
                    fh.write(json.dumps({"batter_id": i, "is_hr": i in (0, 3)}) + "\n")

            class A:
                date = "2026-08-01"
            V.cmd_grade(A())
            V.cmd_grade(A())
            lines = (V.DATA / "hr_v3_record.jsonl").read_text().strip().splitlines()
            check(len(lines) == 1, f"one night must leave one ledger row, left {len(lines)}")
            rec = json.loads(lines[0])
            check(rec["homers"] == 2 and rec["graded"] == 20, "the night's totals are right")
            # batters 0 and 3 homered and v3 ranks them 1st and 4th, so a
            # top-5 slice contains both. A top-3 slice contains only batter 0.
            check(rec["v3_top5"] == [2, 5], f"v3 top-5 holds both homerers, got {rec['v3_top5']}")
            check(rec["v3_top10"] == [2, 10], f"v3 top-10 holds both homerers, got {rec['v3_top10']}")
            check(rec["top_badge"] == [1, 1], "the TOP badge holder homered in the fixture")
        finally:
            V.DATA, V.BBE = old_data, old_bbe


def main() -> int:
    for fn in (test_weights, test_barrel_is_deliberately_absent,
               test_scoring_range_and_monotonicity, test_low_sample_refuses,
               test_missing_terms, test_neutral_is_not_zero,
               test_grade_ledger_is_idempotent):
        fn()
    if FAILS:
        print(f"\n{len(FAILS)} RED")
        return 1
    print("\nall green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
