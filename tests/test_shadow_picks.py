"""stamp_shadow_picks (2026-10-01). Test data, made up: one game, four hitters.
Checks each rule stamps exactly one #1 and one #2 per game, ranks by the
rule's own inputs, skips hitters under 15 PA, and never touches game_pick_role.
Run: PYTHONPATH=. python3 tests/test_shadow_picks.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
sys.argv = ["x"]
import mlb_dashboard as md  # noqa: E402

FAILED, CHECKS = [], 0
def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want: FAILED.append(f"{name}: got {got!r}, want {want!r}")

def row(pid, power, hrw, pside, prob, pa=300, role=""):
    return {"player_id": pid, "game_pk": 9, "season_pa": pa, "game_pick_role": role,
            "season_avg": 0.25 + pid / 100, "season_slg": 0.4 + pid / 50, "season_iso": 0.15 + pid / 100,
            "season_k_rate": 0.25 - pid / 100, "lineup_spot": pid, "pitcher_throws": "R", "avg_vs_rhp": 0.24 + pid / 100,
            "hr_shape_components": {"season_power_baseline": power}, "hrw_score": hrw,
            "pitcher_side_ops": pside, "season_hr_game_probability": prob}
rows = [row(1, 90, 20, 0.700, 0.10, role="TOP"), row(2, 60, 80, 0.900, 0.12),
        row(3, 85, 70, 0.950, 0.20, role="HR"), row(4, 99, 99, 0.999, 0.30, pa=10)]
md.stamp_shadow_picks(rows)
by = {r["player_id"]: r["shadow_pick"] for r in rows}
check("under 15 PA never picked", by[4], {})
for rule in md.SHADOW_PICK_RULES:
    slots = sorted(s for r in rows for k, s in r["shadow_pick"].items() if k == rule)
    check(f"{rule}: one #1 and one #2", slots, [1, 2])
check("power_pside #1 is the best power+pside blend", [p for p, s in by.items() if s.get("power_pside") == 1], [3])
for rule in ("hit:avg_hand_k", "tb:slg_spot"):
    check(f"{rule} stamps a #1", sum(1 for r in rows if r["shadow_pick"].get(rule) == 1), 1)
check("neg_ inputs invert", md._shadow_input({"season_k_rate": 0.2}, "neg_k_rate"), -0.2)
check("avg_vs_hand follows the starter's hand", md._shadow_input({"pitcher_throws": "L", "avg_vs_lhp": 0.31, "avg_vs_rhp": 0.2}, "avg_vs_hand"), 0.31)
check("roles untouched", [r["game_pick_role"] for r in rows], ["TOP", "", "HR", ""])
print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
for f in FAILED: print("  FAIL", f)
sys.exit(1 if FAILED else 0)
