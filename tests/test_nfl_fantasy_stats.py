"""nfl_fantasy_stats.parse_game against a REAL Week 1 summary (TB@CIN,
2026-09-13, ESPN event 401872925, trimmed to the keys the parser reads).

Run: python3 tests/test_nfl_fantasy_stats.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots" / "nfl"))
import nfl_fantasy_stats as fs  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "espn_summary_401872925_tb_cin_2026w1.json"


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    summary = json.loads(FIX.read_text())
    players, defense, notes = fs.parse_game(summary, "CIN", "TB", 33, 27)
    by_name = {v["_name"]: v for v in players.values()}

    # Baker Mayfield: 23/28 216 0 TD 0 INT, 3 fumbles lost, 9-yd rushing TD.
    bm = by_name["Baker Mayfield"]
    check(bm["passing_yards"] == 216 and bm["passing_touchdowns"] == 0 and bm["interceptions"] == 0, bm)
    check(bm["fumbles_lost"] == 3, bm)
    check(bm["rushing_touchdowns"] == 1, bm)

    # Bucky Irving: 8-45-1 rushing.
    bi = by_name["Bucky Irving"]
    check(bi["rushing_yards"] == 45 and bi["rushing_touchdowns"] == 1, bi)

    # Emeka Egbuka: 5-63-0 receiving.
    ee = by_name["Emeka Egbuka"]
    check(ee["receptions"] == 5 and ee["receiving_yards"] == 63, ee)

    # Kickers, placed by distance off scoringPlays: McLaughlin 34 + 51,
    # McPherson 20, 55, 58, 32. XP from the box (3/3, 3/3).
    cm = by_name["Chase McLaughlin"]
    check((cm["field_goals_0_39"], cm["field_goals_40_49"], cm["field_goals_50_plus"]) == (1, 0, 1), cm)
    check(cm["extra_points"] == 3, cm)
    em = by_name["Evan McPherson"]
    check((em["field_goals_0_39"], em["field_goals_40_49"], em["field_goals_50_plus"]) == (2, 0, 2), em)
    check(not [n for n in notes if "FG" in n], notes)

    # D/ST. TB's INT-return TD (Trotter) is a defensive TD for TB; CIN's
    # fumble-return TD (Knight) is one for CIN. Fumble recoveries are the
    # opponent's fumbles lost: TB lost 4 -> CIN recovered 4.
    check(defense["TB"]["points_allowed"] == 33 and defense["CIN"]["points_allowed"] == 27, defense)
    check(defense["TB"]["def_touchdowns"] == 1 and defense["CIN"]["def_touchdowns"] == 1, defense)
    check(defense["CIN"]["def_fumble_recoveries"] == 4, defense)
    check(defense["TB"]["def_interceptions"] >= 1, defense)

    # Every emitted key is one the site's fantasy_points_for_stats reads.
    for row in players.values():
        for k in fs.PLAYER_KEYS:
            check(isinstance(row[k], float), (k, row))

    # A scheduled game contributes nothing (build() skips it; parse_game on
    # an empty summary must not invent a line either).
    p2, d2, _ = fs.parse_game({}, "A", "B", 0, 0)
    check(p2 == {}, p2)
    check(d2["A"]["def_sacks"] == 0 and d2["A"]["points_allowed"] == 0, d2)

    print(f"ok  {len(players)} lines parsed, {len(notes)} note(s)")


if __name__ == "__main__":
    main()
