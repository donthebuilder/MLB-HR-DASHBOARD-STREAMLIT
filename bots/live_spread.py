#!/usr/bin/env python3
"""
live_spread.py -- what a LIVE spread is worth. "Down 15 in the third quarter, they offer +12.5 at +120: is that good?"

    python bots/live_spread.py fit   nba|nfl [--cache DIR]                       build the table from ESPN play-by-play, check it on games it never saw, save models/live_spread_<sport>.json
    python bots/live_spread.py ask   nba|nfl --diff -15 --left 1080 [--pre -3] [--offer 12.5] [--price 120]
    python bots/live_spread.py table nba|nfl

State = (this team's lead or deficit, seconds left, the pregame spread from this team's side). From every NBA game of 2025-26 and NFL game of 2023-25 we sample the state every 2 minutes (NBA) / 3 minutes
(NFL) and record the final margin. For each time on the grid: final margin - current margin ~ Normal(a + b*lead + c*pregame margin, sd). Fitted on the first 70% of games, checked on the last 30%
(calibration of the cover probabilities and of the win probability), then refit on everything for `ask`. `ask` gives the expected final margin, the FAIR live spread (the number at which it is a
coin flip), the chance of covering the offer, the break-even price, and the edge against the price you are offered. Spread sign: negative = this team is favoured by that many (a team down 15 is
normally offered a positive number like +12.5, meaning it covers by losing by 12 or less, or winning).
"""
import argparse, json, math, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
HERE = Path(__file__).resolve().parent
CFG = {"nba": {"espn": "basketball/nba", "grid": list(range(2640, 100, -120)), "period": 720, "n": 4}, "nfl": {"espn": "football/nfl", "grid": list(range(3600, 100, -180)), "period": 900, "n": 4}}
Phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))


def curl(url):
    for _ in range(3):
        raw = subprocess.run(["curl", "-s", "-m", "60", url], capture_output=True, text=True).stdout
        if raw.startswith("{"):
            return json.loads(raw)
    return {}


def secs(clock):
    try:
        m, s = str(clock).split(":"); return int(m) * 60 + float(s)
    except ValueError:
        return None


def game_states(sport, eid, cache):
    f = Path(cache) / f"lsp_{sport}_{eid}.json"
    if f.exists():
        return json.load(open(f))
    cfg = CFG[sport]
    d = curl(f"https://site.api.espn.com/apis/site/v2/sports/{cfg['espn']}/summary?event={eid}")
    pts = []
    plays = d.get("plays") or [pl for dr in ((d.get("drives") or {}).get("previous") or []) + ((d.get("drives") or {}).get("current") and [d["drives"]["current"]] or []) for pl in dr.get("plays", [])]
    for p in plays:
        per, cl = (p.get("period") or {}).get("number"), secs((p.get("clock") or {}).get("displayValue"))
        if per is None or cl is None or p.get("homeScore") is None:
            continue
        left = (cfg["n"] - per) * cfg["period"] + cl if per <= cfg["n"] else cl
        pts.append((left, p["homeScore"] - p["awayScore"]))
    out = {"id": eid, "pts": pts}
    json.dump(out, open(f, "w"))
    return out


def rows_for(sport, cache):
    """[(game order, home_final_margin, pregame_home_margin, {grid_time: home_lead})]"""
    cfg = CFG[sport]; out = []
    if sport == "nba":
        import buckets_backtest as bb, game_lines as G
        ld = Path(cache).parent / "nba_lines"
        gs = sorted([g for s in (2025, 2026) for g in bb.load(s, cache)], key=lambda g: (g["date"], g["id"]))
        meta = [(g["id"], g["date"], (G.close_line("nba", g["id"], ld) or {}).get("spread_home")) for g in gs]
    else:
        import nflreadpy as nfl
        meta = [(str(r["espn"]), str(r["gameday"]), -float(r["spread_line"]) if r["spread_line"] is not None else None)
                for r in nfl.load_schedules([2023, 2024, 2025]).iter_rows(named=True) if r["game_type"] == "REG" and r.get("espn") and r["home_score"] is not None]
        meta.sort(key=lambda m: m[1])
    with ThreadPoolExecutor(10) as ex:
        sts = list(ex.map(lambda m: game_states(sport, m[0], cache), meta))
    for i, ((eid, date, sp), st) in enumerate(zip(meta, sts)):
        pts = st["pts"]
        if len(pts) < 50 or sp is None:
            continue
        final = pts[-1][1]
        lead = {}
        for T in cfg["grid"]:
            before = [d for t, d in pts if t >= T]
            lead[T] = before[-1] if before else 0
        out.append((i, final, -sp, lead))
    return out


def fit_table(rows, sport):
    cfg = CFG[sport]; tab = {}
    for T in cfg["grid"]:
        X = np.array([[1.0, r[3][T], r[2]] for r in rows]); y = np.array([r[1] - r[3][T] for r in rows], float)
        w = np.linalg.solve(X.T @ X + 1e-6 * np.eye(3), X.T @ y)
        res = y - X @ w; s0 = float(np.std(res)); Ls = np.abs(X[:, 1]) / 10.0
        best = (1e18, 1.0, 0.0)                                   # the spread is not the same in a blowout and in a close game: sd = s0 * (alpha + beta*|lead|/10), by maximum likelihood
        for al in np.arange(0.5, 1.5, 0.05):
            for be in np.arange(-0.4, 0.8, 0.05):
                sd_i = s0 * np.maximum(0.3, al + be * Ls)
                nll = float(np.sum(np.log(sd_i) + res ** 2 / (2 * sd_i ** 2)))
                if nll < best[0]:
                    best = (nll, float(al), float(be))
        tab[T] = [float(w[0]), float(w[1]), float(w[2]), s0, best[1], best[2]]
    return tab


def predict(tab, T, lead, pre_margin):
    Ts = sorted(tab)
    Tn = min(Ts, key=lambda t: abs(t - T)) if min(Ts) <= T <= max(Ts) else (min(Ts) if T < min(Ts) else max(Ts))
    a, b, c, sd, al, be = (tab[Tn] + [1.0, 0.0])[:6]
    return lead + a + b * lead + c * pre_margin, max(sd * max(0.3, al + be * abs(lead) / 10.0), 1.0)


def fit(sport, cache):
    rows = rows_for(sport, cache)
    cut = int(len(rows) * 0.7)
    tr, te = rows[:cut], rows[cut:]
    tab = fit_table(tr, sport)
    cfg = CFG[sport]
    print(f"{sport.upper()}: {len(rows)} games with play-by-play and a pregame line; table fitted on the first {len(tr)}, checked on the last {len(te)}")
    pr, ac = [], []
    for r in te:
        for T in cfg["grid"][::2]:
            mu, sd = predict(tab, T, r[3][T], r[2])
            for off in (-8, -4, 0, 4, 8):                                    # P(final margin > current margin + off): a spread of the live-bet kind, both sides
                x = r[3][T] + off
                pr.append(1 - Phi((x - mu) / sd)); ac.append(float(r[1] > x))
    pr, ac = np.array(pr), np.array(ac)
    edges = [0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, 1.0001]
    print("  calibration of P(final margin beats a line within 8 points of the current score): predicted -> actual")
    cal = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (pr >= lo) & (pr < hi)
        if m.sum() >= 50:
            cal.append(f"{100 * pr[m].mean():.0f}%->{100 * ac[m].mean():.0f}% (n={int(m.sum())})")
    print("   " + "   ".join(cal))
    print(f"  log loss {-np.mean(ac * np.log(np.clip(pr, 1e-6, 1)) + (1 - ac) * np.log(np.clip(1 - pr, 1e-6, 1))):.4f} on {len(pr)} state/line pairs")
    full = fit_table(rows, sport)
    (HERE / "models" / f"live_spread_{sport}.json").write_text(json.dumps({"sport": sport, "fit_on": f"{len(rows)} games", "table": {str(k): v for k, v in full.items()}}))
    print(f"  saved models/live_spread_{sport}.json")


def load(sport):
    d = json.load(open(HERE / "models" / f"live_spread_{sport}.json"))
    return {int(k): v for k, v in d["table"].items()}


def am(p):
    p = min(0.999, max(0.001, p))
    return f"+{round(100 * (1 - p) / p)}" if p < 0.5 else f"{round(-100 * p / (1 - p))}"


def ask(sport, diff, left, pre, offer, price):
    tab = load(sport)
    mu, sd = predict(tab, left, diff, -pre if pre is not None else 0.0)       # pre = pregame SPREAD from this team's side (negative = favoured); margin = -spread
    fair = -mu                                                                 # fair live spread: cover probability 50% when the offered number equals this
    print(f"  {sport.upper()}: this team {'leads' if diff > 0 else 'trails'} by {abs(diff):g} with {int(left // 60)}:{int(left % 60):02d} left, pregame spread {pre if pre is not None else 'n/a'}")
    print(f"  expected final margin {mu:+.1f} (sd {sd:.1f}); the fair live spread is {fair:+.1f}; win probability {100 * (1 - Phi((0 - mu) / sd)):.0f}% (fair price {am(1 - Phi((0 - mu) / sd))})")
    if offer is not None:
        pc = 1 - Phi((-offer - mu) / sd)                                       # covers +offer when final margin > -offer
        be = None
        line = f"  at {offer:+g}: covers {100 * pc:.1f}% -> fair price {am(pc)}"
        if price is not None:
            be = 100 / (price + 100) if price > 0 else -price / (-price + 100)
            line += f"; offered {price:+d} needs {100 * be:.1f}% -> {'EDGE' if pc > be else 'no edge'} of {100 * (pc - be):+.1f} pts, EV {100 * (pc * (price / 100 if price > 0 else 100 / -price) - (1 - pc)):+.1f}% per $1"
        print(line)


def table(sport):
    tab = load(sport)
    print(f"{sport.upper()} fair live spread (and win probability) for a team that is DOWN d with t left, pregame pick'em")
    ts = [2160, 1440, 1080, 720, 360, 120] if sport == "nba" else [2700, 1800, 900, 480, 180]
    ds = [5, 10, 15, 20] if sport == "nba" else [3, 7, 10, 14, 21]
    print("  down   " + "  ".join(f"{t // 60:>2d}:{t % 60:02d} left" for t in ts))
    for d in ds:
        cells = []
        for t in ts:
            mu, sd = predict(tab, t, -d, 0.0)
            cells.append(f"{-mu:+5.1f}/{100 * (1 - Phi(-mu / sd)):3.0f}%")
        print(f"  {d:>4}   " + "  ".join(cells))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["fit", "ask", "table"]); ap.add_argument("sport", choices=list(CFG)); ap.add_argument("--cache", default=None)
    ap.add_argument("--diff", type=float); ap.add_argument("--left", type=float); ap.add_argument("--pre", type=float, default=None); ap.add_argument("--offer", type=float, default=None); ap.add_argument("--price", type=int, default=None)
    a = ap.parse_args()
    if a.cmd == "fit":
        fit(a.sport, a.cache or str(Path.home() / {"nba": ".cache/dash_nba", "nfl": ".cache/dash_nfl"}[a.sport]))
    elif a.cmd == "ask":
        ask(a.sport, a.diff, a.left, a.pre, a.offer, a.price)
    else:
        table(a.sport)
