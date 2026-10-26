#!/usr/bin/env python3
"""
live_edge.py -- catch LIVE moneylines, spreads and totals that are priced off our fair number, log every one, and grade them so we know whether it is profitable.

    python bots/live_edge.py loop   OUT_DIR [--minutes 330] [--every 45] [--min-edge 0.04]     poll games in progress + Kalshi's live game markets, log edges
    python bots/live_edge.py once   [--min-edge 0.04]                                           one pass, print what it sees (no file written)
    python bots/live_edge.py report OUT_DIR                                                    settle every logged alert against the final score; ROI by edge bucket

How: for each game in progress (ESPN scoreboard: score, period, clock) we take Kalshi's live markets for it (moneyline "KXNBAGAME", spread "KXNBASPREAD" = "<team> wins by over N points",
total "KXNBATOTAL"; same for NFL and NHL). Our FAIR probability for each comes from the live state:
  NBA / NFL   live_spread.py's table (lead, seconds left, pregame margin from the book's pregame spread) -> Normal(expected final margin, sd)
  NHL         Poisson goals over the time left at the pregame scoring rate, a tied game after regulation = coin flip
An alert = we can BUY yes at the ask (fair - ask - fee) or BUY no at 1 - bid (bid - fair - fee) with an edge of at least `min-edge`, after Kalshi's taker fee 0.07*p*(1-p). Alerts are
logged once per (market, side) until the edge changes by 2 points. `report` replays them against the final score: how often our fair number was right and the return of simply taking every alert.
Nothing here places a bet. It is a paper-trading log until the report says the alerts pay.
"""
import argparse, json, math, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_spread as LS            # noqa: E402

HERE = Path(__file__).resolve().parent
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SPORTS = {"nba": {"espn": "basketball/nba", "game": "KXNBAGAME", "spread": "KXNBASPREAD", "total": "KXNBATOTAL", "period": 720, "n": 4},
          "nfl": {"espn": "football/nfl", "game": "KXNFLGAME", "spread": "KXNFLSPREAD", "total": "KXNFLTOTAL", "period": 900, "n": 4},
          "nhl": {"espn": "hockey/nhl", "game": "KXNHLGAME", "spread": "KXNHLSPREAD", "total": "KXNHLTOTAL", "period": 1200, "n": 3},
          "mlb": {"espn": "baseball/mlb", "game": "KXMLBGAME", "spread": "KXMLBSPREAD", "total": "KXMLBTOTAL", "period": 6, "n": 9}}
Phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
FEE = lambda p: 0.07 * p * (1 - p)


def curl(url):
    raw = subprocess.run(["curl", "-s", "-m", "30", url], capture_output=True, text=True).stdout
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def mlb_outs_left(text, inning):
    """Outs left in nine innings, at HALF-INNING resolution from ESPN's text ("Top 7th", "Bot 3rd", "Mid 5th", "End 5th"): the live-spread tables for baseball are built on finished half-innings."""
    t = (text or "").lower()
    n = int(inning or 1)
    if t.startswith("top"):
        halves = (n - 1) * 2
    elif t.startswith("bot"):
        halves = (n - 1) * 2 + 1
    elif t.startswith("mid"):
        halves = n * 2 - 1
    elif t.startswith("end"):
        halves = n * 2
    else:
        halves = (n - 1) * 2
    return float(max(3, 54 - 3 * halves))


def live_games(sport):
    cfg = SPORTS[sport]
    out = []
    for e in curl(f"https://site.api.espn.com/apis/site/v2/sports/{cfg['espn']}/scoreboard").get("events", []):
        t = e["status"]["type"]
        if t["state"] not in ("in", "pre"):
            continue
        c = {x["homeAway"]: x for x in e["competitions"][0]["competitors"]}
        per, clock = e["status"]["period"], e["status"].get("clock", 0)
        if sport == "mlb":
            left = mlb_outs_left(t.get("shortDetail") or t.get("detail") or "", per)
        else:
            left = (cfg["n"] - per) * cfg["period"] + clock if per <= cfg["n"] else clock
        odds = (e["competitions"][0].get("odds") or [{}])[0]
        out.append({"sport": sport, "id": e["id"], "home": c["home"]["team"]["abbreviation"], "away": c["away"]["team"]["abbreviation"], "home_name": c["home"]["team"]["displayName"], "away_name": c["away"]["team"]["displayName"],
                    "hs": int(c["home"]["score"]), "as": int(c["away"]["score"]), "left": float(left), "period": per, "spread_detail": odds.get("details"), "total": odds.get("overUnder"), "date": e["date"][:10], "state": t["state"]})
    return out


STATE_MISMATCH = []   # (game id, our home win probability, the market's) for games skipped by the state check
PRE_T = {}        # espn game id -> pregame total
DEFAULT_TOTAL = {"nba": 228.0, "nfl": 45.0, "nhl": 6.0, "mlb": 8.6}
PRE = {}          # espn game id -> pregame HOME margin, filled while games are still pregame and persisted by the loop (ESPN drops the line once a game is live)


def parse_pre(g):
    """pregame HOME margin in the sport's units: points for NBA / NFL, (book win probability - 0.5) for NHL, from ESPN's line string ("DAL -198" for hockey)."""
    import re
    m = re.match(r"\s*([A-Z]{2,4})\s+([-+]?\d+(?:\.\d+)?)", str(g.get("spread_detail") or ""))
    if not m:
        return None
    fav, v = m.group(1), float(m.group(2))
    if g["sport"] in ("nhl", "mlb"):
        if g["sport"] == "mlb" and abs(v) < 100:                                    # a run line, not a moneyline: no win probability to read
            return None
        p = (-v / (-v + 100.0) if v < 0 else 100.0 / (v + 100.0)) - 0.02                # the favourite's implied probability with a typical vig taken out
        ph = p if fav == g["home"] else 1 - p if fav == g["away"] else 0.5
        return ph - 0.5
    v = abs(v)
    return v if fav == g["home"] else -v if fav == g["away"] else 0.0


def pre_margin(g):
    """pregame HOME margin: the book's line seen while the game was pregame (cached), else our own team model's line, else 0."""
    if g["id"] in PRE:
        return PRE[g["id"]]
    v = parse_pre(g)
    if v is not None:
        PRE[g["id"]] = v; return v
    try:
        import team_live
        for t in team_live.board(g["sport"], g["date"] if g["sport"] != "nba" else g["date"]):
            if t["home"] == g["home"] and t["away"] == g["away"]:
                PRE[g["id"]] = (t["model_p_home"] - 0.5) if g["sport"] in ("nhl", "mlb") else -t["model_spread_home"]; return PRE[g["id"]]
    except Exception:
        pass
    return 0.0


def _unused_pre_margin(g):
    import re
    m = re.match(r"\s*([A-Z]{2,4})\s+([-+]?\d+(?:\.\d+)?)", str(g.get("spread_detail") or ""))
    if not m:
        return 0.0
    fav, v = m.group(1), abs(float(m.group(2)))
    return v if fav == g["home"] else -v if fav == g["away"] else 0.0


def fair_margin_dist(g, side):
    """(mean, sd) of this side's final margin."""
    lead = (g["hs"] - g["as"]) * (1 if side == "home" else -1)
    pre = pre_margin(g) * (1 if side == "home" else -1)
    return None   # (the probabilities now come from live_spread.p_margin_over, which knows the hockey tables)


def nhl_probs(g, side, need):
    """P(this side's final goal margin > need) by Poisson over the remaining time; ties after regulation go 50/50 only for need == 0."""
    lead = (g["hs"] - g["as"]) * (1 if side == "home" else -1)
    pm = pre_margin(g) * (1 if side == "home" else -1)
    tot = float(g.get("total") or 6.0)
    ph = max(0.2, min(0.8, 0.5 + 0.09 * pm))                                       # pregame share of the scoring (the line is a goal-difference hint)
    mine, theirs = tot * (ph if True else 0.5) * g["left"] / 3600.0, tot * (1 - ph) * g["left"] / 3600.0
    def pk(l, n=14): return [math.exp(-l) * l ** k / math.factorial(k) for k in range(n)]
    a, b = pk(mine), pk(theirs)
    p = 0.0
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            d = lead + i - j
            if d > need:
                p += x * y
            elif d == need and need == 0:
                p += 0.5 * x * y
    return p


KCODE = {"GS": "GSW", "SA": "SAS", "NO": "NOP", "NY": "NYK", "WSH": "WAS", "NJ": "NJD", "LA": "LAK", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA"}      # ESPN abbreviation -> Kalshi's three letters


def kalshi_for(g, cfg):
    from datetime import timedelta
    d0 = datetime.fromisoformat(g["date"])
    days = {d0.strftime("%y%b%d").upper(), (d0 - timedelta(days=1)).strftime("%y%b%d").upper()}      # ESPN dates in UTC, Kalshi's in ET
    tail = KCODE.get(g["away"], g["away"]) + KCODE.get(g["home"], g["home"])
    out = []
    for kind in ("game", "spread", "total"):
        for m in curl(f"{KALSHI}/markets?series_ticker={cfg[kind]}&status=open&limit=1000").get("markets", []):
            ev = m.get("event_ticker", "")
            if any(d in ev for d in days) and ev.endswith(tail):
                out.append((kind, m))
    return out


_MODELS = {}


def models(sport):
    if sport not in _MODELS:
        try:
            _MODELS[sport] = LS.load_models(sport)
        except FileNotFoundError:
            _MODELS[sport] = None
    return _MODELS[sport]


def margin_over(g, side, x):
    """P(this side's final margin > x) from the live-spread tables (hockey: observed distribution); the Poisson model is only the fallback when no table exists."""
    m = models(g["sport"])
    if not m:
        return nhl_probs(g, side, x) if g["sport"] == "nhl" else None
    if g["sport"] == "mlb":
        hl, hp = g["hs"] - g["as"], pre_margin(g)
        ph = lambda y: LS.p_margin_over(m, g["left"], hl, hp, y)
        return ph(x) if side == "home" else 1 - ph(-x)                             # P(away margin > x) = P(home margin < -x); baseball has no tie, lines are half-runs
    lead = (g["hs"] - g["as"]) * (1 if side == "home" else -1)
    pre = pre_margin(g) * (1 if side == "home" else -1)
    return LS.p_margin_over(m, g["left"], lead, pre, x)


def team_side(g, text):
    t = (text or "").lower().strip()
    if not t:
        return None
    for side, nm in (("home", g["home_name"]), ("away", g["away_name"])):          # full text first: "Los Angeles D" is the Dodgers, not the other Los Angeles team
        if nm.lower().startswith(t):
            return side
    for side, nm in (("home", g["home_name"]), ("away", g["away_name"])):
        if nm.split()[-1].lower() in t or nm.lower().startswith(t[:8]):
            return side
    return None


def evaluate(g, kind, m, min_edge):
    sport = g["sport"]
    b, a = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
    if a <= 0 or a >= 1 or b < 0:
        return None
    sub = m.get("yes_sub_title") or ""
    if kind == "game":
        side = team_side(g, sub)
        if not side:
            return None
        fair = margin_over(g, side, 0)
        desc = f"{sub} to win"
    elif kind == "spread":
        side = team_side(g, sub)
        x = m.get("floor_strike")
        if not side or x is None:
            return None
        fair = margin_over(g, side, x)
        desc = f"{sub}"
    else:
        x = m.get("floor_strike")
        if x is None:
            return None
        mm = models(sport)
        if not mm or not mm["ttab"]:
            return None
        if g["id"] not in PRE_T:
            return None                                                            # no pregame total was cached (the loop has to see the game before it starts): no total alerts, never a guess
        L = PRE_T[g["id"]]
        fair = LS.p_total_over(mm, g["left"], L, g["hs"] + g["as"], g["hs"] - g["as"], x)      # YES = over
        if sport in ("nhl", "mlb") and not (0.2 <= fair <= 0.8):                              # hockey totals are only calibrated between about 20% and 80% (see the fit check); outside that no alert
            return None
        desc = sub
    if sport == "nhl" and kind != "total":
        pass
    import staking as ST
    buy_yes = fair - a - FEE(a)
    buy_no = (1 - fair) - (1 - b) - FEE(1 - b)
    best = ("YES", buy_yes, a) if buy_yes >= buy_no else ("NO", buy_no, 1 - b)
    if best[1] < min_edge or best[1] > 0.12:                                       # an "edge" over 12 points is a state / model error, not a bet: the first night's 12+ point alerts priced 60% and won 45%, the 4-12 point ones priced 59% and won 60%
        return None
    pfair = fair if best[0] == "YES" else 1 - fair
    # paper stake: half-trusted model (no live track record yet), quarter Kelly, 1% cap, on the price after the fee; the report shows the paper bankroll it would have grown
    amer = (100 * (1 - best[2]) / best[2]) if best[2] < 0.5 else (-100 * best[2] / (1 - best[2]))
    stake, _, _ = ST.stake_fraction(pfair, int(round(amer)) if best[2] < 0.5 else int(round(amer)), n=500, fee=FEE(best[2]))
    return {"game": f"{g['away']}@{g['home']}", "sport": sport, "kind": kind, "market": m["ticker"], "desc": desc, "side": best[0], "price": round(best[2], 3), "fair": round(pfair, 4),
            "edge": round(best[1], 4), "stake": round(stake, 5), "state": {"hs": g["hs"], "as": g["as"], "left": g["left"], "period": g["period"], "pre_home_margin": pre_margin(g)}, "espn_id": g["id"], "date": g["date"]}


def once(min_edge, sports=("nba", "nfl", "nhl", "mlb")):
    found = []
    for sp in sports:
        for g in live_games(sp):
            if g.get("total"):
                PRE_T.setdefault(g["id"], float(g["total"]))
            if g["state"] == "pre":
                if parse_pre(g) is not None:
                    PRE[g["id"]] = parse_pre(g)
                continue
            ms = kalshi_for(g, SPORTS[sp])
            # STATE CHECK. ESPN's scoreboard can run minutes behind Kalshi's live quotes; a stale score makes every market look mispriced at once. If our win probability and the market's moneyline
            # disagree by more than 15 points the game's state is not trusted this pass: no alerts for it, and the mismatch is counted.
            ml = next((m for k, m in ms if k == "game" and team_side(g, m.get("yes_sub_title") or "") == "home"), None)
            fh = margin_over(g, "home", 0)
            if ml and fh is not None:
                mid = (float(ml.get("yes_bid_dollars") or 0) + float(ml.get("yes_ask_dollars") or 0)) / 2
                if abs(fh - mid) > 0.10:
                    STATE_MISMATCH.append((g["id"], round(fh, 3), round(mid, 3)))
                    continue
            for kind, m in ms:
                r = evaluate(g, kind, m, min_edge)
                if r:
                    found.append(r)
    return found


def loop(out_dir, minutes, every, min_edge):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    seen, end, n = {}, time.time() + 60 * minutes, 0
    pf = Path(out_dir) / "_pre_margins.json"
    if pf.exists():
        saved = json.load(open(pf))
        PRE.update(saved.get("margin", {})); PRE_T.update(saved.get("total", {}))
    while time.time() < end:
        now = datetime.now(timezone.utc)
        pf.write_text(json.dumps({"margin": PRE, "total": PRE_T}))
        for r in once(min_edge):
            k = (r["market"], r["side"])
            if k in seen and abs(seen[k] - r["edge"]) < 0.02:
                continue
            seen[k] = r["edge"]; r["t"] = now.strftime("%Y-%m-%dT%H:%M:%SZ"); n += 1
            with open(Path(out_dir) / f"alerts_{now.date().isoformat()}.jsonl", "a") as f:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        time.sleep(every)
    print(f"{n} alerts logged")


_SUMMARY = {}


def settle(alert):
    """did YES win? from the final score. None if the game is not final yet. (One summary fetch per game, not per alert: a night logs thousands of alerts.)"""
    cfg = SPORTS[alert["sport"]]
    k = (alert["sport"], alert["espn_id"])
    if k not in _SUMMARY:
        _SUMMARY[k] = curl(f"https://site.api.espn.com/apis/site/v2/sports/{cfg['espn']}/summary?event={alert['espn_id']}")
    d = _SUMMARY[k]
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    if not comp.get("status", {}).get("type", {}).get("completed"):
        return None
    sc = {c["homeAway"]: int(float(c.get("score") or 0)) for c in comp.get("competitors", [])}
    names = {c["homeAway"]: c["team"]["displayName"] for c in comp.get("competitors", [])}
    hm = sc["home"] - sc["away"]
    sub = alert["desc"]
    if alert["kind"] == "total":
        import re as _re
        mm = _re.search(r"([\d.]+)", sub)
        return (sc["home"] + sc["away"]) > float(mm.group(1)) if mm else None
    team_text = sub.split(" wins by")[0].split(" to win")[0].strip()                # "Minnesota wins by over 2.5 points" / "Minnesota to win" -> "Minnesota" (Kalshi uses the city, ESPN's name is "Minnesota Vikings")
    side = team_side({"home_name": names["home"], "away_name": names["away"]}, team_text)
    if side is None:
        return None
    margin = hm if side == "home" else -hm
    if alert["kind"] == "game":
        return margin > 0
    x = float(alert["market"].rsplit("-", 1)[-1]) - 0.5 if False else None
    import re
    m = re.search(r"over ([\d.]+)", sub)
    return margin > float(m.group(1)) if m else None


def report(out_dir):
    rows = []
    for f in sorted(Path(out_dir).glob("alerts_*.jsonl")):
        for ln in open(f):
            r = json.loads(ln)
            cache = Path(out_dir) / "_settled.json"
            st = json.load(open(cache)) if cache.exists() else {}
            key = f"{r['market']}|{r['side']}|{r['t']}"
            if key not in st:
                y = settle(r)
                if y is None:
                    continue
                st[key] = y; cache.write_text(json.dumps(st))
            yes_won = st[key]
            won = yes_won if r["side"] == "YES" else not yes_won
            fee = FEE(r["price"])
            rows.append({**r, "won": won, "profit": (1 - r["price"] - fee) if won else (-r["price"] - fee)})
    if not rows:
        print("no settled alerts yet"); return
    print(f"{len(rows)} settled alerts")
    for lo, hi in ((0.04, 0.07), (0.07, 0.12), (0.12, 1.0)):
        rs = [r for r in rows if lo <= r["edge"] < hi]
        if rs:
            print(f"  edge {100 * lo:.0f}-{100 * min(hi, 1) :.0f} pts: {len(rs)} alerts, our fair price averaged {100 * sum(r['fair'] for r in rs) / len(rs):.1f}%, won {100 * sum(r['won'] for r in rs) / len(rs):.1f}%, "
                  f"return {100 * sum(r['profit'] for r in rs) / sum(r['price'] for r in rs):+.1f}% on the money risked")
    for k in ("game", "spread", "total"):
        rs = [r for r in rows if r["kind"] == k]
        if rs:
            print(f"  {k}: {len(rs)} alerts, return {100 * sum(r['profit'] for r in rs) / sum(r['price'] for r in rs):+.1f}%")
    games = {r["game"] + r["date"] for r in rows}
    print(f"  these {len(rows)} alerts come from only {len(games)} games, and one game can log hundreds of them (every rung of a ladder, again each time the edge moves): the effective sample is the games, not the alerts")
    print("  (no paper bankroll is printed: a game's ladder of alerts all win or lose together, so stakes on them cannot be added up as if they were separate bets)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["loop", "once", "report"]); ap.add_argument("out", nargs="?"); ap.add_argument("--minutes", type=float, default=330); ap.add_argument("--every", type=float, default=45)
    ap.add_argument("--min-edge", type=float, default=0.04); a = ap.parse_args()
    if a.cmd == "once":
        r = once(a.min_edge)
        print(f"{len(r)} edges >= {100 * a.min_edge:.0f} pts after fees across live games" + (f"; {len(STATE_MISMATCH)} game(s) skipped by the state check (our win probability vs the market's moneyline more than 10 points apart): {STATE_MISMATCH}" if STATE_MISMATCH else ""))
        for x in sorted(r, key=lambda x: -x["edge"])[:12]:
            s = x["state"]
            print(f"  {x['game']:<9}{x['kind']:<7}{x['desc'][:38]:<40}BUY {x['side']:<3}@{100 * x['price']:.0f}c fair {100 * x['fair']:.0f}% edge {100 * x['edge']:+.1f}  (score {s['as']}-{s['hs']}, {int(s['left'] // 60)}:{int(s['left'] % 60):02d} left)")
    elif a.cmd == "loop":
        loop(a.out, a.minutes, a.every, a.min_edge)
    else:
        report(a.out)
