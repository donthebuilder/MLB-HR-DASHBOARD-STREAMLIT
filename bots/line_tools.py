"""line_tools.py -- shared helpers for the prop-line snapshots (nfl_/nba_/nhl_odds_<date>.json: {player: {market: [line, over, implied, under]}}).

implied_prob / fair_over: American odds -> break-even probability, and the over's probability with the vig taken out (needs the under price; snapshots written before
2026-10-24 carry only the over, so `fair_over` falls back to the over's own implied probability and says so).
open_close: first and last PREGAME snapshot of a (group, player, market) across the saved days: the opening and closing line.
"""
import glob
import json
from pathlib import Path


def implied_prob(american):
    if american is None:
        return None
    return 100.0 / (american + 100.0) if american > 0 else -american / (-american + 100.0)


def fair_over(over, under):
    po, pu = implied_prob(over), implied_prob(under)
    if po is None:
        return None, False
    if pu is None:
        return po, False
    return po / (po + pu), True


def open_close(odds_dir, prefix, group_of, key_of):
    """-> {(group, key, market): {"open": (fetched_at, [..]), "close": (fetched_at, [..])}}; group_of(file_json) e.g. the week or the date, key_of(raw key) e.g. player id / normalised name."""
    out = {}
    for f in sorted(glob.glob(str(Path(odds_dir) / f"{prefix}_odds_20*.json"))):
        d = json.load(open(f))
        g = group_of(d)
        if g is None:
            continue
        opened = d.get("open") or {}
        for pid, mk in d.get("rows", {}).items():
            ko = (d.get("commence") or {}).get(pid)
            if ko and d["fetched_at"] >= ko.replace("Z", "+00:00"):
                continue                                           # fetched after the game began: not a pregame price
            for m, v in mk.items():
                k = (g, key_of(pid), m)
                e = out.setdefault(k, {"open": (d["fetched_at"], v), "close": (d["fetched_at"], v)})
                o = (opened.get(pid) or {}).get(m)
                if o and o[3] and o[3] < e["open"][0]:                      # the file's own first-seen price (opening) when it is earlier than anything else we hold
                    e["open"] = (o[3], [o[0], o[1], None, o[2]])
                if d["fetched_at"] > e["close"][0]:
                    e["close"] = (d["fetched_at"], v)
                if d["fetched_at"] < e["open"][0]:
                    e["open"] = (d["fetched_at"], v)
    return out


def clv_summary(rows, edge=0.04):
    """rows: dicts with model, open_fair, close_fair. Picks = model - open_fair >= edge (over) or <= -edge (under). CLV = how far the market moved toward our side by the close."""
    sel = []
    for r in rows:
        if r.get("open_fair") is None or r.get("close_fair") is None:
            continue
        d = r["model"] - r["open_fair"]
        if abs(d) >= edge:
            sel.append((1 if d > 0 else -1) * (r["close_fair"] - r["open_fair"]))
    if not sel:
        return None
    return {"n": len(sel), "moved_our_way": sum(1 for x in sel if x > 0) / len(sel), "moved_against": sum(1 for x in sel if x < 0) / len(sel), "avg_clv_pts": 100 * sum(sel) / len(sel)}
