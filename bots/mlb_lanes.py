#!/usr/bin/env python3
"""
mlb_lanes.py -- the full-slate MLB hitter lanes (1+ hit, 2+ H+R+RBI, 2+ total bases, 1+ HR) as a live board: fitted, calibrated, for every hitter in a posted lineup.

    python bots/mlb_lanes.py fit   CACHE                 fit on 2024-25 / grade on 2026 (Platt + tail from that holdout), then refit on everything; models/mlb_<lane>_v1.json
    python bots/mlb_lanes.py board CACHE [DAY] [--out F]  today's lineups (StatsAPI, posted ~3 hours before first pitch) -> outputs/mlb_board_latest.json

CACHE is mlb_hits_data.py's pull. The features are exactly mlb_hits_study.py's (his own rates over 100 / 30 / 10 games shrunk to the league, lineup spot and plate appearances, his profile, the opposing
starter, the park), computed from games strictly before the date, so the live board and the backtest are one code path. It does not use the postseason (the logs are regular season): a hitter's form is as
of the end of the regular season until the logs are refreshed.
"""
import bisect, datetime as dt, json, math, subprocess, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlb_hits_study as S              # noqa: E402
from tune_models import fit_logistic   # noqa: E402
from headline_lane import headline_lane   # noqa: E402

HERE = Path(__file__).resolve().parent
LANES = {"hit": "1+ hit", "hrr": "2+ hits+runs+RBI", "tb2": "2+ total bases", "hr": "1+ home run"}
BASE = ["spot", "spot2", "home", "pa_pg", "ba", "k", "bb", "iso", "tbpa", "hrpa", "p_h", "p_k", "p_bb", "p_hr", "p_er", "p_outs", "same_hand", "pk_h", "pk_tb", "pk_hr"]


def cols_for(lane):
    return [c for c in BASE if c not in ("tbpa",)] if False else ([f"{lane}100", f"{lane}30", f"{lane}10"] + BASE if lane != "hr" else BASE)


class Ctx:
    def __init__(self, cache):
        self.games, self.people, hitlogs, pitlogs = S.load(cache)
        self.H = {pid: S.Series(S.prep_hit(rs), S.HF) for pid, rs in hitlogs.items()}
        self.P = {pid: S.Series([{**r, "start": 1} for r in rs], S.PF) for pid, rs in pitlogs.items()}
        tot = defaultdict(float)
        for s in self.H.values():
            for r in s.rows:
                if r["date"] < "2025-01-01":
                    for f in S.HF:
                        tot[f] += r[f]
        self.L = {f: tot[f] / tot["g"] for f in ("hit", "hrr", "tb2")}
        self.L.update({"ba": tot["h"] / tot["ab"], "k": tot["k"] / tot["pa"], "bb": tot["bb"] / tot["pa"], "iso": (tot["tb"] - tot["h"]) / tot["ab"], "tbpa": tot["tb"] / tot["pa"], "hrpa": tot["hr"] / tot["pa"], "pa": tot["pa"] / tot["g"]})
        ptot = defaultdict(float)
        for s in self.P.values():
            for r in s.rows:
                if r["date"] < "2025-01-01":
                    for f in S.PF:
                        ptot[f] += r[f]
        self.LP = {"h": ptot["h"] / ptot["bf"], "k": ptot["k"] / ptot["bf"], "bb": ptot["bb"] / ptot["bf"], "hr": ptot["hr"] / ptot["bf"], "er": ptot["er"] / ptot["start"], "outs": ptot["outs"] / ptot["start"]}
        self.venue = defaultdict(lambda: defaultdict(float)); self.lg = defaultdict(float)

    def park(self, venue):
        v = self.venue[venue]; lg = self.lg; pf = {}
        for f in ("h", "tb", "hr"):
            pf[f] = ((v[f] + 3000 * (lg[f] / lg["pa"] if lg["pa"] else 0.0)) / (v["pa"] + 3000)) / (lg[f] / lg["pa"]) if lg["pa"] > 5000 and lg[f] else 1.0
        return pf

    def pitcher(self, opp_sp, date):
        sh = lambda s_, n, w, prior: (s_ + w * prior) / (n + w)
        ps = self.P.get(opp_sp)
        if not ps:
            return {"p_h": 1.0, "p_k": 1.0, "p_bb": 1.0, "p_hr": 1.0, "p_er": 1.0, "p_outs": 1.0}
        pidx = ps.before(date); nst = min(pidx, 30); bf30 = ps.window(pidx, 30, "bf")[0]; LP = self.LP
        return {"p_h": sh(ps.window(pidx, 30, "h")[0], bf30, 100, LP["h"]) / LP["h"], "p_k": sh(ps.window(pidx, 30, "k")[0], bf30, 100, LP["k"]) / LP["k"],
                "p_bb": sh(ps.window(pidx, 30, "bb")[0], bf30, 100, LP["bb"]) / LP["bb"], "p_hr": sh(ps.window(pidx, 30, "hr")[0], bf30, 100, LP["hr"]) / LP["hr"],
                "p_er": sh(ps.window(pidx, 30, "er")[0], nst, 4, LP["er"]) / LP["er"], "p_outs": sh(ps.window(pidx, 10, "outs")[0], min(pidx, 10), 3, LP["outs"]) / LP["outs"]}

    def hitter(self, pid, idx, spot, home, opp_sp, pitch, pf):
        s, L = self.H[pid], self.L
        sh = lambda s_, n, w, prior: (s_ + w * prior) / (n + w)
        f = {}
        for out in ("hit", "hrr", "tb2"):
            f[f"{out}100"] = sh(s.window(idx, 100, out)[0], s.window(idx, 100, "g")[0], 20, L[out]); f[f"{out}30"] = sh(s.window(idx, 30, out)[0], s.window(idx, 30, "g")[0], 10, L[out])
            f[f"{out}10"] = sh(s.window(idx, 10, out)[0], s.window(idx, 10, "g")[0], 5, L[out])
        ab, pa = s.window(idx, 100, "ab")[0], s.window(idx, 100, "pa")[0]
        f["ba"] = sh(s.window(idx, 100, "h")[0], ab, 80, L["ba"]); f["k"] = sh(s.window(idx, 100, "k")[0], pa, 80, L["k"]); f["bb"] = sh(s.window(idx, 100, "bb")[0], pa, 80, L["bb"])
        f["iso"] = sh(s.window(idx, 100, "tb")[0] - s.window(idx, 100, "h")[0], ab, 80, L["iso"]); f["tbpa"] = sh(s.window(idx, 100, "tb")[0], pa, 80, L["tbpa"]); f["hrpa"] = sh(s.window(idx, 100, "hr")[0], pa, 150, L["hrpa"])
        f["pa_pg"] = sh(s.window(idx, 30, "pa")[0], s.window(idx, 30, "g")[0], 10, L["pa"])
        f["spot"] = float(spot); f["spot2"] = float(spot * spot); f["home"] = float(home)
        bats = (self.people.get(pid) or {}).get("bats"); thr = (self.people.get(opp_sp) or {}).get("throws")
        f["same_hand"] = float(bool(bats and thr and bats == thr))
        f.update(pitch); f["pk_h"], f["pk_tb"], f["pk_hr"] = pf["h"], pf["tb"], pf["hr"]
        return f


def history_rows(ctx):
    rows = []
    for pk in sorted(ctx.games, key=lambda k: (ctx.games[k]["date"], k)):
        g = ctx.games[pk]
        if len(g["home_lineup"]) != 9 or len(g["away_lineup"]) != 9 or not g["home_sp"] or not g["away_sp"]:
            continue
        date = g["date"]; pf = ctx.park(g["venue"])
        for lineup, opp_sp, home in ((g["home_lineup"], g["away_sp"], 1), (g["away_lineup"], g["home_sp"], 0)):
            pitch = ctx.pitcher(opp_sp, date)
            for spot, pid in enumerate(lineup, 1):
                s = ctx.H.get(pid)
                if not s:
                    continue
                idx = s.before(date)
                rec = next((r for r in s.rows[idx:idx + 3] if r["pk"] == pk), None)
                if rec is None or idx < 30:
                    continue
                rows.append({"season": int(date[:4]), "date": date, "game": pk, "pid": pid, "f": ctx.hitter(pid, idx, spot, home, opp_sp, pitch, pf),
                             "y": {"hit": int(rec["h"] >= 1), "hrr": int(rec["h"] + rec["r"] + rec["rbi"] >= 2), "tb2": int(rec["tb"] >= 2), "hr": int(rec["hr"] >= 1)}})
        for lineup in (g["home_lineup"], g["away_lineup"]):
            for pid in lineup:
                s = ctx.H.get(pid)
                if not s:
                    continue
                idx = s.before(date)
                rec = next((r for r in s.rows[idx:idx + 3] if r["pk"] == pk), None)
                if rec:
                    for f_ in ("h", "tb", "hr", "pa"):
                        ctx.venue[g["venue"]][f_] += rec[f_]; ctx.lg[f_] += rec[f_]
    return rows


def fit(cache):
    ctx = Ctx(cache); rows = history_rows(ctx)
    tr = [r for r in rows if r["season"] in (2024, 2025)]; te = [r for r in rows if r["season"] == 2026]
    print(f"{len(rows)} lineup hitter-games ({len(tr)} fit, {len(te)} graded on 2026)")
    out = HERE / "models"
    for lane, label in LANES.items():
        cols = cols_for(lane)
        X = lambda rs: np.array([[r["f"][c] for c in cols] for r in rs], float)
        Xtr = X(tr); mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        w = fit_logistic((Xtr - mu) / sd, np.array([r["y"][lane] for r in tr], float), l2=10.0)
        z = ((X(te) - mu) / sd) @ w[1:] + w[0]; y = np.array([r["y"][lane] for r in te], float)
        p = 1 / (1 + np.exp(-z))
        ab = fit_logistic(z.reshape(-1, 1), y, l2=0.01)
        q = np.sort(1 / (1 + np.exp(-(ab[0] + ab[1] * z)))); t = float(q[int(len(q) * 0.99)])
        o = np.argsort(p); k = len(o) // 8
        cal = " ".join(f"{100 * p[o[i * k:(i + 1) * k if i < 7 else None]].mean():.0f}>{100 * y[o[i * k:(i + 1) * k if i < 7 else None]].mean():.0f}" for i in range(8))
        print(f"  {label:<18} base {100 * y.mean():5.1f}%  log loss {-np.mean(y * np.log(np.clip(p, 1e-6, 1)) + (1 - y) * np.log(np.clip(1 - p, 1e-6, 1))):.4f}  calibration by eighth: {cal}  | Platt a {ab[0]:+.3f} b {ab[1]:.3f}")
        Xa = X(rows); amu, asd = Xa.mean(0), Xa.std(0) + 1e-9
        aw = fit_logistic((Xa - amu) / asd, np.array([r["y"][lane] for r in rows], float), l2=10.0)
        (out / f"mlb_{lane}_v1.json").write_text(json.dumps({"model_version": f"mlb_{lane}_v1", "target": label, "features": cols, "mu": amu.tolist(), "sd": asd.tolist(), "intercept": float(aw[0]), "weights": aw[1:].tolist(),
                                                              "fit_on": f"{len(rows)} lineup hitter-games, 2024-26 regular season", "platt": {"a": float(ab[0]), "b": float(ab[1]), "tail": {"t": t, "slope": 0.25}}}, indent=1))
    print("  saved models/mlb_{hit,hrr,tb2,hr}_v1.json")


def jget(url):
    return json.loads(subprocess.run(["curl", "-s", "-m", "60", url], capture_output=True, text=True).stdout or "{}")


def calibrate(m, z):
    pl = m["platt"]; q = 1 / (1 + math.exp(-(pl["a"] + pl["b"] * z))); t = pl["tail"]
    return t["t"] + (q - t["t"]) * t["slope"] if q > t["t"] else q


def board(cache, day):
    ctx = Ctx(cache); history_rows(ctx)                                  # also fills the park accumulators through the end of the logs
    models = {l: json.load(open(HERE / "models" / f"mlb_{l}_v1.json")) for l in LANES}
    j = jget(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={day}&hydrate=lineups,probablePitcher,team")
    rows = []
    for dd in j.get("dates", []):
        for g in dd["games"]:
            lu = g.get("lineups") or {}; t = g["teams"]
            for side, home in (("away", 0), ("home", 1)):
                players = lu.get(f"{side}Players", []); opp_sp = (t["home" if side == "away" else "away"].get("probablePitcher") or {}).get("id")
                if len(players) != 9:
                    continue
                pitch = ctx.pitcher(opp_sp, day); pf = ctx.park((g.get("venue") or {}).get("id"))
                for spot, p in enumerate(players, 1):
                    s = ctx.H.get(p["id"])
                    if not s:
                        continue
                    f = ctx.hitter(p["id"], s.before(day), spot, home, opp_sp, pitch, pf)
                    r = {"name": p.get("fullName"), "pid": p["id"], "team": t[side]["team"].get("abbreviation") or t[side]["team"]["name"], "opponent": t["home" if side == "away" else "away"]["team"].get("abbreviation") or t["home" if side == "away" else "away"]["team"]["name"],
                         "game_pk": g["gamePk"], "spot": spot, "opp_starter": (ctx.people.get(opp_sp) or {}).get("name"), "start_utc": g.get("gameDate"), "lineup_status": "posted", "p_play": 0.98}
                    for lane, m in models.items():
                        z = m["intercept"] + sum(w * (f[n] - mu) / sd for n, w, mu, sd in zip(m["features"], m["weights"], m["mu"], m["sd"]))
                        r[f"p_{lane}"] = round(calibrate(m, z), 4)
                    r["lane_base"] = {"hit": round(f["hit100"], 4), "hrr": round(f["hrr100"], 4), "tb2": round(f["tb2100"], 4), "hr": round(f["hrpa"] * f["pa_pg"], 4)}
                    r["headline_lane"] = headline_lane({k: r.get(f"p_{k}") for k in r["lane_base"]}, r["lane_base"], "mlb")
                    rows.append(r)
    rows.sort(key=lambda r: -r["p_hr"])
    return rows


if __name__ == "__main__":
    if sys.argv[1] == "fit":
        fit(sys.argv[2])
    else:
        day = next((a for a in sys.argv[3:] if a[:2] == "20"), dt.date.today().isoformat())
        rows = board(sys.argv[2], day)
        out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else HERE / "outputs" / "mlb_board_latest.json"
        out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps({"date": day, "model_version": "mlb_lanes_v1", "players": rows}, indent=1))
        print(f"{len(rows)} hitters in posted lineups for {day} -> {out}")
        for r in rows[:8]:
            print(f"  {r['name']:<22}{r['team']:<4}vs {r['opponent']:<4}spot {r['spot']}  hit {100 * r['p_hit']:.0f}%  H+R+RBI {100 * r['p_hrr']:.0f}%  TB2 {100 * r['p_tb2']:.0f}%  HR {100 * r['p_hr']:.1f}%")
