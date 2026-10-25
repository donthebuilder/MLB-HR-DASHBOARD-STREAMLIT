"""lane_fit.py -- calibrated probabilities for the NFL yardage / volume / kicking / passing-TD markets (models/nfl_<market>_fit_v1.json from nfl_lanes.py --save).
score_all(slate) -> {market: {player_id: probability}}; a price check beside the hand scores, never a rank."""
import json
import math
from pathlib import Path

import polars as pl

MODELS = Path(__file__).resolve().parent.parent / "models"
MARKETS = ["rec_yds", "rec", "rush_yds", "rush_att", "pass_yds", "kick_pts", "pass_td", "td2", "qb_td", "pass_comp", "pass_int", "long_rec"]
OUTCOMES = {"rec_yds": lambda r: (r.get("receiving_yards") or 0) >= 100, "rec": lambda r: (r.get("receptions") or 0) >= 4,
            "rush_yds": lambda r: (r.get("rushing_yards") or 0) >= 100, "rush_att": lambda r: (r.get("carries") or 0) >= 12,
            "pass_yds": lambda r: (r.get("passing_yards") or 0) >= 300, "pass_td": lambda r: (r.get("passing_tds") or 0) >= 2,
            "kick_pts": lambda r: (r.get("fg_made") or 0) * 3 + (r.get("pat_made") or 0) >= 6,
            "td2": lambda r: (r.get("rushing_tds") or 0) + (r.get("receiving_tds") or 0) >= 2, "qb_td": lambda r: (r.get("rushing_tds") or 0) >= 1,
            "pass_comp": lambda r: (r.get("completions") or 0) >= 23, "pass_int": lambda r: (r.get("passing_interceptions") or 0) >= 1,
            "long_rec": lambda r: (r.get("longest_rec") or 0) >= 25}


def score_all(slate, cache=None):
    import nfl_scoring as S
    d = S.derive(slate)
    if cache:                                   # trailing interceptions / sacks / longest reception are not in the weekly slate table: add them from the game history
        import nfl_more
        ex = nfl_more.live_extras(cache)
        d = d.with_columns([pl.col("player_id").map_elements(lambda p, k=k: (ex.get(p) or {}).get(k, 0.0), return_dtype=pl.Float64).alias(k) for k in ("f_int", "f_sacks", "f_longest")])
    out = {}
    for m in MARKETS:
        fp = MODELS / f"nfl_{m}_fit_v1.json"
        if not fp.exists():
            continue
        mod = json.load(open(fp)); pl_ = mod["platt"]; t = pl_["tail"]
        res = {}
        for r in d.filter(pl.col("position").is_in(mod["positions"])).iter_rows(named=True):
            z = mod["intercept"]
            for n, w, mu, sd in zip(mod["features"], mod["weights"], mod["mu"], mod["sd"]):
                v = (1.0 if r.get("position") == n else 0.0) if n in ("QB", "RB", "WR", "TE", "K") else float(r.get(n) or 0.0)
                z += w * (v - mu) / sd
            q = 1 / (1 + math.exp(-(pl_["a"] + pl_["b"] * z)))
            res[str(r["player_id"])] = round(t["t"] + (q - t["t"]) * t["slope"] if q > t["t"] else q, 4)
        out[m] = res
    return out
