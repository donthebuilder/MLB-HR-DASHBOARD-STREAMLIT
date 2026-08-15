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

# ── odds-api.io ──────────────────────────────────────────────────────────────
#
# Donovan, 2026-08-15: "i think i want to use them more they hq." So this leads
# the order by default — and the order is an env var, so changing your mind
# later is a repo-variable edit rather than a code change.
#
# The shape, from their OpenAPI spec:
#   GET /v3/events?apiKey=&sport=baseball&status=pending&from=&to=
#   GET /v3/odds/multi?apiKey=&eventIds=<UP TO 10>&bookmakers=<REQUIRED, max 30>
#
# Ten events per call means a ~15-game MLB slate is 1 events call + 2 odds
# calls = 3 requests a run. At 13 runs a day that's ~39/day against a free tier
# of 100/hour and 500/day — comfortable, which is why this can lead.
#
# `bookmakers` is REQUIRED. Donovan bets Fanatics and DraftKings (2026-08-15),
# so those are what gets asked for — and that is a correctness decision, not
# just a preference. A "consensus" median across twelve books is noise if you
# can only place a bet at two of them: the number that decides anything is the
# price at YOUR book. Everything downstream — the median, best_over, the
# break-even the site renders — is therefore computed over his books only.
#
# The wide list is kept as a FALLBACK and only fires if the first request comes
# back with nothing, which is what a wrong bookmaker key looks like. One extra
# request, only on failure, and the log names every book actually seen so a
# mismatch is obvious on the first run instead of looking like "no odds today".
OAIO = "https://api.odds-api.io/v3"
OAIO_BOOKS = "fanatics,draftkings"
OAIO_BOOKS_FALLBACK = ("fanatics,draftkings,fanduel,bet365,betmgm,caesars,"
                       "pointsbet,betrivers,unibet,williamhill,pinnacle,bovada")

# Their markets are selected by EXACT NAME (case-insensitive) via `markets`.
# Player props arrive under the generic "Player Props" name with the specific
# prop in each outcome's `label`, which is why PROP_HINTS below does the real
# work of deciding what a quote actually is.
OAIO_MARKETS = "Player Props"

# What a market has to be CALLED to count as one of ours. Substring match,
# case-folded, because no id is knowable from here without calling the API.
#
# ORDERED MOST-SPECIFIC-FIRST, AND THAT ORDER IS LOAD-BEARING. "hit" is a
# substring of "Hits + Runs + RBIs", so a dict-order scan filed the compound
# market as plain hits and the HRR board would have carried the wrong price
# with nothing to show it was wrong. A list, first match wins, compound
# markets ahead of the single they contain.
PROP_HINTS = [
    ("batter_hits_runs_rbis", ("hits+runs", "hits + runs", "hits runs rbis",
                               "h+r+rbi", "hits runs and rbis")),
    ("batter_total_bases", ("total base",)),
    ("batter_home_runs", ("home run",)),
    ("batter_runs_scored", ("runs scored", "run scored")),
    ("batter_rbis", ("rbi", "runs batted in")),
    # Last, and only after every compound has had its turn — "hit" is a
    # substring of half the names above it.
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

# The PROPS GRID has rows the pick categories don't: runs and RBIs. Donovan,
# 2026-08-15: "on thbe props grid it can have the odds for each prop." Both
# exist on the API, so fetch them too — the grid is where a price and a hit
# rate finally sit on the same row, which is the whole argument for carrying
# odds at all.
#
# Its row for 1+ BB and 1+ K have no batter market published; those rows simply
# carry no price, which is honest and visible.
GRID_MARKETS = ["batter_runs_scored", "batter_rbis"]
MARKETS = sorted(set(CATEGORY_MARKET.values()) | set(GRID_MARKETS))

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


# ── FORENSICS (2026-08-15, Donovan: "the odds never ran wtf") ────────────────
#
# Three providers, three request shapes written blind (the sandbox can't reach
# any of these hosts), zero quotes — and the only evidence lived in an Actions
# log nobody opens. Every HTTP attempt now leaves a sanitized record: endpoint
# path, status code, and the first bytes of the response body, with the key
# scrubbed if a body ever echoes it. The records ride odds_status.json on
# every write, so the next scheduled run publishes its own diagnosis to the
# data branch — readable with nothing but a browser, no Actions tab needed.
FORENSICS: list = []

# --insecure (PROBE-ONLY DIAGNOSTIC). Donovan's own Mac fails TLS verification
# against every odds host — "self-signed certificate in certificate chain" —
# which is a local interception (VPN / security software / cert store), not
# the servers: the same hosts verify cleanly elsewhere, and the GitHub runner
# is unaffected. This flag exists so the probe can still SHOW him the API's
# answers from that machine. Production runs never set it.
_SSL = None


def _open_kw() -> dict:
    kw = {"timeout": 45}
    if _SSL is not None:
        kw["context"] = _SSL
    return kw


def _rec(provider: str, path: str, key: str = "", **kw) -> None:
    if len(FORENSICS) >= 24:
        return
    row = {"provider": provider, "path": path, **kw}
    if key:
        for k2, v in list(row.items()):
            if isinstance(v, str) and key in v:
                row[k2] = v.replace(key, "•KEY•")
    FORENSICS.append(row)


def _snip(e) -> str:
    try:
        return e.read()[:220].decode(errors="replace")
    except Exception:
        return ""


def api(path: str, key: str, **params) -> object:
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(url, headers={"x-api-key": key})
    try:
        with urllib.request.urlopen(req, **_open_kw()) as r:
            body = r.read().decode()
            _rec("theoddsapi", path, key, http=getattr(r, "status", 200), bytes=len(body))
            return json.loads(body)
    except urllib.error.HTTPError as e:
        # NOTE: reading the body here consumes it — callers printing e.read()
        # will see b''. The forensics record is the copy that survives.
        _rec("theoddsapi", path, key, http=e.code, body=_snip(e))
        raise
    except Exception as e:
        _rec("theoddsapi", path, key, error=f"{type(e).__name__}: {e}")
        raise


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
    if not key:
        print("no ODDS_API_KEY — skipping the theoddsapi probe\n")
    else:
        print(f"probing {BASE} …\n")
    for path, params in ((("/me", {}), ("/me/usage", {}), ("/sports/", {})) if key else ()):
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
    for path, params in ((
        ("/odds/", {"sport_key": SPORT, "markets": ",".join(MARKETS),
                    "regions": "us", "oddsFormat": "american"}),
        ("/props/", {"sport_key": SPORT, "markets": ",".join(MARKETS),
                     "regions": "us", "oddsFormat": "american"}),
    ) if key else ()):
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

    # ── the PRIMARY, if its key is set (probe upgrade, 2026-08-15) ──────────
    # The one unknown their docs don't publish is what their MLB prop MARKETS
    # are actually named — PROP_HINTS matches by name, so seeing the real
    # names once settles the mapping for good.
    k2 = os.environ.get("ODDSAPI_IO_KEY", "").strip()
    if k2:
        print(f"probing {OAIO} …\n")
        for slug in ("mlb", "baseball"):
            try:
                evs = _listish(oaio("/events", k2, sport=slug, limit=25), "events")
                print(f"── /events sport={slug}\n  {len(evs)} event(s)")
                ids = [str(e.get('id') or e.get('eventId')) for e in evs[:3]
                       if isinstance(e, dict)]
                ids = [i for i in ids if i and i != 'None']
                if ids:
                    payload = oaio("/odds/multi", k2, eventIds=",".join(ids),
                                   bookmakers=OAIO_BOOKS_FALLBACK)
                    names = set()
                    for ev in _listish(payload, "events", "odds"):
                        for _bk, mkts in (ev.get("bookmakers") or {}).items():
                            for mk in mkts or []:
                                if isinstance(mk, dict) and mk.get("name"):
                                    names.add(str(mk["name"]))
                    print(f"  market names seen ({len(names)}):")
                    for nm2 in sorted(names)[:40]:
                        print(f"    · {nm2}")
                    smp = json.dumps(payload, indent=1)[:1500]
                    print("  sample:\n" + smp + "\n")
                    break
            except urllib.error.HTTPError as e:
                print(f"── /events sport={slug}\n  HTTP {e.code}: {e.read()[:300].decode(errors='replace')}\n")
            except Exception as e:
                print(f"── /events sport={slug}\n  {type(e).__name__}: {e}\n")

    # ── the backup, if its key is set ───────────────────────────────────────
    k3 = os.environ.get("ODDSPAPI_KEY", "").strip()
    if k3:
        print(f"probing {PAPI} …\n")
        for path, params in (("/tournaments", {"sport": "baseball"}),
                             ("/markets", {"sport": "baseball"})):
            try:
                j = papi(path, k3, **params)
                print(f"── {path}\n" + json.dumps(j, indent=1)[:1200] + "\n")
            except urllib.error.HTTPError as e:
                print(f"── {path}\n  HTTP {e.code}: {e.read()[:300].decode(errors='replace')}\n")
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
                # With two books a disagreement on WHERE the line sits is a
                # coin flip resolved silently by the tie-break above. Recording
                # that it happened is nearly free and stops a 0.5-vs-1.5 split
                # from reading as settled fact.
                "lines_seen": len(counts),
                "name": rs[0]["name"],
                "game": f'{rs[0].get("away") or "?"} @ {rs[0].get("home") or "?"}',
            }
    return out


# ── odds-api.io (the primary) ────────────────────────────────────────────────

def oaio(path: str, key: str, **params) -> object:
    q = {k: v for k, v in params.items() if v not in (None, "")}
    q["apiKey"] = key
    url = f"{OAIO}{path}?" + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(urllib.request.Request(url), **_open_kw()) as r:
            body = r.read().decode()
            _rec("oddsapiio", path, key, http=getattr(r, "status", 200), bytes=len(body))
            return json.loads(body)
    except urllib.error.HTTPError as e:
        _rec("oddsapiio", path, key, http=e.code, body=_snip(e))
        raise
    except Exception as e:
        _rec("oddsapiio", path, key, error=f"{type(e).__name__}: {e}")
        raise


def _prop_key(label: str) -> str | None:
    """Which of our markets a prop LABEL describes, most-specific-first."""
    nm = str(label or "").lower()
    for ours, hints in PROP_HINTS:
        if any(h in nm for h in hints):
            return ours
    return None


def _dec_to_american(v) -> int | None:
    """They quote decimal odds; everything downstream speaks American."""
    try:
        d = float(v)
    except (TypeError, ValueError):
        return None
    if d <= 1.0:
        return None
    return int(round((d - 1) * 100)) if d >= 2.0 else int(round(-100 / (d - 1)))


def fetch_oddsapiio(key: str, books: str, _wide: bool = False) -> list[dict]:
    # Their documented listing is GET /events?sport={slug}&to=&limit=&skip= —
    # the old call sent status/from params the docs don't know. The slug for
    # baseball isn't published anywhere reachable, so try the two candidates
    # in order and keep whichever returns events; the forensics record both.
    now = dt.datetime.now(dt.timezone.utc)
    evs = []
    for slug in ("mlb", "baseball"):
        try:
            evs = _listish(oaio("/events", key, sport=slug,
                                to=(now + dt.timedelta(days=2)).isoformat(),
                                limit=200), "events")
        except Exception as e:
            print(f"  odds-api.io: events sport={slug} failed ({type(e).__name__}: {e})")
            continue
        if evs:
            print(f"  odds-api.io: sport slug '{slug}' returned {len(evs)} event(s)")
            break
    if not evs:
        return []

    # Baseball is not only MLB. Keep the ones whose league says so, and if no
    # league field exists at all, keep everything rather than filter to nothing.
    mlb = [e for e in evs if isinstance(e, dict)
           and ("mlb" in str(e.get("league", "")).lower()
                or "major league" in str(e.get("league", "")).lower())]
    if not mlb and evs:
        mlb = [e for e in evs if isinstance(e, dict)]
        print("  odds-api.io: no league field matched MLB — using all baseball events")
    ids = [str(e.get("id") or e.get("eventId")) for e in mlb]
    ids = [i for i in ids if i and i != "None"]
    print(f"  odds-api.io: {len(ids)} event(s)")
    if not ids:
        return []

    rows: list[dict] = []
    # TEN PER CALL is their documented cap. Chunking is what keeps a 15-game
    # slate at three requests instead of fifteen.
    for i in range(0, len(ids), 10):
        chunk = ids[i:i + 10]
        try:
            payload = oaio("/odds/multi", key, eventIds=",".join(chunk),
                           bookmakers=books)
        except Exception as e:
            print(f"  odds-api.io: odds chunk {i // 10 + 1} failed "
                  f"({type(e).__name__}: {e})")
            continue
        # DOCUMENTED SHAPE (their own player-props guide, read 2026-08-15):
        #   event.bookmakers = { "DraftKings": [ {name, odds:[{label, hdp,
        #   over, under}]} ] } — the MARKET's name is on the market object,
        #   the PLAYER is each odds row's `label`, the line is `hdp`, and the
        #   prices are decimal. The old parser ran the market hints against
        #   the PLAYER'S NAME (o.label) and then looked for the player under
        #   keys that don't exist — two independent kills, every outcome
        #   dropped, "0 prop quotes" forever. This is that bug's funeral.
        for ev in _listish(payload, "events", "odds"):
            if not isinstance(ev, dict):
                continue
            home, away = (ev.get("home") or ev.get("homeTeam"),
                          ev.get("away") or ev.get("awayTeam"))
            for book, mkts in (ev.get("bookmakers") or {}).items():
                for mk in mkts or []:
                    if not isinstance(mk, dict):
                        continue
                    ours = _prop_key(mk.get("name"))
                    if not ours:
                        continue
                    for o in mk.get("odds") or []:
                        if not isinstance(o, dict):
                            continue
                        who = o.get("label") or o.get("player") or o.get("participant")
                        if not who:
                            continue
                        line = o.get("hdp", o.get("line", o.get("handicap")))
                        for side, raw in (("over", o.get("over")),
                                          ("under", o.get("under"))):
                            price = _dec_to_american(raw)
                            if price is None:
                                continue
                            rows.append({
                                "name": str(who), "norm": norm_name(who),
                                "market": ours, "side": side, "book": str(book),
                                "point": line, "price": price,
                                "home": home, "away": away,
                                "commence": ev.get("date") or ev.get("startTime"),
                            })
    seen = sorted({r["book"] for r in rows})
    print(f"  odds-api.io: {len(rows)} prop quote(s) from {len(seen)} book(s)"
          + (f": {', '.join(seen)}" if seen else ""))

    # Nothing back from the named books is what a WRONG BOOKMAKER KEY looks
    # like, and it is indistinguishable from "no odds today" unless we check.
    # One retry across the wide list; if that returns quotes, the log names the
    # books that do work and the variable can be corrected.
    if not rows and not _wide and books != OAIO_BOOKS_FALLBACK:
        print("  odds-api.io: nothing from the named books — retrying wide "
              "to find out what this key actually carries")
        return fetch_oddsapiio(key, OAIO_BOOKS_FALLBACK, _wide=True)
    return rows


# ── the backup provider ──────────────────────────────────────────────────────

def papi(path: str, key: str, **params) -> object:
    """oddspapi.io. Auth is a QUERY PARAM, not a header — different from above."""
    q = {k: v for k, v in params.items() if v not in (None, "")}
    q["apiKey"] = key
    url = f"{PAPI}{path}?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, **_open_kw()) as r:
            body = r.read().decode()
            _rec("oddspapi", path, key, http=getattr(r, "status", 200), bytes=len(body))
            return json.loads(body)
    except urllib.error.HTTPError as e:
        _rec("oddspapi", path, key, http=e.code, body=_snip(e))
        raise
    except Exception as e:
        _rec("oddspapi", path, key, error=f"{type(e).__name__}: {e}")
        raise


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
            for ours, hints in PROP_HINTS:
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
    # /props/ IS THE PLAYER-PROP ENDPOINT (verified against their live docs,
    # 2026-08-15 — the forensics hunt). /odds/ only carries h2h/spreads/totals,
    # which is why calling it with batter_* markets returned nothing forever.
    # /props/ leads; /odds/ stays as a fallback so a plan that surfaces props
    # there anyway still works. NOTE their docs mark /props/ as Business tier —
    # a lower-tier key gets an honest 403 here, which the forensics publish.
    for path in ("/props/", "/odds/"):
        try:
            raw = api(path, key, sport_key=SPORT, markets=",".join(MARKETS),
                      regions=regions, oddsFormat="american")
        except urllib.error.HTTPError as e:
            print(f"  theoddsapi {path}: HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
            continue
        except Exception as e:
            print(f"  theoddsapi {path}: {type(e).__name__}: {e}")
            continue
        items = unwrap(raw)
        rows = walk_outcomes(items)
        shape = "nested"
        if not rows:
            rows = flat_rows(items)
            shape = "flat"
        print(f"  theoddsapi {path}: {len(items)} item(s) · {len(rows)} quote(s) · {shape}")
        if rows:
            return rows
        if isinstance(raw, (list, dict)):
            keys = sorted(raw) if isinstance(raw, dict) else (
                sorted(raw[0]) if raw and isinstance(raw[0], dict) else [])
            print(f"  theoddsapi {path}: unrecognised shape · keys {keys[:15]}")
    return []


# ── THE REQUEST LOCK ─────────────────────────────────────────────────────────
#
# Donovan, 2026-08-15: "i dont want the odds to us a lot of request so some sort
# of lock is okay. just stop that from over using."
#
# today.yml fires THIRTEEN times a day and every run was a full fetch. Prices
# barely move between two runs an hour apart, so twelve of those thirteen buy
# nothing and spend quota that the one that matters — the last snapshot before
# first pitch — depends on.
#
# So: skip when the published snapshot is younger than ODDS_MIN_INTERVAL_MIN,
# and stop entirely after ODDS_MAX_PER_SLATE fetches for the same slate. Both
# are env vars, both have defaults that land around five fetches a day.
#
# The state lives in the PUBLISHED file, read over plain HTTP from the data
# branch — a static file fetch, not an API request, so checking costs nothing.
# The runner's working copy is empty at the start of every run, so there is
# nowhere else it could live.
DATA_RAW = ("https://raw.githubusercontent.com/donthebuilder/"
            "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")


def published(name: str):
    """The current published copy of a data-branch file, or None."""
    try:
        req = urllib.request.Request(f"{DATA_RAW}/{name}",
                                     headers={"User-Agent": "moonshot-odds"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def write_status(out: Path, **kw) -> None:
    """Say what happened, every single time, including nothing.

    A step that is `continue-on-error: true` and prints to a log nobody opens
    is a feature that can be dead for a week without anyone noticing — which is
    what happened here: odds_latest.json 404s on the branch and the only way to
    know why was to open an Actions run. This file is the answer, and it
    publishes on the skip and the failure paths too, because those are exactly
    the times someone is asking.
    """
    now = dt.datetime.now(dt.timezone.utc)
    out.mkdir(parents=True, exist_ok=True)
    (out / "odds_status.json").write_text(json.dumps(
        {"checked_at": now.isoformat(),
         "checked_at_human": now.strftime("%b %-d, %-I:%M %p UTC"), **kw},
        separators=(",", ":")))
    print(f"status: {kw.get('state')} — {kw.get('reason', '')}")

    # AND ON THE ACTIONS PAGE ITSELF. This step is continue-on-error, so its
    # log is a green check nobody opens — which is exactly how odds_latest.json
    # 404'd for days with the workflow reporting success every run. The job
    # summary is the one surface you see without clicking into anything.
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            icon = {"ok": "✅", "skipped": "⏸", "capped": "⏸"}.get(kw.get("state"), "⚠️")
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(f"### {icon} Odds — {kw.get('state')}\n\n{kw.get('reason','')}\n\n")
                for k in ("provider", "players", "match_rate", "fetches_this_slate",
                          "providers_tried", "keys_present", "next_eligible_at"):
                    if kw.get(k) not in (None, "", [], {}):
                        fh.write(f"- **{k}**: `{kw[k]}`\n")
                if kw.get("forensics"):
                    fh.write("\n<details><summary>request forensics</summary>\n\n```json\n"
                             + json.dumps(kw["forensics"], indent=1)[:3500]
                             + "\n```\n\n</details>\n")
                fh.write("\n")
        except Exception:
            pass


def write_empty_board(out: Path, reason: str, **extra) -> None:
    """Publish an EMPTY but valid odds_latest.json rather than no file at all.

    A 404 is the least informative failure this project can produce: it looks
    identical to a wrong URL, a branch that never published, and a bot that was
    never installed. A 200 carrying an empty board and the reason it is empty
    tells the site — and anyone with a browser — that the pipeline ran and came
    back with nothing, which is a completely different problem from the
    pipeline not existing.
    """
    now = dt.datetime.now(dt.timezone.utc)
    out.mkdir(parents=True, exist_ok=True)
    (out / "odds_latest.json").write_text(json.dumps({
        "source": "", "sport": SPORT,
        "fetched_at": now.isoformat(),
        "fetched_at_human": now.strftime("%b %-d, %-I:%M %p UTC"),
        "category_market": CATEGORY_MARKET,
        "by_player_id": {}, "by_name": {},
        "match_rate": 0, "unmatched": [],
        "empty": True, "reason": reason, **extra,
        "note": ("No prices this run. The file exists so the site can say WHY "
                 "instead of showing a missing-file 404 that looks the same as "
                 "a broken path."),
    }, separators=(",", ":")))
    print(f"wrote an empty odds_latest.json — {reason}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="report what the key can see; write nothing")
    ap.add_argument("--slate", type=str, default="",
                    help="slate to join names against; default tries both known locations")
    ap.add_argument("--out", type=str, default="public/data/current")
    ap.add_argument("--regions", type=str, default="us")
    ap.add_argument("--force", action="store_true",
                    help="ignore the request lock — fetch even if a fresh snapshot exists")
    ap.add_argument("--insecure", action="store_true",
                    help="PROBE DIAGNOSTIC ONLY: skip TLS verification, for machines "
                         "whose network intercepts certificates")
    a = ap.parse_args()
    out = Path(a.out)

    if a.insecure:
        import ssl
        globals()["_SSL"] = ssl._create_unverified_context()
        print("⚠ TLS verification OFF — diagnostic run only, never for publishing")

    key = os.environ.get("ODDS_API_KEY", "").strip()
    backup = os.environ.get("ODDSPAPI_KEY", "").strip()
    if not key and not backup and not os.environ.get("ODDSAPI_IO_KEY", "").strip():
        # Not an error: no key configured means the site simply has no odds
        # file and every surface that reads it degrades to the score alone.
        print("no odds key set (ODDSAPI_IO_KEY / ODDS_API_KEY / ODDSPAPI_KEY) "
              "— skipping (not a failure)")
        write_status(out, state="no_key",
                     reason="No ODDSAPI_IO_KEY / ODDS_API_KEY / ODDSPAPI_KEY is set "
                            "in the repo secrets, so nothing was fetched.")
        write_empty_board(out, "no odds key is set in the repo secrets", state="no_key")
        return 0

    if a.probe:
        # runs with whichever provider keys are in the environment — a probe
        # with no theoddsapi key still probes the primary and the backup
        return probe(key)

    # ORDER MATTERS AND IS THE WHOLE POINT OF A BACKUP. The primary is one
    # request for the entire slate. The backup's free tier is 250 requests a
    # MONTH against a workflow that fires 13 times a day, so it must only ever
    # run when the primary came back empty — never alongside it.
    # ── the lock ───────────────────────────────────────────────────────────
    try:
        min_gap = int(os.environ.get("ODDS_MIN_INTERVAL_MIN", "") or 90)
    except ValueError:
        min_gap = 90
    try:
        max_slate = int(os.environ.get("ODDS_MAX_PER_SLATE", "") or 5)
    except ValueError:
        max_slate = 5

    prev = published("odds_latest.json") if not a.force else None
    prev_count = 0
    if isinstance(prev, dict):
        stamp = prev.get("fetched_at")
        prev_count = int(prev.get("fetches_this_slate") or 0)
        # AN EMPTY BOARD DOESN'T DESERVE THE FULL LOCK (2026-08-15, Donovan:
        # "i still see no odds on the site" while the status read "skipped —
        # snapshot from 10 minutes ago is still fresh"). The 90-minute gap
        # exists to stop re-buying prices we already hold; when the last fetch
        # returned NOTHING there is nothing being protected, and books post
        # their MLB props mid-morning — a board that came up empty at 7am
        # must try again soon, not at 8:30. Empty snapshots retry on a short
        # leash and don't count against the per-slate cap.
        if prev.get("empty"):
            min_gap = min(min_gap, 20)
        try:
            last = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60
        except Exception:
            age, last = 1e9, None
        if age < min_gap:
            nxt = (last + dt.timedelta(minutes=min_gap)).isoformat() if last else None
            print(f"lock: last fetch was {age:.0f} min ago (< {min_gap}) — skipping, 0 requests")
            write_status(out, state="skipped",
                         reason=f"A snapshot from {age:.0f} minutes ago is still fresh "
                                f"(the lock allows one fetch every {min_gap} minutes).",
                         last_fetch=stamp, next_eligible_at=nxt,
                         fetches_this_slate=prev_count, players=len(prev.get("by_player_id") or {}))
            return 0
        if prev_count >= max_slate:
            print(f"lock: {prev_count} fetches already this slate (cap {max_slate}) — skipping")
            write_status(out, state="capped",
                         reason=f"{prev_count} fetches already made for this slate; the cap "
                                f"is {max_slate}. Resets when the slate date changes.",
                         last_fetch=stamp, fetches_this_slate=prev_count,
                         players=len(prev.get("by_player_id") or {}))
            return 0

    oaio_key = os.environ.get("ODDSAPI_IO_KEY", "").strip()
    books = os.environ.get("ODDSAPI_IO_BOOKMAKERS", "").strip() or OAIO_BOOKS

    available = {
        "oddsapiio": (oaio_key, lambda: fetch_oddsapiio(oaio_key, books)),
        "theoddsapi": (key, lambda: fetch_theoddsapi(key, a.regions)),
        "oddspapi": (backup, lambda: fetch_oddspapi(backup)),
    }
    # Order is an env var so changing which provider leads is a repo-variable
    # edit, not a code change and a redeploy.
    order = [n.strip() for n in
             (os.environ.get("ODDS_PROVIDER_ORDER", "").strip()
              or "oddsapiio,theoddsapi,oddspapi").split(",") if n.strip()]

    rows, source = [], ""
    for name, k, fn in ((n, *available[n]) for n in order if n in available):
        if not k:
            print(f"{name}: no key configured — skipping")
            continue
        print(f"trying {name} …")
        rows = fn()
        _rec(name, "·result", quotes=len(rows))
        if rows:
            source = name
            break
        print(f"  {name} yielded nothing — falling through")

    if not rows:
        print("no odds from any provider — publishing nothing, "
              "the site falls back to scores alone")
        write_status(out, state="empty",
                     reason="Every configured provider was tried and none returned a "
                            "quote. Usually a spent quota, a wrong bookmaker key, or "
                            "no props posted for this slate yet.",
                     providers_tried=[n for n in order if n in available],
                     keys_present=[n for n in order
                                   if n in available and available[n][0]],
                     forensics=FORENSICS)
        write_empty_board(
            out,
            "every configured provider was tried and none returned a quote",
            state="empty",
            providers_tried=[n for n in order if n in available],
            keys_present=[n for n in order if n in available and available[n][0]])
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

    # WHICH SLATE IS THIS? Not "what is today in UTC" -- today.yml fires at
    # 00:00, 01:00, 02:00 and 03:00 UTC, which is still the PREVIOUS day's card
    # on the west coast. Filing those under the UTC date puts the truest
    # closing snapshot of the night (03:00 UTC, minutes before a Pacific first
    # pitch) on the wrong day, where odds_history would settle it against the
    # wrong box scores.
    #
    # The first attempt read `slate.get("slate_date")` behind an
    # `isinstance(s, dict)` guard. public/data/today.json is a BARE LIST --
    # mlb_dashboard writes `[asdict(r) for r in rows]` -- so that branch never
    # ran once and every snapshot silently took the UTC fallback. The rows
    # themselves carry the answer, so take it from them.
    slate_date = None
    try:
        if slate_path.exists():
            s = json.loads(slate_path.read_text())
            if isinstance(s, dict):
                slate_date = s.get("slate_date") or s.get("date")
                if not slate_date:
                    s = s.get("players") or []
            if isinstance(s, list) and s:
                # Every row carries the slate's own date; game_time is the
                # fallback, and it is a UTC timestamp for a game that belongs
                # to the LOCAL day, so it is converted to US Eastern the same
                # way the site's StaleBanner pins its own idea of "tonight".
                for r in s[:200]:
                    if not isinstance(r, dict):
                        continue
                    d = r.get("slate_date") or r.get("date") or r.get("game_date")
                    if d:
                        slate_date = str(d)[:10]
                        break
                if not slate_date:
                    t = next((r.get("game_time") for r in s
                              if isinstance(r, dict) and r.get("game_time")), None)
                    if t:
                        ts = dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=dt.timezone.utc)
                        slate_date = (ts - dt.timedelta(hours=4)).date().isoformat()
    except Exception as e:
        print(f"  slate date not readable ({type(e).__name__}: {e})")
    if not slate_date:
        # Last resort, and it is US Eastern rather than UTC for the same reason.
        slate_date = (now - dt.timedelta(hours=4)).date().isoformat()
        print(f"  WARNING: no slate date found; filing under {slate_date} (US/Eastern of now)")
    slate_date = str(slate_date)[:10]

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
        # Counts toward ODDS_MAX_PER_SLATE. Resets when the slate date rolls,
        # so a long night can't spend tomorrow's budget.
        "fetches_this_slate": (prev_count + 1
                               if isinstance(prev, dict) and prev.get("slate_date") == slate_date
                               else 1),
        "slate_date": slate_date,
        "note": ("Consensus line is the one the most books post; over/under are the "
                 "median price at that line; best_over is the best available price "
                 "for taking the over. `implied` is the break-even percentage — "
                 "compare it to a hit rate, not to a score."),
    }
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "odds_latest.json"
    dest.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {dest} ({dest.stat().st_size / 1024:.0f} KB)")

    # ── THE DATED SNAPSHOT — the only irreplaceable thing this script does ───
    #
    # odds_latest.json is overwritten thirteen times a day and again tomorrow.
    # Tonight's closing price, once it's gone, is gone: no free API sells you
    # back the number a hitter was at last Tuesday. So every run also drops a
    # dated, slim copy, and bots/odds_history.py joins those to the graded
    # files to answer the question a single night never can — is the book
    # right about THIS hitter.
    #
    # Slim on purpose: line, over price, break-even. No book names, no game
    # strings, no by-name board. ~50 KB a night instead of ~450 KB, because
    # this file is kept for months and the rest of it is reconstructible or
    # irrelevant after first pitch.
    #
    # LAST WRITE WINS, and that is the behaviour we want: the workflow fires
    # through the evening, so the final snapshot before the branch publish is
    # the closest thing to a closing line this pipeline can get.

    slim: dict[str, dict] = {}
    for pid, mkts in matched.items():
        row = {m: [q.get("line"), q.get("over"), q.get("implied")]
               for m, q in mkts.items()
               if isinstance(q, dict) and q.get("over") is not None and q.get("line") is not None}
        if row:
            slim[pid] = row
    snap = out / f"odds_{slate_date}.json"
    snap.write_text(json.dumps({
        "date": slate_date,
        "fetched_at": now.isoformat(),
        "source": source,
        "rows": slim,
        "note": ("Pre-game snapshot kept for bots/odds_history.py. Each row is "
                 "{market: [line, over, implied]}. Keyed by MLB player_id only — "
                 "an unjoined name has no outcome to settle against, so it would "
                 "never be usable here."),
    }, separators=(",", ":")))
    print(f"wrote {snap} — {len(slim)} players priced on {slate_date} "
          f"({snap.stat().st_size / 1024:.0f} KB)")
    if not slim:
        print("  WARNING: nothing joined to the slate, so the history gets no "
              "row for tonight. Check the slate join above.")

    write_status(out, state="ok",
                 reason=f"Fetched from {source}; {len(matched)} of {len(board)} priced "
                        f"players joined to the slate.",
                 provider=source, slate_date=slate_date,
                 players=len(matched), priced=len(board),
                 match_rate=payload["match_rate"],
                 fetches_this_slate=payload["fetches_this_slate"],
                 snapshot_players=len(slim),
                 # kept on the good path too — a fallback provider succeeding
                 # while the primary 401s is a finding worth publishing
                 forensics=FORENSICS)

    try:
        u = api("/me/usage", key)
        print(f"quota: {json.dumps(u)[:200]}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
