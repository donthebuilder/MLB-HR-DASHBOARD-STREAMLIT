#!/usr/bin/env python3
"""
🚬 SMOKE TEST — does the scorer still run at all?

2026-08-09, Donovan: "the site's bot crashed today."

It crashed after a day in which I changed the blend weights, added two terms,
rewrote contact_score, retired the 6-man pool and rewrote pair_score — and
every one of those edits was verified by `python3 -m py_compile`, which proves
only that the file PARSES. A NameError, a KeyError on a thin slate, an index
into a list that got shorter: none of those are syntax errors, and none of them
were caught.

So: build a record, push it through the scorer, build a slate, push it through
the section builders. Runs in about a second and would have caught it.

    python3 bots/smoke_test.py
"""
from __future__ import annotations

import dataclasses
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlb_dashboard as M  # noqa: E402


def make_record(**kw):
    flds = {f.name: f for f in dataclasses.fields(M.HitterRecord)}
    args = {}
    for n, f in flds.items():
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        t = str(f.type)
        args[n] = 0 if "int" in t else (0.0 if "float" in t else ("" if "str" in t else None))
    args.update(kw)
    return M.HitterRecord(**args)


def a_slate(n=40):
    """A believable slate: varied power, varied arms, both sides of the plate."""
    out = []
    for i in range(n):
        out.append(make_record(
            player_id=1000 + i, name=f"Hitter {i}", team=f"T{i % 10}",
            opponent=f"O{i % 10}", game_pk=800000 + (i % 10), bats="LR"[i % 2],
            lineup_spot=(i % 9) + 1, venue_name=f"Park {i % 10}",
            season_avg=0.230 + (i % 9) * 0.008, season_iso=0.110 + (i % 12) * 0.018,
            season_slg=0.360 + (i % 12) * 0.020, season_hr=5 + i % 30,
            season_pa=250 + i * 4, season_k_rate=0.16 + (i % 10) * 0.016,
            last5_hr=i % 3, last10_hr=i % 4, last5_hits=i % 7, last5_xbh=i % 4,
            last10_xbh=i % 6, l20pa_hr=i % 3, l20pa_xbh=i % 4,
            pitcher_throws="RL"[i % 2], pitcher_whip=1.00 + (i % 9) * 0.08,
            pitcher_era=2.80 + (i % 9) * 0.35, pitcher_hr9=0.7 + (i % 9) * 0.16,
            pitcher_side_ops=0.640 + (i % 9) * 0.028,
            pitcher_side_slug=0.350 + (i % 9) * 0.022,
        ))
    return out


def main() -> int:
    fails = []

    def step(name, fn):
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:
            fails.append(name)
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            traceback.print_exc()

    rows = a_slate()
    step("score one hitter", lambda: M.apply_model_v2_layers(rows[0]))
    step("score a whole slate", lambda: [M.apply_model_v2_layers(r) for r in rows])

    # Every weighted term must reach the blend and the weights must still sum.
    def weights_ok():
        w = M.MODEL_WEIGHTS["hr_blend"]
        assert abs(sum(w.values()) - 1.0) < 1e-9, f"hr_blend sums to {sum(w.values())}"
    step("hr_blend weights sum to 1.00", weights_ok)

    # MODEL FOUNDATION (2026-08-21): the registry must import/validate, and
    # stamping a row for the prediction log must never touch its score.
    # Full coverage lives in tests/test_model_foundation.py; this is the
    # ~1-second version that runs on every commit, matching this file's own
    # purpose statement above.
    def model_registry_ok():
        assert M.MODEL_REGISTRY is not None, "model_registry failed to import"
        assert M.MODEL_REGISTRY.MODEL_VERSIONS.get("hr"), "no hr version registered"
    step("model_registry imports and validates", model_registry_ok)

    def stamping_does_not_touch_score():
        r = make_record(player_id=1, name="Stamp Check", season_iso=0.280)
        M.apply_model_v2_layers(r)
        before = r.hr_score
        r.model_version = M.MODEL_REGISTRY.MODEL_VERSIONS["hr"]
        r.run_id = "smoke-test-run"
        assert r.hr_score == before, "stamping model_version/run_id changed hr_score"
    step("stamping model_version/run_id does not change hr_score", stamping_does_not_touch_score)

    # recent_form is the term that was silently reading 0.0 — assert it moves.
    def form_lives():
        hot = make_record(player_id=1, name="Hot", last5_hr=3, last10_hr=5,
                          l20pa_hr=3, l20pa_xbh=4, last5_xbh=3, season_iso=0.300)
        cold = make_record(player_id=2, name="Cold", season_iso=0.300)
        M.apply_model_v2_layers(hot)
        M.apply_model_v2_layers(cold)
        assert hot.hr_score > cold.hr_score, (
            f"recent form is not reaching the score: hot {hot.hr_score} "
            f"vs cold {cold.hr_score}")
    step("recent form actually moves the score", form_lives)

    for name in ("build_pair_sections", "build_alt_looks_section", "build_top_section"):
        if hasattr(M, name):
            step(f"{name} on a full slate", lambda n=name: getattr(M, n)(rows))
            # A thin slate is where index errors live.
            step(f"{name} on a 6-row slate", lambda n=name: getattr(M, n)(rows[:6]))
            step(f"{name} on an EMPTY slate", lambda n=name: getattr(M, n)([]))

    print()
    if fails:
        print(f"{len(fails)} step(s) failed: {', '.join(fails)}")
        return 1
    print("smoke test clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
