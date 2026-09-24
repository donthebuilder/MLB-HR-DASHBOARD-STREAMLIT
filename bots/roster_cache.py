"""Every active MLB roster, cached nightly, with each man's season line.

2026-09-24, Donovan: "cache every active roster ... when I'm looking for a
player that's not in the lineup I can see all their data ... I should be able
to search every active player." Three shapes of missing man drove this:

  - moved:   Victor Mesa Jr. (MIA -> TB) -- the site's search still said MIA
             because the only roster it knew was tonight's slate.
  - out:     Ozzie Albies -- not in tonight's ATL lineup, so not on the slate,
             so not searchable with any data behind him.
  - dropped: Jared Serna -- IN the posted MIA lineup, dropped from the slate by
             extract_lineup() (fixed the same day in mlb_dashboard.py).

The slate stays the slate: ~270 hitters the model rates for tonight. This is
the other ~800: who is on which club right now, in what state (active, IL-10,
IL-60, paternity, ...), and what he has done this season. The site's search
reads it first (instant, no live API call per keystroke) and the player card
opens for anyone on it with the season line up top and every live panel
(splits, EV log, spray archive) behind it -- no model score, and it says so.

Source: MLB StatsAPI, public, ~33 calls a run.
  /teams?sportId=1                               the 30 clubs
  /teams/{id}/roster?rosterType=40Man            everyone on the 40-man, with
                                                 status (A / D10 / D60 / ...)
  /stats?stats=season&group=hitting&playerPool=ALL   one call, every hitter's
                                                 season line

Writes public/data/current/rosters_mlb.json. publish_data.sh ships it.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://statsapi.mlb.com/api/v1"
OUT = Path(__file__).resolve().parents[1] / "public" / "data" / "current" / "rosters_mlb.json"


def get(path: str, params: dict | None = None, tries: int = 3):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "moonshot-roster-cache/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def season_year(today: dt.date) -> int:
    # January-February belongs to the season just played.
    return today.year if today.month >= 3 else today.year - 1


def fnum(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def main() -> int:
    today = dt.date.today()
    season = season_year(today)
    teams = get("/teams", {"sportId": 1, "season": season, "fields": "teams,id,abbreviation,name"})["teams"]
    abbr = {t["id"]: t["abbreviation"] for t in teams}

    # one call: every hitter's season line
    hitting = {}
    blob = get("/stats", {"stats": "season", "group": "hitting", "season": season, "sportIds": 1,
                          "playerPool": "ALL", "limit": 5000})
    for sp in (blob.get("stats") or [{}])[0].get("splits") or []:
        pid = (sp.get("player") or {}).get("id")
        st = sp.get("stat") or {}
        if not pid:
            continue
        pa = int(st.get("plateAppearances") or 0)
        hitting[pid] = {
            "g": int(st.get("gamesPlayed") or 0), "pa": pa, "ab": int(st.get("atBats") or 0),
            "h": int(st.get("hits") or 0), "hr": int(st.get("homeRuns") or 0),
            "r": int(st.get("runs") or 0), "rbi": int(st.get("rbi") or 0),
            "bb": int(st.get("baseOnBalls") or 0), "k": int(st.get("strikeOuts") or 0),
            "sb": int(st.get("stolenBases") or 0), "xbh": int(st.get("doubles") or 0) + int(st.get("triples") or 0) + int(st.get("homeRuns") or 0),
            "avg": fnum(st.get("avg")), "obp": fnum(st.get("obp")), "slg": fnum(st.get("slg")), "ops": fnum(st.get("ops")),
            "iso": (round(fnum(st.get("slg")) - fnum(st.get("avg")), 3) if fnum(st.get("slg")) is not None and fnum(st.get("avg")) is not None else None),
        }

    players = []
    for t in teams:
        roster = get(f"/teams/{t['id']}/roster", {"rosterType": "40Man", "season": season,
                     "hydrate": "person(batSide,pitchHand)"})
        for r in roster.get("roster") or []:
            person = r.get("person") or {}
            pid = person.get("id")
            if not pid:
                continue
            status = r.get("status") or {}
            pos = (r.get("position") or {}).get("abbreviation") or ""
            players.append({
                "player_id": pid,
                "name": person.get("fullName") or "",
                "team": abbr.get(t["id"], t.get("abbreviation")),
                "team_id": t["id"],
                "team_name": t.get("name"),
                "pos": pos,
                "is_pitcher": pos in ("P", "SP", "RP"),
                "bats": ((person.get("batSide") or {}).get("code")) or "?",
                "throws": ((person.get("pitchHand") or {}).get("code")) or "?",
                "jersey": r.get("jerseyNumber") or None,
                "status": status.get("code") or "",
                "status_word": status.get("description") or "",
                "season": hitting.get(pid),
            })
        time.sleep(0.2)

    players.sort(key=lambda p: (p["team"], p["name"]))
    out = {
        "season": season,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "statsapi.mlb.com rosterType=40Man + stats?playerPool=ALL",
        "teams": len(teams),
        "n": len(players),
        "players": players,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    by_status = {}
    for p in players:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    print(f"rosters_mlb.json: {len(players)} players on {len(teams)} 40-man rosters, "
          f"{sum(1 for p in players if p['season'])} with a season line, statuses {by_status}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
