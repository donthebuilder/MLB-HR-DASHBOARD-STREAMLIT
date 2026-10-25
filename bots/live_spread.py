#!/usr/bin/env python3
"""
live_spread.py -- what a LIVE spread, moneyline or total is worth. "Down 15 in the third quarter, they offer +12.5 at +120: is that good?"

    python bots/live_spread.py fit   nba|nfl|nhl [--cache DIR]                    build the tables from ESPN play-by-play, check them on games they never saw, save models/live_spread_<sport>.json
    python bots/live_spread.py ask   nba|nfl|nhl --diff -15 --left 1080 [--pre -3] [--offer 12.5] [--price 120] [--total-line 224.5 --score-total 190 --over 221.5]
    python bots/live_spread.py table nba|nfl|nhl

State = (this team's lead or deficit, seconds left, the pregame margin the book had for this team, the pregame total, the combined score so far). From every NBA game of 2025-26, NFL game of
2023-25 and NHL game of 2023-25 we sample the state every 2 / 3 / 3 minutes and record the final margin and final total. For each time on the grid:
  margin  final margin - current lead ~ Normal(a + b*lead + c*pregame margin, sd), sd widening or narrowing with the size of the lead (maximum likelihood)
  total   final total - current total ~ Normal(a + b*pregame total*(time left / game) + c*(combined score - pregame total*(time played / game)) + d*|lead|, sd): the rest of the game at the
          pregame pace, plus how much of the pace the game has run over or under so far persists, plus the garbage-time effect of a big lead.
Fitted on the first 70% of games, checked on the last 30% (calibration of the cover and over probabilities), then refit on everything for `ask`. Spread sign: negative = this team is favoured; a
team down 15 is normally offered a positive number like +12.5 (it covers by losing by 12 or less, or winning). NHL pregame margin is the book's win probability minus 0.5 (hockey has no spread).
"""
import argparse, json, math, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
HERE = Path(__file__).resolve().parent
CFG = {"nba": {"espn": "basketball/nba", "grid": list(range(2640, 100, -120)), "period": 720, "n": 4, "total_len": 2880},
       "nfl": {"espn": "football/nfl", "grid": list(range(3600, 100, -180)), "period": 900, "n": 4, "total_len": 3600},
       "nhl": {"espn": "hockey/nhl", "grid": list(range(3420, 100, -180)), "period": 1200, "n": 3, "total_len": 3600},
       # baseball: the clock is OUTS LEFT in nine innings (54 at the first pitch, 51 after the first half-inning ... 3 after the top of the ninth)
       "mlb": {"espn": "baseball/mlb", "grid": list(range(51, 0, -3)), "period": 6, "n": 9, "total_len": 54}}
DISCRETE = ("nhl", "mlb")          # sports whose margins come in lumps: observed distributions, negative-binomial totals
Phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
USE_EMP_TOTAL = False      # the empirical remaining-goals table was tried for NHL totals and made the check WORSE (log loss 1.21 vs 0.66): kept in the file, not used


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
    f = Path(cache) / f"{'lsp3' if sport == 'nhl' else 'lsp2'}_{sport}_{eid}.json"
    if f.exists():
        return json.load(open(f))
    cfg = CFG[sport]
    d = curl(f"https://site.api.espn.com/apis/site/v2/sports/{cfg['espn']}/summary?event={eid}")
    pts = []
    plays = [p for p in d.get("plays", []) if sport != "nhl" or p.get("scoringPlay")] or [pl for dr in ((d.get("drives") or {}).get("previous") or []) for pl in dr.get("plays", [])]
    for p in plays:
        per, cl = (p.get("period") or {}).get("number"), secs((p.get("clock") or {}).get("displayValue"))
        if per is None or cl is None or p.get("homeScore") is None:
            continue
        left = (cfg["n"] - per) * cfg["period"] + cl if per <= cfg["n"] else cl
        pts.append((left, p["homeScore"] - p["awayScore"], p["homeScore"] + p["awayScore"], per, cl))
    if sport == "nhl":                                                             # ESPN lists hockey scoring plays out of order: put them in game order (period, then clock counting down)
        pts.sort(key=lambda x: (x[3], -x[4], x[2]))
    pts = [x[:3] for x in pts]
    out = {"id": eid, "pts": pts}
    json.dump(out, open(f, "w"))
    return out


def novig(h, a):
    ih = lambda m: None if m is None else (100 / (m + 100) if m > 0 else -m / (-m + 100))
    x, y = ih(h), ih(a)
    return x / (x + y) if x and y else None


def rows_for(sport, cache):
    """[(order, home final margin, pregame home margin feature, {T: home lead}, final total, pregame total, {T: combined score})]"""
    cfg = CFG[sport]; out = []
    if sport == "mlb":
        import team_models as tm
        games = tm.games_mlb(cache, seasons=(2023, 2024, 2025, 2026))
        rows, _ = tm.build(games, "mlb", 4, 25)                                    # pregame strength: Elo win probability (a fixed K and home edge; the live tables only need the rough size of the edge)
        by_id = {r["id"]: r for r in rows}
        rs, rg = {}, {}                                                            # trailing runs scored / allowed per team (20 games) for the pregame run environment
        for i, g in enumerate(games):
            inn = g["innings"]
            if len(inn) < 8 or g["id"] not in by_id:
                continue
            r_ = by_id[g["id"]]
            def env(t):
                h = rs.get(t, [])[-20:]; a_ = rg.get(t, [])[-20:]
                return (sum(h) / len(h) if h else 4.4, sum(a_) / len(a_) if a_ else 4.4)
            (hs_, ha_), (as_, aa_) = env(g["home"]), env(g["away"])
            L = (hs_ + aa_) / 2 + (as_ + ha_) / 2
            lead, sc = {}, {}
            for T in cfg["grid"]:
                halves = (54 - T) // 3                                             # half-innings finished: top 1 = 1, bottom 1 = 2, ...
                ar = sum(x[0] for x in inn[: (halves + 1) // 2]); hr_ = sum(x[1] for x in inn[: halves // 2])
                lead[T], sc[T] = hr_ - ar, hr_ + ar
            pm = 1 / (1 + 10 ** (-r_["elo_gap"] / 400)) - 0.5
            out.append((i, g["hs"] - g["as"], pm, lead, g["hs"] + g["as"], float(L), sc))
            for t, a, b in ((g["home"], g["hs"], g["as"]), (g["away"], g["as"], g["hs"])):
                rs.setdefault(t, []).append(a); rg.setdefault(t, []).append(b)
        return out
    if sport == "nba":
        import buckets_backtest as bb, game_lines as G
        ld = Path(cache).parent / "nba_lines"
        gs = sorted([g for s in (2025, 2026) for g in bb.load(s, cache)], key=lambda g: (g["date"], g["id"]))
        meta = []
        for g in gs:
            ln = G.close_line("nba", g["id"], ld) or {}
            meta.append((g["id"], g["date"], -ln["spread_home"] if ln.get("spread_home") is not None else None, ln.get("total")))
    elif sport == "nfl":
        import nflreadpy as nfl
        meta = [(str(r["espn"]), str(r["gameday"]), float(r["spread_line"]) if r["spread_line"] is not None else None, r["total_line"])
                for r in nfl.load_schedules([2023, 2024, 2025]).iter_rows(named=True) if r["game_type"] == "REG" and r.get("espn") and r["home_score"] is not None]
        meta.sort(key=lambda m: m[1])
    else:
        import game_lines as G
        ids = json.load(open(Path(cache) / "espn_nhl_games.json"))
        ld = Path(cache) / "nhl_lines"
        meta = []
        for k, eid in sorted(ids.items()):
            ln = G.close_line("nhl", eid, ld) or {}
            p = novig(ln.get("ml_home"), ln.get("ml_away"))
            meta.append((eid, k.split("|")[0], (p - 0.5) if p is not None else None, ln.get("total")))
    with ThreadPoolExecutor(10) as ex:
        sts = list(ex.map(lambda m: game_states(sport, m[0], cache), meta))
    for i, ((eid, date, pm, tot), st) in enumerate(zip(meta, sts)):
        pts = st["pts"]
        if len(pts) < (4 if sport in DISCRETE else 50) or pm is None or tot is None:
            continue
        lead, sc = {}, {}
        for T in cfg["grid"]:
            before = [(d, s) for t, d, s in pts if t >= T]
            lead[T], sc[T] = (before[-1] if before else (0, 0))
        out.append((i, pts[-1][1], pm, lead, pts[-1][2], float(tot), sc))
    return out


def fit_table(rows, sport):
    cfg = CFG[sport]; tab = {}
    for T in cfg["grid"]:
        X = np.array([[1.0, r[3][T], r[2]] for r in rows]); y = np.array([r[1] - r[3][T] for r in rows], float)
        w = np.linalg.solve(X.T @ X + 1e-6 * np.eye(3), X.T @ y)
        res = y - X @ w; s0 = float(np.std(res)); Ls = np.abs(X[:, 1]) / 10.0
        best = (1e18, 1.0, 0.0)                                   # sd = s0 * (alpha + beta*|lead|/10), by maximum likelihood
        for al in np.arange(0.5, 1.5, 0.05):
            for be in np.arange(-0.4, 0.8, 0.05):
                sd_i = s0 * np.maximum(0.3, al + be * Ls)
                nll = float(np.sum(np.log(sd_i) + res ** 2 / (2 * sd_i ** 2)))
                if nll < best[0]:
                    best = (nll, float(al), float(be))
        tab[T] = [float(w[0]), float(w[1]), float(w[2]), s0, best[1], best[2]]
    return tab


def total_feats(T, L, S, lead, sport):
    tl = CFG[sport]["total_len"]
    return [1.0, L * (T / tl), S - L * (1 - T / tl), abs(lead)]


def fit_total_table(rows, sport):
    cfg = CFG[sport]; tab = {}
    for T in cfg["grid"]:
        X = np.array([total_feats(T, r[5], r[6][T], r[3][T], sport) for r in rows]); y = np.array([r[4] - r[6][T] for r in rows], float)
        w = np.linalg.solve(X.T @ X + 1e-6 * np.eye(4), X.T @ y)
        tab[T] = [float(v) for v in w] + [float(np.std(y - X @ w))]
    return tab


def fit_empirical(rows, sport, lo=-6, hi=6):  # lead and change clipped to +-6
    """Hockey margins are lumpy (a 3-goal lead with six minutes left is almost always a 3-goal win), so a bell curve gets the tails wrong. Instead: for each clock and lead, the observed
    distribution of (final margin - lead), pooling the neighbouring grid times for samples. {T: {lead: [counts for delta lo..hi]}}"""
    cfg = CFG[sport]; grid = cfg["grid"]; emp = {}
    for i, T in enumerate(grid):
        near = [t for t in grid[max(0, i - 1): i + 2]]
        d = {}
        for r in rows:
            for t in near:
                lead = int(max(-6, min(6, r[3][t])))
                dl = int(max(lo, min(hi, r[1] - r[3][t])))
                d.setdefault(lead, [0] * (hi - lo + 1))[dl - lo] += 1
        emp[T] = d
    return emp


def emp_over(emp, T, lead, x, lo=-6, hi=6, min_n=30):
    """P(final margin > x) from the empirical table; None if that state has too few games."""
    e = emp.get(nearest(emp, T), {})
    L = int(max(-6, min(6, round(lead))))
    c = e.get(L)
    if not c or sum(c) < min_n:
        return None
    n = sum(c)
    return sum(c[k] for k in range(len(c)) if lead + (k + lo) * (1 if lead == L else 1) > x) / n


def nearest(tab, T):
    Ts = sorted(tab)
    return min(Ts, key=lambda t: abs(t - T)) if min(Ts) <= T <= max(Ts) else (min(Ts) if T < min(Ts) else max(Ts))


def predict(tab, T, lead, pre_margin):
    a, b, c, sd, al, be = (tab[nearest(tab, T)] + [1.0, 0.0])[:6]
    return lead + a + b * lead + c * pre_margin, max(sd * max(0.3, al + be * abs(lead) / 10.0), 0.5)


def fit_total_nb(rows, sport, ttab):
    """NHL goals still to come: per clock, (1) a linear correction of the model's expected goals-left to what was actually scored after it (late-game empty-net and pulled-goalie goals make the raw
    projection too small), and (2) an over-dispersion r for a negative binomial (variance = mean + mean^2 / r), because goals come in bunches. {T: [a, b, r]}"""
    cfg = CFG[sport]; out = {}
    for T in cfg["grid"]:
        mus, ys = [], []
        for r in rows:
            mu, _ = predict_total(ttab, T, r[5], r[6][T], r[3][T], sport)
            mus.append(max(mu - r[6][T], 0.0)); ys.append(float(r[4] - r[6][T]))
        mus, ys = np.array(mus), np.array(ys)
        A = np.column_stack([np.ones(len(mus)), mus]); w = np.linalg.lstsq(A, ys, rcond=None)[0]
        fit = np.maximum(A @ w, 0.02); var = float(np.mean((ys - fit) ** 2 - fit)); m2 = float(np.mean(fit ** 2))
        r_ = m2 / var if var > 1e-6 else 1e6
        out[T] = [float(w[0]), float(w[1]), float(min(max(r_, 0.5), 1e6))]
    return out


def nb_over(mean, r, k):
    """P(X > k) for a negative binomial with the given mean and dispersion r (r -> infinity is Poisson)."""
    mean = max(mean, 1e-4); p = r / (r + mean); c = 0.0
    lg = math.lgamma
    for i in range(0, int(max(k, 0)) + 1):
        c += math.exp(lg(i + r) - lg(r) - lg(i + 1) + r * math.log(p) + i * math.log(1 - p))
    return max(0.0, 1 - c) if k >= 0 else 1.0


def fit_total_emp(rows, sport, ttab):
    """NHL totals: the distribution of goals STILL TO COME, by clock and by the model's expected goals still to come (bucketed by 0.25). The Poisson was too thin in the tails (a line one goal away
    with minutes left: empty-net and late goals). {T: {bucket: [counts for 0..8 goals]}}"""
    cfg = CFG[sport]; emp = {}
    for i, T in enumerate(cfg["grid"]):
        near = cfg["grid"][max(0, i - 1): i + 2]
        d = {}
        for r in rows:
            for t in near:
                mu, _ = predict_total(ttab, t, r[5], r[6][t], r[3][t], sport)
                b = int(max(0, min(24, round(max(mu - r[6][t], 0.0) / 0.25))))
                k = int(max(0, min(8, r[4] - r[6][t])))
                d.setdefault(b, [0] * 9)[k] += 1
        emp[T] = d
    return emp


def emp_total_over(emp, T, mu_rem, k, min_n=40):
    """P(goals still to come > k) from the empirical table; None if the bucket is too thin."""
    e = emp.get(nearest(emp, T), {})
    b = int(max(0, min(24, round(max(mu_rem, 0.0) / 0.25))))
    c = None
    for db in (0, 1, -1, 2, -2):                                    # borrow from the neighbouring bucket when this one is thin
        cc = e.get(b + db)
        if cc and sum(cc) >= min_n:
            c = cc; break
    if not c:
        return None
    n = sum(c)
    return sum(c[j] for j in range(len(c)) if j > k) / n


def poisson_over(mu, k):
    """P(X > k) for X ~ Poisson(mu)."""
    mu = max(mu, 1e-6); p, c = math.exp(-mu), 0.0
    for i in range(0, int(max(k, 0)) + 1):
        c += p; p *= mu / (i + 1)
    return 1 - c


def predict_total(ttab, T, L, S, lead, sport):
    w = ttab[nearest(ttab, T)]
    f = total_feats(T, L, S, lead, sport)
    return S + sum(a * b for a, b in zip(w[:4], f)), max(w[4], 0.5)


def load_models(sport):
    d = json.load(open(HERE / "models" / f"live_spread_{sport}.json"))
    emp = {int(T): {int(k): v for k, v in e.items()} for T, e in (d.get("emp") or {}).items()}
    temp = {int(T): {int(k): v for k, v in e.items()} for T, e in (d.get("total_emp") or {}).items()}
    return {"tab": {int(k): v for k, v in d["table"].items()}, "ttab": {int(k): v for k, v in (d.get("total_table") or {}).items()}, "emp": emp, "temp": temp, "nb": {int(T): v for T, v in (d.get("total_nb") or {}).items()}, "sport": sport}


def p_margin_over(m, T, lead, pre, x):
    """P(this side's final margin > x). Hockey: the observed distribution for that clock and lead (bell-curve fallback if the state is rare). Basketball / football: Normal."""
    if m["sport"] in DISCRETE and m["emp"]:
        p = emp_over(m["emp"], T, lead, x)
        if p is not None:
            return p
    mu, sd = predict(m["tab"], T, lead, pre)
    return 1 - Phi((x - mu) / sd)


def p_total_over(m, T, L, S, lead, x):
    """P(final total > x). Hockey: Poisson over the goals still to come (mean from the fitted pace model); others Normal."""
    mu, sd = predict_total(m["ttab"], T, L, S, lead, m["sport"])
    if m["sport"] in DISCRETE:
        k = math.floor(x - S)                                                       # over a x.5 line = more than floor(x - S) goals still to come
        if m.get("nb"):
            a_, b_, r_ = m["nb"][nearest(m["nb"], T)]
            return min(0.97, max(0.03, nb_over(max(a_ + b_ * max(mu - S, 0.0), 0.02), r_, k)))      # the extremes were never reliable in the check (2% -> 14%): kept inside 3-97%
        if m.get("temp") and USE_EMP_TOTAL:
            p = emp_total_over(m["temp"], T, max(mu - S, 0.0), k)
            if p is not None:
                return p
        return poisson_over(max(mu - S, 0.45 * (6.2 * T / 3600.0 if m["sport"] == "nhl" else 8.8 * T / 54.0) + 0.03), x - S)
    return 1 - Phi((x - mu) / sd)


def fit(sport, cache):
    rows = rows_for(sport, cache)
    cut = int(len(rows) * 0.7)
    tr, te = rows[:cut], rows[cut:]
    tab, ttab = fit_table(tr, sport), fit_total_table(tr, sport)
    mod = {"tab": tab, "ttab": ttab, "emp": fit_empirical(tr, sport) if sport == "nhl" else {}, "temp": {}, "nb": fit_total_nb(tr, sport, ttab) if sport in DISCRETE else {}, "sport": sport}
    cfg = CFG[sport]
    print(f"{sport.upper()}: {len(rows)} games with play-by-play and a pregame line; tables fitted on the first {len(tr)}, checked on the last {len(te)}")
    offs = (-1.5, -0.5, 0.5, 1.5) if sport in DISCRETE else (-8, -4, 0, 4, 8)
    for label, kind in (("margin: P(final margin beats a line within a few points of the current score)", "m"), ("total: P(final total goes over a line near the projected total)", "t")):
        pr, ac = [], []
        for r in te:
            for T in cfg["grid"][::2]:
                if kind == "m":
                    for off in offs:
                        x = r[3][T] + off; pr.append(p_margin_over(mod, T, r[3][T], r[2], x)); ac.append(float(r[1] > x))
                else:
                    mu, sd = predict_total(ttab, T, r[5], r[6][T], r[3][T], sport)
                    for off in ([-1.0, 0.0, 1.0] if sport in DISCRETE else [-0.5 * sd, 0.0, 0.5 * sd]):
                        x = (math.floor(mu) + 0.5 + off) if sport in DISCRETE else mu + off
                        pr.append(p_total_over(mod, T, r[5], r[6][T], r[3][T], x)); ac.append(float(r[4] > x))
        pr, ac = np.array(pr), np.array(ac)
        edges = [0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, 1.0001]
        cal = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            m = (pr >= lo) & (pr < hi)
            if m.sum() >= 50:
                cal.append(f"{100 * pr[m].mean():.0f}%->{100 * ac[m].mean():.0f}%")
        print(f"  {label}\n    predicted -> actual: " + "  ".join(cal) + f"   (log loss {-np.mean(ac * np.log(np.clip(pr, 1e-6, 1)) + (1 - ac) * np.log(np.clip(1 - pr, 1e-6, 1))):.4f}, n={len(pr)})")
    full, ftab = fit_table(rows, sport), fit_total_table(rows, sport)
    emp = fit_empirical(rows, sport) if sport in DISCRETE else {}
    temp = {}
    (HERE / "models" / f"live_spread_{sport}.json").write_text(json.dumps({"sport": sport, "fit_on": f"{len(rows)} games", "table": {str(k): v for k, v in full.items()}, "total_table": {str(k): v for k, v in ftab.items()},
                                                                         "emp": {str(T): {str(k): v for k, v in e.items()} for T, e in emp.items()},
                                                                         "total_emp": {str(T): {str(k): v for k, v in e.items()} for T, e in temp.items()},
                                                                         "total_nb": {str(T): v for T, v in (fit_total_nb(rows, sport, ftab) if sport in DISCRETE else {}).items()}}))
    print(f"  saved models/live_spread_{sport}.json")


def load_all(sport):
    d = json.load(open(HERE / "models" / f"live_spread_{sport}.json"))
    return {int(k): v for k, v in d["table"].items()}, {int(k): v for k, v in (d.get("total_table") or {}).items()}


def load(sport):
    return load_all(sport)[0]


def am(p):
    p = min(0.999, max(0.001, p))
    return f"+{round(100 * (1 - p) / p)}" if p < 0.5 else f"{round(-100 * p / (1 - p))}"


def ask(sport, diff, left, pre, offer, price, total_line=None, score_total=None, over=None):
    tab, ttab = load_all(sport)
    mu, sd = predict(tab, left, diff, -pre if pre is not None else 0.0)
    fair = -mu
    print(f"  {sport.upper()}: this team {'leads' if diff > 0 else 'trails'} by {abs(diff):g} with {int(left // 60)}:{int(left % 60):02d} left, pregame spread {pre if pre is not None else 'n/a'}")
    print(f"  expected final margin {mu:+.1f} (sd {sd:.1f}); the fair live spread is {fair:+.1f}; win probability {100 * (1 - Phi((0 - mu) / sd)):.0f}% (fair price {am(1 - Phi((0 - mu) / sd))})")
    if offer is not None:
        pc = 1 - Phi((-offer - mu) / sd)
        line = f"  at {offer:+g}: covers {100 * pc:.1f}% -> fair price {am(pc)}"
        if price is not None:
            be = 100 / (price + 100) if price > 0 else -price / (-price + 100)
            line += f"; offered {price:+d} needs {100 * be:.1f}% -> {'EDGE' if pc > be else 'no edge'} of {100 * (pc - be):+.1f} pts, EV {100 * (pc * (price / 100 if price > 0 else 100 / -price) - (1 - pc)):+.1f}% per $1"
        print(line)
    if total_line is not None and score_total is not None and ttab:
        tm, ts = predict_total(ttab, left, total_line, score_total, diff, sport)
        print(f"  total: projected final {tm:.1f} (sd {ts:.1f}) vs pregame {total_line:g}" + (f"; over {over:g} {100 * (1 - Phi((over - tm) / ts)):.1f}%" if over is not None else ""))


def table(sport):
    tab = load(sport)
    print(f"{sport.upper()} fair live spread (and win probability) for a team that is DOWN d with t left, pregame pick'em")
    ts = {"nba": [2160, 1440, 1080, 720, 360, 120], "nfl": [2700, 1800, 900, 480, 180], "nhl": [2400, 1800, 1200, 600, 300], "mlb": [45, 36, 27, 18, 9, 3]}[sport]
    ds = {"nba": [5, 10, 15, 20], "nfl": [3, 7, 10, 14, 21], "nhl": [1, 2, 3], "mlb": [1, 2, 3, 5]}[sport]
    print("  down   " + "  ".join((f"{t:>2d} outs left" if sport == "mlb" else f"{t // 60:>2d}:{t % 60:02d} left") for t in ts))
    for d in ds:
        cells = []
        for t in ts:
            mu, sd = predict(tab, t, -d, 0.0)
            cells.append(f"{-mu:+5.1f}/{100 * (1 - Phi(-mu / sd)):3.0f}%")
        print(f"  {d:>4}   " + "  ".join(cells))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["fit", "ask", "table"]); ap.add_argument("sport", choices=list(CFG)); ap.add_argument("--cache", default=None)
    ap.add_argument("--diff", type=float); ap.add_argument("--left", type=float); ap.add_argument("--pre", type=float, default=None); ap.add_argument("--offer", type=float, default=None); ap.add_argument("--price", type=int, default=None)
    ap.add_argument("--total-line", type=float, default=None); ap.add_argument("--score-total", type=float, default=None); ap.add_argument("--over", type=float, default=None)
    a = ap.parse_args()
    if a.cmd == "fit":
        fit(a.sport, a.cache or str(Path.home() / {"nba": ".cache/dash_nba", "nfl": ".cache/dash_nfl", "nhl": ".cache/dash_nhl", "mlb": ".cache/dash_mlb"}[a.sport]))
    elif a.cmd == "ask":
        ask(a.sport, a.diff, a.left, a.pre, a.offer, a.price, a.total_line, a.score_total, a.over)
    else:
        table(a.sport)
