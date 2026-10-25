#!/usr/bin/env python3
"""
live_price_log.py -- the TRUE PRICE of a prop at every moment of the game, so we can see what it was pregame, at the start, and the instant the player actually hit.

    python bots/live_price_log.py loop   OUT_DIR [--minutes 330] [--every 60] [--series KXNBAPTS ...]     poll Kalshi, append only the prices that changed
    python bots/live_price_log.py report OUT_DIR                                                           join the ticks to what happened (ESPN play-by-play)

Kalshi is the one public, live, per-player market (yes bid / ask for "Player: N+"), so it is the source: NBA points / rebounds / assists / PRA, NHL goal / point / assist, NFL anytime TD
(MLB home run when the season is on). A tick is saved only when a market's bid, ask or last price changed (price_ticks_<date>.jsonl: {"t", "k": ticker, "b", "a", "l"}); each
ticker's player, strike and game go once into price_meta_<date>.json.
report: for every market that settled, the event time (NBA: when his running points reached the strike, from ESPN's wall clock; NHL: when his Nth goal was posted) and
  price pregame (the last tick before the game began), price at the start, and price in the minutes just before the hit, for the ones that hit and the ones that did not.
"""
import argparse, json, math, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"nba": ["KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBAPRA"], "nhl": ["KXNHLGOAL", "KXNHLPTS", "KXNHLAST"], "nfl": ["KXNFLANYTD"], "mlb": ["KXMLBHR"]}


def curl(url):
    out = subprocess.run(["curl", "-s", "-m", "30", url], capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except ValueError:
        return {}


def markets(series):
    out, cur = [], None
    while True:
        j = curl(f"{KALSHI}/markets?series_ticker={series}&status=open&limit=1000" + (f"&cursor={cur}" if cur else ""))
        out += j.get("markets", [])
        cur = j.get("cursor")
        if not cur or not j.get("markets"):
            return out


def fnum(x):
    try:
        return round(float(x), 4)
    except (TypeError, ValueError):
        return None


def poll_once(out_dir, series_list, last, meta):
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    ticks = []
    for s in series_list:
        for m in markets(s):
            b, a, l = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars")), fnum(m.get("last_price_dollars"))
            if (b is None or b <= 0.01) and (a is None or a >= 0.99):
                continue                                                       # nothing quoted
            k = m["ticker"]
            if k not in meta:
                meta[k] = {"series": s, "event": m.get("event_ticker"), "sub": m.get("yes_sub_title"), "strike": m.get("floor_strike"), "close": m.get("close_time")}
            if last.get(k) != (b, a, l):
                last[k] = (b, a, l)
                ticks.append({"t": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "k": k, "b": b, "a": a, "l": l})
    if ticks:
        with open(Path(out_dir) / f"price_ticks_{day}.jsonl", "a") as f:
            f.write("\n".join(json.dumps(t, separators=(",", ":")) for t in ticks) + "\n")
        mp = Path(out_dir) / f"price_meta_{day}.json"
        old = json.load(open(mp)) if mp.exists() else {}
        old.update(meta); mp.write_text(json.dumps(old, separators=(",", ":")))
    return len(ticks)


def loop(out_dir, minutes, every, series_list):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    last, meta = {}, {}
    end = time.time() + 60 * minutes
    n = 0
    while time.time() < end:
        n += poll_once(out_dir, series_list, last, meta)
        time.sleep(every)
    print(f"{n} price changes saved")


# ----------------------------------------------------------------------------------------------- report
ESPN = {"nba": "basketball/nba", "nhl": "hockey/nhl"}


def parse_event(ev):
    """'KXNBAPTS-26OCT25DETPHI' -> ('2026-10-25', 'DET', 'PHI')"""
    code = ev.split("-")[1]
    yy, mon, dd, rest = code[:2], code[2:5], code[5:7], code[7:]
    mm = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"].index(mon) + 1
    return f"20{yy}-{mm:02d}-{dd}", rest[:3], rest[3:]


def espn_game(sport, day, away3, home3, cache):
    key = (sport, day)
    if key not in cache:
        cache[key] = curl(f"https://site.api.espn.com/apis/site/v2/sports/{ESPN[sport]}/scoreboard?dates={day.replace('-', '')}").get("events", [])
    norm = lambda x: {"WSH": "WAS", "NJ": "NJD", "LA": "LAK", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA", "NO": "NOP", "NY": "NYK", "GS": "GSW", "SA": "SAS"}.get(x, x)
    for e in cache[key]:
        c = {x["homeAway"]: norm(x["team"]["abbreviation"]) for x in e["competitions"][0]["competitors"]}
        if c.get("away") == norm(away3) and c.get("home") == norm(home3):
            return e
    return None


def event_times(sport, game_id, cache):
    """{player display name: [wall-clock time of each scoring event]}: NBA points (a time per point), NHL goals (a time per goal)."""
    if game_id not in cache:
        d = curl(f"https://site.api.espn.com/apis/site/v2/sports/{ESPN[sport]}/summary?event={game_id}")
        ev = {}
        for p in d.get("plays", []):
            if not p.get("scoringPlay"):
                continue
            who = ((p.get("participants") or [{}])[0].get("athlete") or {}).get("displayName")
            t = p.get("wallclock") if sport == "nba" else (p.get("modified") + ":00Z" if p.get("modified") and len(p["modified"]) == 17 else p.get("modified"))
            if not who or not t:
                continue
            n = int(p.get("scoreValue") or 1) if sport == "nba" else 1
            ev.setdefault(who, []).extend([t] * n)
        cache[game_id] = ev
    return cache[game_id]


def iso(t):
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def report(out_dir):
    import injury_status
    ticks, meta = {}, {}
    for f in sorted(Path(out_dir).glob("price_meta_*.json")):
        meta.update(json.load(open(f)))
    for f in sorted(Path(out_dir).glob("price_ticks_*.jsonl")):
        for ln in open(f):
            r = json.loads(ln)
            ticks.setdefault(r["k"], []).append(r)
    sb, ev = {}, {}
    rows = []
    for k, mk in meta.items():
        sport = "nba" if k.startswith("KXNBA") else "nhl" if k.startswith("KXNHL") else None
        if not sport or k not in ticks or not mk.get("sub"):
            continue
        name, need = mk["sub"].split(":")[0].strip(), int(math.ceil(mk["strike"] or 0.5))
        day, away, home = parse_event(mk["event"])
        g = espn_game(sport, day, away, home, sb)
        if not g or not g["status"]["type"]["completed"]:
            continue
        times = event_times(sport, g["id"], ev)
        key = injury_status.key(name)
        mine = next((v for n, v in times.items() if injury_status.key(n) == key), [])
        hit_t = iso(mine[need - 1]) if len(mine) >= need and k.split("-")[0] in ("KXNBAPTS", "KXNHLGOAL") else None
        if k.split("-")[0] not in ("KXNBAPTS", "KXNHLGOAL"):
            continue                                                       # rebounds / assists / PRA / points+assists events need other play types: ticks are kept, graded later
        start = iso(g["date"])
        tk = ticks[k]
        at = lambda t: next((x for x in reversed(tk) if iso(x["t"]) <= t), None)
        pre = at(start)
        before = at(hit_t - __import__("datetime").timedelta(minutes=2)) if hit_t else None
        just = at(hit_t) if hit_t else None
        mid = lambda x: None if not x or x["b"] is None or x["a"] is None else (x["b"] + x["a"]) / 2
        rows.append({"market": k, "player": name, "strike": need, "hit": hit_t is not None, "pre": mid(pre), "two_min_before": mid(before), "just_before": mid(just), "ticks": len(tk)})
    print(f"{len(rows)} settled goal / points markets joined to their games; {sum(1 for r in rows if r['pre'] is not None)} have a pregame tick, {sum(1 for r in rows if r['hit'])} hit")
    if not rows:
        return
    for hit in (True, False):
        rs = [r for r in rows if r["hit"] == hit and r["pre"] is not None]
        if rs:
            line = f"  {'HIT' if hit else 'did not hit'}: {len(rs)} markets, average pregame price {100 * sum(r['pre'] for r in rs) / len(rs):.1f}%"
            js = [r for r in rs if r["just_before"] is not None]
            if hit and js:
                line += f", average price in the last tick before the hit {100 * sum(r['just_before'] for r in js) / len(js):.1f}% ({len(js)} with ticks)"
            print(line)
    json.dump(rows, open(Path(out_dir) / "live_price_report.json", "w"), indent=1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["loop", "report"]); ap.add_argument("out"); ap.add_argument("--minutes", type=float, default=330); ap.add_argument("--every", type=float, default=60)
    ap.add_argument("--series", nargs="*", default=None); a = ap.parse_args()
    if a.cmd == "loop":
        loop(a.out, a.minutes, a.every, a.series or SERIES["nba"] + SERIES["nhl"])
    else:
        report(a.out)
