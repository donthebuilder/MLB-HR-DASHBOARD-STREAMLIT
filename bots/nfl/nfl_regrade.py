#!/usr/bin/env python3
"""nfl_regrade.py -- correct a published week's grade (2026-10-01).

Donovan, 10-01: regrade week 1 from the pregame card, and count each player
once in weeks 2 and 3. Two tools, one script:

  --rebuild-card W   Rebuild week W's card from the PREGAME record: for every
                     player and market, the latest nfl_prediction_log row
                     generated before HIS team's kickoff (nflverse schedule),
                     then nfl_picks.build()'s own rule -- top DEPTH by score,
                     low_sample rows only as backfill. Written as
                     nfl_picks_<season>_w<WW>.json with "rebuilt" provenance.
                     Week 1 needs this: no week-1 card was archived, and the
                     published w01 grade used the live card -- already week 2's,
                     built with week-1 results in its form features.
  --grade W[,W...]   Re-run nfl_results.py for each week against its archived
                     card (grade() now counts each player once per market).

Never writes a pregame number after the fact: the rebuilt card is made only
of rows logged before each team's kickoff, and nothing is re-scored.

  python3 nfl_regrade.py --dir ../../public/data/current --rebuild-card 1 --grade 1,2,3
"""
from __future__ import annotations
import argparse, datetime as dt, glob, json, os, subprocess, sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def kickoffs(season: int, week: int) -> dict[str, dt.datetime]:
    """team -> kickoff (UTC) for that week, from nflverse's schedule."""
    import nflreadpy as nfl
    sch = nfl.load_schedules(seasons=[season])
    et = ZoneInfo("America/New_York")
    out: dict[str, dt.datetime] = {}
    for r in sch.filter(sch["week"] == week).iter_rows(named=True):
        if not r.get("gameday") or not r.get("gametime"):
            continue
        h, m = map(int, str(r["gametime"]).split(":")[:2])
        y, mo, d = map(int, str(r["gameday"]).split("-"))
        t = dt.datetime(y, mo, d, h, m, tzinfo=et).astimezone(dt.timezone.utc)
        for side in ("home_team", "away_team"):
            out[str(r[side])] = t
    return out


def pregame_rows(current: Path, season: int, week: int, ko: dict[str, dt.datetime]) -> list[dict]:
    """One row per player with every market's pregame score: the latest log
    row generated before his team's kickoff. Week-mode logs only (next-week
    look-ahead logs are a different card)."""
    best: dict[tuple[str, str], tuple[dt.datetime, dict]] = {}
    for f in sorted(glob.glob(str(current / f"nfl_prediction_log_{season}-wk{week:02d}.*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            header = json.loads(fh.readline() or "{}")
            if header.get("mode") != "week" or int(header.get("week") or -1) != week:
                continue
            gen = dt.datetime.fromisoformat(str(header["generated_at"]).replace("Z", "+00:00"))
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                k = ko.get(str(r.get("team")))
                if k is None or gen >= k or not isinstance(r.get("score"), (int, float)):
                    continue
                key = (str(r["player_id"]), str(r["market"]))
                if key not in best or gen > best[key][0]:
                    best[key] = (gen, r)
    players: dict[str, dict] = {}
    for (pid, market), (_gen, r) in best.items():
        p = players.setdefault(pid, {"player_id": pid, "name": r.get("player"), "team": r.get("team"),
                                     "opp": r.get("opp"), "position": r.get("position"),
                                     "scores": {}, "low_sample": False, "questionable": False, "carryover": False})
        p["scores"][market] = r["score"]
        p["low_sample"] = p["low_sample"] or bool(r.get("low_sample"))
        p["questionable"] = p["questionable"] or bool(r.get("questionable"))
        p["carryover"] = p["carryover"] or bool(r.get("carryover"))
    return list(players.values())


def rebuild_card(current: Path, season: int, week: int) -> Path:
    import nfl_picks
    ko = kickoffs(season, week)
    rows = pregame_rows(current, season, week, ko)
    if not rows:
        raise SystemExit(f"no pregame log rows for {season} week {week} -- nothing rebuilt")
    card = nfl_picks.build(rows)
    out = current / f"nfl_picks_{season}_w{week:02d}.json"
    out.write_text(json.dumps({
        "season": season, "week": week, "card": card,
        "rebuilt": {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "from": "nfl_prediction_log rows generated before each team's kickoff",
                    "why": "no week card was archived; the first grade used the next week's live card"},
    }, separators=(",", ":")))
    print(f"  rebuilt {out.name}: " + "; ".join(f"{k} {[r['name'] for r in v['rungs']]}" for k, v in card.items()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--rebuild-card", type=int, action="append", default=[])
    ap.add_argument("--grade", type=str, default="")
    a = ap.parse_args()
    current = Path(a.dir)
    for w in a.rebuild_card:
        rebuild_card(current, a.season, w)
    # nfl_results.py also rewrites the LIVE nfl_results.json (this week's
    # page) on every run; a regrade of a past week must only touch that week's
    # archive, so the live file is set aside and put back.
    live = current / "nfl_results.json"
    saved = live.read_bytes() if live.exists() else None
    try:
        for w in [int(x) for x in a.grade.split(",") if x.strip()]:
            card = current / f"nfl_picks_{a.season}_w{w:02d}.json"
            print(f"  regrading week {w} against {card.name}")
            subprocess.run([sys.executable, str(HERE / "nfl_results.py"), "--mode", "week", "--season", str(a.season),
                            "--week", str(w), "--card", str(card), "--out", str(current), "--prefix", "nfl_"], check=True)
    finally:
        if saved is not None:
            live.write_bytes(saved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
