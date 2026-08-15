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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="report what the key can see; write nothing")
    ap.add_argument("--slate", type=str, default="public/data/current/today.json",
                    help="slate to join names against (for the match-rate report)")
    ap.add_argument("--out", type=str, default="public/data/current")
    ap.add_argument("--regions", type=str, default="us")
    a = ap.parse_args()

    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        # Not an error: no key configured means the site simply has no odds
        # file and every surface that reads it degrades to the score alone.
        print("ODDS_API_KEY not set — skipping odds (this is not a failure)")
        return 0

    if a.probe:
        return probe(key)

    try:
        events = api("/odds/", key, sport_key=SPORT, markets=",".join(MARKETS),
                     regions=a.regions, oddsFormat="american")
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode(errors="replace")
        print(f"odds fetch failed — HTTP {e.code}: {body}")
        return 0            # never fail the slate build over odds
    except Exception as e:
        print(f"odds fetch failed — {type(e).__name__}: {e}")
        return 0

    rows = walk_outcomes(events if isinstance(events, list) else [])
    print(f"{len(events) if isinstance(events, list) else 0} event(s) · {len(rows)} prop quote(s)")
    if not rows:
        print("no player-prop outcomes in the response — run --probe and check the shape")
        return 0

    board = consensus(rows)

    # ── join to the slate, and REPORT the miss rate ────────────────────────
    matched, unmatched = {}, []
    slate_path = Path(a.slate)
    if slate_path.exists():
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
        "source": "theoddsapi",
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
