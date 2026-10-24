"""
injury_status.py -- who is hurt, how likely he is to play, and who that helps.

    python bots/injury_status.py nba|nhl|nfl|mlb        print the current report and write a dated snapshot

Source: ESPN's public injury feeds (one per league). Every score the models make is
for a player who PLAYS, so a score is multiplied by the chance he does:

    expected_score = final_score x p_play

CHANCE OF PLAYING
  NFL     MEASURED. 2022-2025 regular seasons, every injury-report line joined to
          whether the player took a snap that week (QB/RB/WR/TE):
              Questionable 64%   Doubtful 0.5%   Out 0%   listed, no designation 92%
          Doubtful is Out. Questionable is two in three.
  others  PRIORS, not measurements -- NBA/NHL/MLB feeds only say "Day-To-Day" or
          "Out", and no history of their reports exists to measure against. Each
          nightly snapshot is saved (bbe-history branch, public/data/current/injury_snapshots/) so these can be
          measured after a few weeks. Until then the comment text sets the number:
          "game-time decision" / "questionable" / "probable" / "doubtful".

WHO GAINS
  teammates_out_value() adds up what the missing regulars were scoring. In the NBA
  backtest that number was the third-strongest factor (tune_models.py): when
  regulars sit, the others score more. In the NHL it was zero.
"""
import json
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

FEEDS = {
    "nba": "basketball/nba", "nhl": "hockey/nhl", "nfl": "football/nfl", "mlb": "baseball/mlb",
}
URL = "https://site.api.espn.com/apis/site/v2/sports/{}/injuries"

# status (lowercase) -> (chance he plays, where the number comes from)
NFL_P = {"out": 0.0, "doubtful": 0.005, "questionable": 0.64, "active": 0.92, "probable": 0.92}
OTHER_P = {"out": 0.0, "injured reserve": 0.0, "suspension": 0.0, "60-day-il": 0.0, "15-day-il": 0.0,
           "10-day-il": 0.0, "7-day-il": 0.0, "day-to-day": 0.70, "active": 0.92}
OUT = 0.0


def key(name: str) -> str:
    """'Wydett Williams Jr.' / 'wydett williams' -> 'wydett williams'."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", "", s)
    return re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", s).split() and " ".join(re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", s).split())


OUT_WORDS = re.compile(r"\b(ruled out|is out|are out|will not play|won't play|will miss|has been ruled out|sidelined|out for)\b")
GTD_WORDS = re.compile(r"game-time decision|\bquestionable\b|\bday-to-day\b")
YES_WORDS = re.compile(r"\bprobable\b|expected to play|will play|is available|cleared to play|will return")


def chance(sport: str, status: str, comment: str = "", name: str = "") -> (float, str):
    st = (status or "").strip().lower()
    if sport == "nfl" and st in NFL_P:
        return NFL_P[st], "measured (NFL 2022-25)"
    p = OTHER_P.get(st, 0.70)
    txt = (comment or "").lower()[:160]
    # The note has to be ABOUT him: it starts with his name ("Thiero (knee) is out ...").
    # Notes that mention a teammate's injury must not move his number.
    parts = [t for t in re.split(r"\W+", (name or "").lower()) if len(t) > 2]
    about_him = bool(parts) and any(txt.startswith(t) for t in parts)
    if st in ("day-to-day",) and about_him:
        if OUT_WORDS.search(txt):
            p = 0.0
        elif "doubtful" in txt:
            p = 0.10
        elif GTD_WORDS.search(txt):
            p = 0.50
        elif YES_WORDS.search(txt):
            p = 0.90
    return p, "prior (not yet measured)"


def fetch(sport: str) -> List[Dict[str, Any]]:
    raw = subprocess.run(["curl", "-s", "-m", "30", URL.format(FEEDS[sport])], capture_output=True, text=True).stdout
    out = []
    for team in json.loads(raw).get("injuries", []):
        for i in team.get("injuries", []):
            a = i.get("athlete", {})
            name = a.get("displayName") or ""
            p, src = chance(sport, i.get("status", ""), i.get("shortComment", ""), name)
            out.append({"sport": sport, "team": (a.get("team") or {}).get("abbreviation") or team.get("displayName", ""),
                        "name": name, "key": key(name), "status": i.get("status", ""), "p_play": p, "p_source": src,
                        "detail": (i.get("shortComment") or "")[:200], "updated": i.get("date", "")})
    return out


def snapshot(rows: List[Dict[str, Any]], sport: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sport}_{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%MZ')}.json"
    path.write_text(json.dumps(rows, indent=1))
    return path


def apply(rows: Iterable[Dict[str, Any]], injuries: List[Dict[str, Any]], name_field: str = "name") -> int:
    """Stamp p_play / inj_status / expected_score on scored rows. Returns how many matched.
    Rows without a match are left at p_play 1.0 -- no report is not a report of health,
    but it is what the feed says."""
    by_name = {}
    for r in injuries:
        by_name.setdefault(r["key"], []).append(r)
    n = 0
    for row in rows:
        hit = by_name.get(key(str(row.get(name_field) or "")))
        if hit:
            team = str(row.get("team") or "")
            hit = [h for h in hit if not team or h["team"] == team] or hit
            h = hit[0]
            row["inj_status"], row["p_play"], row["inj_detail"] = h["status"], h["p_play"], h["detail"]
            n += 1
        else:
            row["inj_status"], row["p_play"], row["inj_detail"] = "", 1.0, ""
        if "final_score" in row:
            row["expected_score"] = round(row["final_score"] * row["p_play"], 2)
    return n


def teammates_out_value(team: str, injuries: List[Dict[str, Any]], recent_output: Dict[str, float],
                        threshold: float = 0.5) -> float:
    """What the regulars who are probably missing were producing (points / goals / touches
    per game, from `recent_output`, keyed by normalised name)."""
    return sum(recent_output.get(h["key"], 0.0) for h in injuries
               if h["team"] == team and h["p_play"] < threshold)


if __name__ == "__main__":
    # python bots/injury_status.py nba|nhl|nfl|mlb|all [snapshot_dir]
    which = sys.argv[1] if len(sys.argv) > 1 else "nba"
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent / "outputs" / "injury_snapshots"
    for sport in (FEEDS if which == "all" else [which]):
        try:
            rows = fetch(sport)
        except Exception as e:                  # one dead feed must not cost the other three
            print(f"{sport}: feed failed ({e})", file=sys.stderr)
            continue
        path = snapshot(rows, sport, out_dir)
        print(f"{sport}: {len(rows)} on the report, snapshot -> {path}")
        if which != "all":
            for r in sorted(rows, key=lambda r: (-r["p_play"], r["team"]))[:15]:
                print(f"  {r['team']:>4} {r['name']:<24} {r['status']:<16} plays {int(100 * r['p_play']):>3}%  {r['detail'][:60]}")
