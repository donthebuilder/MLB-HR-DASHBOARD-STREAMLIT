#!/usr/bin/env python3
"""
CONTEXT PACK (2026-08-08, Donovan: "please ensure the actual bot is being
updated with all these stats we have added... just in case anything happens
with the site").

Every context stat the SITE computes live in the browser, recomputed here by
the bot and published to the data branch nightly. If the site vanished
tomorrow, this file is the record:

  teams.{ABBR}.rest_flags     day-after-night / doubleheader / travel / 3-in-3
  teams.{ABBR}.pen            season reliever-only HR/9 + yesterday's workload
  players.{pid}.venue_hr      HR at tonight's park, 2 seasons, + his own pace
                              over the same window (vs_self ratio)
  players.{pid}.xpa           expected PA from lineup slot (league table)
  players.{pid}.pull_wall     pull-side line/gap at tonight's park + pctile
  walls.{venue}               full fieldInfo dimensions, all parks

Every endpoint here was verified live on the site side before being used
(fieldInfo, schedule range dayNight/doubleHeader/venue, statSplits rp,
gameLog+schedule join). Two-lane rule holds: this file is CONTEXT — nothing
in it ever feeds a score.

Usage: python bots/context_pack.py --slate public/data/today_slate.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import requests

API = "https://statsapi.mlb.com/api/v1"
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "bots" else SCRIPT_DIR
OUTPUTS_DIR = REPO_ROOT / "bots" / "outputs"
PUBLIC_CURRENT = REPO_ROOT / "public" / "data" / "current"

S = requests.Session()
S.headers["User-Agent"] = "moonshot-context-pack/1.0"


def get(url: str) -> dict | None:
    try:
        r = S.get(url, timeout=25)
        return r.json() if r.ok else None
    except Exception:
        return None


# ── slate ────────────────────────────────────────────────────────────────────
PLAYER_KEYS = ("players", "all_players", "player_pool", "slate_players", "rows",
               "picks", "top_picks", "hr_picks", "hit_picks", "hrr_picks",
               "contact_picks", "top_board", "hr_board", "alt_looks")


def load_slate(path: Path) -> tuple[str, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    date = str(data.get("slate_date") or data.get("date") or dt.date.today().isoformat())[:10]
    seen: dict[str, dict] = {}

    def eat(rows):
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            pid = str(r.get("player_id") or r.get("id") or r.get("mlb_id") or "")
            if not pid or pid in seen:
                continue
            seen[pid] = {
                "player_id": pid,
                "name": r.get("name") or r.get("player_name") or "",
                "team": str(r.get("team") or "").upper(),
                "venue_name": r.get("venue_name") or "",
                "game_pk": r.get("game_pk"),
                "bats": str(r.get("bats") or r.get("handedness") or "?")[:1].upper(),
                "lineup_spot": r.get("lineup_spot") or r.get("batting_order"),
            }

    if isinstance(data, list):
        eat(data)
    else:
        for k in PLAYER_KEYS:
            v = data.get(k)
            if isinstance(v, list):
                eat(v)
            elif isinstance(v, dict):
                eat(v.values())
    return date, list(seen.values())


# ── xPA (same static league table as lib/xpa.js — no nightly feed exists,
#    a static long-run table is the honest implementation) ───────────────────
XPA_BY_SLOT = {1: 4.65, 2: 4.55, 3: 4.44, 4: 4.34, 5: 4.24,
               6: 4.13, 7: 4.02, 8: 3.90, 9: 3.77}


# ── team abbreviations ───────────────────────────────────────────────────────
def team_abbrs() -> dict[int, str]:
    j = get(f"{API}/teams?sportId=1&fields=teams,id,abbreviation")
    return {t["id"]: t.get("abbreviation", "") for t in (j or {}).get("teams", []) if t.get("id")}


# ── rest & travel (port of lib/restTravel.js) ────────────────────────────────
def rest_travel(slate_date: str) -> dict[int, list[dict]]:
    d0 = dt.date.fromisoformat(slate_date)
    j = get(f"{API}/schedule?sportId=1&startDate={d0 - dt.timedelta(days=2)}&endDate={d0}")
    by_team: dict[int, dict[str, list[dict]]] = {}
    for d in (j or {}).get("dates", []):
        for g in d.get("games", []):
            for side in ("home", "away"):
                tid = (((g.get("teams") or {}).get(side) or {}).get("team") or {}).get("id")
                if not tid:
                    continue
                by_team.setdefault(tid, {}).setdefault(d["date"], []).append({
                    "dayNight": g.get("dayNight"), "doubleHeader": g.get("doubleHeader"),
                    "venueId": (g.get("venue") or {}).get("id"),
                })
    d1, d2 = str(d0 - dt.timedelta(days=1)), str(d0 - dt.timedelta(days=2))
    out: dict[int, list[dict]] = {}
    for tid, days in by_team.items():
        today, yest = days.get(slate_date, []), days.get(d1, [])
        if not today:
            continue
        flags = []
        if any(g["dayNight"] == "day" for g in today) and any(g["dayNight"] == "night" for g in yest):
            flags.append({"flag": "day_after_night", "label": "day-after-night"})
        if len(today) >= 2 or any((g.get("doubleHeader") or "N") != "N" for g in today):
            flags.append({"flag": "doubleheader", "label": "doubleheader"})
        v_t = today[0].get("venueId")
        v_y = yest[-1].get("venueId") if yest else None
        if v_t and v_y and v_t != v_y and any(g["dayNight"] == "night" for g in yest):
            flags.append({"flag": "travel_night", "label": "travel night"})
        elif today and yest and days.get(d2):
            flags.append({"flag": "three_in_three", "label": "3-in-3"})
        if flags:
            out[tid] = flags
    return out


# ── bullpen: season reliever HR/9 + yesterday's workload (port of bullpen.js)
def pen_stats() -> dict[int, dict]:
    yr = dt.date.today().year
    j = get(f"{API}/teams/stats?season={yr}&group=pitching&stats=statSplits&sitCodes=rp&sportIds=1"
            "&fields=stats,splits,team,id,stat,homeRunsPer9,inningsPitched,homeRuns")
    out = {}
    for sp in ((j or {}).get("stats") or [{}])[0].get("splits", []):
        tid = (sp.get("team") or {}).get("id")
        if not tid:
            continue
        try:
            hr9 = float(sp["stat"]["homeRunsPer9"])
        except (KeyError, TypeError, ValueError):
            hr9 = None
        out[tid] = {"hr9": hr9, "hr": sp.get("stat", {}).get("homeRuns"),
                    "ip": sp.get("stat", {}).get("inningsPitched")}
    return out


def team_defense() -> dict[int, dict]:
    """BABIP-against per team (2026-08-08, Donovan: defensive stats). The
    cleanest public defense proxy: (H−HR)/(BF−K−BB−HBP−HR). All input
    fields verified live on all 30 teams. Published so the graded archive
    can validate it BEFORE it ever touches a score (two-lane rule)."""
    yr = dt.date.today().year
    j = get(f"{API}/teams/stats?season={yr}&group=pitching&stats=season&sportIds=1"
            "&fields=stats,splits,team,id,stat,hits,homeRuns,strikeOuts,baseOnBalls,battersFaced,hitByPitch")
    rows = []
    for sp in ((j or {}).get("stats") or [{}])[0].get("splits", []):
        s = sp.get("stat") or {}
        bip = (s.get("battersFaced") or 0) - (s.get("strikeOuts") or 0) \
            - (s.get("baseOnBalls") or 0) - (s.get("hitByPitch") or 0) - (s.get("homeRuns") or 0)
        tid = (sp.get("team") or {}).get("id")
        if not tid or bip < 200:
            continue
        rows.append((tid, ((s.get("hits") or 0) - (s.get("homeRuns") or 0)) / bip))
    rows.sort(key=lambda x: x[1])
    out = {}
    for i, (tid, babip) in enumerate(rows):
        pct = round(100 * i / max(1, len(rows) - 1))
        out[tid] = {"babip_against": round(babip, 4), "pctile": pct,
                    "word": "elite glove" if pct <= 20 else "leaky defense" if pct >= 80 else "league-normal"}
    return out


def pen_fatigue(slate_date: str) -> dict[int, dict]:
    yday = dt.date.fromisoformat(slate_date) - dt.timedelta(days=1)
    sched = get(f"{API}/schedule?sportId=1&startDate={yday}&endDate={yday}&fields=dates,games,gamePk")
    out: dict[int, dict] = {}
    for d in (sched or {}).get("dates", []):
        for g in d.get("games", []):
            box = get(f"{API}/game/{g['gamePk']}/boxscore")
            if not box:
                continue
            for side in ("home", "away"):
                t = (box.get("teams") or {}).get(side) or {}
                tid = (t.get("team") or {}).get("id")
                if not tid:
                    continue
                rec = out.setdefault(tid, {"used": 0, "pitches": 0, "names": []})
                for p in (t.get("players") or {}).values():
                    pit = (p.get("stats") or {}).get("pitching") or {}
                    if not pit or pit.get("gamesStarted"):
                        continue
                    # bulk arms (4+ IP) poison the reliever read — same
                    # exclusion the site's lib/bullpen.js earned by test
                    try:
                        if float(pit.get("inningsPitched") or 0) >= 4:
                            continue
                    except ValueError:
                        pass
                    n = int(pit.get("numberOfPitches") or 0)
                    if n <= 0:
                        continue
                    rec["used"] += 1
                    rec["pitches"] += n
                    rec["names"].append({"name": (p.get("person") or {}).get("fullName", ""), "pitches": n})
    return out


# ── walls (port of lib/walls.js) ─────────────────────────────────────────────
def walls() -> dict[str, dict]:
    j = get(f"{API}/venues?sportId=1&hydrate=fieldInfo")
    out = {}
    for v in (j or {}).get("venues", []):
        f = v.get("fieldInfo") or {}
        if f.get("leftLine") is None or f.get("rightLine") is None:
            continue
        out[v["name"]] = {"id": v["id"], "leftLine": f.get("leftLine"),
                          "leftCenter": f.get("leftCenter"), "center": f.get("center"),
                          "rightCenter": f.get("rightCenter"), "rightLine": f.get("rightLine")}
    return out


def pull_wall(bats: str, venue: str, wall_map: dict[str, dict]) -> dict | None:
    v = wall_map.get(venue)
    if not v or bats not in ("L", "R", "S"):
        return None
    sides = ([("RF", v["rightLine"], v["rightCenter"], "rightLine")] if bats == "L"
             else [("LF", v["leftLine"], v["leftCenter"], "leftLine")] if bats == "R"
             else [("LF", v["leftLine"], v["leftCenter"], "leftLine"),
                   ("RF", v["rightLine"], v["rightCenter"], "rightLine")])
    best = min(sides, key=lambda s: s[1] or 999)
    if best[1] is None:
        return None
    vals = [w[best[3]] for w in wall_map.values() if w.get(best[3]) is not None]
    pct = round(100 * sum(1 for x in vals if x < best[1]) / len(vals)) if vals else None
    return {"side": best[0], "line": best[1], "gap": best[2], "line_pctile": pct}


# ── venue HR + his own pace (port of lib/venueHr.js) ─────────────────────────
def game_logs(pid: str, season: int) -> list[dict]:
    j = get(f"{API}/people/{pid}/stats?stats=gameLog&group=hitting&season={season}"
            "&fields=stats,splits,stat,homeRuns,plateAppearances,game,gamePk")
    return [{"pk": (sp.get("game") or {}).get("gamePk"),
             "hr": int(sp.get("stat", {}).get("homeRuns") or 0),
             "pa": int(sp.get("stat", {}).get("plateAppearances") or 0)}
            for sp in ((j or {}).get("stats") or [{}])[0].get("splits", [])
            if (sp.get("game") or {}).get("gamePk")]


def venues_for(pks: list[int]) -> dict[int, dict]:
    """gamePk → {id, name}. ID is the match key (2026-08-08 — exact-name
    matching silently dropped whole parks after renames; same fix as the
    site's lib/venueHr.js, same day)."""
    out = {}
    for i in range(0, len(pks), 40):
        j = get(f"{API}/schedule?sportId=1&gamePks={','.join(map(str, pks[i:i+40]))}"
                "&fields=dates,games,gamePk,venue,id,name")
        for d in (j or {}).get("dates", []):
            for g in d.get("games", []):
                if g.get("gamePk") and (g.get("venue") or {}).get("name"):
                    out[g["gamePk"]] = {"id": g["venue"].get("id"), "name": g["venue"]["name"]}
    return out


def _norm_name(s: str) -> str:
    return "".join(c for c in str(s or "").lower() if c.isalnum())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slate", required=True)
    args = ap.parse_args()
    slate_path = Path(args.slate)
    if not slate_path.exists():
        print(f"No slate at {slate_path}", file=sys.stderr)
        return 1
    slate_date, players = load_slate(slate_path)
    print(f"Context pack for {slate_date}: {len(players)} players", file=sys.stderr)

    abbrs = team_abbrs()
    rest = rest_travel(slate_date)
    pens = pen_stats()
    fat = pen_fatigue(slate_date)
    wall_map = walls()
    defense = team_defense()

    teams_out: dict[str, Any] = {}
    for tid, ab in abbrs.items():
        entry: dict[str, Any] = {}
        if tid in rest:
            entry["rest_flags"] = rest[tid]
        pen = dict(pens.get(tid) or {})
        if tid in fat:
            pen["yesterday"] = fat[tid]
        if pen:
            entry["pen"] = pen
        if tid in defense:
            entry["defense"] = defense[tid]
        if entry:
            teams_out[ab] = entry

    # venue HR — the expensive lane, so game logs are fetched once per player
    # and gamePk→venue is resolved in shared batches
    yr = dt.date.today().year
    logs: dict[str, list[dict]] = {}
    all_pks: set[int] = set()
    for p in players:
        rows = game_logs(p["player_id"], yr) + game_logs(p["player_id"], yr - 1)
        logs[p["player_id"]] = rows
        all_pks.update(x["pk"] for x in rows)
        # tonight's game rides in the same batch — its venue ID is the target
        try:
            if p.get("game_pk"):
                all_pks.add(int(p["game_pk"]))
        except (TypeError, ValueError):
            pass
    vmap = venues_for(sorted(all_pks))

    players_out: dict[str, Any] = {}
    for p in players:
        pid = p["player_id"]
        rows = logs.get(pid) or []
        try:
            target_id = (vmap.get(int(p.get("game_pk") or 0)) or {}).get("id")
        except (TypeError, ValueError):
            target_id = None

        def _here(x):
            v = vmap.get(x["pk"])
            if not v:
                return False
            if target_id is not None and v.get("id") is not None:
                return v["id"] == target_id
            return _norm_name(v.get("name")) == _norm_name(p["venue_name"])
        here = [x for x in rows if _here(x)]
        hr_here = sum(x["hr"] for x in here)
        hr_all = sum(x["hr"] for x in rows)
        rate = hr_here / len(here) if here else None
        rate_all = hr_all / len(rows) if rows else None
        spot = p.get("lineup_spot")
        try:
            xpa = XPA_BY_SLOT.get(int(spot)) if spot is not None else None
        except (TypeError, ValueError):
            xpa = None
        players_out[pid] = {
            "name": p["name"], "team": p["team"],
            "venue_hr": {
                "venue": p["venue_name"], "hr": hr_here, "games": len(here),
                "hr_all": hr_all, "games_all": len(rows),
                "rate": round(rate, 4) if rate is not None else None,
                "rate_all": round(rate_all, 4) if rate_all is not None else None,
                "vs_self": round(rate / rate_all, 3) if rate is not None and rate_all else None,
                "seasons": f"{yr - 1}–{str(yr)[2:]}",
            },
            "xpa": xpa,
            "pull_wall": pull_wall(p["bats"], p["venue_name"], wall_map),
        }

    payload = {
        "slate_date": slate_date,
        "generated": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "teams": teams_out,
        "players": players_out,
        "walls": wall_map,
    }
    PUBLIC_CURRENT.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    for base in (PUBLIC_CURRENT, OUTPUTS_DIR):
        (base / "context_pack_latest.json").write_text(json.dumps(payload), encoding="utf-8")
        (base / f"context_pack_{slate_date}.json").write_text(json.dumps(payload), encoding="utf-8")
    print(f"Written: context_pack_latest.json ({len(players_out)} players, "
          f"{len(teams_out)} teams, {len(wall_map)} parks)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
