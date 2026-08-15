#!/usr/bin/env python3
"""
player_splits.py — day/night, home/away, day-of-week and win/loss splits.

Why this exists
---------------
The scoring bot only carries vs-RHP / vs-LHP splits. It has nothing for
day vs night, home vs away, day of week, or how a hitter does in wins vs
losses, so the dashboard couldn't show any of it.

MLB's gameLog endpoint returns one row per game with `date`, `isHome`,
`isWin` and `game.dayNight` attached to that game's batting line. A single
request per hitter therefore yields all four split families at once --
cheaper and more flexible than statSplits, which needs separate sitCodes
and can't do day-of-week at all.

Output: public/data/current/splits/<slate>/<player_id>.json

Usage:
    python bots/player_splits.py --slate public/data/today_slate.json
    python bots/player_splits.py --slate ... --label tomorrow
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

import requests

MLB_BASE = "https://statsapi.mlb.com/api/v1"
REPO_ROOT = Path(__file__).resolve().parent.parent
CURRENT_DIR = REPO_ROOT / "public" / "data" / "current"
TIMEOUT = 30
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def num(v: Any, d: float = 0.0) -> float:
    try:
        if v in (None, "", ".---", "-.--"):
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def blank() -> Dict[str, float]:
    return {k: 0.0 for k in
            ("g", "pa", "ab", "h", "hr", "2b", "3b", "bb", "k", "rbi", "r", "tb")}


def add(acc: Dict[str, float], stat: Dict[str, Any]) -> None:
    acc["g"] += 1
    acc["pa"] += num(stat.get("plateAppearances"))
    acc["ab"] += num(stat.get("atBats"))
    acc["h"] += num(stat.get("hits"))
    acc["hr"] += num(stat.get("homeRuns"))
    acc["2b"] += num(stat.get("doubles"))
    acc["3b"] += num(stat.get("triples"))
    acc["bb"] += num(stat.get("baseOnBalls"))
    acc["k"] += num(stat.get("strikeOuts"))
    acc["rbi"] += num(stat.get("rbi"))
    acc["r"] += num(stat.get("runs"))
    acc["tb"] += num(stat.get("totalBases"))


def finish(acc: Dict[str, float]) -> Dict[str, Any]:
    """Rate stats computed from the totals, not averaged from per-game rates --
    averaging rates would weight a 1-AB game the same as a 5-AB game."""
    ab, pa, h, tb = acc["ab"], acc["pa"], acc["h"], acc["tb"]
    xbh = acc["2b"] + acc["3b"] + acc["hr"]
    obp_den = ab + acc["bb"]
    return {
        "G": int(acc["g"]), "PA": int(pa), "AB": int(ab), "H": int(h),
        "HR": int(acc["hr"]), "XBH": int(xbh), "BB": int(acc["bb"]),
        "K": int(acc["k"]), "RBI": int(acc["rbi"]), "R": int(acc["r"]),
        "AVG": round(h / ab, 3) if ab else 0.0,
        "OBP": round((h + acc["bb"]) / obp_den, 3) if obp_den else 0.0,
        "SLG": round(tb / ab, 3) if ab else 0.0,
        "OPS": round((h / ab if ab else 0) + (tb / ab if ab else 0), 3) if ab else 0.0,
        "ISO": round((tb - h) / ab, 3) if ab else 0.0,
        "HR/PA": round(acc["hr"] / pa, 4) if pa else 0.0,
        "K%": round(acc["k"] / pa, 3) if pa else 0.0,
    }


def build_splits(games: List[Dict[str, Any]]) -> Dict[str, Any]:
    buckets: Dict[str, Dict[str, Dict[str, float]]] = {
        "day_night": defaultdict(blank),
        "home_away": defaultdict(blank),
        "day_of_week": defaultdict(blank),
        "win_loss": defaultdict(blank),
    }
    # One raw row per game, alongside the four pre-aggregated tables below
    # (2026-08-13). Donovan: "how a player does on monday splits then at
    # home in a win day game... i can't filter like that." The four
    # families above are independent, single-dimension aggregates -- they
    # can't answer a combined question like that. But day-of-week,
    # home/away, win/loss and day/night are all GAME-level facts, and this
    # loop already visits every game once with all four in hand -- keeping
    # one row per game costs nothing new (no extra API call) and lets the
    # site build any AND-combination of these four client-side. This is
    # NOT the same as full play-by-play: PA-level things like RISP, outs,
    # or count still aren't here, and combining THOSE with anything still
    # needs a bigger build. See PlayerSplits.js's ComboFilter for the
    # consumer side of this.
    games_raw: List[Dict[str, Any]] = []
    for g in games:
        stat = g.get("stat") or {}
        if not stat:
            continue
        game = g.get("game") or {}

        dn = str(game.get("dayNight") or "").lower()
        dn_label = "Day" if dn == "day" else "Night" if dn == "night" else None
        if dn_label:
            add(buckets["day_night"][dn_label], stat)

        home = g.get("isHome")
        if home is not None:
            add(buckets["home_away"]["Home" if home else "Away"], stat)

        win = g.get("isWin")
        if win is not None:
            add(buckets["win_loss"]["Win" if win else "Loss"], stat)

        dow_label = None
        try:
            d = dt.date.fromisoformat(str(g.get("date")))
            dow_label = DOW[d.weekday()]
            add(buckets["day_of_week"][dow_label], stat)
        except Exception:
            pass

        games_raw.append({
            "date": str(g.get("date") or ""), "dow": dow_label,
            "home": home, "win": win, "dn": dn_label,
            "pa": num(stat.get("plateAppearances")), "ab": num(stat.get("atBats")),
            "h": num(stat.get("hits")), "hr": num(stat.get("homeRuns")),
            "2b": num(stat.get("doubles")), "3b": num(stat.get("triples")),
            "bb": num(stat.get("baseOnBalls")), "k": num(stat.get("strikeOuts")),
            "rbi": num(stat.get("rbi")), "r": num(stat.get("runs")),
            "tb": num(stat.get("totalBases")),
        })

    out: Dict[str, Any] = {}
    for family, groups in buckets.items():
        out[family] = {k: finish(v) for k, v in groups.items()}
    # Keep weekdays in calendar order rather than whatever order they appeared.
    if out.get("day_of_week"):
        out["day_of_week"] = {d: out["day_of_week"][d] for d in DOW
                              if d in out["day_of_week"]}
    out["games"] = games_raw
    return out


# ── THE RUN BOARD ────────────────────────────────────────────────────────────
#
# 2026-08-15, Donovan sent a competitor's "Player board" and "Hottest active
# runs" and asked for them here. Both are the same underlying object: for a
# chosen bar, every hitter's last thirty games as a strip, the length of his
# ACTIVE run, and his rate over a few windows.
#
# THIS COSTS NO NEW REQUEST. The loop below already pulls every hitter's game
# log to build his splits file; the same rows carry hits, total bases, runs,
# RBI, home runs, home/away and day/night. So the run board rides along.
#
# NOTHING IS PRE-AGGREGATED PER MARKET, ON PURPOSE. Publishing "his 2+ TB run
# is 5" would fix the bar at 2 and force a new field for every threshold anyone
# ever wants. Publishing the last thirty RAW LINES instead lets the site
# compute a run, a rate and a strip for ANY market at ANY threshold instantly,
# with no round trip — which is what makes the market and line chips on that
# board feel immediate rather than like a page load.
#
# One row per game, newest first:
#   [date, opp, H, TB, H+R+RBI, HR, isHome, "D"|"N"]
RUN_GAMES = 30


def run_log(games: List[Dict[str, Any]]) -> List[List[Any]]:
    """The last RUN_GAMES lines, newest first, in the compact shape above."""
    rows: List[List[Any]] = []
    for g in games:
        stat = g.get("stat") or {}
        if not stat:
            continue
        ab = int(num(stat.get("atBats")))
        bb = int(num(stat.get("baseOnBalls")))
        # A game he never came up in is not a miss — it is not a game. Same
        # rule as lib/gamelogs.js on the site and the void rule everywhere
        # else in this project; leaving it in would break every run in the
        # board with a night he was rested.
        if ab == 0 and bb == 0:
            continue
        h = int(num(stat.get("hits")))
        r = int(num(stat.get("runs")))
        rbi = int(num(stat.get("rbi")))
        game = g.get("game") or {}
        dn = str(game.get("dayNight") or "").lower()
        rows.append([
            str(g.get("date") or "")[5:10],
            str((g.get("opponent") or {}).get("abbreviation")
                or (g.get("opponent") or {}).get("name") or "")[:3].upper(),
            h,
            int(num(stat.get("totalBases"))),
            h + r + rbi,
            int(num(stat.get("homeRuns"))),
            1 if g.get("isHome") else 0,
            "D" if dn == "day" else "N" if dn == "night" else "",
        ])
    # The API returns oldest-first; the board reads newest-first everywhere.
    rows.reverse()
    return rows[:RUN_GAMES]


def fetch_game_log(session: requests.Session, player_id: Any, season: int,
                   group: str = "hitting") -> List[Dict[str, Any]]:
    try:
        r = session.get(
            f"{MLB_BASE}/people/{player_id}/stats",
            params={"stats": "gameLog", "group": group, "season": season},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        stats = (r.json() or {}).get("stats") or []
        return stats[0].get("splits", []) if stats else []
    except Exception:
        return []


# ── PITCHERS ────────────────────────────────────────────────────────────────
# Same four split families as the hitters, but a pitching line is a different
# shape: outs recorded rather than at-bats, and every rate runs the other way
# (a high ERA is bad, a high K/9 is good). Kept in this file rather than a new
# one because the fetch, the bucketing and the day-of-week ordering are
# identical -- only the stat extraction differs.

def blank_p() -> Dict[str, float]:
    return {k: 0.0 for k in
            ("g", "outs", "bf", "h", "hr", "er", "r", "bb", "k", "ab")}


def add_p(acc: Dict[str, float], stat: Dict[str, Any]) -> None:
    acc["g"] += 1
    # inningsPitched comes as "5.2" meaning 5 innings and 2 outs -- decimal
    # arithmetic on that string is wrong (5.2 + 5.2 is 11.1 innings, not
    # 10.4), so it's converted to outs and back only at the end.
    ip = str(stat.get("inningsPitched") or "0")
    try:
        whole, _, frac = ip.partition(".")
        acc["outs"] += int(whole or 0) * 3 + int(frac or 0)
    except ValueError:
        pass
    acc["bf"] += num(stat.get("battersFaced"))
    acc["h"] += num(stat.get("hits"))
    acc["hr"] += num(stat.get("homeRuns"))
    acc["er"] += num(stat.get("earnedRuns"))
    acc["r"] += num(stat.get("runs"))
    acc["bb"] += num(stat.get("baseOnBalls"))
    acc["k"] += num(stat.get("strikeOuts"))
    acc["ab"] += num(stat.get("atBats"))


def finish_p(acc: Dict[str, float]) -> Dict[str, Any]:
    outs = acc["outs"]
    ip = outs / 3.0
    ab = acc["ab"]
    return {
        "G": int(acc["g"]),
        "IP": round(ip, 1),
        "BF": int(acc["bf"]),
        "H": int(acc["h"]),
        "HR": int(acc["hr"]),
        "ER": int(acc["er"]),
        "R": int(acc["r"]),
        "BB": int(acc["bb"]),
        "K": int(acc["k"]),
        "ERA": round(acc["er"] * 9.0 / ip, 2) if ip else 0.0,
        "WHIP": round((acc["h"] + acc["bb"]) / ip, 2) if ip else 0.0,
        "HR/9": round(acc["hr"] * 9.0 / ip, 2) if ip else 0.0,
        "K/9": round(acc["k"] * 9.0 / ip, 1) if ip else 0.0,
        "BB/9": round(acc["bb"] * 9.0 / ip, 1) if ip else 0.0,
        "BAA": round(acc["h"] / ab, 3) if ab else 0.0,
    }


def build_pitcher_splits(games: List[Dict[str, Any]]) -> Dict[str, Any]:
    buckets: Dict[str, Dict[str, Dict[str, float]]] = {
        "day_night": defaultdict(blank_p),
        "home_away": defaultdict(blank_p),
        "day_of_week": defaultdict(blank_p),
        "win_loss": defaultdict(blank_p),
    }
    for g in games:
        stat = g.get("stat") or {}
        if not stat:
            continue
        game = g.get("game") or {}

        dn = str(game.get("dayNight") or "").lower()
        if dn in ("day", "night"):
            add_p(buckets["day_night"]["Day" if dn == "day" else "Night"], stat)
        if g.get("isHome") is not None:
            add_p(buckets["home_away"]["Home" if g["isHome"] else "Away"], stat)
        if g.get("isWin") is not None:
            add_p(buckets["win_loss"]["Win" if g["isWin"] else "Loss"], stat)
        try:
            d = dt.date.fromisoformat(str(g.get("date")))
            add_p(buckets["day_of_week"][DOW[d.weekday()]], stat)
        except Exception:
            pass

    out: Dict[str, Any] = {}
    for family, groups in buckets.items():
        out[family] = {k: finish_p(v) for k, v in groups.items()}
    if out.get("day_of_week"):
        out["day_of_week"] = {d: out["day_of_week"][d] for d in DOW
                              if d in out["day_of_week"]}
    return out


def build_pitcher_contact_log(pitcher_id: Any, season: int,
                              limit: int = 60) -> List[Dict[str, Any]]:
    """Every ball put in play against this pitcher, newest first.

    Mirrors the hitter contact log field-for-field so the dashboard can feed
    both through one renderer. Pulled from Statcast via pybaseball, which the
    scoring bot already depends on; if it isn't importable this returns an
    empty list and the section simply doesn't render.
    """
    try:
        from pybaseball import statcast_pitcher
    except Exception:
        return []
    try:
        df = statcast_pitcher(f"{season}-03-01",
                              dt.date.today().isoformat(), pitcher_id)
    except Exception:
        return []
    if df is None or len(df) == 0:
        return []

    try:
        d = df[df["launch_speed"].notna()].copy()
        if d.empty:
            return []
        d = d.sort_values("game_date", ascending=False).head(limit)

        # Who actually hit it. statcast_pitcher's `player_name` is the PITCHER
        # -- on his own log that just repeats his name on every row, which is
        # useless. The batter is only present as an MLBAM id, so resolve it.
        batter_names: Dict[Any, str] = {}
        try:
            from pybaseball import playerid_reverse_lookup
            ids = [int(x) for x in d["batter"].dropna().unique()] if "batter" in d.columns else []
            if ids:
                look = playerid_reverse_lookup(ids, key_type="mlbam")
                for _, lr in look.iterrows():
                    batter_names[int(lr["key_mlbam"])] = (
                        f"{str(lr.get('name_first', '')).title()} "
                        f"{str(lr.get('name_last', '')).title()}".strip())
        except Exception:
            batter_names = {}

        def _batter(r) -> str:
            bid = r.get("batter")
            try:
                bid = int(bid) if bid is not None else None
            except (TypeError, ValueError):
                bid = None
            if bid is not None and bid in batter_names:
                return batter_names[int(bid)]
            # Fallback: the play description leads with the batter's name.
            des = str(r.get("des") or "").strip()
            if des:
                for verb in (" homers", " singles", " doubles", " triples",
                             " grounds", " flies", " lines", " pops", " reaches",
                             " hits", " out"):
                    if verb in des:
                        return des.split(verb)[0].strip()
            return str(bid or "")

        rows: List[Dict[str, Any]] = []
        for _, r in d.iterrows():
            ev = r.get("launch_speed")
            rows.append({
                "date": str(r.get("game_date"))[:10],
                # The hitter log calls this column "pitcher"; on a pitcher's
                # log the opponent is the batter, so the shared column carries
                # HIS name and the renderer needs no special case. `batter` is
                # kept explicitly too so the pitcher view can label it.
                "pitcher": _batter(r),
                "batter": _batter(r),
                "arm": str(r.get("stand") or ""),
                "pitch_name": str(r.get("pitch_name") or ""),
                "ev": None if ev is None else round(float(ev), 1),
                "launch_angle": (None if r.get("launch_angle") is None
                                 else round(float(r.get("launch_angle")), 0)),
                "distance": (None if r.get("hit_distance_sc") is None
                             else round(float(r.get("hit_distance_sc")), 0)),
                "pitch_velocity": (None if r.get("release_speed") is None
                                   else round(float(r.get("release_speed")), 1)),
                "result": str(r.get("events") or "").replace("_", " "),
                "trajectory": str(r.get("bb_type") or "").replace("_", " "),
                "is_hr": str(r.get("events") or "") == "home_run",
            })
        return rows
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description="Build per-hitter situational splits.")
    ap.add_argument("--slate", required=True, help="Path to the slate JSON")
    ap.add_argument("--label", default="today", choices=["today", "tomorrow"])
    ap.add_argument("--season", type=int, default=dt.date.today().year)
    ap.add_argument("--skip-pitchers", action="store_true",
                    help="Hitters only — skip starter splits and contact logs.")
    ap.add_argument("--skip-pitcher-bbe", action="store_true",
                    help="Build pitcher splits but skip the Statcast contact log.")
    args = ap.parse_args()

    src = Path(args.slate)
    if not src.exists():
        print(f"ERROR: slate not found: {src}", file=sys.stderr)
        return 1
    rows = json.loads(src.read_text(encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows.get("players") or rows.get("rows") or []

    ids: Dict[Any, str] = {}
    teams: Dict[Any, str] = {}
    opps: Dict[Any, str] = {}
    for r in rows:
        pid = r.get("player_id")
        if pid not in (None, "") and pid not in ids:
            ids[pid] = r.get("name") or r.get("player_name") or str(pid)
            teams[pid] = r.get("team") or ""
            opps[pid] = r.get("opponent") or ""
    run_rows: List[Dict[str, Any]] = []

    out_dir = CURRENT_DIR / "splits" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    written = empty = 0
    for i, (pid, name) in enumerate(ids.items(), start=1):
        games = fetch_game_log(session, pid, args.season)
        if not games:
            # Fall back one season: early in a season a hitter may have no log
            # yet, and last year's splits beat showing nothing at all.
            games = fetch_game_log(session, pid, args.season - 1)
        if not games:
            empty += 1
            continue

        payload = build_splits(games)
        payload["player_id"] = pid
        payload["name"] = name
        payload["games_logged"] = len(games)
        payload["season"] = args.season
        (out_dir / f"{pid}.json").write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        written += 1

        # NOT `rows` — that name is the SLATE list this loop's caller still
        # needs. The first ship shadowed it here, so by the time the run-board
        # block below asked the slate for its date, `rows` was one player's
        # game lines and r.get() blew up on a list — AFTER every splits file
        # had written, which made the failure read as "splits fine, runs
        # missing" on the branch. Found by executing main() against a stubbed
        # API; the compile step can never catch a shadow.
        glog = run_log(games)
        if glog:
            run_rows.append({
                "player_id": pid, "name": name,
                "team": teams.get(pid, ""), "opp": opps.get(pid, ""),
                "g": glog,
            })

        if i % 25 == 0:
            print(f"  {i}/{len(ids)} hitters…", file=sys.stderr)
        time.sleep(0.08)  # be polite to the API

    print(f"splits: wrote {written} hitter files ({empty} with no game log) -> {out_dir}",
          file=sys.stderr)

    # ── the run board ────────────────────────────────────────────────────────
    # Written for the TODAY slate only. A run board is a "who is hot right now"
    # question, and building one for tomorrow's card would just publish the
    # same rows under a name that implies they're about a different night.
    if args.label == "today":
        slate_date = ""
        for r in rows:
            slate_date = str(r.get("slate_date") or r.get("date") or "")[:10]
            if slate_date:
                break
        dest = CURRENT_DIR / "runs_latest.json"
        dest.write_text(json.dumps({
            "slate": args.label,
            "slate_date": slate_date,
            "season": args.season,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "games_per_player": RUN_GAMES,
            "columns": ["date", "opp", "H", "TB", "HRR", "HR", "isHome", "dayNight"],
            "players": run_rows,
            "note": ("Raw lines, newest first, NOT pre-aggregated per market. The "
                     "site derives the active run, the window rates and the strip "
                     "for any bar at any threshold from these, so changing the "
                     "market is instant and adding one costs nothing here. Games "
                     "with no plate appearance are already dropped — a night he "
                     "was rested is not a miss and must not break a run."),
        }, separators=(",", ":")), encoding="utf-8")
        print(f"runs: {len(run_rows)} hitters x <= {RUN_GAMES} games -> {dest} "
              f"({dest.stat().st_size / 1024:.0f} KB)", file=sys.stderr)

    # ── Starters ────────────────────────────────────────────────────────────
    # ~30 pitchers per slate against ~260 hitters, so this adds well under a
    # minute even with the Statcast pull. Each starter gets the same four
    # split families plus a log of every ball put in play against him.
    if not args.skip_pitchers:
        parms: Dict[Any, str] = {}
        for r in rows:
            pid = r.get("pitcher_id")
            if pid not in (None, "") and pid not in parms:
                parms[pid] = r.get("pitcher_name") or str(pid)

        p_written = p_empty = 0
        for i, (pid, name) in enumerate(parms.items(), start=1):
            games = fetch_game_log(session, pid, args.season, group="pitching")
            if not games:
                games = fetch_game_log(session, pid, args.season - 1, group="pitching")
            if not games:
                p_empty += 1
                continue

            payload = build_pitcher_splits(games)
            payload["player_id"] = pid
            payload["name"] = name
            payload["games_logged"] = len(games)
            payload["season"] = args.season
            payload["is_pitcher"] = True
            if not args.skip_pitcher_bbe:
                payload["contact_log"] = build_pitcher_contact_log(pid, args.season)
            (out_dir / f"pitcher_{pid}.json").write_text(
                json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            p_written += 1

            if i % 10 == 0:
                print(f"  {i}/{len(parms)} pitchers…", file=sys.stderr)
            time.sleep(0.08)

        print(f"splits: wrote {p_written} pitcher files ({p_empty} with no game log)",
              file=sys.stderr)
        written += p_written

    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
