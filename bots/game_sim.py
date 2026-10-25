#!/usr/bin/env python3
"""
game_sim.py -- ONE joint simulation per game, so the player numbers and the game result come from the same simulated games.

    python bots/game_sim.py nhl|nba DAY [--sims 20000] [--out FILE]

Why: the player lanes and the team (moneyline / spread) models were separate. A player's goal chance never knew his team's expected goals, and a team's win chance never
knew who was scoring. Here a game is simulated N times:
  1. the game: expected team scores come from the book's total and OUR team model's win probability (team_live.py / team_models.py), solved so the simulated win rate matches
     the model's (NHL: Poisson goals, tied after regulation goes to a coin flip; NBA: normal team points);
  2. the players: each team's scoring is handed out to its players in proportion to their own fitted rates (the nhl_live / nba_live probabilities), scaled so the team's players add up
     to the team's expected scoring; availability is sampled from p_play (injury report);
  (NBA: each player's spread is set so his simulated P(25+) equals the board's own fitted probability, and the team's simulated points move all his teammates together; the roster check
     `roster_vs_team_points` flags a team whose listed players do not add up to its expected points, which means the board's roster is stale or incomplete.)
  3. every simulated game yields everything at once: team W/L, each player's goals / points / assists, so the output has P(player scores), P(team wins), and the JOINT: P(he scores AND they win).
The same player appears in the same simulated games as his team's result: the "Chourio homers in the sims where his team wins" picture. (MLB uses the same sampler once its
board is on; no games until spring.) Coherence check printed at the end: how far each player's simulated probability sits from the board's.
"""
import argparse, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
HERE = Path(__file__).resolve().parent
LG_TOTAL = {"nhl": 6.2, "nba": 230.0}


def solve_split(total, p_home, hfa_tie=0.5, n=40):
    """NHL: lam_h + lam_a = total, find lam_h so P(home wins incl. OT coin flip) = p_home."""
    from math import exp, factorial
    def pw(lh):
        la = total - lh
        ph = [exp(-lh) * lh ** k / factorial(k) for k in range(n)]; pa = [exp(-la) * la ** k / factorial(k) for k in range(n)]
        win = sum(ph[i] * sum(pa[:i]) for i in range(n)); tie = sum(ph[i] * pa[i] for i in range(n))
        return win + hfa_tie * tie
    lo, hi = 0.3, total - 0.3
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if pw(mid) < p_home else (lo, mid)
    return (lo + hi) / 2, total - (lo + hi) / 2


def team_inputs(sport, day):
    import team_live
    out = {}
    for g in team_live.board(sport, day):
        out[(g["home"], g["away"])] = g
    return out


def sim_nhl(day, sims, rng):
    import nhl_live  # noqa: F401  (the board file is produced by nhl_live.py)
    board = json.load(open(HERE / "outputs" / "nhl_board_latest.json"))
    games = team_inputs("nhl", day)
    by_team = defaultdict(list)
    for r in board["players"]:
        by_team[r["team"]].append(r)
    res = []
    for (h, a), g in games.items():
        total = float(g.get("book_total") or LG_TOTAL["nhl"])
        lam_h, lam_a = solve_split(total, g["model_p_home"])
        teams = {h: lam_h, a: lam_a}
        goals = {t: rng.poisson(lam, sims) for t, lam in teams.items()}
        hw = (goals[h] > goals[a]) | ((goals[h] == goals[a]) & (rng.random(sims) < 0.5))
        for t, opp in ((h, a), (a, h)):
            ps = by_team.get(t, [])
            if not ps:
                continue
            lam = np.array([-math.log(1 - min(0.95, r["p_goal"])) for r in ps]); la = np.array([-math.log(1 - min(0.95, r["p_assist"] or 0.01)) for r in ps])
            avail = rng.random((sims, len(ps))) < np.array([r["p_play"] for r in ps])
            scale = 0.95 * teams[t] / max(1e-9, float((lam * np.array([r["p_play"] for r in ps])).sum()))     # players share ~95% of the team's goals
            w = lam * scale
            G, win = goals[t], (hw if t == h else ~hw)
            pg = np.zeros((sims, len(ps)), int); pa = np.zeros((sims, len(ps)), int)
            for s in range(sims):
                if G[s] == 0:
                    continue
                wa = w * avail[s]
                if wa.sum() <= 0:
                    continue
                sc = rng.multinomial(int(G[s]), wa / wa.sum()); pg[s] = sc
                for i in np.flatnonzero(sc):                              # assists: ~1.7 a goal, drawn from the other available players by assist rate
                    for _ in range(int(sc[i])):
                        k = rng.choice([0, 1, 2], p=[0.1, 0.25, 0.65])
                        wo = la * avail[s]; wo = wo.copy(); wo[i] = 0
                        if k and wo.sum() > 0:
                            idx = rng.choice(len(ps), size=min(k, int((wo > 0).sum())), replace=False, p=wo / wo.sum()); pa[s, idx] += 1
            for j, r in enumerate(ps):
                goal = pg[:, j] >= 1; pt = (pg[:, j] + pa[:, j]) >= 1; av = avail[:, j]
                res.append({"name": r["name"], "team": t, "opp": opp, "game": f"{a}@{h}", "p_team_win": round(float(win.mean()), 3),
                            "p_goal_sim": round(float(goal.mean()), 4), "p_goal_board": r["p_goal"] * r["p_play"], "p_point_sim": round(float(pt.mean()), 4), "p_points_board": (r["p_points"] or 0) * r["p_play"],
                            "p_goal_and_win": round(float((goal & win).mean()), 4), "p_goal_given_win": round(float(goal[win].mean()), 4) if win.any() else None,
                            "p_goal_given_loss": round(float(goal[~win].mean()), 4) if (~win).any() else None, "p_2plus_goals": round(float((pg[:, j] >= 2).mean()), 4)})
    return res


def sim_nba(day, sims, rng):
    board = json.load(open(HERE / "outputs" / "nba_board_latest.json"))
    games = team_inputs("nba", day)
    by_team = defaultdict(list)
    for r in board["players"]:
        by_team[r["team"]].append(r)
    res = []
    for (h, a), g in games.items():
        bt = g.get("book_total"); total = float(bt) if bt else LG_TOTAL["nba"]
        sd_margin = 12.0
        margin = -g["model_spread_home"]                                   # expected home margin from the team model
        mu = {h: (total + margin) / 2, a: (total - margin) / 2}
        z = rng.standard_normal(sims); e = rng.standard_normal(sims)       # game total noise and margin noise
        T = {h: mu[h] + 7.5 * z * 0.5 + 0.5 * sd_margin * e, a: mu[a] + 7.5 * z * 0.5 - 0.5 * sd_margin * e}
        win = T[h] > T[a]
        for t, opp in ((h, a), (a, h)):
            ps = by_team.get(t, [])
            if not ps:
                continue
            m = np.array([0.5 * (r["ppg"] or 0) + 0.5 * (r["l10_ppg"] or r["ppg"] or 0) for r in ps])
            avail = rng.random((sims, len(ps))) < np.array([r["p_play"] for r in ps])
            # player spread chosen so his simulated P(25+) equals the board's own probability (the fitted lane is the marginal); the team result enters through T / mu
            from statistics import NormalDist
            sdj, mj = [], []
            for mj_, r in zip(m, ps):
                p = min(0.995, max(0.005, r["p_25"])); zq = NormalDist().inv_cdf(1 - p)
                if abs(zq) > 0.05 and (24.5 - mj_) / zq > 0 and 2.0 <= (24.5 - mj_) / zq <= 14.0:
                    sdj.append((24.5 - mj_) / zq); mj.append(mj_)                 # his average and the board agree: keep his average, fit the spread
                else:
                    sd_ = 0.30 * mj_ + 2.0; sdj.append(sd_); mj.append(max(0.0, 24.5 - sd_ * zq))   # the fitted lane sees something his average does not (matchup, minutes, a blowout): move the mean
            sdj = np.array(sdj); mj = np.array(mj)
            ratio = float((m * np.array([r["p_play"] for r in ps])).sum() / (0.85 * mu[t]))     # roster points vs the team's expected points: far from 1 = a stale / incomplete roster on the board
            pts = np.maximum(0, (mj[None, :] * (T[t][:, None] / mu[t])) + sdj[None, :] * rng.standard_normal((sims, len(ps)))) * avail
            for j, r in enumerate(ps):
                p25 = pts[:, j] >= 25
                res.append({"name": r["name"], "team": t, "opp": opp, "game": f"{a}@{h}", "p_team_win": round(float(win.mean() if t == h else 1 - win.mean()), 3),
                            "p_25_sim": round(float(p25.mean()), 4), "p_25_board": round(r["p_25"] * r["p_play"], 4), "p_25_and_win": round(float((p25 & (win if t == h else ~win)).mean()), 4),
                            "exp_pts_sim": round(float(pts[:, j].mean()), 1), "roster_vs_team_points": round(ratio, 2)})
    return res


def snapshot(sport, day, out_dir, sims=20000):
    """Pregame record: only games already saved (and still unstarted) in shadow_team_<sport>_<day>.json, written once."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = Path(out_dir) / f"sim_{sport}_{day}.json"
    team_f = Path(out_dir) / f"shadow_team_{sport}_{day}.json"
    if not team_f.exists():
        print(f"{day} sim {sport}: no team snapshot yet"); return
    have = json.load(open(path)) if path.exists() else {"date": day, "sport": sport, "sims": sims, "players": []}
    done = {r["game"] for r in have["players"]}
    keep = set(json.load(open(team_f))["games"]) - done                  # games saved pregame by team_live and not simulated yet; a game already in the file is never redone
    if not keep:
        print(f"{day} sim {sport}: nothing new"); return
    import subprocess
    subprocess.run([sys.executable, str(HERE / f"{sport}_live.py"), day], check=True, capture_output=True)      # fresh player board for the day (outputs/<sport>_board_latest.json)
    rows = [r for r in (sim_nhl if sport == "nhl" else sim_nba)(day, sims, np.random.default_rng(7)) if r["game"] in keep]
    have["players"] += rows
    path.write_text(json.dumps(have))
    print(f"{day} sim {sport}: +{len(rows)} players, {len(have['players'])} in file")


def report(out_dir):
    """Join each saved simulation with what happened (player actuals from shadow_<sport>_<day>.json, results from shadow_team_<sport>_<day>.json)."""
    for sport, key, akey in (("nhl", "p_goal", "goal"), ("nba", "p_25", "pts25")):
        rows = []
        for f in sorted(Path(out_dir).glob(f"sim_{sport}_*.json")):
            d = json.load(open(f))
            lane = Path(out_dir) / f"shadow_{sport}_{d['date']}.json"; team = Path(out_dir) / f"shadow_team_{sport}_{d['date']}.json"
            if not lane.exists() or not team.exists():
                continue
            act = {r["name"]: r.get("actual", {}).get(akey) for r in json.load(open(lane))["rows"]}
            res = json.load(open(team))["games"]
            for r in d["players"]:
                g = res.get(r["game"]); y = act.get(r["name"])
                if y is None or not g or g.get("home_margin") is None:
                    continue
                home = r["game"].split("@")[1] == r["team"]
                won = (g["home_margin"] > 0) == home
                rows.append((r[f"{key}_sim"], r[f"{key}_and_win"], r["p_team_win"], y, int(won)))
        if len(rows) < 100:
            print(f"  {sport.upper()} sim: {len(rows)} graded player-games (too few to read)"); continue
        a = np.array(rows, float)
        print(f"  {sport.upper()} sim, {len(a)} graded player-games: player lane predicted {100 * a[:, 0].mean():.1f}% vs actual {100 * a[:, 3].mean():.1f}%; "
              f"team win predicted {100 * a[:, 2].mean():.1f}% vs actual {100 * a[:, 4].mean():.1f}%; scores AND wins predicted {100 * a[:, 1].mean():.1f}% vs actual {100 * (a[:, 3] * a[:, 4]).mean():.1f}%")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("sport", choices=["nhl", "nba", "report", "snapshot"]); ap.add_argument("day", nargs="?"); ap.add_argument("extra", nargs="?"); ap.add_argument("--sims", type=int, default=20000); ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.sport == "report":
        return report(a.day)
    if a.sport == "snapshot":
        return snapshot(a.day, a.extra, sys.argv[4] if len(sys.argv) > 4 else ".")
    rng = np.random.default_rng(7)
    rows = (sim_nhl if a.sport == "nhl" else sim_nba)(a.day, a.sims, rng)
    out = Path(a.out) if a.out else HERE / "outputs" / f"{a.sport}_sim_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps({"date": a.day, "sims": a.sims, "players": rows}, indent=1))
    key = "p_goal" if a.sport == "nhl" else "p_25"
    d = [abs(r[f"{key}_sim"] - r[f"{key}_board"]) for r in rows]
    print(f"{len(rows)} players in {len({r['game'] for r in rows})} games, {a.sims} sims each -> {out}")
    print(f"coherence: mean |sim - board| {100 * np.mean(d):.1f} pts on {key}; sim mean {100 * np.mean([r[f'{key}_sim'] for r in rows]):.1f}% vs board mean {100 * np.mean([r[f'{key}_board'] for r in rows]):.1f}%")
    top = sorted(rows, key=lambda r: -r[f"{key}_sim"])[:8]
    for r in top:
        extra = f"  P(win) {100 * r['p_team_win']:.0f}%  P(scores AND wins) {100 * (r.get('p_goal_and_win') or r.get('p_25_and_win')):.1f}%"
        print(f"  {r['name']:<22}{r['team']:<5}sim {100 * r[f'{key}_sim']:5.1f}%  board {100 * r[f'{key}_board']:5.1f}%{extra}")


if __name__ == "__main__":
    main()
