#!/usr/bin/env python3
"""MLB Moonshot: what the PUBLISHED graded ledger can and cannot say (baseline audit, not a backtest).

    python model_lab/mlb_ledger_audit.py <dir with graded_results_YYYY-MM-DD.json files from the data branch>

graded_results_<date>.json['results'] holds the SLOTS the product published that day (about 20), each with its
locked pregame hr_score and the graded outcome (got_hr). That is a SELECTED sample (players the board chose), so
the hit rates below are conditional on being picked: they say how the called slots did, they cannot say how the
whole board ranks, and they cannot support a model comparison. A full-board MLB backtest needs every rated player
on every slate joined to a box score (prediction_log_*.jsonl x boxscores); the box scores are not reachable from
this environment, so that experiment is NOT run here and nothing is imputed for it.
"""
import glob, json, os, sys, collections
import numpy as np

def auc(y, s):
    y = np.asarray(y); s = np.asarray(s, float)
    pos, neg = s[y == 1], s[y == 0]
    if not len(pos) or not len(neg): return float("nan")
    ranks = np.argsort(np.argsort(np.concatenate([pos, neg]))) + 1
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))

def main(d):
    rows, days, mv, ch = [], 0, collections.Counter(), collections.Counter()
    for f in sorted(glob.glob(os.path.join(d, "graded_results_*.json"))):
        j = json.load(open(f)); days += 1
        # older files are a bare list of result rows; newer ones are a dict with `results`
        date = os.path.basename(f)[len("graded_results_"):-len(".json")]
        res = j if isinstance(j, list) else (j.get("results") or [])
        for r in res:
            if r.get("is_final") != 1 or r.get("fair_test_void") == 1 or r.get("got_hr") is None or r.get("hr_score") is None:
                continue
            if r.get("feature_snapshot") != "locked":
                continue                                    # only the pregame-locked rows count as predictions
            rows.append((date, float(r["hr_score"]), int(r["got_hr"]), r.get("board_rank"), r.get("pick_type")))
            mv[r.get("model_version")] += 1; ch[(r.get("config_hash") or "")[:19]] += 1
    y = np.array([r[2] for r in rows]); s = np.array([r[1] for r in rows])
    print(f"days {days}  locked graded slot rows {len(rows)}  HR rate {y.mean():.3f}  AUC(hr_score) among picked rows {auc(y, s):.3f}")
    print("model versions:", dict(mv)); print("config hashes:", dict(ch))
    edges = np.quantile(s, [0, .25, .5, .75, 1.0])
    for i in range(4):
        m = (s >= edges[i]) & ((s <= edges[i + 1]) if i == 3 else (s < edges[i + 1]))
        se = np.sqrt(y[m].mean() * (1 - y[m].mean()) / m.sum())
        print(f"  hr_score quartile {i+1} [{edges[i]:.1f},{edges[i+1]:.1f}]  n={m.sum():4}  HR {y[m].mean()*100:5.1f}% ± {1.96*se*100:.1f}")
    byp = collections.defaultdict(list)
    for r in rows: byp[r[4]].append(r[2])
    for k, v in sorted(byp.items(), key=lambda kv: str(kv[0])): print(f"  pick_type {k}: n={len(v)} HR {np.mean(v)*100:.1f}%")
    json.dump({"days": days, "n": len(rows), "hr_rate": float(y.mean()), "auc_picked": auc(y, s)}, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "mlb_ledger_audit.json"), "w"))

if __name__ == "__main__":
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results"), exist_ok=True)
    main(sys.argv[1])
