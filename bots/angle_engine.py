"""
angle_engine.py -- one way to test an angle, for every sport.

An "angle" is a yes/no tag on a player-game that is known BEFORE the game (rested, hot,
facing a leaky goalie, home...). It is tested against the same group of players WITHOUT the tag,
using each player's outcome minus HIS OWN usual, so a star is not credited for being a star.

Because dozens of angles get tested, some will look real by luck. Three guards:

  1. Benjamini-Hochberg false-discovery control across every angle tested in the run.
  2. Replication: the effect must point the same way in EVERY season and clear |t| 1.5 in each.
  3. Size: it has to move the outcome by a meaningful amount, not just be statistically detectable
     in a big sample (`min_effect`, in the outcome's own units).

Verdicts
  SIGNAL  passes all three         -> safe to move a ranking
  LEAN    t >= 2 pooled but fails replication or the false-discovery bar -> watch it, don't act
  NOISE   everything else
"""
import math
from collections import defaultdict
from typing import Callable, Dict, List


def _mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def _var(xs):
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)


def welch(a: List[float], b: List[float]):
    """difference in means (a - b) and its t-score."""
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan")
    d = _mean(a) - _mean(b)
    se = math.sqrt(_var(a) / len(a) + _var(b) / len(b))
    return d, (d / se if se else float("nan"))


def p_from_t(t: float) -> float:
    """two-sided p, normal approximation (fine: every sample here is in the hundreds+)."""
    return math.erfc(abs(t) / math.sqrt(2)) if t == t else 1.0


def bh_threshold(pvals: List[float], q: float = 0.05) -> float:
    """largest p that survives Benjamini-Hochberg at false-discovery rate q."""
    ps = sorted(pvals)
    m = len(ps)
    cut = 0.0
    for i, p in enumerate(ps, 1):
        if p <= q * i / m:
            cut = p
    return cut


def evaluate(rows: List[Dict], tags: List[str], metric: str, min_effect: float, min_n: int = 100,
             season_key: str = "season") -> List[Dict]:
    """rows: dicts with row['tags'][name] -> bool, row[metric] -> float, row[season_key].
    Returns one result per tag, with a verdict."""
    out = []
    seasons = sorted({r[season_key] for r in rows})
    for name in tags:
        w = [r for r in rows if r["tags"].get(name)]
        wo = [r for r in rows if not r["tags"].get(name)]
        if len(w) < min_n or len(wo) < min_n:
            continue
        d, t = welch([r[metric] for r in w], [r[metric] for r in wo])
        per = []
        for s in seasons:
            ds, ts = welch([r[metric] for r in w if r[season_key] == s], [r[metric] for r in wo if r[season_key] == s])
            per.append((ds, ts))
        out.append({"angle": name, "n": len(w), "effect": d, "t": t, "p": p_from_t(t), "per_season": per})
    cut = bh_threshold([o["p"] for o in out])
    for o in out:
        same_way = all(ds == ds and (ds > 0) == (o["effect"] > 0) and abs(ts) >= 1.5 for ds, ts in o["per_season"])
        survives = o["p"] <= cut and cut > 0
        big = abs(o["effect"]) >= min_effect
        if survives and same_way and big:
            o["verdict"] = "SIGNAL"
        elif abs(o["t"]) >= 2:
            o["verdict"] = "LEAN"
        else:
            o["verdict"] = "NOISE"
        o["why"] = ", ".join(x for x, ok in (("fails false-discovery bar", not survives),
                                            ("does not replicate in every season", not same_way),
                                            ("effect too small to act on", not big)) if ok and o["verdict"] != "SIGNAL")
    return sorted(out, key=lambda o: ({"SIGNAL": 0, "LEAN": 1, "NOISE": 2}[o["verdict"]], -abs(o["t"])))


def print_table(results: List[Dict], unit: str):
    print(f"   {'angle':<46}{'n':>6}{'effect':>9}{'t':>6}   by season (effect/t)      verdict")
    for o in results:
        seas = "  ".join(f"{ds:+.2f}/{ts:.1f}" for ds, ts in o["per_season"])
        print(f"   {o['angle']:<46}{o['n']:>6}{o['effect']:>+8.2f}{unit[:1]}{o['t']:>6.1f}   {seas:<38}{o['verdict']}"
              + (f"  ({o['why']})" if o["why"] else ""))
