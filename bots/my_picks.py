#!/usr/bin/env python3
"""
my_picks.py -- YOUR picks, logged and graded, so your style gets measured against the model and the prices.

    python bots/my_picks.py report PICKS_CSV SHADOW_DIR            grade every pick it can against the shadow logs and print the report
    python bots/my_picks.py template                                print an example picks file

PICKS_CSV (my_picks/picks.csv in the repo; edit it on GitHub, no computer needed). One line per pick, header row required:
    date,sport,player,market,line,price,note
    2026-10-25,nfl,Michael Wilson,td,+245,numerology
    2026-10-25,nfl,Mark Andrews,td,+260,tight end day
    2026-10-24,nhl,Kirill Kaprizov,goal,-110,
    2026-10-24,nba,Luka Doncic,pts25,,
market: nfl td | first_td | rec_yds | rec | rush_yds | rush_att | pass_yds | pass_td | td2 | long_rec | qb_td ... ;  nhl goal | points | assists | shots3 ;  nba pts25 | reb10 | ast8 | tpm4 | pra35 | dd.
price is the American price you took (blank = unpriced). A pick is graded only if the player is in that day's shadow log with a result (a player who did not play is not graded as a miss).
The report: hit rate and the return at your prices, against what our model gave each pick (so you can see whether your picks beat the model), split by market, position, price band and note.
"""
import csv, json, sys, unicodedata, re
from collections import defaultdict
from pathlib import Path

NFL_MARKET = {"td": ("p_td_fit", "td"), "first_td": ("p_first_td", "first_td")}


def key(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", re.sub(r"[^a-z ]", "", s)).split())


def profit(price, won):
    if price is None:
        return None
    return (price / 100.0 if price > 0 else 100.0 / -price) if won else -1.0


def load_nfl(shadow_dir):
    """[(player key, kickoff date, row)] from every NFL weekly shadow file."""
    out = []
    for f in sorted(Path(shadow_dir).glob("shadow_nfl_*.json")):
        d = json.load(open(f))
        for tm, g in d["teams"].items():
            ko = (g.get("kickoff") or "")[:10]
            for r in g["rows"]:
                out.append((key(r["name"]), ko, r))
    return out


def days_apart(a, b):
    from datetime import date
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except ValueError:
        return 99


def grade_nfl(p, idx):
    k = key(p["player"])
    cands = [(days_apart(ko, p["date"]), r) for kk, ko, r in idx if kk == k and days_apart(ko, p["date"]) <= 3]
    if not cands:
        return None
    r = sorted(cands, key=lambda c: c[0])[0][1]
    m = p["market"]
    if p.get("line") and m != "first_td":                       # a line was taken (e.g. rec_yds 63.5): graded on the raw stat, a push is no result
        stat = (r.get("stats") or {}).get(m)
        if stat is None:
            return None
        ln = float(p["line"])
        if stat == ln:
            return None
        return {"prob": None, "won": stat > ln, "pos": r.get("pos")}
    if m in NFL_MARKET:
        prob_key, res_key = NFL_MARKET[m]
        prob, y = r.get(prob_key), r.get(res_key)
    else:
        prob, y = (r.get("p_lane") or {}).get(m), (r.get("lane_y") or {}).get(m)
    if y is None:
        return None
    return {"prob": prob, "won": bool(y), "pos": r.get("pos")}


def grade_other(p, shadow_dir):
    f = Path(shadow_dir) / f"shadow_{p['sport']}_{p['date']}.json"
    if not f.exists():
        return None
    for r in json.load(open(f))["rows"]:
        if key(r["name"]) == key(p["player"]):
            a = r.get("actual")
            if not a or p["market"] not in a:
                return None
            return {"prob": (r.get("p") or {}).get(p["market"]), "won": bool(a[p["market"]]), "pos": r.get("pos")}
    return None


def report(csv_path, shadow_dir):
    picks = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(open(csv_path))]
    idx = load_nfl(shadow_dir)
    graded, pending = [], []
    for p in picks:
        if not p.get("player"):
            continue
        try:
            price = int(p["price"].replace("+", "")) if p.get("price") else None
        except ValueError:
            price = None
        p["price_i"] = price
        g = grade_nfl(p, idx) if p["sport"] == "nfl" else grade_other(p, shadow_dir)
        if g is None:
            pending.append(p); continue
        graded.append({**p, **g})
    print(f"{len(picks)} picks in the file; {len(graded)} graded, {len(pending)} not graded yet (game not final, player not in the shadow log, or no result)")
    if not graded:
        return
    def line(label, rs):
        n = len(rs); w = sum(r["won"] for r in rs)
        pr = [r["prob"] for r in rs if r.get("prob") is not None]
        priced = [r for r in rs if r["price_i"] is not None]
        imp = [100 / (r["price_i"] + 100) if r["price_i"] > 0 else -r["price_i"] / (-r["price_i"] + 100) for r in priced]
        roi = f"{100 * sum(profit(r['price_i'], r['won']) for r in priced) / len(priced):+.0f}% on {len(priced)} priced" if priced else "no prices"
        print(f"  {label:<26}{n:>4} picks, hit {100 * w / n:5.1f}%   our model gave them {100 * sum(pr) / len(pr):5.1f}%   price implied {100 * sum(imp) / len(imp):5.1f}%   return {roi}" if pr and imp else
              f"  {label:<26}{n:>4} picks, hit {100 * w / n:5.1f}%   model {('%.1f%%' % (100 * sum(pr) / len(pr))) if pr else 'n/a'}   return {roi}")
    print("\nOverall"); line("all", graded)
    for title, keyf in (("By market", lambda r: f"{r['sport']} {r['market']}"), ("By position", lambda r: r.get("pos") or "?"), ("By note", lambda r: r.get("note") or "(none)"),
                        ("By price band", lambda r: "unpriced" if r["price_i"] is None else "favourite (-110 or shorter)" if r["price_i"] <= -110 else "even to +250" if r["price_i"] <= 250 else "long shot (+251 and up)")):
        groups = defaultdict(list)
        for r in graded:
            groups[keyf(r)].append(r)
        print(f"\n{title}")
        for k in sorted(groups, key=lambda k: -len(groups[k])):
            line(k, groups[k])
    better = [r for r in graded if r.get("prob") is not None]
    if better:
        hi = [r for r in better if r["prob"] >= sum(x["prob"] for x in better) / len(better)]
        lo = [r for r in better if r not in hi]
        print(f"\nDid the model's own ranking of your picks hold? Picks the model liked more: hit {100 * sum(r['won'] for r in hi) / len(hi):.1f}% (n={len(hi)}); liked less: {100 * sum(r['won'] for r in lo) / max(1, len(lo)):.1f}% (n={len(lo)})")


if __name__ == "__main__":
    if sys.argv[1] == "template":
        print("date,sport,player,market,line,price,note\n2026-10-25,nfl,Michael Wilson,td,,+245,wide receiver\n2026-10-25,nfl,Garrett Wilson,rec,5.5,-115,pinned\n2026-10-24,nhl,Kirill Kaprizov,goal,,-110,")
    else:
        report(sys.argv[2], sys.argv[3])
