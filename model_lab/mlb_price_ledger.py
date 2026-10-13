#!/usr/bin/env python3
"""MLB home runs: what prices the published picks sat at, and how they did against those prices.

    python model_lab/mlb_price_ledger.py <graded_results dir> <odds_YYYY-MM-DD.json dir>

Joins the data branch's graded_results_<date>.json (locked, graded, published slot rows with got_hr) to the day's
pre-game price snapshot odds_<date>.json (source oddsapiio, rows[player_id]['batter_home_runs'] = [line, American over
price, implied %]). One row per (date, player) however many slots he held. Only PLUS-MONEY HR prices are used.
Limits, stated: PICKED players only (a selected sample); 34 days that have both files; the implied % includes the
book's margin, so "hit minus implied" understates any edge; the price is the snapshot's, not necessarily the price
you could have bet; bands slice the same 999 rows four ways (no correction applied).
Interval: 95% bootstrap resampling whole DAYS.
"""
import collections, glob, json, os, random, statistics, sys

BANDS = [(100, 299, "+100 to +299"), (300, 499, "+300 to +499"), (500, 799, "+500 to +799"), (800, 9999, "+800 and up"), (100, 9999, "ALL plus money")]


def join(graded_dir, odds_dir):
    rows = {}
    for f in sorted(glob.glob(os.path.join(odds_dir, "odds_*.json"))):
        date = os.path.basename(f)[5:15]
        gp = os.path.join(graded_dir, f"graded_results_{date}.json")
        if not os.path.exists(gp):
            continue
        od = json.load(open(f))["rows"]; gj = json.load(open(gp)); res = gj if isinstance(gj, list) else gj.get("results") or []
        for r in res:
            if r.get("is_final") != 1 or r.get("feature_snapshot") != "locked" or r.get("got_hr") is None or r.get("fair_test_void") == 1:
                continue
            m = od.get(str(r.get("player_id")), {}).get("batter_home_runs")
            if m and m[1] >= 100:
                rows.setdefault((date, str(r["player_id"])), (m[1], int(r["got_hr"]), m[2]))
    return rows


def profit(price, hit):
    return price / 100.0 if hit else -1.0


def boot_ci(sel, B=3000, seed=7):
    days = collections.defaultdict(list)
    for (d, _), v in sel:
        days[d].append(v)
    keys = list(days); rng = random.Random(seed); out = []
    for _ in range(B):
        tot = n = 0
        for _ in keys:
            for v in days[rng.choice(keys)]:
                tot += profit(v[0], v[1]); n += 1
        out.append(tot / n)
    out.sort()
    return out[int(.025 * B)], out[int(.975 * B)]


def main(graded_dir, odds_dir):
    rows = join(graded_dir, odds_dir); vals = list(rows.items())
    print(f"unique player-days {len(vals)} over {len({k[0] for k, _ in vals})} days")
    print(f"{'band':16}{'n':>5}{'hit':>8}{'implied':>9}{'profit/bet':>12}   95% CI")
    out = {}
    for lo, hi, name in BANDS:
        sel = [kv for kv in vals if lo <= kv[1][0] <= hi]
        n = len(sel); h = sum(v[1] for _, v in sel); pr = sum(profit(v[0], v[1]) for _, v in sel); imp = sum(v[2] for _, v in sel) / 100
        l, u = boot_ci(sel)
        out[name] = {"n": n, "hits": h, "hit_rate": h / n, "implied": imp / n, "profit_per_bet": pr / n, "ci": [l, u]}
        print(f"{name:16}{n:>5}{h / n * 100:>7.1f}%{imp / n * 100:>8.1f}%{pr / n:>+12.3f}   [{l:+.2f}, {u:+.2f}]  hits {h}")
    p = sorted(v[0] for _, v in vals)
    out["price_range"] = {"median": statistics.median(p), "p10": p[int(.1 * len(p))], "p90": p[int(.9 * len(p))], "min": p[0], "max": p[-1]}
    print("price range of the picks (American):", out["price_range"])
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results"), exist_ok=True)
    json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "mlb_price_ledger.json"), "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
