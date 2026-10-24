#!/usr/bin/env python3
"""
ev_process.py -- the "EV dingers" process, as a checklist a machine can run (written from the notes Donovan pasted 2026-10-24).

    python bots/ev_process.py check --price +450 --kalshi "+400/-426" --kalshi-depth 1000/1000 \\
        --kalshi-no-levels "-456@1800,-488@1900" --book "Circa=+365/-470" \\
        --exchange "Novig:yes=+400,no=-400@60,-413@800,-456@255" --exchange "ProphetX:yes=+400,no=-490@16"
    python bots/ev_process.py scan nba|nhl|nfl|mlb [--limit 8] [--min-ev 0.03]

THE PROCESS (his steps, and how each is coded)
  1  Kalshi two-way, $1,000+ resting on BOTH sides -> a price we can trust. De-vig it (PROBIT) and price our bet against it.
  2  Kalshi order book at deeper price points on the other side ($1,800 at -456, $1,900 at -488): our price must stay +EV
     against every one of those, not just the top of the book.
  3  A sharp book's two-way (Circa +365/-470): +EV against it too.
  4  Exchange order books (Novig): a thin top quote (-400 with $60) is not trusted; the next traded prices with real size are.
  5  A venue with a tiny, never-traded quote (ProphetX -490, $16) is ignored when the others agree; and it is under 1% of volume.
  Green flag = a TRUSTED source that is +EV (>= min EV). Red = a trusted source that is NOT. Thin quotes are ignored, not counted.
  Verdict: STRONG (3+ green, no red) - GOOD (2 green, no red) - WEAK (1 green) - PASS (any red, or none).

Kalshi order-book maths: yes ask = 1 - best NO bid; no ask = 1 - best YES bid; the k-th deeper no-ask is 1 - the k-th YES bid. Resting size
is measured in dollars at risk (price x contracts of the resting bid). Assumptions are printed with every result.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devig            # noqa: E402

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
TRUST = {"kalshi": 1000.0, "exchange": 250.0}        # dollars resting at the quote needed on EACH side


def am(s):
    s = str(s).strip().replace("−", "-")
    return float(s.lstrip("+")) if s.lstrip("+-").replace(".", "").isdigit() else float(s)


def fmt(a):
    a = int(round(a))
    return f"+{a}" if a > 0 else str(a)


def levels(spec):
    """'-456@1800,-488@1900' -> [(-456.0, 1800.0), ...]; size optional."""
    out = []
    for part in [p for p in (spec or "").split(",") if p.strip()]:
        price, _, size = part.partition("@")
        out.append((am(price), float(size) if size else None))
    return out


def judge(name, kind, fair_p, target, trusted, min_ev):
    e = devig.ev(fair_p, target)
    if not trusted:
        return {"source": name, "ev": e, "flag": "IGNORED (thin)", "trusted": False, "fair": fair_p}
    return {"source": name, "ev": e, "flag": "GREEN" if e >= min_ev else "RED" if e < 0 else "YELLOW", "trusted": True, "fair": fair_p}


def verdict(rows):
    green = sum(r["flag"] == "GREEN" for r in rows)
    red = sum(r["flag"] == "RED" for r in rows)
    if red:
        return "PASS", green, red
    return ("STRONG" if green >= 3 else "GOOD" if green == 2 else "WEAK" if green == 1 else "PASS"), green, red


def run_checks(price, kalshi, kalshi_depth, kalshi_no_levels, kalshi_yes_levels, books, exchanges, min_ev, method="probit"):
    """Everything in the process, from supplied quotes. Returns (rows, verdict tuple). Each row is one flag."""
    rows = []
    if kalshi:
        y, n = kalshi
        fy, _ = devig.fair(y, n, method)
        ok = kalshi_depth is not None and min(kalshi_depth) >= TRUST["kalshi"]
        rows.append({**judge("Kalshi top of book", "kalshi", fy, price, ok, min_ev), "quote": f"{fmt(y)}/{fmt(n)}", "depth": kalshi_depth})
        # step 2: deeper price points, each paired with the other side's top quote
        top_ok = ok                                  # deeper price points only count when the touch itself is a market
        for p_, sz in kalshi_no_levels:
            f2, _ = devig.fair(y, p_, method)
            rows.append({**judge(f"Kalshi no-side {fmt(p_)}", "kalshi", f2, price, top_ok and (sz is None or sz >= 500), min_ev), "quote": f"{fmt(y)}/{fmt(p_)}", "depth": sz})
        for p_, sz in kalshi_yes_levels:
            f2, _ = devig.fair(p_, n, method)
            rows.append({**judge(f"Kalshi yes-side {fmt(p_)}", "kalshi", f2, price, top_ok and (sz is None or sz >= 500), min_ev), "quote": f"{fmt(p_)}/{fmt(n)}", "depth": sz})
    for name, (y, n) in books.items():
        fy, _ = devig.fair(y, n, method)
        rows.append({**judge(name, "book", fy, price, True, min_ev), "quote": f"{fmt(y)}/{fmt(n)}", "depth": None})
    for name, (y, lv) in exchanges.items():
        for p_, sz in lv:
            fy, _ = devig.fair(y, p_, method)
            rows.append({**judge(f"{name} {fmt(p_)}", "exchange", fy, price, sz is not None and sz >= TRUST["exchange"], min_ev), "quote": f"{fmt(y)}/{fmt(p_)}", "depth": sz})
    return rows, verdict(rows)


def cmd_check(a):
    price = am(a.price)
    ky, kn = (am(x) for x in a.kalshi.split("/")) if a.kalshi else (None, None)
    kd = tuple(float(x) for x in a.kalshi_depth.split("/")) if a.kalshi_depth else None
    books = {}
    for b in a.book or []:
        n_, _, q = b.partition("=")
        books[n_] = tuple(am(x) for x in q.split("/"))
    exch = {}
    for e in a.exchange or []:
        n_, _, rest = e.partition(":")
        m = re.match(r"yes=([^,]+),no=(.+)", rest)
        exch[n_] = (am(m.group(1)), levels(m.group(2)))
    rows, (v, g, r) = run_checks(price, (ky, kn) if a.kalshi else None, kd, levels(a.kalshi_no_levels), levels(a.kalshi_yes_levels), books, exch, a.min_ev)
    print(f"Our price: {fmt(price)}  (breaks even at {100 * devig.implied(price):.1f}%)   method: probit   green = +EV of {100 * a.min_ev:.0f}% or more on a trusted source\n")
    print(f"{'source':<24}{'quote':>12}{'resting $':>12}{'fair':>8}{'EV':>8}  flag")
    for x in rows:
        d = "" if x["depth"] is None else (f"{x['depth']:.0f}" if not isinstance(x["depth"], tuple) else "/".join(f"{d:.0f}" for d in x["depth"]))
        print(f"{x['source']:<24}{x['quote']:>12}{d:>12}{100 * x['fair']:>7.1f}%{100 * x['ev']:>+7.1f}%  {x['flag']}")
    print(f"\nVERDICT: {v}   ({g} green, {r} red; thin quotes ignored)")
    return 0


# ------------------------------------------------------------------ scan (live): book prices (SGO) vs Kalshi, per the process
SERIES = {("nba", "Points"): "KXNBAPTS", ("nba", "Rebounds"): "KXNBAREB", ("nba", "Assists"): "KXNBAAST", ("nba", "PRA"): "KXNBAPRA",
          ("nhl", "Anytime goal"): "KXNHLGOAL", ("nhl", "Any point (G+A)"): "KXNHLPTS", ("nhl", "Any assist"): "KXNHLAST", ("nfl", "Anytime TD"): "KXNFLANYTD", ("mlb", "Home run"): "KXMLBHR"}


def kget(path):
    return json.loads(subprocess.run(["curl", "-s", "-m", "30", f"{KALSHI}{path}"], capture_output=True, text=True).stdout)


def kalshi_markets(series):
    out, cur = [], None
    while True:
        j = kget(f"/markets?series_ticker={series}&status=open&limit=1000" + (f"&cursor={cur}" if cur else ""))
        out += j.get("markets", [])
        cur = j.get("cursor")
        if not cur or not j.get("markets"):
            return out


def kalshi_book(ticker):
    ob = kget(f"/markets/{ticker}/orderbook").get("orderbook_fp", {})
    yes = sorted(((float(p), float(c)) for p, c in ob.get("yes_dollars", [])), reverse=True)     # bids, best first
    no = sorted(((float(p), float(c)) for p, c in ob.get("no_dollars", [])), reverse=True)
    return yes, no


def cmd_scan(a):
    import devig_sgo as S
    import injury_status as I
    key = S.load_key(a.env)
    import requests
    now = datetime.now(timezone.utc)
    r = requests.get(f"{S.SGO}/events", headers={"x-api-key": key}, params={"leagueID": S.LEAGUE[a.sport], "oddsAvailable": "true", "limit": a.limit,
                     "startsAfter": now.isoformat(), "startsBefore": (now + timedelta(hours=a.hours)).isoformat()}, timeout=40)
    r.raise_for_status()
    props = S.rows_for(a.sport, r.json().get("data", []))
    index = {}
    for (sp, label), series in SERIES.items():
        if sp != a.sport:
            continue
        for m in kalshi_markets(series):
            nm = re.match(r"(.+?): (\d+)\+", m["title"])
            if nm:
                index[(label, I.key(nm.group(1)), int(nm.group(2)) - 0.5)] = m          # "25+ points" = over 24.5
    cands, skipped = [], 0
    for p in props:
        line = float(p["line"]) if p["line"] is not None else 0.5
        m = index.get((p["market"], I.key(p["player"]), line))
        if not m:
            continue
        ya, na = f(m.get("yes_ask_dollars")), 1 - (f(m.get("yes_bid_dollars")) or 0)
        if not ya or not (0 < ya < 1) or not (0 < na < 1):
            continue
        yes_am, no_am = devig.to_american(ya), devig.to_american(na)
        fy, _ = devig.fair(yes_am, no_am)
        best = max(p["prices"].items(), key=lambda kv: devig.ev(fy, kv[1]))
        e_top = devig.ev(fy, best[1])
        if e_top > 0.40:                       # no real edge is this big: the two prices are for different bets (line, period, rules)
            skipped += 1
            continue
        cands.append((e_top, p, m, best, yes_am, no_am))
    cands.sort(key=lambda c: c[0], reverse=True)
    print(f"{a.sport.upper()}: {len(props)} SGO props, {len(cands)} matched to a priced Kalshi market (+{skipped} dropped as not comparable: an 'EV' over 40% means the bets differ); "
          f"checking the order book for the top {min(a.depth_checks, len(cands))}\n")
    results = []
    for e0, p, m, best, yes_am, no_am in cands[: a.depth_checks]:
        yes_b, no_b = kalshi_book(m["ticker"])
        if not yes_b or not no_b:
            continue
        dollars = (no_b[0][0] * no_b[0][1], yes_b[0][0] * yes_b[0][1])         # resting $ behind the yes ask / behind the no ask
        near_y = [(pb, c) for pb, c in yes_b[1:] if yes_b[0][0] - pb <= 0.03][:2]      # price points within 3 cents of the touch
        near_n = [(pb, c) for pb, c in no_b[1:] if no_b[0][0] - pb <= 0.03][:2]
        no_lv = [(devig.to_american(1 - pb), pb * c) for pb, c in near_y]              # deeper no-ask price points
        yes_lv = [(devig.to_american(1 - pb), pb * c) for pb, c in near_n]
        rows, (v, g, r_) = run_checks(best[1], (yes_am, no_am), dollars, no_lv, yes_lv, {}, {}, a.min_ev)
        results.append({"player": p["player"], "game": p["game"], "market": p["market"], "line": p["line"], "book": best[0], "price": best[1], "verdict": v,
                        "green": g, "red": r_, "kalshi_yes_no": f"{fmt(yes_am)}/{fmt(no_am)}", "resting_usd": [round(d) for d in dollars],
                        "top_ev_pct": round(100 * e0, 1), "flags": [{k: (round(x[k], 4) if isinstance(x[k], float) else x[k]) for k in ("source", "quote", "ev", "flag")} for x in rows]})
    order = {"STRONG": 0, "GOOD": 1, "WEAK": 2, "PASS": 3}
    results.sort(key=lambda x: (order[x["verdict"]], -x["top_ev_pct"]))
    print(f"{'player':<22}{'market':<14}{'book':>11}{'price':>7}{'Kalshi':>13}{'$ behind':>12}{'EV top':>8}  verdict")
    for x in results[:16]:
        ln = f"{x['market']} {x['line']}" if x["line"] is not None else x["market"]
        print(f"{x['player'][:21]:<22}{ln[:13]:<14}{x['book']:>11}{fmt(x['price']):>7}{x['kalshi_yes_no']:>13}{('/'.join(str(d) for d in x['resting_usd'])):>12}{x['top_ev_pct']:>+7.1f}%  {x['verdict']} ({x['green']}g/{x['red']}r)")
    out = Path(__file__).resolve().parent / "outputs" / f"{a.sport}_ev_process_latest.json"
    out.write_text(json.dumps({"fetched_utc": now.isoformat(), "rows": results}, indent=1))
    print(f"\n-> {out}   (sportsbook-side: SGO book prices; Kalshi side: public order book. Circa / Novig / ProphetX quotes are not in these feeds: "
          f"add them with `check`)")
    return 0


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--price", required=True)
    c.add_argument("--kalshi")
    c.add_argument("--kalshi-depth")
    c.add_argument("--kalshi-no-levels", default="")
    c.add_argument("--kalshi-yes-levels", default="")
    c.add_argument("--book", action="append")
    c.add_argument("--exchange", action="append")
    c.add_argument("--min-ev", type=float, default=0.03)
    s = sub.add_parser("scan")
    s.add_argument("sport", choices=["nba", "nhl", "nfl", "mlb"])
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--hours", type=int, default=36)
    s.add_argument("--min-ev", type=float, default=0.03)
    s.add_argument("--depth-checks", type=int, default=25)
    s.add_argument("--env", default=str(Path.home() / "Desktop/moonshot-push/.env.local"))
    a = ap.parse_args()
    return cmd_check(a) if a.cmd == "check" else cmd_scan(a)


if __name__ == "__main__":
    sys.exit(main())
