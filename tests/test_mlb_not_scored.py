#!/usr/bin/env python3
"""MOONSHOT's not-scored hitters (2026-10-18, mlb_dashboard.not_scored_row).

The PA filter's dropped hitters ship in their own file with their season line and
nothing else: no score field, no rank, no pick. A man with 0 PA carries no rate
(the defaults are not measurements). And the writer never touches rows_payload.
"""
import os, re, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import mlb_dashboard as m  # noqa: E402

FAILED = []
def check(name, got, want):
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")

base = dict(game_pk=1, player_id=7, name="Test Hitter", team="SEA", opponent="HOU", bats="R",
            lineup_spot=9, lineup_confirmed=True, l20pa_pa=6, season_hr=1,
            season_avg=.210, season_obp=.280, season_slg=.350, season_iso=.140,
            hr_score=88.0, overall_score=70.0)
r = m.not_scored_row(types.SimpleNamespace(**base, season_pa=40))
check("keeps exactly the season line", set(r), set(m.NOT_SCORED_FIELDS) | {"reason"})
check("no score leaks out", "hr_score" in r, False)
check("real rates kept", (r["season_avg"], r["season_hr"]), (.210, 1))
check("reason names the filter", r["reason"], f"under {m.GLOBAL_MIN_RECENT_PA} recent PA and {m.GLOBAL_MIN_SEASON_PA} season PA")
z = m.not_scored_row(types.SimpleNamespace(**base, season_pa=0))
check("0 PA: no rates", [z[k] for k in ("season_avg", "season_obp", "season_slg", "season_iso", "season_hr")], [None] * 5)

src = open(m.__file__, encoding="utf-8").read()
check("rows_payload never reads not_scored rows", bool(re.search(r"rows_payload\s*=.*not_scored", src)), False)

if FAILED:
    print("\n".join(FAILED))
print(f"mlb not scored: {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
