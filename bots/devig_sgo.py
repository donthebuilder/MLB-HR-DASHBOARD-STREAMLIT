#!/usr/bin/env python3
"""
devig_sgo.py -- the "EV dingers" method for any sport: find prices at a book that are better than the market's fair price.

    python bots/devig_sgo.py nfl|nhl|nba|mlb [--limit 6] [--min-ev 0.04] [--env PATH]

For each player prop SGO (SportsGameOdds) returns a de-vigged `fairOdds` (their consensus fair price, cut taken out) and every
book's own price. A bet at a book is +EV when its payout beats the fair chance:

    EV = fair chance x payout - (1 - fair chance)          (per $1 staked; +5% means 5 cents of expected profit)

That is the whole method behind "EV dinger" style posts: same props, same math, sorted by EV. What makes one trustworthy:
  * sharp-agree: Pinnacle / Circa (the lowest-cut books) land within 1.5 points of SGO's fair chance
  * many books: the fair chance is a consensus of several prices, not one
  * not stale: a price that already moved is gone by the time you read this (lines move fast; check the book)
EV only says the price is generous against the market. It says nothing about who will win; a +8% EV on a 10% chance still
loses nine times in ten. Judge it over hundreds of bets, never one.

Markets (SGO oddIDs, measured on live events 2026-10-24):
  NFL  touchdowns-<P>-game-yn-yes      anytime TD        NHL  points-<P>-game-yn-yes = ANYTIME GOAL (SGO names goals 'points'),
  NBA  points|rebounds|assists|points+rebounds+assists-<P>-game-ou-over      goals+assists = points, firstToScore = first goal
  MLB  batting_homeRuns-<P>-game-yn-yes  (verified in season)
The key is read from SGO_API_KEY (env) or --env file, used for the request, and never printed. SGO budget: one object per event.
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

SGO = "https://api.sportsgameodds.com/v2"
SHARP = ("pinnacle", "circa")
LEAGUE = {"nfl": "NFL", "nhl": "NHL", "nba": "NBA", "mlb": "MLB"}
MARKETS = {
    "nfl": [("touchdowns", "yn", "Anytime TD")],
    # SGO's NHL stat names are not what they sound like (read from the marketName on live events, 2026-10-24):
    #   points = GOALS ("Any Goals Yes/No", "Goals Over/Under 0.5")   <- the anytime goal market
    #   goals+assists = POINTS     firstToScore = FIRST goal of the game     assists, shots_onGoal as named
    "nhl": [("points", "yn", "Anytime goal"), ("goals+assists", "yn", "Any point (G+A)"), ("assists", "yn", "Any assist"), ("shots_onGoal", "ou", "Shots on goal")],
    "nba": [("points", "ou", "Points"), ("rebounds", "ou", "Rebounds"), ("assists", "ou", "Assists"), ("points+rebounds+assists", "ou", "PRA")],
    "mlb": [("batting_homeRuns", "yn", "Home run")],
}


def load_key(env_path):
    key = os.environ.get("SGO_API_KEY", "")
    if not key and env_path and Path(env_path).exists():
        for line in open(env_path):
            if line.strip().startswith("SGO_API_KEY="):
                key = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise SystemExit("SGO_API_KEY not found (set it in the environment or pass --env)")
    return key


def prob(american):
    a = int(american)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def ev(p, american):
    a = int(american)
    pay = a / 100 if a > 0 else 100 / -a
    return p * pay - (1 - p)


def name_of(pid):
    return re.sub(r"_\d+_[A-Z]+$", "", pid).replace("_", " ").title()


def fmt(a):
    a = int(a)
    return f"+{a}" if a > 0 else str(a)


def rows_for(sport, events):
    out = []
    for e in events:
        t = e.get("teams", {})
        game = f"{t.get('away', {}).get('names', {}).get('short', '?')}@{t.get('home', {}).get('names', {}).get('short', '?')}"
        for key, o in (e.get("odds") or {}).items():
            parts = key.split("-")
            if len(parts) < 5 or parts[2] != "game" or o.get("ended"):
                continue
            for stat, kind, label in MARKETS[sport]:
                if parts[0] != stat or parts[3] != kind:
                    continue
                if kind == "yn" and parts[4] != "yes":
                    continue
                if kind == "ou" and parts[4] != "over":
                    continue
                fair = o.get("fairOdds")
                if not fair:
                    continue
                line = o.get("fairOverUnder") or o.get("bookOverUnder")      # yes/no markets carry their line too (points yes = OVER 1.5, not 1+)
                p = prob(fair)
                books = {}
                for b, d in (o.get("byBookmaker") or {}).items():
                    if d.get("available") is False or not d.get("odds"):
                        continue
                    if kind == "ou" and line is not None and d.get("overUnder") not in (None, line, str(line)):
                        continue                                  # a different line is a different bet
                    books[b.lower()] = int(d["odds"])
                if not books:
                    continue
                evs = {b: ev(p, a) for b, a in books.items()}
                best = max(evs, key=evs.get)
                # second opinion that does not lean on SGO's own fair price: what do the OTHER books say?
                allp = [prob(a) for a in books.values()]
                scale = p / (sum(allp) / len(allp))                        # SGO's fair / the average book (about 1 / (1 + the cut))
                others = sorted(prob(a) for b, a in books.items() if b != best)
                ev_others = None
                if others:
                    med = others[len(others) // 2] if len(others) % 2 else (others[len(others) // 2 - 1] + others[len(others) // 2]) / 2
                    ev_others = ev(med * scale, books[best])
                sharp = [prob(books[b]) for b in SHARP if b in books]
                gap = abs(sum(sharp) / len(sharp) - p) * 100 if sharp else None
                out.append({"game": game, "market": label, "line": line, "player": name_of(parts[1]), "fair_pct": round(100 * p, 1), "fair_odds": int(fair),
                            "best_book": best, "best_price": books[best], "best_ev_pct": round(100 * evs[best], 1), "books": len(books),
                            "ev_vs_other_books_pct": round(100 * ev_others, 1) if ev_others is not None else None,
                            "outlier": bool(ev_others is not None and ev_others < 0.5 * evs[best]),
                            "sharp": ("sharp-agree" if gap is not None and gap < 1.5 else f"sharp gap {gap:.1f}pts" if gap is not None else "no sharp book"),
                            "prices": books})
    # rank by the CONSERVATIVE read: the smaller of "EV against SGO's fair price" and "EV against the other books"
    return sorted(out, key=lambda r: min(r["best_ev_pct"], r["ev_vs_other_books_pct"] if r["ev_vs_other_books_pct"] is not None else -999), reverse=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sport", choices=list(LEAGUE))
    ap.add_argument("--limit", type=int, default=6, help="events to request (one SGO object each)")
    ap.add_argument("--min-ev", type=float, default=0.04)
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--env", default=str(Path.home() / "Desktop/moonshot-push/.env.local"))
    a = ap.parse_args()
    key = load_key(a.env)
    now = datetime.now(timezone.utc)
    r = requests.get(f"{SGO}/events", headers={"x-api-key": key},
                     params={"leagueID": LEAGUE[a.sport], "oddsAvailable": "true", "limit": a.limit,
                             "startsAfter": now.isoformat(), "startsBefore": (now + timedelta(hours=a.hours)).isoformat()}, timeout=40)
    r.raise_for_status()
    events = r.json().get("data", [])
    rows = rows_for(a.sport, events)
    print(f"{a.sport.upper()}: {len(events)} events, {len(rows)} props with a fair price and book prices\n")
    print(f"{'player':<22}{'game':<9}{'market':<11}{'fair':>6}{'best book':>11}{'price':>7}{'EV(SGO)':>9}{'EV(others)':>11}  books / flags")
    shown = 0
    for x in rows:
        if min(x["best_ev_pct"], x["ev_vs_other_books_pct"] if x["ev_vs_other_books_pct"] is not None else -999) / 100 < a.min_ev:
            break
        ln = f" {x['line']}" if x["line"] is not None else ""
        print(f"{x['player'][:21]:<22}{x['game']:<9}{(x['market'] + ln)[:10]:<11}{x['fair_pct']:>5.1f}%{x['best_book']:>11}{fmt(x['best_price']):>7}{x['best_ev_pct']:>+8.1f}%{x['ev_vs_other_books_pct']:>+10.1f}%  {x['books']} / {x['sharp']}{' / OUTLIER price' if x['outlier'] else ''}")
        shown += 1
        if shown >= 14:
            break
    out = Path(__file__).resolve().parent / "outputs" / f"{a.sport}_ev_latest.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"fetched_utc": now.isoformat(), "sport": a.sport, "rows": rows}, indent=1))
    both = [x for x in rows if min(x["best_ev_pct"], x["ev_vs_other_books_pct"] if x["ev_vs_other_books_pct"] is not None else -999) / 100 >= a.min_ev]
    print(f"\n{len(both)} props clear {int(a.min_ev * 100)}% EV on BOTH reads (SGO's fair price and the other books); all rows -> {out}")
    print("   EV(others) re-prices the best book against the median of the other books; a big gap between the two columns = one book is off the market,"
          "\n   which usually means a stale price, a tiny limit, or a book that will cut you off, not free money.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
