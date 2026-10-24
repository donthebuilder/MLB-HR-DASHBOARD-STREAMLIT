#!/usr/bin/env python3
"""
devig_board.py -- our probability against the price, for the markets where a clean price exists.

    python bots/devig_board.py nhl [--min-edge 0.03] [--board FILE]

HOW A PRICE BECOMES A "FAIR" CHANCE (plain English)
  Sportsbooks: the price carries the book's cut. Add up every outcome's implied chance and you get more than 100%; the
  extra is the cut. Dividing each chance by that total (two-way: over/under; many-way: the whole field) gives the book's
  fair chance. SGO publishes such a de-vigged `fairOdds` per prop; sharp books (Pinnacle, Circa) are the best single
  reference because their cut is smallest. A bet is +EV when the chance is higher than fair by more than the remaining cut.
  Kalshi (exchange): a Yes contract pays $1 and costs its price, so the price IS the chance -- nothing to de-vig -- but a
  fee comes off the winnings, and thin books mislead, so depth ($500+ resting on BOTH sides) marks a price worth trusting.

THE CORRECTION THIS FILE EXISTS FOR (2026-10-24): scripts/nhl/lamp-atg-devig.py was labelled "anytime goal scorer" but reads SGO's
`firstToScore` market -- the FIRST goal of the game (a star prices around +800 to +1,200; his anytime price is near +150). SGO does
carry the anytime-goal market, under the stat name `points` (marketName "Any Goals Yes/No"; `goals+assists` is points) -- see
devig_sgo.py / ev_process.py. This file reads Kalshi's anytime-goal series (KXNHLGOAL), against OUR goal probability
(nhl_live.py's nhl_goal_v2, calibrated on 42,815 holdout games).

  edge = our chance - the price you pay - the fee      positive = we think the market is too low on him
Everything here is a comparison, not a recommendation: one night of a few dozen prices cannot say who is right.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import injury_status   # noqa: E402

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
DEPTH_MIN = 500.0
FEE_RATE = 0.07            # Kalshi's general taker fee: 7% x price x (1 - price), per contract, rounded up to the cent


def _get(url):
    return json.loads(subprocess.run(["curl", "-s", "-m", "30", url], capture_output=True, text=True).stdout)


def fee(price):
    import math
    return math.ceil(FEE_RATE * price * (1 - price) * 100) / 100


def markets(series):
    out, cursor = [], None
    while True:
        j = _get(f"{KALSHI}/markets?series_ticker={series}&status=open&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        out += j.get("markets", [])
        cursor = j.get("cursor")
        if not cursor or not j.get("markets"):
            return out


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def depth(ticker):
    j = _get(f"{KALSHI}/markets/{ticker}/orderbook").get("orderbook_fp", {})
    yes = sum(float(e[1]) for e in j.get("yes_dollars", []) if len(e) >= 2)
    no = sum(float(e[1]) for e in j.get("no_dollars", []) if len(e) >= 2)
    return yes, no


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sport", choices=["nhl"])
    ap.add_argument("--min-edge", type=float, default=0.03)
    ap.add_argument("--board", default=str(Path(__file__).resolve().parent / "outputs" / "nhl_board_latest.json"))
    a = ap.parse_args()
    board = json.load(open(a.board))
    ours = {injury_status.key(p["name"]): p for p in board["players"]}
    ms = markets("KXNHLGOAL")
    rows = []
    for m in ms:
        t = m["title"]                               # "Curtis Douglas: 1+ goals"
        mm = re.match(r"(.+?): (\d)\+ goals", t)
        if not mm or mm.group(2) != "1":
            continue
        p = ours.get(injury_status.key(mm.group(1)))
        if not p:
            continue
        start = datetime.fromisoformat(p["start_utc"].replace("Z", "+00:00"))
        if start <= datetime.now(timezone.utc):
            continue                                  # started or finished: the Kalshi price now reflects the game, not the pregame view
        bid, ask, last = f(m.get("yes_bid_dollars")), f(m.get("yes_ask_dollars")), f(m.get("last_price_dollars"))
        if ask is None or ask <= 0 or ask >= 1:
            continue
        mid = (bid + ask) / 2 if bid else last or ask
        rows.append({"player": p["name"], "team": p["team"], "ticker": m["ticker"], "kalshi_bid": bid, "kalshi_ask": ask, "kalshi_mid": round(mid, 3),
                     "our_p": p["p_goal"], "p_play": p["p_play"], "tier": p["tier"], "fee": fee(ask),
                     "edge": round(p["p_goal"] * p["p_play"] - ask - fee(ask), 4), "our_gap_to_mid": round(p["p_goal"] * p["p_play"] - mid, 4)})
    rows.sort(key=lambda r: r["edge"], reverse=True)
    for r in [r for r in rows if r["edge"] >= a.min_edge][:15] + rows[-3:]:
        y, n = depth(r["ticker"])
        r["depth_yes"], r["depth_no"], r["trusted"] = round(y), round(n), (y >= DEPTH_MIN and n >= DEPTH_MIN)
    trusted_rows = [r for r in rows if r.get("trusted")]
    out = Path(__file__).resolve().parent / "outputs" / "nhl_vs_kalshi_latest.json"
    out.write_text(json.dumps({"date": board["date"], "markets": len(ms), "joined": len(rows), "rows": rows}, indent=1))
    print(f"Kalshi KXNHLGOAL: {len(ms)} open markets, {len(rows)} not-yet-started games priced and on our board, {len(trusted_rows)} with $500+ on both sides  -> {out}\n")
    print("   caution: this season has 5-9 games a player, and our model has not seen probabilities above ~45% reliably; a big gap on a\n"
          "   thin book (a few dollars resting on YES) is a stale quote, not an edge. TRUSTED = $500+ both sides.\n")
    print(f"{'player':<22}{'team':<5}{'ours':>6}{'plays':>6}{'Kalshi bid/ask':>16}{'fee':>6}{'edge':>8}  depth")
    for r in rows[:12]:
        d = f"yes ${r.get('depth_yes', '?')} / no ${r.get('depth_no', '?')} {'TRUSTED' if r.get('trusted') else ''}"
        print(f"{r['player'][:21]:<22}{r['team']:<5}{100 * r['our_p']:>5.0f}%{100 * r['p_play']:>5.0f}%{(r['kalshi_bid'] or 0):>8.2f} /{r['kalshi_ask']:<6.2f}{r['fee']:>6.2f}{100 * r['edge']:>+7.1f}%  {d}")
    print("\n   bottom of the list (we think the market is too HIGH on them):")
    for r in rows[-3:]:
        print(f"   {r['player'][:21]:<22}{r['team']:<5}ours {100 * r['our_p']:.0f}%  ask {r['kalshi_ask']:.2f}  edge {100 * r['edge']:+.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
