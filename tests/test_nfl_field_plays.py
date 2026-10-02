"""nfl_field.team_plays -- The Field's per-play file (2026-09-30).

Test data, made up for this file: two games, a handful of plays. No network:
pbp and FTN are passed in. Checks:
  1. a target is a pass_attempt with a receiver; sacks and two-point tries
     are not targets.
  2. results: catch / inc / int / td, and yac + gain are 0 on an incompletion.
  3. FTN joins on (game_id, play_id); "0" hash / box is null, never a value;
     a play FTN didn't chart is null, not dropped.
  4. the red-zone list is every target and every carry inside the 20
     (yardline_100 <= 20), carries as carry / td.
  5. weeks most recent first; an empty FTN frame still builds.

Run: PYTHONPATH=. python3 tests/test_nfl_field_plays.py
"""
import os
import sys

import polars as pl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))
import nfl_field  # noqa: E402

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def _pbp():
    base = dict(season_type="REG", posteam="NO", defteam="ATL", qtr=1.0, down=1.0, ydstogo=10.0,
                two_point_attempt=0.0, rush_attempt=0.0, rusher_player_id=None, rusher_player_name=None,
                rush_touchdown=0.0, interception=0.0, pass_touchdown=0.0, complete_pass=0.0,
                yards_after_catch=None, epa=0.5)
    rows = [
        # wk1 catch, 12 air + 3 yac, from the 60
        dict(base, game_id="g1", play_id=1.0, week=1, pass_attempt=1.0, receiver_player_id="R1",
             receiver_player_name="T.Est", yardline_100=60.0, pass_location="right", air_yards=12.0,
             complete_pass=1.0, yards_after_catch=3.0, yards_gained=15.0),
        # wk1 incompletion
        dict(base, game_id="g1", play_id=2.0, week=1, pass_attempt=1.0, receiver_player_id="R1",
             receiver_player_name="T.Est", yardline_100=45.0, pass_location="left", air_yards=25.0,
             yards_gained=0.0),
        # wk1 sack: no receiver, not a target
        dict(base, game_id="g1", play_id=3.0, week=1, pass_attempt=1.0, receiver_player_id=None,
             receiver_player_name=None, yardline_100=50.0, pass_location=None, air_yards=None,
             yards_gained=-7.0),
        # wk2 interception
        dict(base, game_id="g2", play_id=1.0, week=2, pass_attempt=1.0, receiver_player_id="R2",
             receiver_player_name="O.Ther", yardline_100=30.0, pass_location="middle", air_yards=8.0,
             interception=1.0, yards_gained=0.0),
        # wk2 red-zone TD catch from the 7
        dict(base, game_id="g2", play_id=2.0, week=2, pass_attempt=1.0, receiver_player_id="R1",
             receiver_player_name="T.Est", yardline_100=7.0, pass_location="left", air_yards=6.0,
             complete_pass=1.0, pass_touchdown=1.0, yards_after_catch=1.0, yards_gained=7.0),
        # wk2 two-point try: not a target
        dict(base, game_id="g2", play_id=3.0, week=2, pass_attempt=1.0, receiver_player_id="R1",
             receiver_player_name="T.Est", yardline_100=2.0, pass_location="left", air_yards=2.0,
             two_point_attempt=1.0, yards_gained=0.0),
        # wk2 red-zone carry for a TD, and one outside the 20 (not listed)
        dict(base, game_id="g2", play_id=4.0, week=2, pass_attempt=0.0, receiver_player_id=None,
             receiver_player_name=None, yardline_100=3.0, pass_location=None, air_yards=None,
             rush_attempt=1.0, rusher_player_id="B1", rusher_player_name="R.Unner",
             rush_touchdown=1.0, yards_gained=3.0),
        dict(base, game_id="g2", play_id=5.0, week=2, pass_attempt=0.0, receiver_player_id=None,
             receiver_player_name=None, yardline_100=40.0, pass_location=None, air_yards=None,
             rush_attempt=1.0, rusher_player_id="B1", rusher_player_name="R.Unner", yards_gained=4.0),
    ]
    return pl.DataFrame(rows, infer_schema_length=None)


def _ftn():
    return pl.DataFrame({
        "game_id": ["g1", "g1", "g2"], "play_id": [1.0, 2.0, 1.0],
        "hash": ["L", None, "R"], "box": [7, None, 6], "pa": [True, False, False], "sc": [False, False, True],
    })


out = nfl_field.team_plays(2026, pbp=_pbp(), ftn=_ftn())
check("one offence", sorted(out), ["NO"])
no = out["NO"]
rows = [dict(zip(no["cols"], r)) for r in no["plays"]]
check("targets (sack + two-point dropped)", len(rows), 4)
check("results in order", [r["res"] for r in rows], ["catch", "inc", "int", "td"])
check("catch air/yac/gain", (rows[0]["air"], rows[0]["yac"], rows[0]["gain"]), (12, 3, 15))
check("inc yac + gain are 0", (rows[1]["yac"], rows[1]["gain"]), (0, 0))
check("lanes", [r["lane"] for r in rows], ["R", "L", "M", "L"])
check("ftn joined", (rows[0]["hash"], rows[0]["box"], rows[0]["pa"], rows[0]["sc"]), ("L", 7, 1, 0))
check("ftn null stays null", (rows[1]["hash"], rows[1]["box"]), (None, None))
check("uncharted play kept, nulls", (rows[3]["hash"], rows[3]["box"]), (None, None))
rz = [dict(zip(no["rz_cols"], r)) for r in no["redzone"]]
check("red zone touches", sorted((r["pid"], r["d"], r["kind"], r["res"]) for r in rz),
      [("B1", 3, "rush", "td"), ("R1", 7, "pass", "td")])
check("weeks most recent first", no["weeks"], [2, 1])
check("names", no["names"], {"R1": "T.Est", "R2": "O.Ther", "B1": "R.Unner"})

bare = nfl_field.team_plays(2026, pbp=_pbp(), ftn=pl.DataFrame())
check("empty FTN still builds", len(bare["NO"]["plays"]), 4)
check("empty FTN nulls", bare["NO"]["plays"][0][no["cols"].index("hash")], None)

# ── ROUTES (2026-10-02): the participation join and the defence totals.
# Test data: routes for three of the four targets; g2/play 2 (the TD) has no
# participation row and must keep nulls, not drop.
_routes = pl.DataFrame({
    "game_id": ["g1", "g1", "g2", "g1"], "play_id": [1.0, 2.0, 1.0, 1.0],
    "route": ["IN/DIG", "GO", "SLANT", "IN/DIG"],                      # g1/1 twice: deduped
    "defense_man_zone_type": ["ZONE_COVERAGE", "MAN_COVERAGE", "ZONE_COVERAGE", "ZONE_COVERAGE"],
    "defense_coverage_type": ["COVER_3", None, "COVER_2", "COVER_3"],
})
rb = nfl_field.route_bundle(2025, pbp=_pbp(), ftn=_ftn(), routes=_routes, grids={"def_pass": {"ATL": {}}, "league_pass": {}})
rno = rb["teams"]["NO"]
rr = [dict(zip(rno["cols"], r)) for r in rno["plays"]]
check("route cols", rno["cols"][-3:], ["rt", "mz", "cv"])
check("route file keeps every target (dup participation row deduped)", len(rr), 4)
check("routes joined", [r["rt"] for r in rr], ["IN/DIG", "GO", "SLANT", None])
check("man/zone + shell", [(r["mz"], r["cv"]) for r in rr], [("Z", "C3"), ("M", None), ("Z", "C2"), (None, None)])
check("route file has no red zone (the 2026 file carries it)", rno["redzone"], [])
check("who: most targets first", rb["who"], {"R1": ["NO"], "R2": ["NO"]})
d = rb["def"]
check("def by route", d["def_route"]["ATL"]["IN/DIG"], {"tgt": 1, "cmp": 1, "yds": 15, "td": 0, "ypt": 15.0})
check("def tgt adds up to the routed targets", sum(v["tgt"] for v in d["def_route"]["ATL"].values()), 3)
check("def by coverage (M/Z + shells)", sorted(d["def_cov"]["ATL"]), ["C2", "C3", "M", "Z"])
check("zone tally", d["def_cov"]["ATL"]["Z"], {"tgt": 2, "cmp": 1, "yds": 15, "td": 0, "ypt": 7.5})
check("league = the one defence here", d["league_route"], d["def_route"]["ATL"])
check("season + grids carried", (d["season"], d["def_pass"]), (2025, {"ATL": {}}))
plain = nfl_field.team_plays(2026, pbp=_pbp(), ftn=_ftn())
check("current-season file unchanged", plain["NO"]["cols"], nfl_field.PLAY_COLS)

print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
for f in FAILED:
    print("  FAIL", f)
sys.exit(1 if FAILED else 0)
