"""nfl_disruption.py -- the defense/action stat layer, graded not raw (2026-09-13).

Upgrade prompt Phase 2's "tackles, TFL, pass breakups, pressures, forced
fumbles... formation percentage" item, built as a percentile grade within
position group (DL/LB/DB) rather than a raw stat table, matching Competitive
Reference #2 ("a single 0-100 grade... plus the underlying columns colored by
percentile").

This test mocks nflreadpy's load_player_stats/load_participation/load_pbp (no
network) and checks:
  1. player_grades(): tackles = solo+assist summed; MIN_GAMES filters out
     low-sample players; percentile is computed WITHIN position group, not
     across all defenders; a non-defensive position never enters the output
     even if it carries def_* stats (the WR-credited-tackle case the module's
     own docstring calls out); preseason rows are excluded; the grade only
     averages the stats GROUP_STATS says matter for that group, while the
     percentiles dict still shows every stat (a DB's sack percentile is real
     and shown, it just doesn't drag his grade); a position group of exactly
     one player scores 50.0 rather than dividing by zero.
  2. team_context(): the participation-to-pbp join resolves correctly;
     formation mix is a percentage of that team's OWN charted-formation
     snaps (not all snaps); pressure is split by role (created = this team's
     defense, allowed = this team's offense) off the same was_pressure flag;
     preseason rows are excluded via the joined pbp's season_type.

Run: PYTHONPATH=. python3 tests/test_nfl_disruption.py
"""
import os
import sys
from unittest import mock

import polars as pl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bots.nfl import nfl_disruption  # noqa: E402

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


def checkClose(name, got, want, tol=0.05):
    global CHECKS
    CHECKS += 1
    if got is None or abs(got - want) > tol:
        FAILED.append(f"{name}: got {got!r}, want ~{want!r}")


# ── player_grades(): fake per-week player_stats rows ────────────────────────
# Three DL, evenly spaced so percentile ranks are unambiguous (1 sack < 3 <
# 5 -> 0 / 50 / 100 within the DL group); one LB; one lone DB (a group of
# exactly one, the divide-by-zero guard); one WR with real def_* numbers
# (the pick-six-tackle case) that must never surface as a graded defender;
# one DL under MIN_GAMES; one DL row from the PRESEASON that must not count
# toward a real player's total.
def _weekly_row(pid, name, team, pos, week, season_type="REG", **stats):
    row = {
        "player_id": pid, "player_display_name": name, "team": team,
        "position": pos, "week": week, "season_type": season_type,
        "def_tackles_solo": 0, "def_tackles_with_assist": 0,
        "def_tackles_for_loss": 0, "def_sacks": 0.0, "def_qb_hits": 0,
        "def_pass_defended": 0, "def_fumbles_forced": 0,
    }
    row.update(stats)
    return row

ROWS = []
# DL_A: 5 games, low end of the DL group
for w in range(1, 6):
    ROWS.append(_weekly_row("DL_A", "Low DL", "AAA", "DE", w,
                             def_tackles_solo=1, def_tackles_for_loss=0,
                             def_sacks=0.2, def_qb_hits=1, def_fumbles_forced=0))
# DL_B: 5 games, middle of the DL group
for w in range(1, 6):
    ROWS.append(_weekly_row("DL_B", "Mid DL", "BBB", "DT", w,
                             def_tackles_solo=2, def_tackles_for_loss=1,
                             def_sacks=0.6, def_qb_hits=2, def_fumbles_forced=0))
# DL_C: 5 games, top of the DL group
for w in range(1, 6):
    ROWS.append(_weekly_row("DL_C", "Top DL", "CCC", "NT", w,
                             def_tackles_solo=3, def_tackles_for_loss=2,
                             def_sacks=1.0, def_qb_hits=3, def_fumbles_forced=1))
# LB_A: only 3 games -- below MIN_GAMES, must not appear at all
for w in range(1, 4):
    ROWS.append(_weekly_row("LB_A", "Under Sample LB", "DDD", "MLB", w,
                             def_tackles_solo=5, def_tackles_for_loss=1))
# DB_A: 5 games, the ONLY DB -- group-of-one percentile guard, and a real
# sack (unusual for a corner, but the data allows it) to check it's still
# reported even though sacks isn't in DB's GROUP_STATS grade set
for w in range(1, 6):
    ROWS.append(_weekly_row("DB_A", "Lone DB", "EEE", "CB", w,
                             def_tackles_solo=1, def_pass_defended=1,
                             def_sacks=1.0, def_fumbles_forced=0))
# WR_X: a receiver credited with a return tackle -- must never be graded
# as a defender no matter how good the "stats" look
for w in range(1, 6):
    ROWS.append(_weekly_row("WR_X", "Pick Six WR", "AAA", "WR", w,
                             def_tackles_solo=1))
# DL_C also has a PRESEASON row with an enormous sack total that must be
# excluded by the REG filter, not summed into his real total
ROWS.append(_weekly_row("DL_C", "Top DL", "CCC", "NT", 0, season_type="PRE",
                         def_sacks=99.0, def_tackles_solo=99))


def _fake_weekly():
    return pl.DataFrame(ROWS)


nfl_disruption._weekly.cache_clear()
with mock.patch.object(nfl_disruption.nfl, "load_player_stats", return_value=_fake_weekly()):
    grades = nfl_disruption.player_grades(2026)

check("under-MIN_GAMES player excluded entirely", "LB_A" in grades, False)
check("non-defensive position never graded even with def_* stats present",
      "WR_X" in grades, False)
checkTrue("qualifying DL/DB players present", all(p in grades for p in ("DL_A", "DL_B", "DL_C", "DB_A")))

check("preseason row excluded from the real season's sack total",
      grades["DL_C"]["stats"]["sacks"], 5.0)
check("tackles = solo + assist, summed across games (no assists here, still additive)",
      grades["DL_A"]["stats"]["tackles"], 5.0)

check("lowest-in-group DL sits at percentile 0 on sacks",
      grades["DL_A"]["percentiles"]["sacks"], 0.0)
check("middle-of-group DL sits at percentile 50 on sacks",
      grades["DL_B"]["percentiles"]["sacks"], 50.0)
check("top-of-group DL sits at percentile 100 on sacks",
      grades["DL_C"]["percentiles"]["sacks"], 100.0)

check("a position group of exactly one (the lone DB) scores 50 on every stat, not a crash",
      grades["DB_A"]["percentiles"]["tackles_for_loss"], 50.0)

check("DB's grade set excludes sacks/TFL/qb_hits per GROUP_STATS",
      set(nfl_disruption.GROUP_STATS["DB"]), {"tackles", "pass_defended", "fumbles_forced"})
checkTrue("DB's sack percentile is still REPORTED even though it doesn't count toward grade",
          "sacks" in grades["DB_A"]["percentiles"])
db_a = grades["DB_A"]
want_grade = round(sum(db_a["percentiles"][s] for s in ("tackles", "pass_defended", "fumbles_forced")) / 3, 1)
check("DB's grade averages only tackles/pass_defended/fumbles_forced, sacks excluded",
      db_a["grade"], want_grade)

dl_b = grades["DL_B"]
dl_group_stats = nfl_disruption.GROUP_STATS["DL"]
want_dl_b_grade = round(sum(dl_b["percentiles"][s] for s in dl_group_stats) / len(dl_group_stats), 1)
check("DL's grade averages exactly its GROUP_STATS set", dl_b["grade"], want_dl_b_grade)


# ── team_context(): fake participation joined to fake pbp ──────────────────
PART_ROWS = [
    # AAA on offense, three snaps: two SHOTGUN, one UNDER CENTER
    {"nflverse_game_id": "g1", "play_id": 1, "posteam": "AAA",
     "offense_formation": "SHOTGUN", "was_pressure": False},
    {"nflverse_game_id": "g1", "play_id": 2, "posteam": "AAA",
     "offense_formation": "SHOTGUN", "was_pressure": True},
    {"nflverse_game_id": "g1", "play_id": 3, "posteam": "AAA",
     "offense_formation": "UNDER CENTER", "was_pressure": False},
    # a charting miss -- offense_formation null, must not count toward AAA's
    # formation snap denominator
    {"nflverse_game_id": "g1", "play_id": 4, "posteam": "AAA",
     "offense_formation": None, "was_pressure": False},
    # a preseason play that must be excluded entirely by the join's REG filter
    {"nflverse_game_id": "g2", "play_id": 1, "posteam": "AAA",
     "offense_formation": "SHOTGUN", "was_pressure": True},
]
PBP_ROWS = [
    {"game_id": "g1", "play_id": 1, "season_type": "REG", "posteam": "AAA", "defteam": "BBB", "pass_attempt": 0},
    {"game_id": "g1", "play_id": 2, "season_type": "REG", "posteam": "AAA", "defteam": "BBB", "pass_attempt": 1},
    {"game_id": "g1", "play_id": 3, "season_type": "REG", "posteam": "AAA", "defteam": "BBB", "pass_attempt": 0},
    {"game_id": "g1", "play_id": 4, "season_type": "REG", "posteam": "AAA", "defteam": "BBB", "pass_attempt": 1},
    {"game_id": "g2", "play_id": 1, "season_type": "PRE", "posteam": "AAA", "defteam": "BBB", "pass_attempt": 1},
]


def _fake_participation():
    return pl.DataFrame(PART_ROWS)


def _fake_pbp():
    return pl.DataFrame(PBP_ROWS)


nfl_disruption._participation_pbp.cache_clear()
with mock.patch.object(nfl_disruption.nfl, "load_participation", return_value=_fake_participation()), \
     mock.patch.object(nfl_disruption.nfl, "load_pbp", return_value=_fake_pbp()):
    tc = nfl_disruption.team_context(2026)

check("AAA's formation snap denominator excludes the null-formation play and the preseason play",
      tc["AAA"]["formation"]["snaps"], 3)
checkClose("AAA ran shotgun on 2 of 3 charted snaps", tc["AAA"]["formation"]["shotgun_pct"], 66.7)
checkClose("AAA ran under center on 1 of 3 charted snaps", tc["AAA"]["formation"]["under_center_pct"], 33.3)
check("a formation AAA never ran is simply absent, not a fabricated 0",
      "pistol_pct" in tc["AAA"]["formation"], False)

check("BBB's defense created pressure on 1 of 2 REG pass plays (preseason excluded)",
      tc["BBB"]["pressure"]["created_plays"], 2)
checkClose("BBB created pressure 50% of those pass plays", tc["BBB"]["pressure"]["created_pct"], 50.0)
check("AAA's offense allowed pressure on 1 of 2 REG pass plays",
      tc["AAA"]["pressure"]["allowed_plays"], 2)
checkClose("AAA allowed pressure 50% of those pass plays", tc["AAA"]["pressure"]["allowed_pct"], 50.0)


# ── fails soft: a broken nflreadpy call must never take the module down ────

nfl_disruption._weekly.cache_clear()
with mock.patch.object(nfl_disruption.nfl, "load_player_stats", side_effect=RuntimeError("boom")):
    try:
        nfl_disruption.player_grades(2026)
        checkTrue("player_grades propagates the underlying error rather than hiding it "
                  "(nfl_bot.py's own extras loop is the fail-soft layer, not this module)", False)
    except RuntimeError:
        checkTrue("player_grades lets a real fetch error surface to its caller", True)


print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
if FAILED:
    print("FAILED:")
    for f in FAILED:
        print(f"  · {f}")
    sys.exit(1)
else:
    print("ok   nfl_disruption: player_grades() sums real per-week def_* columns, filters by "
          "MIN_GAMES and REG season, grades percentile WITHIN position group (not across all "
          "defenders), keeps a lone-player group from dividing by zero, and reports every stat's "
          "percentile even when GROUP_STATS excludes it from that group's grade; team_context() "
          "resolves formation mix off its own charted-snap denominator and splits pressure by "
          "role (created vs allowed) off one was_pressure column, both via the same "
          "participation-to-pbp join nfl_coverage.py already established.")
    sys.exit(0)
