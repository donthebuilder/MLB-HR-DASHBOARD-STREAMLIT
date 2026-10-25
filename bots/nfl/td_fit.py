"""td_fit.py -- calibrated anytime-TD probability from the fitted model (models/nfl_td_fit_v1.json, made by nfl_tuned_td.py --save).
score(tbl) -> {player_id: probability}. A shadow price check beside nfl_td_v2; it never ranks the live board (v2 picks the top better)."""
import json
import math
from pathlib import Path

MODEL = Path(__file__).resolve().parent.parent / "models" / "nfl_td_fit_v1.json"


def score(tbl):
    m = json.load(open(MODEL))
    pl_ = m["platt"]
    out = {}
    for r in tbl.iter_rows(named=True):
        z = m["intercept"]
        for n, w, mu, sd in zip(m["features"], m["weights"], m["mu"], m["sd"]):
            v = (1.0 if r.get("position") == n else 0.0) if n in ("QB", "RB", "WR", "TE") else float(r.get(n) or 0.0)
            z += w * (v - mu) / sd
        q = 1 / (1 + math.exp(-(pl_["a"] + pl_["b"] * z)))
        t = pl_["tail"]
        out[str(r["player_id"])] = round(t["t"] + (q - t["t"]) * t["slope"] if q > t["t"] else q, 4)
    return out
