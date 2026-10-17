#!/usr/bin/env python3
"""nfl_shadow_report.py -- live, pregame comparison of the published TD score against every logged shadow.

    python bots/nfl/nfl_shadow_report.py --season 2026 --weeks 6 7 8 --dir <dir with nfl_prediction_log_*.jsonl>

READS ONLY. For every player the log's LAST pregame TD line (generated before his team's kickoff, the same rule
nfl_regrade / nfl_td_v3_backtest use) carries the published score (`score`, model nfl_td_v2) and, in `shadow`, each
shadow version's score (nfl_td_v3, nfl_td_reweight_v1, nfl_td_rzonly_v1). A player with no stat line is a void,
not a miss. Per week and pooled it prints, for each version: top-5 and top-15 hits (card rule: low-sample rows out)
and the within-week AUC, then the paired week-bootstrap difference in AUC against the published score.

THE PROMOTION RULE (fixed here before any week is graded, 2026-10-13): a shadow is eligible to be PROPOSED for
promotion only after at least MIN_WEEKS graded weeks, when the 95% week-bootstrap interval of (shadow AUC minus
published AUC) lies above zero AND its pooled top-15 hit rate is not lower. Meeting the rule proposes; it never
promotes. Scores are rankings, not probabilities, so the rule is on ranking quality.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os

import numpy as np

MIN_WEEKS = 8
PUBLISHED = "published"


def pregame_rows(lines_by_file: list[list[dict]], kickoffs: dict, week: int) -> dict:
    """{player_id: TD line} -- the latest line generated before the player's team kicked off, for `week`."""
    best: dict = {}
    for body in lines_by_file:
        if not body:
            continue
        h = body[0]
        if h.get("mode") != "week" or int(h.get("week") or -1) != week:
            continue
        gen = dt.datetime.fromisoformat(str(h["generated_at"]).replace("Z", "+00:00"))
        for r in body[1:]:
            k = kickoffs.get(str(r.get("team")))
            if r.get("market") != "TD" or not isinstance(r.get("score"), (int, float)) or k is None or gen >= k:
                continue
            if r["player_id"] not in best or gen > best[r["player_id"]][0]:
                best[r["player_id"]] = (gen, r)
    return {pid: r for pid, (_, r) in best.items()}


def versions_in(rows: dict) -> list[str]:
    out: list[str] = []
    for r in rows.values():
        for v in (r.get("shadow") or {}):
            if v not in out:
                out.append(v)
    return out


def _score(r: dict, version: str):
    if version == PUBLISHED:
        return r["score"]
    s = (r.get("shadow") or {}).get(version) or {}
    return s.get("score") if isinstance(s.get("score"), (int, float)) else None


def week_metrics(rows: dict, outcome: dict, version: str) -> dict | None:
    """Top-5 / top-15 hits (low-sample rows out, voids out) and the AUC numerator parts for one week and one version."""
    pool = [(pid, r) for pid, r in rows.items() if not r.get("low_sample") and pid in outcome and _score(r, version) is not None]
    if len(pool) < 20:
        return None
    pool.sort(key=lambda t: (-_score(t[1], version), t[0]))
    y = np.array([1 if outcome[p] >= 1 else 0 for p, _ in pool])
    s = np.array([_score(r, version) for _, r in pool], float)
    pos, neg = int(y.sum()), int((1 - y).sum())
    ranks = (np.argsort(np.argsort(s)) + 1).astype(float)          # ties are rare on 0-100 scores; average-rank below
    for v in np.unique(s):
        m = s == v
        if m.sum() > 1:
            ranks[m] = ranks[m].mean()
    auc_num = float(ranks[y == 1].sum() - pos * (pos + 1) / 2) if pos else 0.0
    return {"n": len(pool), "top5": int(y[:5].sum()), "top15": int(y[:15].sum()), "auc_num": auc_num, "auc_den": float(pos * neg)}


def pooled(weekly: list[dict]) -> dict:
    n = sum(1 for w in weekly if w)
    t5 = sum(w["top5"] for w in weekly if w); t15 = sum(w["top15"] for w in weekly if w)
    den = sum(w["auc_den"] for w in weekly if w); num = sum(w["auc_num"] for w in weekly if w)
    k5 = 5 * n; k15 = 15 * n
    return {"weeks": n, "top5": t5, "top5_n": k5, "top15": t15, "top15_n": k15, "auc": num / den if den else float("nan")}


def auc_diff_ci(weekly_a: list, weekly_b: list, n_boot: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    """Week-bootstrap interval of (AUC a - AUC b); each week is resampled whole."""
    idx = [i for i, (a, b) in enumerate(zip(weekly_a, weekly_b)) if a and b]
    if not idx:
        return float("nan"), float("nan"), float("nan")
    def auc(ws, ii):
        den = sum(ws[i]["auc_den"] for i in ii); num = sum(ws[i]["auc_num"] for i in ii)
        return num / den if den else float("nan")
    point = auc(weekly_a, idx) - auc(weekly_b, idx)
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(n_boot):
        pick = [idx[j] for j in rng.integers(0, len(idx), len(idx))]
        stats.append(auc(weekly_a, pick) - auc(weekly_b, pick))
    lo, hi = np.nanpercentile(stats, [2.5, 97.5])
    return float(point), float(lo), float(hi)


def verdict(weeks: int, lo: float, top15_shadow: int, top15_published: int) -> str:
    if weeks < MIN_WEEKS:
        return f"too early ({weeks} of {MIN_WEEKS} graded weeks)"
    if lo > 0 and top15_shadow >= top15_published:
        return "MEETS THE RULE: may be proposed for promotion (needs approval)"
    return "does not meet the rule"


def report(weeks_rows: dict[int, dict], outcomes: dict[int, dict]) -> dict:
    """weeks_rows {week: {pid: line}}, outcomes {week: {pid: TDs}} -> per-version weekly metrics and the verdicts."""
    weeks = sorted(weeks_rows)
    versions = [PUBLISHED] + [v for v in dict.fromkeys(v for w in weeks for v in versions_in(weeks_rows[w]))]
    weekly = {v: [week_metrics(weeks_rows[w], outcomes.get(w, {}), v) for w in weeks] for v in versions}
    out = {"weeks": weeks, "versions": {}}
    base = pooled(weekly[PUBLISHED])
    for v in versions:
        p = pooled(weekly[v])
        entry = {"pooled": p}
        if v != PUBLISHED:
            d, lo, hi = auc_diff_ci(weekly[v], weekly[PUBLISHED])
            entry.update({"auc_diff": d, "lo": lo, "hi": hi, "verdict": verdict(p["weeks"], lo, p["top15"], base["top15"])})
        out["versions"][v] = entry
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--weeks", type=int, nargs="+", required=True)
    ap.add_argument("--dir", required=True, help="directory holding nfl_prediction_log_<season>-wkNN.*.jsonl")
    a = ap.parse_args()
    import nflreadpy as nfl
    import polars as pl
    from nfl_regrade import kickoffs
    stats = nfl.load_player_stats(seasons=[a.season], summary_level="week")
    rows, outs = {}, {}
    for wk in a.weeks:
        files = sorted(glob.glob(os.path.join(a.dir, f"nfl_prediction_log_{a.season}-wk{wk:02d}.*.jsonl")))
        bodies = [[json.loads(x) for x in open(f).read().splitlines() if x.strip()] for f in files]
        rows[wk] = pregame_rows(bodies, kickoffs(a.season, wk), wk)
        outs[wk] = {r["player_id"]: (r["rushing_tds"] or 0) + (r["receiving_tds"] or 0)
                    for r in stats.filter(pl.col("week") == wk).iter_rows(named=True)}
    res = report(rows, outs)
    print(f"weeks graded: {res['weeks']}  (rule needs {MIN_WEEKS})")
    for v, e in res["versions"].items():
        p = e["pooled"]
        line = f"  {v:24} top-5 {p['top5']}/{p['top5_n']}  top-15 {p['top15']}/{p['top15_n']}  AUC {p['auc']:.4f}"
        if "auc_diff" in e:
            line += f"  dAUC {e['auc_diff']:+.4f} [{e['lo']:+.4f}, {e['hi']:+.4f}]  -> {e['verdict']}"
        print(line)


if __name__ == "__main__":
    main()
