"""Small-sample shrinkage (the Veen bug) + the coverage slot / WATCH tier /
per-game coverage report (2026-08-23).

Three changes, one design (claude/moonshot-precision-coverage-design.md):

  1. shrink_to_league(): a season rate is regressed toward league average
     by PA, K=150, so a 15-PA "ISO .500" (Zac Veen, 2026-08-17, ranked over
     Max Muncy's 435-PA .249 -- and Muncy homered) reads ~.19, not elite.
     K is from first principles, NOT swept on the leaked archive.
  2. HR is the COVERAGE slot: build_game_pick_role_map() badges the best
     remaining power bat, required distinct from TOP (reverts the 08-12
     same-man double-up now that the two slots have different jobs), and
     stamps a WATCH tier -- the next 3 bats by hr_score -- so the covered
     set per game is TOP + HR + HIT + HRR + CONTACT + 3 WATCH.
  3. build_pick_coverage_report(): the per-game scoreboard for Donovan's
     standing target ("90% or close -- any designated pick homers, per
     game"), with uncovered games listed by name.

Run: python tests/test_shrinkage_and_coverage.py
"""
import dataclasses
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bots.mlb_dashboard as md  # noqa: E402
import bots.live_results_tracker as lrt  # noqa: E402

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def checkTrue(name, cond):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILED.append(f"{name}: expected truthy, got falsy")


# ── 1. the shrinkage arithmetic ──────────────────────────────────────────
s = md.shrink_to_league
K, L = md.SHRINK_K_PA, md.LEAGUE_ISO
check("0 PA -> exactly league", s(0.999, 0, L), L)
check("PA == K -> halfway between player and league",
      round(s(0.300, K, L), 4), round((0.300 + L) / 2, 4))
veen = s(0.500, 15, L)
muncy = s(0.249, 435, L)
check("VEEN: 15 PA 'ISO .500' shrinks to ~.191", round(veen, 3), 0.191)
check("MUNCY: 435 PA .249 keeps most of itself (~.226)", round(muncy, 3), 0.226)
checkTrue("the fluke now ranks BELOW the real bat", veen < muncy)
checkTrue("a large sample barely moves (600 PA .249 within .02 of itself)",
          abs(s(0.249, 600, L) - 0.249) < 0.02)
check("negative/garbage PA treated as 0", s(0.4, -50, L), L)
checkTrue("shrink_to_league is config_hash-covered",
          md.shrink_to_league in md._HR_CONFIG_FORMULA_FUNCS)


# ── HitterRecord factory (134 required fields -> typed zeros) ────────────
def mk(**kw):
    args = {}
    for f in dataclasses.fields(md.HitterRecord):
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            t = str(f.type)
            if "int" in t:
                args[f.name] = 0
            elif "float" in t:
                args[f.name] = 0.0
            elif "bool" in t:
                args[f.name] = False
            else:
                args[f.name] = "" if "str" in t else None
    args.update(kw)
    return md.HitterRecord(**args)


def roles_of(role_map, game_pk):
    """{role_token: player_id} for single-token lookups."""
    out = {}
    for (gp, pid), roles in role_map.items():
        if gp != game_pk:
            continue
        for tok in roles.split("/"):
            out.setdefault(tok, []).append(pid)
    return out


# ── 2. the Veen game, end to end through build_game_pick_role_map ────────
GP = 900500
veen_r = mk(game_pk=GP, player_id=1, name="Zac Veen", season_iso=0.500,
            season_pa=15, last5_hr=0, hr_score=70.0, overall_score=55.0,
            hit_score=10.0, hrr_score=10.0, contact_score=10.0)
muncy_r = mk(game_pk=GP, player_id=2, name="Max Muncy", season_iso=0.249,
             season_pa=435, last5_hr=1, hr_score=66.5, overall_score=60.0,
             hit_score=20.0, hrr_score=20.0, contact_score=20.0)
others = [mk(game_pk=GP, player_id=10 + i, name=f"Bat {i}", season_iso=0.150,
             season_pa=300, hr_score=30.0 - i, overall_score=30.0 - i,
             hit_score=50.0 - i, hrr_score=40.0 - i, contact_score=45.0 - i)
          for i in range(5)]
rm = md.build_game_pick_role_map([veen_r, muncy_r] + others)
r = roles_of(rm, GP)
check("TOP goes to the real power bat, not the 15-PA fluke (the Veen fix)",
      r.get("TOP"), [2])
check("HR is a DIFFERENT player than TOP (the coverage slot)",
      r.get("HR"), [1])
checkTrue("no player holds both TOP and HR",
          not set(r.get("TOP", [])) & set(r.get("HR", [])))
check("WATCH tier stamps exactly 3 bats", len(r.get("WATCH", [])), 3)
checkTrue("WATCH never includes TOP or HR holders",
          not set(r.get("WATCH", [])) & {r["TOP"][0], r["HR"][0]})
# WATCH = next 3 by hr_score after TOP/HR: others' top hr_scores 30,29,28
check("WATCH is the next bats by hr_score", sorted(r["WATCH"]), [10, 11, 12])

# a HIT/HRR/CONTACT holder may also carry WATCH (combined tags supported)
combined = [v for (gp, pid), v in rm.items() if gp == GP and "WATCH" in v and v != "WATCH"]
checkTrue("combined tags like HIT/WATCH survive the join", len(combined) >= 1)


# ── 3. the per-game coverage report ──────────────────────────────────────
rows = [
    {"game_pk": 1, "player_id": 11, "name": "Top Guy", "game_pick_role": "TOP"},
    {"game_pk": 1, "player_id": 12, "name": "Hr Guy", "game_pick_role": "HR"},
    {"game_pk": 2, "player_id": 21, "name": "Hit Guy", "game_pick_role": "HIT"},
    {"game_pk": 3, "player_id": 31, "name": "Watch Guy", "game_pick_role": "WATCH"},
    {"game_pk": 4, "player_id": 41, "name": "Nobody Named", "game_pick_role": ""},
    {"game_pk": 5, "player_id": 51, "name": "Quiet Top", "game_pick_role": "TOP"},
]
homers = [
    {"game_pk": 1, "player_id": 11, "name": "Top Guy"},       # covered: TOP
    {"game_pk": 2, "player_id": 21, "name": "Hit Guy"},       # covered: badge, not TOP/HR
    {"game_pk": 3, "player_id": 31, "name": "Watch Guy"},     # covered ONLY by WATCH
    {"game_pk": 4, "player_id": 41, "name": "Nobody Named"},  # uncovered
    # game 5 had no homer -> not an HR game at all
]
rep = lrt.build_pick_coverage_report(rows, homers)
check("HR games counted", rep["games_with_hr"], 4)
check("TOP/HR ring", rep["covered_top_hr"], 1)
check("any-real-badge ring (WATCH excluded)", rep["covered_any_pick"], 2)
check("with-WATCH ring", rep["covered_with_watch"], 3)
check("pct_with_watch", rep["pct_with_watch"], 75.0)
check("targets ride with the numbers", rep["targets"]["pct_with_watch"], 90.0)
check("the missed game is listed by name",
      rep["uncovered_games"], [{"game_pk": 4, "homered": ["Nobody Named"]}])
empty = lrt.build_pick_coverage_report(rows, [])
check("no homers -> honest None percentages, never a crash",
      (empty["games_with_hr"], empty["pct_with_watch"]), (0, None))

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   shrinkage + coverage slot + WATCH + coverage report: {CHECKS} "
      f"assertions — a 15-PA fluke no longer outranks a 435-PA bat, TOP and HR "
      f"are two different players with two different jobs, WATCH widens the "
      f"covered set to 8 per game, and the nightly scoreboard reports all "
      f"three coverage rings with misses listed by name")
