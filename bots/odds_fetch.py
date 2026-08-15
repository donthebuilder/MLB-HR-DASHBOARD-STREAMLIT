#!/usr/bin/env python3
"""odds_fetch.py — the book's line next to the bot's score.

    python bots/odds_fetch.py --probe          # what does my key actually see?
    python bots/odds_fetch.py --out public/data/current

THE KEY IS A SECRET AND LIVES IN ONE PLACE: the ODDS_API_KEY repo secret, read
from the environment here. It must NEVER reach moonshot-mlb. That site is a
static Vercel build with no server — anything it holds is in view-source, and a
published key is a stranger spending your quota. So the bot fetches, the bot
publishes a plain JSON file to the data branch, and the site reads that file
exactly like every other payload. Same shape as the whole project already has.

WHY THIS MATTERS MORE THAN ANY OTHER FEATURE ON THE LIST. Every board on the
site scores against a DEFAULT bar — 40 receiving yards, 4 receptions, a home
run. If the book has him at 1.5 homers, or the hit line is 1.5 instead of 0.5,
then "cleared the bar 76% of the time" is a fact about a bar nobody is offering.
A prop tool without a line is a research tool. With one it's a decision tool.

THE CATEGORY MAP IS EXACT, WHICH IS LUCKY AND WORTH SAYING OUT LOUD. This API
carries batter_hits_runs_rbis — literally the HRR category — so all five pick
categories map onto a real market with no approximation:

    TOP / HR   batter_home_runs
    HIT        batter_hits
    HRR        batter_hits_runs_rbis
    CONTACT    batter_total_bases

JOINING IS BY NAME, AND THAT IS THE WEAK POINT. The book publishes names, not
MLB ids, so the join is normalised-name + team. Every miss is reported rather
than silently dropped: `match_rate` in the payload is the number that says
whether this file is worth reading, and `unmatched` names the players it lost
so the normaliser can be improved against real failures instead of guesses.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.theoddsapi.com"
SPORT = "baseball_mlb"

# ── BACKUP PROVIDER ──────────────────────────────────────────────────────────
#
# 2026-08-15, Donovan asked for oddspapi.io as a backup. Two things from their
# own docs that shape how it's used, and both argue for BACKUP rather than
# co-primary:
#
#   1. THE FREE TIER IS 250 REQUESTS A MONTH. today.yml fires 13 times a day.
#      Even at one request per run that is ~400/month — over the cap. As a
#      fallback that only fires when the primary returns nothing, it might burn
#      a handful a month, which is exactly what a backup should cost.
#
#   2. MLB PLAYER PROPS ARE ADVERTISED BUT NOT DOCUMENTED. The site says
#      "player props"; the docs enumerate no baseball prop markets and the one
#      market they name is soccer's 1X2. So nothing here hardcodes a market id —
#      it asks /markets what exists and matches on NAME. If baseball props
#      aren't there, discovery comes back empty, the provider yields nothing,
#      and the run says so instead of publishing an empty file.
#
# Uses /odds-by-tournaments, not /odds: the per-fixture endpoint would be one
# request per game, and this is one request for the whole slate.
PAPI = "https://api.oddspapi.io/v4"

# What a market has to be CALLED to count as one of ours. Substring match,
# case-folded, because no id is knowable from here without calling the API.
#
# ORDERED MOST-SPECIFIC-FIRST, AND THAT ORDER IS LOad-BEARING. "hit" is a
# substring of "Hits + Runs + RBIs", so a dict-order scan filed the compound
# market as plain hits and the HRR board would have carried the wrong price
# with nothing to show it was wrong. A list, first match wins, compound
# markets ahead of the single they contain.
PAPI_MARKET_HINTS = [
    ("batter_hits_runs_rbis", ("hits+runs", "hits + runs", "hits runs rbis",
                               "h+r+rbi", "hits runs and rbis")),
    ("batter_total_bases", ("total base",)),
    ("batter_home_runs", ("home run",)),
    # Last, and only after the compounds have had their turn.
    ("batter_hits", ("hit",)),
]

# pick category -> the market that actually settles it
CATEGORY_MARKET = {
    "TOP": "batter_home_runs",
    "HR": "batter_home_runs",
    "HIT": "batter_hits",
    "HRR": "batter_hits_runs_rbis",
    "CONTACT": "batter_total_bases",
}
MARKETS = sorted(set(CATEGORY_MARKET.values()))

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(s: str) -> str:
    """'José Ramírez Jr.' -> 'jose ramirez'. Accents, punctuation, suffixes."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if c.isalnum() or c.isspace() else " " for c in s).lower()
    parts = [p for p in s.split() if p]
    while len(parts) > 1 and parts[-1] in SUFFIXES:
        parts.pop()
    return " ".join(parts)


def api(path: str, key: str, **params) -> object:
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(url, headers={"x-api-key": key})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode())


def american(v) -> int | None:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n or None


def implied(odds: int | None) -> float | None:
    """American odds -> break-even probability, as a percentage.

    This is the whole point of carrying the price. A 62% hit rate against a
    -180 line (64.3% break-even) is a LOSING bet, and the score alone can
    never tell you that.
    """
    if odds is None:
        return None
    p = (-odds) / ((-odds) + 100) if odds < 0 else 100 / (odds + 100)
    return round(100 * p, 1)


def probe(key: str) -> int:
    """Answer 'what does my key actually see' before anything depends on it."""
    print(f"probing {BASE} …\n")
    for path, params in (("/me", {}), ("/me/usage", {}), ("/sports/", {})):
        try:
            j = api(path, key, **params)
            txt = json.dumps(j, indent=1)
            if path == "/sports/" and isinstance(j, list):
                mlb = [s for s in j if str(s.get("key", "")) == SPORT]
                txt = f"{len(j)} sports; baseball_mlb present: {bool(mlb)}"
                if mlb:
                    txt += "\n " + json.dumps(mlb[0])
            print(f"── {path}\n{txt[:1200]}\n")
        except urllib.error.HTTPError as e:
            print(f"── {path}\n  HTTP {e.code}: {e.read()[:300].decode(errors='replace')}\n")
        except Exception as e:
            print(f"── {path}\n  {type(e).__name__}: {e}\n")

    # The one that decides the whole design: do player props come back on
    # /odds directly, or does each event need its own request?
    for path, params in (
        ("/odds/", {"sport_key": SPORT, "markets": ",".join(MARKETS),
                    "regions": "us", "oddsFormat": "american"}),
        ("/props/", {"sport_key": SPORT, "markets": ",".join(MARKETS),
                     "regions": "us", "oddsFormat": "american"}),
    ):
        try:
            j = api(path, key, **params)
            n = len(j) if isinstance(j, list) else "?"
            print(f"── {path} with player-prop markets\n  OK · {n} event(s)")
            sample = (j[0] if isinstance(j, list) and j else j)
            print("  sample:\n" + json.dumps(sample, indent=1)[:2000] + "\n")
        except urllib.error.HTTPError as e:
            print(f"── {path}\n  HTTP {e.code}: {e.read()[:400].decode(errors='replace')}\n")
        except Exception as e:
            print(f"── {path}\n  {type(e).__name__}: {e}\n")
    return 0


def unwrap(payload) -> list:
    """The response as a list of events, whatever it arrived wrapped in.

    I could not call this endpoint to see the real shape — the sandbox proxy
    blocks the host — so rather than bet the integration on one guess, this
    accepts the documented bare list and the three common wrappers. Whichever
    it finds is printed, so the first Actions run tells us which is true.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "events", "odds", "results", "games"):
            v = payload.get(k)
            if isinstance(v, list):
                print(f"  response wrapped in '{k}'")
                return v
        # A single event, unwrapped.
        if payload.get("bookmakers") or payload.get("home_team"):
            return [payload]
    return []


def flat_rows(items: list) -> list[dict]:
    """Fallback for a FLAT prop shape — one object per player/market/book.

    Some prop endpoints publish this instead of the nested bookmakers tree.
    Detected by the absence of `bookmakers` and the presence of something that
    looks like a player and a price.
    """
    rows = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("bookmakers"):
            continue
        who = (it.get("player") or it.get("player_name") or it.get("description")
               or it.get("participant") or it.get("name"))
        mkey = str(it.get("market") or it.get("market_key") or it.get("key") or "")
        if not who or mkey not in MARKETS:
            continue
        book = it.get("bookmaker") or it.get("book") or it.get("sportsbook") or "?"
        point = it.get("point", it.get("line", it.get("handicap")))
        # over/under may be two fields on one row rather than two rows
        pairs = []
        if it.get("over_price") is not None or it.get("under_price") is not None:
            pairs = [("over", it.get("over_price")), ("under", it.get("under_price"))]
        else:
            side = str(it.get("side") or it.get("name") or "").strip().lower()
            side = {"yes": "over", "no": "under"}.get(side, side)
            pairs = [(side, it.get("price", it.get("odds")))]
        for side, price in pairs:
            if side not in ("over", "under") or price is None:
                continue
            rows.append({
                "name": str(who), "norm": norm_name(who), "market": mkey,
                "side": side, "book": str(book), "point": point,
                "price": american(price),
                "home": it.get("home_team"), "away": it.get("away_team"),
                "commence": it.get("commence_time"),
            })
    return rows


def walk_outcomes(events: list) -> list[dict]:
    """Flatten the standard odds shape into one row per (player, market, book).

    Written against the documented event -> bookmakers[] -> markets[] ->
    outcomes[] shape and tolerant of the pieces moving: anything it can't read
    is skipped rather than guessed at, and the caller reports the totals.
    """
    rows = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        home, away = ev.get("home_team"), ev.get("away_team")
        for bk in ev.get("bookmakers") or []:
            book = bk.get("key") or bk.get("title") or "?"
            for mk in bk.get("markets") or []:
                mkey = str(mk.get("key") or "")
                if mkey not in MARKETS:
                    continue
                for oc in mk.get("outcomes") or []:
                    who = oc.get("description") or oc.get("participant") or oc.get("name")
                    side = str(oc.get("name") or "").strip().lower()
                    if side not in ("over", "under"):
                        # Some books publish yes/no on a 0.5 homer line.
                        side = {"yes": "over", "no": "under"}.get(side, side)
                    if not who or side not in ("over", "under"):
                        continue
                    rows.append({
                        "name": str(who), "norm": norm_name(who),
                        "market": mkey, "side": side, "book": book,
                        "point": oc.get("point"),
                        "price": american(oc.get("price")),
                        "home": home, "away": away,
                        "commence": ev.get("commence_time"),
                    })
    return rows


def consensus(rows: list[dict]) -> dict:
    """{norm_name: {market: {line, over, under, best_over, best_book, books}}}

    The MAIN line is the one the most books agree on — not the first one seen.
    Books disagree on where to set a hits line and taking whichever arrived
    first would make the published number depend on response ordering.
    """
    by: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        by.setdefault(r["norm"], {}).setdefault(r["market"], []).append(r)

    out: dict[str, dict] = {}
    for norm, mkts in by.items():
        for market, rs in mkts.items():
            counts: dict[float, int] = {}
            for r in rs:
                try:
                    pt = float(r["point"]) if r["point"] is not None else 0.5
                except (TypeError, ValueError):
                    continue
                counts[pt] = counts.get(pt, 0) + 1
            if not counts:
                continue
            line = max(counts.items(), key=lambda kv: (kv[1], -abs(kv[0])))[0]
            at_line = [r for r in rs
                       if (r["point"] is None and line == 0.5)
                       or (r["point"] is not None and float(r["point"]) == line)]
            overs = [r for r in at_line if r["side"] == "over" and r["price"]]
            unders = [r for r in at_line if r["side"] == "under" and r["price"]]
            # Best price for a BETTOR taking the over: the largest number on the
            # American scale (+150 beats +120 beats -110 beats -200).
            best = max(overs, key=lambda r: r["price"]) if overs else None
            med = sorted(r["price"] for r in overs)[len(overs) // 2] if overs else None
            out.setdefault(norm, {})[market] = {
                "line": line,
                "over": med,
                "under": sorted(r["price"] for r in unders)[len(unders) // 2] if unders else None,
                "implied": implied(med),
                "best_over": best["price"] if best else None,
                "best_book": best["book"] if best else None,
                "books": len({r["book"] for r in at_line}),
                "name": rs[0]["name"],
                "game": f'{rs[0].get("away") or "?"} @ {rs[0].get("home") or "?"}',
            }
    return out


# ── the backup provider ──────────────────────────────────────────────────────

def papi(path: str, key: str, **params) -> object:
    """oddspapi.io. Auth is a QUERY PARAM, not a header — different from above."""
    q = {k: v for k, v in params.items() if v not in (None, "")}
    q["apiKey"] = key
    url = f"{PAPI}{path}?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode())


def _listish(payload, *keys):
    """Their responses vary in wrapper; take the first list we recognise."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in (*keys, "data", "items", "results"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []


def _named(o: dict) -> str:
    for k in ("name", "title", "marketName", "tournamentName", "sportName", "label"):
        v = o.get(k)
        if v:
            return str(v)
    return ""


def _ident(o: dict, *keys) -> str:
    for k in (*keys, "id", "_id"):
        v = o.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def papi_discover(key: str) -> tuple[str, dict[str, str]]:
    """(mlb_tournament_id, {their_market_id: our_market_key}).

    NOTHING IS HARDCODED. Their ids aren't knowable without calling the API, so
    this asks what exists and matches on name. An empty result is a real answer
    — it means baseball props aren't on this plan — and the caller treats it as
    "no backup available" rather than publishing an empty file.
    """
    tid = ""
    try:
        for t in _listish(papi("/tournaments", key, sport="baseball"), "tournaments"):
            nm = _named(t).lower()
            if "mlb" in nm or "major league baseball" in nm:
                tid = _ident(t, "tournamentId")
                print(f"  oddspapi: tournament '{_named(t)}' -> {tid}")
                break
    except Exception as e:
        print(f"  oddspapi: tournament lookup failed ({type(e).__name__}: {e})")

    markets: dict[str, str] = {}
    try:
        for m in _listish(papi("/markets", key, sport="baseball"), "markets"):
            nm = _named(m).lower()
            for ours, hints in PAPI_MARKET_HINTS:
                if any(h in nm for h in hints):
                    mid = _ident(m, "marketId")
                    if mid:
                        markets[mid] = ours
                    break
    except Exception as e:
        print(f"  oddspapi: market lookup failed ({type(e).__name__}: {e})")

    if markets:
        print(f"  oddspapi: matched {len(markets)} prop market(s) by name")
    else:
        print("  oddspapi: no baseball prop markets found by name — backup unusable")
    return tid, markets


def fetch_oddspapi(key: str) -> list[dict]:
    """Rows in the SAME shape walk_outcomes() produces, so consensus() is shared."""
    tid, markets = papi_discover(key)
    if not tid or not markets:
        return []
    try:
        payload = papi("/odds-by-tournaments", key, tournamentIds=tid)
    except Exception as e:
        print(f"  oddspapi: odds fetch failed ({type(e).__name__}: {e})")
        return []

    rows = []
    for ev in _listish(payload, "fixtures", "events", "odds"):
        if not isinstance(ev, dict):
            continue
        home, away = ev.get("homeTeam") or ev.get("home_team"), ev.get("awayTeam") or ev.get("away_team")
        for o in _listish(ev, "odds", "markets", "outcomes"):
            if not isinstance(o, dict):
                continue
            ours = markets.get(_ident(o, "marketId"))
            if not ours:
                continue
            who = (o.get("player") or o.get("playerName") or o.get("participant")
                   or o.get("outcomeName") or _named(o))
            side = str(o.get("outcomeName") or o.get("side") or o.get("type") or "").lower()
            side = ("over" if "over" in side or side == "yes"
                    else "under" if "under" in side or side == "no" else "")
            if not who or not side:
                continue
            rows.append({
                "name": str(who), "norm": norm_name(who), "market": ours,
                "side": side, "book": str(o.get("bookmaker") or o.get("bookmakerName") or "?"),
                "point": o.get("handicap", o.get("line", o.get("point"))),
                "price": american(o.get("price", o.get("odds", o.get("decimal")))),
                "home": home, "away": away, "commence": ev.get("startTime"),
            })
    print(f"  oddspapi: {len(rows)} prop quote(s)")
    return rows


def fetch_theoddsapi(key: str, regions: str) -> list[dict]:
    try:
        raw = api("/odds/", key, sport_key=SPORT, markets=",".join(MARKETS),
                  regions=regions, oddsFormat="american")
    except urllib.error.HTTPError as e:
        print(f"  theoddsapi: HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
        return []
    except Exception as e:
        print(f"  theoddsapi: {type(e).__name__}: {e}")
        return []
    items = unwrap(raw)
    rows = walk_outcomes(items)
    shape = "nested"
    if not rows:
        rows = flat_rows(items)
        shape = "flat"
    print(f"  theoddsapi: {len(items)} item(s) · {len(rows)} quote(s) · {shape}")
    if not rows and isinstance(raw, (list, dict)):
        keys = sorted(raw) if isinstance(raw, dict) else (
            sorted(raw[0]) if raw and isinstance(raw[0], dict) else [])
        print(f"  theoddsapi: unrecognised shape · keys {keys[:15]}")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="report what the key can see; write nothing")
    ap.add_argument("--slate", type=str, default="",
                    help="slate to join names against; default tries both known locations")
    ap.add_argument("--out", type=str, default="public/data/current")
    ap.add_argument("--regions", type=str, default="us")
    a = ap.parse_args()

    key = os.environ.get("ODDS_API_KEY", "").strip()
    backup = os.environ.get("ODDSPAPI_KEY", "").strip()
    if not key and not backup:
        # Not an error: no key configured means the site simply has no odds
        # file and every surface that reads it degrades to the score alone.
        print("no odds key set (ODDS_API_KEY / ODDSPAPI_KEY) — skipping (not a failure)")
        return 0

    if a.probe:
        return probe(key) if key else 0

    # ORDER MATTERS AND IS THE WHOLE POINT OF A BACKUP. The primary is one
    # request for the entire slate. The backup's free tier is 250 requests a
    # MONTH against a workflow that fires 13 times a day, so it must only ever
    # run when the primary came back empty — never alongside it.
    rows, source = [], ""
    for name, k, fn in (("theoddsapi", key, lambda: fetch_theoddsapi(key, a.regions)),
                        ("oddspapi", backup, lambda: fetch_oddspapi(backup))):
        if not k:
            print(f"{name}: no key configured — skipping")
            continue
        print(f"trying {name} …")
        rows = fn()
        if rows:
            source = name
            break
        print(f"  {name} yielded nothing — falling through")

    if not rows:
        print("no odds from any provider — publishing nothing, "
              "the site falls back to scores alone")
        return 0
    print(f"using {source} · {len(rows)} quote(s)")

    board = consensus(rows)

    # ── join to the slate, and REPORT the miss rate ────────────────────────
    matched, unmatched = {}, []
    # BOTH LOCATIONS. mlb_dashboard writes public/data/today.json; other
    # payloads land in public/data/current/. pick_lock.py already carries the
    # same pair for the same reason, and defaulting to current/ alone would
    # have made this join silently match nobody on the very first run — the
    # file would publish, look fine, and be keyed only by name.
    candidates = ([Path(a.slate)] if a.slate else
                  [Path("public/data/today.json"),
                   Path("public/data/current/today.json"),
                   Path("public/data/today_slim.json"),
                   Path("public/data/current/today_slim.json")])
    slate_path = next((c for c in candidates if c.exists()), candidates[0])
    if slate_path.exists():
        print(f"  joining against {slate_path}")
        try:
            slate = json.loads(slate_path.read_text())
            players = slate.get("players") if isinstance(slate, dict) else slate
            by_norm = {}
            for p in players or []:
                n = norm_name(p.get("name") or p.get("player_name"))
                if n:
                    by_norm.setdefault(n, p)
            for norm, mkts in board.items():
                p = by_norm.get(norm)
                if not p:
                    unmatched.append(board[norm].get(MARKETS[0], {}).get("name") or norm)
                    continue
                matched[str(p.get("player_id"))] = mkts
            rate = round(100 * len(matched) / max(1, len(board)), 1)
            print(f"joined {len(matched)}/{len(board)} priced players to the slate ({rate}%)")
            if unmatched:
                print(f"  unmatched: {', '.join(sorted(unmatched)[:12])}"
                      + (" …" if len(unmatched) > 12 else ""))
        except Exception as e:
            print(f"  slate join skipped ({type(e).__name__}: {e})")
    else:
        print(f"  no slate at {slate_path} — publishing by name only")

    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "source": source,
        "sport": SPORT,
        "regions": a.regions,
        "fetched_at": now.isoformat(),
        "fetched_at_human": now.strftime("%b %-d, %-I:%M %p UTC"),
        "category_market": CATEGORY_MARKET,
        # Keyed by MLB player_id where the join worked — that's what the site
        # can actually use — with the by-name board kept beside it so a miss is
        # visible rather than just absent.
        "by_player_id": matched,
        "by_name": board,
        "match_rate": round(100 * len(matched) / max(1, len(board)), 1),
        "unmatched": sorted(unmatched),
        "note": ("Consensus line is the one the most books post; over/under are the "
                 "median price at that line; best_over is the best available price "
                 "for taking the over. `implied` is the break-even percentage — "
                 "compare it to a hit rate, not to a score."),
    }
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "odds_latest.json"
    dest.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {dest} ({dest.stat().st_size / 1024:.0f} KB)")

    try:
        u = api("/me/usage", key)
        print(f"quota: {json.dumps(u)[:200]}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
