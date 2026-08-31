#!/usr/bin/env python3
"""
📡 SAVANT FEEDS — the two league-wide tables StatsAPI does not carry.

Donovan, 2026-08-23: "try to pull static data from savant and espn if needed
for model and site use wild pitches, pickoffs, pitcher SB-against, catcher
CS%, team defense, ABS challenge record."

Of those six, FOUR were never actually missing. wildPitches, pickoffs,
stolenBases and caughtStealing are top-level keys on StatsAPI's season pitching
blob, which bots/mlb_dashboard.py has been fetching for every starter all along
— see compute_pitcher_extended_stats, where they now get read. No new host, no
new call, nothing to install.

The remaining two are genuinely elsewhere, and this module is where they come
from:

  CATCHER THROWING   who is behind the plate and can he throw. The steal board
                     has been shipping with a written refusal about this since
                     the day it was built ("Who is catching is not on this
                     board — the slate does not carry the opposing catcher, and
                     that is the other half of a steal").
  TEAM DEFENCE       Outs Above Average by team, including the split by batter
                     hand, which is the half that matters for a pull hitter.

WHY CSV AND NOT SCRAPING. Both endpoints take `csv=true` and return a real
comma-separated file with a stable header row. That is a published interface,
not a page layout, and it does not break when Savant restyles a table.

EVERY COLUMN NAME IN THIS FILE WAS READ OFF THE LIVE ENDPOINT BEFORE A LINE OF
IT WAS WRITTEN. That is the standing rule here and it is not ceremony: this
repo has the receipt for what guessing an API's shape costs — the odds pipeline
needed "eight round trips of failure" to learn one provider's response. The
2026 header rows, verbatim, are in the docstrings of the two loaders below.

TWO JOIN TRAPS, BOTH HANDLED, BOTH FOUND BY READING REAL ROWS:

  1. The catcher feed's `player_name` is "León, Sandy" — LAST, FIRST, with the
     accent. Joining it against a slate that says "Sandy León" on a raw string
     compare silently matches nobody, and a feed that matches nobody looks
     exactly like a feed that says every catcher is average.
  2. The team-defence feed's `team_name` is a NICKNAME ("Angels", "Astros"),
     not the abbreviation the slate uses ("LAA", "HOU"). It also carries
     `team_id`, which is MLB's own id and the same one the bot already has on
     every row. Join on the id; the nickname is for humans.

STDLIB ONLY. No requests, no pandas. The stats probe runs this with no pip
install at all, and the bot should not gain a dependency to read two CSVs.

NOTHING HERE EVER RETURNS A SILENT EMPTY. Every loader returns
(data, status) and the status is "ok", "empty", or "error:<Type>". A caller
that gets {} with no status cannot tell "Savant is down" from "there are no
catchers in Major League Baseball", and the second reading is the one that
ships a board full of zeroes.

Usage:
    python3 bots/savant_feeds.py            # print what both feeds return
    python3 bots/savant_feeds.py --season 2026
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

SAVANT = "https://baseballsavant.mlb.com"
UA = "moonshot-mlb/1.0 (+https://github.com/donthebuilder/moonshot-mlb)"
TIMEOUT = 30

# Process-lifetime cache. One bot run touches each feed once; a second call in
# the same run should not be a second round trip.
_CACHE: Dict[str, Tuple[float, Any, str]] = {}
_TTL = 6 * 3600


def _fetch_csv(url: str, key: str) -> Tuple[list, str]:
    """(rows as dicts, status). Never raises."""
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1], hit[2]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/csv,*/*"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return [], f"error:HTTP{e.code}"
    except Exception as e:                                  # noqa: BLE001
        return [], f"error:{type(e).__name__}"
    # A Savant endpoint that does not like your parameters answers with an HTML
    # page and a 200, so "did it parse as CSV" is the real check, not the code.
    head = raw.lstrip()[:200].lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        return [], "error:HTMLNotCSV"
    rows = [r for r in csv.DictReader(io.StringIO(raw)) if r]
    status = "ok" if rows else "empty"
    _CACHE[key] = (time.time(), rows, status)
    return rows, status


def _f(v, default=None):
    try:
        s = str(v).strip().replace("%", "")
        if s == "" or s.lower() in ("nan", "null", "none"):
            return default
        return float(s)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    f = _f(v, None)
    return int(f) if f is not None else default


def norm_name(s: Any) -> str:
    """A join key that survives "León, Sandy" vs "Sandy Leon".

    Accents stripped, punctuation dropped, order normalised to first-last, and
    lower-cased. Savant writes "Last, First"; the slate writes "First Last".
    Comparing them raw matches nobody, and a feed that matches nobody is
    indistinguishable from a feed that says everyone is average — which is the
    failure this function exists to prevent.
    """
    t = str(s or "").strip()
    if not t:
        return ""
    if "," in t:
        last, _, first = t.partition(",")
        t = f"{first.strip()} {last.strip()}"
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = "".join(c if (c.isalnum() or c == " ") else " " for c in t)
    return " ".join(t.lower().split())


# ── A FEED THAT PARSES AND JOINS NOTHING IS NOT "ok" (2026-08-31) ───────────
#
# Found by reading the published slate, not the logs. On 2026-08-30, every one
# of the 251 rows carried opp_catcher_status="unqualified" with pop_time,
# arm_strength and cs_rate all null and opp_catcher_sb_attempts=0 -- for all 28
# catchers on the slate, Adley Rutschman, Cal Raleigh, Alejandro Kirk, Sean
# Murphy and Austin Hedges among them. Those men are not unqualified. Nobody
# is. The map was empty.
#
# And it was INVISIBLE, because of how the two statuses compose. _fetch_csv
# already returns "empty" for zero rows, so a dead endpoint would have shown
# up. This was worse: rows parsed fine, `status` came back "ok", and then every
# row was skipped by `if not pid: continue`, leaving an empty dict wearing an
# ok. mlb_dashboard's status ladder then reads "the feed is fine, so it must be
# this catcher" and stamps "unqualified" on the entire league.
#
# team_defense -- same file, same fetcher, same host -- joined on all 251 rows
# the same night (oaa -16, success_rate 78.0, status ok). So Savant is
# reachable and CSV parsing works. The difference between the two functions is
# the join key: team_defense keys on team_id and works; catcher_throwing keys
# on player_id and produced nothing. A renamed or missing id column is the
# whole failure, and Savant renames columns.
#
# Three defences, in order of how much they assume:
#   1. Read the id from whichever column is actually there.
#   2. Keep a BY-NAME index as well. name_key has been computed on every row
#      since this feed was written and never used for anything; the caller can
#      fall back to it when an id lookup misses.
#   3. Never return "ok" for a map that came back empty. Whatever else breaks,
#      it must never again take weeks of slates to notice.
_ID_COLS = ("player_id", "entity_id", "catcher", "id", "player_id_mlb")


def _row_id(r: dict) -> int:
    """The MLB player id, from whichever column this CSV happens to use."""
    for c in _ID_COLS:
        v = _i(r.get(c), 0)
        if v:
            return v
    return 0


def _joined_status(status: str, rows: list, out: dict) -> str:
    """Downgrade a fetch-level "ok" that produced nothing joinable."""
    if status != "ok":
        return status
    if not out:
        return f"error:NoJoinableRows({len(rows)}parsed)"
    return "ok"


def catcher_throwing(season: int) -> Tuple[Dict[int, dict], str]:
    """Every qualified catcher's throwing line, keyed by MLB player id.

    Live 2026 header, verbatim:
      player_id, player_name, team_name, start_year, end_year, sb_attempts,
      catcher_stealing_runs, caught_stealing_above_average, n_cs, rate_cs,
      est_cs_pct, cs_aa_per_throw, seasonal_runner_speed,
      runner_distance_from_second, pop_time, exchange_time, arm_strength, ...

    `rate_cs` is his ACTUAL caught-stealing rate; `est_cs_pct` is what Statcast
    expected given the throws he faced. The gap between them is the catcher, as
    distinct from the pitchers in front of him — which is the number worth
    having, because a catcher behind arms who never check a runner will show a
    poor raw rate and a fine expected one.

    `rate_cs` is left as None under 10 attempts. Sandy León at 5-of-16 is a
    rate; a backup at 1-of-2 is not, and 50% at the top of a board is worse
    than no board.
    """
    url = (f"{SAVANT}/leaderboard/catcher-throwing?season={season}&min=q&type=Cat"
           f"&sortColumn=cs_above_avg&sortDirection=desc&csv=true")
    rows, status = _fetch_csv(url, f"catcher:{season}")
    out: Dict[int, dict] = {}
    MIN_ATTEMPTS = 10
    for r in rows:
        pid = _row_id(r)
        if not pid:
            continue
        att = _i(r.get("sb_attempts"), 0)
        rate = _f(r.get("rate_cs"))
        out[pid] = {
            "player_id": pid,
            "name": str(r.get("player_name") or "").strip(),
            "name_key": norm_name(r.get("player_name")),
            "team": str(r.get("team_name") or "").strip(),
            "sb_attempts": att,
            "cs": _i(r.get("n_cs"), 0),
            # None under the floor, never 0.0 — see the docstring.
            "cs_rate": round(rate, 3) if (rate is not None and att >= MIN_ATTEMPTS) else None,
            "cs_rate_expected": _f(r.get("est_cs_pct")),
            "cs_above_avg": _f(r.get("caught_stealing_above_average")),
            "pop_time": _f(r.get("pop_time")),
            "exchange_time": _f(r.get("exchange_time")),
            "arm_strength": _f(r.get("arm_strength")),
            "attempts_below_floor": att < MIN_ATTEMPTS,
        }
    # The by-name index rides along in the SAME dict under string keys, so no
    # caller signature changes and an id lookup can never collide with it.
    # mlb_dashboard falls back to it when the boxscore's id finds nothing.
    for v in list(out.values()):
        k = v.get("name_key")
        if k and k not in out:
            out[k] = v
    return out, _joined_status(status, rows, out)


def team_defense(season: int) -> Tuple[Dict[int, dict], str]:
    """Outs Above Average by team, keyed by MLB team id.

    Live 2026 header, verbatim:
      team_name, team_id, year, primary_pos_formatted, outs_above_average,
      outs_above_average_infront, outs_above_average_lateral_toward3bline,
      outs_above_average_lateral_toward1bline, outs_above_average_behind,
      outs_above_average_rhh, outs_above_average_lhh,
      actual_success_rate_formatted, adj_estimated_success_rate_formatted,
      diff_success_rate_formatted

    `team_name` is the NICKNAME ("Angels"), not the abbreviation the slate uses.
    Join on team_id, which is MLB's own and already on every bot row.

    The `_rhh` / `_lhh` split is the half worth having for this site: a defence
    that is -17 against left-handed bats and -10 against right-handed ones is a
    different matchup depending on who is hitting, and that is precisely the
    question every other pitcher term here is already asking.

    The success-rate columns arrive as "78%" strings, not numbers.
    """
    url = (f"{SAVANT}/leaderboard/outs_above_average?type=Fielding_Team"
           f"&startYear={season}&endYear={season}&split=no&team=&range=year"
           f"&min=q&pos=&roles=&viz=hide&csv=true")
    rows, status = _fetch_csv(url, f"teamdef:{season}")
    out: Dict[int, dict] = {}
    for r in rows:
        tid = _i(r.get("team_id"), 0)
        if not tid:
            continue
        out[tid] = {
            "team_id": tid,
            "team_name": str(r.get("team_name") or "").strip(),
            "oaa": _i(r.get("outs_above_average"), 0),
            "oaa_vs_rhb": _i(r.get("outs_above_average_rhh"), 0),
            "oaa_vs_lhb": _i(r.get("outs_above_average_lhh"), 0),
            # "78%" -> 78.0
            "success_rate": _f(r.get("actual_success_rate_formatted")),
            "success_rate_expected": _f(r.get("adj_estimated_success_rate_formatted")),
            "success_rate_diff": _f(r.get("diff_success_rate_formatted")),
        }
    # This one joins fine today; the guard is here so it cannot fail the same
    # silent way tomorrow.
    return out, _joined_status(status, rows, out)


# ── ABS: the one that is NOT wired yet, and why ─────────────────────────────
#
# https://baseballsavant.mlb.com/leaderboard/abs-challenges exists, serves
# `csv=true`, and its 2026 header is known verbatim:
#
#   entity_name, team_abbr, level, parent_org, total_vs_expected, net_for,
#   net_against, n_challenges, n_overturns, n_confirms, rate_overturns,
#   exp_chal, exp_chal_gained, exp_chal_lost, exp_rate_overturns,
#   net_chal_gained, net_chal_lost, n_strikeouts_flip, n_walks_flip,
#   exp_rate_challenges, exp_rate_challenges_diff, ...and the same again with
#   an "_against" suffix
#
# What is NOT known is the parameter that selects Batters vs Catchers vs
# Pitchers. The control is rendered by JavaScript, and `type=Pit`, `type=Cat`,
# `type=Pitcher` and `playerType=Pitcher` all return the batter table with a
# 200 — it does not error, it silently ignores you. Building on that would give
# every pitcher a batter's challenge record and nothing anywhere would say so.
#
# So it goes through the probe: ABS_PARAM_CANDIDATES is swept in
# bots/stats_probe.py, each result checked against names that are known to be
# pitchers, and the one that actually changes the table gets wired. Until the
# probe answers, this is the entire ABS support in the bot, on purpose.
ABS_URL = f"{SAVANT}/leaderboard/abs-challenges"
ABS_PARAM_CANDIDATES = [
    ("type", ["Pit", "Pitcher", "pitcher", "P"]),
    ("playerType", ["Pit", "Pitcher", "pitcher", "P"]),
    ("player_type", ["Pit", "Pitcher", "pitcher", "P"]),
    ("perspective", ["Pit", "Pitcher", "pitcher", "P"]),
    ("role", ["Pit", "Pitcher", "pitcher", "P"]),
    ("entity", ["Pit", "Pitcher", "pitcher", "P"]),
    ("entity_type", ["Pit", "Pitcher", "pitcher", "P"]),
    ("abs_type", ["Pit", "Pitcher", "pitcher", "P"]),
]


def abs_challenges_raw(season: int, param: str = "", value: str = "") -> Tuple[list, str]:
    """The ABS table exactly as served, for the probe to inspect.

    Deliberately returns RAW rows and no parsed shape: nothing should build a
    field off this until the probe has said which parameter selects a role.
    """
    url = f"{ABS_URL}?year={season}&csv=true"
    if param and value:
        url += f"&{param}={value}"
    return _fetch_csv(url, f"abs:{season}:{param}:{value}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=0)
    ap.add_argument("--json", default="", help="write both feeds here")
    a = ap.parse_args()
    season = a.season or time.gmtime().tm_year

    cat, cat_status = catcher_throwing(season)
    tm, tm_status = team_defense(season)

    print(f"catcher-throwing {season}: {cat_status}, {len(cat)} catchers")
    for pid, v in list(cat.items())[:5]:
        print(f"   {pid:>7}  {v['name']:<22} {v['team']:<4} "
              f"{v['cs']}/{v['sb_attempts']}  cs={v['cs_rate']}  "
              f"xcs={v['cs_rate_expected']}  pop={v['pop_time']}  arm={v['arm_strength']}")
    print(f"\nteam defence {season}: {tm_status}, {len(tm)} teams")
    for tid, v in list(tm.items())[:5]:
        print(f"   {tid:>4}  {v['team_name']:<12} OAA {v['oaa']:>4}  "
              f"vsR {v['oaa_vs_rhb']:>4}  vsL {v['oaa_vs_lhb']:>4}  "
              f"succ {v['success_rate']}%")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"season": season,
                       "catcher_throwing": {"status": cat_status, "rows": cat},
                       "team_defense": {"status": tm_status, "rows": tm}}, f, indent=2)
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
