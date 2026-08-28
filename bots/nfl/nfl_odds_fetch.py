#!/usr/bin/env python3
"""nfl_odds_fetch.py — the book's line next to the NFL bot's score.

    python bots/nfl/nfl_odds_fetch.py --probe
    python bots/nfl/nfl_odds_fetch.py --out public/data/current

The NFL sibling of bots/odds_fetch.py (MLB). Same reason to exist: nfl_scoring
produces a 0-100 RANKING for seven markets (TD, REC_YDS, REC, RUSH_YDS,
RUSH_ATT, PASS_YDS, KICK_PTS) with no price attached, so "top play this week"
is a rank, not a bet. This fetches a real line and joins it to the slate by
player_id, same shape as MLB's odds_latest.json, so the site can show
"scores well AND the book agrees" instead of a bare number.

THE KEYS ARE SECRETS, SHARED ACROSS EVERY WORKFLOW IN THIS REPO. ODDS_API_KEY /
ODDSAPI_IO_KEY / ODDSPAPI_KEY already exist as repo secrets for the MLB fetch
(.github/workflows/today.yml, odds-probe.yml) and this reads the same three —
nothing new to provision. They must NEVER reach moonshot-mlb/moonshot-nfl:
those sites are static builds, so the bot fetches and publishes a plain JSON
file to the data branch, exactly like every other payload.

── SCOPE CUT FROM THE MLB VERSION, AND WHY (read before extending this) ───────

bots/odds_fetch.py is ~1800 lines because it juggles THREE independently-
shaped providers (odds-api.io primary, theoddsapi.com backup, oddspapi.io
second backup), a per-slate request budget with its own six-day deadlock bug
once, and a freeze-at-first-pitch rule per player. None of that is free, and
Donovan asked for speed on this pass. What got cut, on purpose:

  1. ONE PROVIDER, NOT THREE. The task spec for this file says "same
     key-reading pattern (try ODDS_API_KEY, fall back to the other two)" --
     i.e. three CREDENTIALS tried against one endpoint, not three different
     integrations. That is what this implements: the `api()` helper below is
     a straight port of odds_fetch.py's `fetch_theoddsapi` path (same BASE,
     same x-api-key header, same /props/-then-/odds/ shape, same sport_key +
     markets + oddsFormat params) — the one MLB provider whose request shape
     is simple enough to trust without a live call to verify it, since the
     sandbox this was written in cannot reach any odds host (same limitation
     odds_fetch.py's own `unwrap()` docstring notes). odds-api.io's request
     shape (OAIO_BOOKS, /odds-by-tournaments, apiKey query param) and
     oddspapi.io's are NOT ported: their request/response shapes were reverse
     engineered against baseball prop labels specifically (PROP_HINTS is a
     hand-tuned substring table for "Hits + Runs + RBIs" spelling variants),
     and re-guessing that exercise blind for football markets this session
     cannot verify would be compounding one unverified guess on another.
     CONFIDENCE: LOW that ODDSAPI_IO_KEY / ODDSPAPI_KEY are valid credentials
     for the api.theoddsapi.com host at all -- they are provisioned for
     different services in the MLB script. They are tried in the documented
     order anyway because that is what was asked for; a wrong key here fails
     the same way an absent one does (empty result, reported honestly in
     odds_status.json), so trying it costs one clearly-logged HTTP call, not
     a silent wrong answer.

  2. NO nfl_odds_history.json (dated snapshot). MLB's odds_YYYY-MM-DD.json
     exists so bots/odds_history.py can join a closing line to a graded box
     score after the fact. Nothing on the NFL side reads a dated odds
     snapshot yet, and NFL history isn't lost the way MLB's is -- odds
     archives (Pro Football Reference, the-odds-api's own historical
     endpoint) exist for football in a way same-night baseball lines don't.
     Skip for now; add it the day something needs to grade against it.

  3. NO per-slate fetch cap / deadlock-prone counter. MLB's ODDS_MAX_PER_SLATE
     gate had a real production bug (see odds_fetch.py's "SIX-DAY DEADLOCK"
     comment) from conflating "fetches this slate" with "slate changed."
     NFL's slate is a WEEK, not a night, and nfl.yml already fires only ~12
     times a week on its own schedule (vs MLB's ~13/day) -- the schedule
     itself is most of the rate limiting. A simple min-interval lock (below)
     is kept because it's cheap and prevents back-to-back manual re-runs from
     burning quota; the slate-cap counter and its failure mode are not.

  4. NO freeze-at-first-pitch (kickoff). MLB freezes a hitter's price at his
     own game's first pitch so a graded record never compares a rate to a
     LIVE (in-game) price. The same argument applies to NFL kickoff and this
     is the next thing to port if odds get graded against outcomes here --
     cut only because "load-bearing pieces for a first pass" (per the task
     spec) are latest + status, and this doubles the size of main() for a
     feature nothing downstream reads yet.

Everything else -- name normalisation, American-odds math, the consensus-line
algorithm, the empty-board-is-still-a-valid-file discipline, the status file
answering "did the fetch even happen" -- is a straight port, because none of
that is MLB-specific.
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
SPORT = "americanfootball_nfl"

# ── MARKET MAP: nfl_scoring.MODELS key -> the-odds-api player-prop market key ─
#
# nfl_scoring.MODELS (bots/nfl/nfl_scoring.py) is the source of truth for the
# seven market keys and their bars. Every one of the seven has a documented
# the-odds-api NFL player-prop market (https://the-odds-api.com convention --
# `player_<stat>` for totals, `player_anytime_td` for the TD market), unlike
# MLB where two GRID_MARKETS rows (walks, strikeouts) simply have no batter
# market and carry no price. Confidence noted per row; nothing here is forced
# -- if a row turns out wrong on the first real probe, the fix is one line.
#
#   TD        player_anytime_td       HIGH   -- the standard "any player to
#                                                score a TD" market; every
#                                                major book prices it.
#   REC_YDS   player_reception_yds    HIGH   -- standard total-yards prop.
#   REC       player_receptions      HIGH   -- standard reception-count prop;
#                                                note the plural (matches
#                                                MLB's own batter_hits vs
#                                                batter_total_bases naming
#                                                split between count and yards
#                                                markets).
#   RUSH_YDS  player_rush_yds        HIGH   -- standard total-yards prop.
#   RUSH_ATT  player_rush_attempts   HIGH   -- standard attempt-count prop.
#   PASS_YDS  player_pass_yds        HIGH   -- standard total-yards prop.
#   KICK_PTS  player_kicking_points  MEDIUM -- exists in the-odds-api's docs,
#                                                but two things make it the
#                                                least trustworthy row here:
#                                                (a) it's a thinner market --
#                                                fewer books price it, so an
#                                                empty result for JUST this
#                                                key would not be surprising
#                                                and should not be read as the
#                                                whole fetch being broken; (b)
#                                                the semantic match is looser
#                                                than the others -- the model
#                                                grades fg_made*3 + pat_made
#                                                (nfl_scoring.OUTCOME["KICK_PTS"]),
#                                                and this market key needs to
#                                                mean the same "total kicking
#                                                points" scoring, which the
#                                                first live probe should
#                                                confirm rather than assume.
#
# ── B1 ADDITIONS (2026-08-28 master plan, Donovan's market list) ────────────
# Two more of his named markets, NOT yet in nfl_scoring.MODELS -- there is no
# graded bar for either one today, so these publish PRICE DATA ONLY (via
# nfl_odds_latest.json's by_player_id/by_name maps, same as the seven above)
# with nothing on the site to compare it against yet. That's the literal ask
# ("make sure we can get data for that") and it's harmless to add: nothing
# below keys off "exactly seven markets" (see CONFIDENCE note under each) --
# fetch_odds/walk_outcomes/consensus/probe all just iterate whatever's in
# MARKETS, and nfl_scoring.MODELS is never imported by this file, so a market
# with no MODELS entry doesn't error, it just never gets scored (yet).
#
#   FTD       player_1st_td          UNCONFIRMED -- documented on
#                                                the-odds-api's market list as
#                                                a standard companion to
#                                                player_anytime_td, but tier
#                                                availability is exactly what
#                                                this probe exists to answer
#                                                -- unlike the seven above,
#                                                this one hasn't shipped
#                                                anywhere in this repo before,
#                                                so there's no prior probe run
#                                                to point to.
#   LONG_REC  player_longest_reception  UNCONFIRMED -- same situation: a
#                                                documented market key, no
#                                                prior confirmation it prices
#                                                for NFL games on this key's
#                                                tier. "Longest Reception" is
#                                                Donovan's own phrase for it
#                                                (2026-08-28 notebook).
CATEGORY_MARKET = {
    "TD":       "player_anytime_td",
    "REC_YDS":  "player_reception_yds",
    "REC":      "player_receptions",
    "RUSH_YDS": "player_rush_yds",
    "RUSH_ATT": "player_rush_attempts",
    "PASS_YDS": "player_pass_yds",
    "KICK_PTS": "player_kicking_points",
    "FTD":      "player_1st_td",
    "LONG_REC": "player_longest_reception",
}
MARKETS = sorted(set(CATEGORY_MARKET.values()))

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(s: str) -> str:
    """'Ja'Marr Chase Jr.' -> 'ja marr chase'. Must match lib/nfl/oddsMatch.js's
    normName() -- see that file's comment; a drift here silently breaks the
    by-name fallback exactly like it would on the MLB side."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if c.isalnum() or c.isspace() else " " for c in s).lower()
    parts = [p for p in s.split() if p]
    while len(parts) > 1 and parts[-1] in SUFFIXES:
        parts.pop()
    return " ".join(parts)


# ── FORENSICS ────────────────────────────────────────────────────────────────
# Same discipline as odds_fetch.py: every HTTP attempt leaves a sanitised
# record (endpoint, status, first bytes of body) so odds_status.json can
# answer "what actually happened" without anyone opening the Actions log.
FORENSICS: list = []
RAW_DUMP: list = []
_SSL = None


def _open_kw() -> dict:
    kw = {"timeout": 45}
    if _SSL is not None:
        kw["context"] = _SSL
    return kw


def _rec(provider: str, path: str, key: str = "", **kw) -> None:
    row = {"provider": provider, "path": path, **kw}
    if key:
        for k2, v in list(row.items()):
            if isinstance(v, str) and key in v:
                row[k2] = v.replace(key, "•KEY•")
    http = row.get("http")
    if (isinstance(http, int) and http >= 400) or row.get("error"):
        why = row.get("body") or row.get("error") or ""
        print(f"    ↳ {provider} {path} → {http or 'error'}: {str(why)[:200]}")
    if len(FORENSICS) < 40:
        FORENSICS.append(row)


def _snip(e) -> str:
    try:
        return e.read()[:220].decode(errors="replace")
    except Exception:
        return ""


def api(path: str, key: str, **params) -> object:
    """Direct port of odds_fetch.py's api() -- same host, same auth header,
    same call shape. See the module docstring for why this is the one
    provider ported rather than all three."""
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
    """American odds -> break-even probability, as a percentage."""
    if odds is None:
        return None
    p = (-odds) / ((-odds) + 100) if odds < 0 else 100 / (odds + 100)
    return round(100 * p, 1)


def unwrap(payload) -> list:
    """The response as a list of events, whatever it arrived wrapped in.
    Same tolerant unwrap as MLB's -- this endpoint's exact NFL response shape
    is unverified from this sandbox (it cannot reach the host); accepting the
    documented bare list and the common wrappers means a first live run
    either works or prints the real shape instead of guessing wrong twice."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "events", "odds", "results", "games"):
            v = payload.get(k)
            if isinstance(v, list):
                print(f"  response wrapped in '{k}'")
                return v
        if payload.get("bookmakers") or payload.get("home_team"):
            return [payload]
    return []


def flat_rows(items: list) -> list[dict]:
    """Fallback for a FLAT prop shape -- one object per player/market/book."""
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
    """Flatten the standard event -> bookmakers[] -> markets[] -> outcomes[]
    shape into one row per (player, market, book)."""
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


def fetch_odds(key: str, regions: str) -> list[dict]:
    """Port of odds_fetch.py's fetch_theoddsapi(). /props/ is the player-prop
    endpoint on this host (per that file's own forensics -- verified there
    against live 2026-08-15 traffic for MLB; unverified for NFL specifically,
    since the sport is selected by `sport_key` rather than the path). /odds/
    stays as a fallback for a plan that surfaces props there instead."""
    for path in ("/props/", "/odds/"):
        try:
            raw = api(path, key, sport_key=SPORT, markets=",".join(MARKETS),
                      regions=regions, oddsFormat="american")
        except urllib.error.HTTPError as e:
            print(f"  theoddsapi {path}: HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
            if path == "/props/" and e.code == 403:
                print("  theoddsapi: player props need their Business tier -- "
                      "this key can't serve them. Skipping /odds/ (game lines only).")
                _rec("theoddsapi", "·verdict",
                     note="403 on /props/ -- key's plan does not include player props; "
                          "/odds/ carries no player_* markets, so this provider is "
                          "unusable for props until the plan changes")
                return []
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


def consensus(rows: list[dict]) -> dict:
    """{norm_name: {market: {line, over, under, best_over, best_book, books}}}
    Direct port of odds_fetch.py's consensus() -- generic over rows, nothing
    MLB-specific in the algorithm itself."""
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
                "lines_seen": len(counts),
                "name": rs[0]["name"],
                "game": f'{rs[0].get("away") or "?"} @ {rs[0].get("home") or "?"}',
                "commence": rs[0].get("commence"),
            }
    return out


# ── PUBLISHED-STATE HELPERS ─────────────────────────────────────────────────
# NFL rides the same repo/data branch as MLB -- confirmed against
# lib/nfl/dataSource.js's own REPO constant, so this is not a guess.
DATA_RAW = ("https://raw.githubusercontent.com/donthebuilder/"
            "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")


def published(name: str):
    """The current published copy of a data-branch file, or None."""
    try:
        req = urllib.request.Request(f"{DATA_RAW}/{name}",
                                     headers={"User-Agent": "moonshot-nfl-odds"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def write_status(out: Path, **kw) -> None:
    """Say what happened, every single time, including nothing. Same
    discipline as odds_fetch.py's write_status() -- a step that silently
    continue-on-errors is a feature that can be dead for weeks unnoticed."""
    now = dt.datetime.now(dt.timezone.utc)
    out.mkdir(parents=True, exist_ok=True)
    (out / "nfl_odds_status.json").write_text(json.dumps(
        {"checked_at": now.isoformat(),
         "checked_at_human": now.strftime("%b %-d, %-I:%M %p UTC"), **kw},
        separators=(",", ":")))
    print(f"status: {kw.get('state')} — {kw.get('reason', '')}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            icon = {"ok": "✅", "skipped": "⏸", "capped": "⏸"}.get(kw.get("state"), "⚠️")
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(f"### {icon} NFL odds — {kw.get('state')}\n\n{kw.get('reason','')}\n\n")
                for k in ("provider", "players", "match_rate",
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
    """Publish an EMPTY but valid nfl_odds_latest.json rather than no file at
    all -- a 404 looks identical to "never installed"; this looks like what it
    is."""
    now = dt.datetime.now(dt.timezone.utc)
    out.mkdir(parents=True, exist_ok=True)
    (out / "nfl_odds_latest.json").write_text(json.dumps({
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
    print(f"wrote an empty nfl_odds_latest.json — {reason}")


def resolve_key() -> tuple[str, str]:
    """Which credential to use, in the documented order. See the module
    docstring's scope-cut #1 for why these three env vars (provisioned for
    three DIFFERENT MLB providers) are all tried against this one host."""
    for name in ("ODDS_API_KEY", "ODDSAPI_IO_KEY", "ODDSPAPI_KEY"):
        v = os.environ.get(name, "").strip()
        if v:
            return name, v
    return "", ""


def probe(key: str, regions: str) -> int:
    """What does the key actually see for NFL player props -- run this before
    anything depends on it."""
    if not key:
        print("no odds key — skipping the probe\n")
        return 0
    print(f"probing {BASE} sport_key={SPORT} markets={','.join(MARKETS)} …")
    rows = fetch_odds(key, regions)
    if not rows:
        print("no quotes came back — see the forensics above for the real reason")
        return 1
    seen_markets = sorted({r["market"] for r in rows})
    seen_books = sorted({r["book"] for r in rows})
    print(f"\n{len(rows)} quote(s) across {len(seen_markets)} market(s), {len(seen_books)} book(s)")
    print(f"  markets: {seen_markets}")
    print(f"  books:   {seen_books}")
    for m in MARKETS:
        n = sum(1 for r in rows if r["market"] == m)
        cat = next((c for c, mk in CATEGORY_MARKET.items() if mk == m), "?")
        print(f"  {m:<26} ({cat:<9}): {n} quote(s)" + ("" if n else "  — NOT PRICED"))
    return 0


def _write_dump(path: str) -> None:
    if not path or not RAW_DUMP:
        return
    try:
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        txt = json.dumps(RAW_DUMP, indent=1)[:4_000_000]
        for env_key in ("ODDSAPI_IO_KEY", "ODDS_API_KEY", "ODDSPAPI_KEY"):
            v = os.environ.get(env_key, "").strip()
            if v:
                txt = txt.replace(v, "•KEY•")
        p.write_text(txt)
        print(f"raw payload written to {path} ({len(txt) // 1024} KB) — keys stripped, safe to send")
    except Exception as e:
        print(f"could not write {path}: {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="report what the key can see; write nothing")
    ap.add_argument("--slate", type=str, default="",
                    help="slate file to join names against; default is <out>/nfl_week.json")
    ap.add_argument("--out", type=str, default="public/data/current")
    ap.add_argument("--regions", type=str, default="us")
    ap.add_argument("--force", action="store_true",
                    help="ignore the request lock — fetch even if a fresh snapshot exists")
    ap.add_argument("--insecure", action="store_true",
                    help="PROBE DIAGNOSTIC ONLY: skip TLS verification, for machines "
                         "whose network intercepts certificates")
    ap.add_argument("--dump", type=str, default="",
                    help="write the raw provider payload to this file")
    a = ap.parse_args()
    out = Path(a.out)

    if a.insecure:
        import ssl
        globals()["_SSL"] = ssl._create_unverified_context()
        print("⚠ TLS verification OFF — diagnostic run only, never for publishing")

    key_name, key = resolve_key()
    if not key:
        print("no odds key set (ODDS_API_KEY / ODDSAPI_IO_KEY / ODDSPAPI_KEY) "
              "— skipping (not a failure)")
        write_status(out, state="no_key",
                     reason="No ODDS_API_KEY / ODDSAPI_IO_KEY / ODDSPAPI_KEY is set "
                            "in the repo secrets, so nothing was fetched.")
        write_empty_board(out, "no odds key is set in the repo secrets", state="no_key")
        return 0

    if a.probe:
        return probe(key, a.regions)

    # ── the lock: a light rate limit, not MLB's per-slate cap (see module
    # docstring #3 for why the cap itself was left out) ──────────────────────
    try:
        min_gap = int(os.environ.get("NFL_ODDS_MIN_INTERVAL_MIN", "") or 60)
    except ValueError:
        min_gap = 60

    prev = published("nfl_odds_latest.json") if not a.force else None
    if isinstance(prev, dict):
        stamp = prev.get("fetched_at")
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
                         reason=(f"A snapshot from {age:.0f} minutes ago is still fresh "
                                 f"(the lock allows one fetch every {min_gap} minutes)."
                                 + (f" That snapshot was EMPTY — {prev.get('reason') or 'no reason recorded'}"
                                    if prev.get("empty") else "")),
                         last_fetch=stamp, next_eligible_at=nxt,
                         players=len(prev.get("by_player_id") or {}),
                         last_attempt_forensics=prev.get("forensics") or [])
            return 0

    print(f"trying theoddsapi ({key_name}) …")
    rows = fetch_odds(key, a.regions)
    _rec("theoddsapi", "·result", quotes=len(rows))
    _write_dump(a.dump)

    if not rows:
        print("no odds from the provider — publishing nothing, the site falls back to scores alone")
        verdicts = [f"{r['provider']}: {r['note']}" for r in FORENSICS
                    if r.get("path") in ("·verdict", "·isolate") and r.get("note")]
        reason = (" · ".join(dict.fromkeys(verdicts)) if verdicts else
                  "The provider returned no quote for any of the seven markets. Usually a "
                  "spent quota, a wrong key, or no NFL player props posted for this week yet.")
        write_status(out, state="empty", reason=reason,
                     keys_present=[n for n in ("ODDS_API_KEY", "ODDSAPI_IO_KEY", "ODDSPAPI_KEY")
                                   if os.environ.get(n, "").strip()],
                     forensics=FORENSICS)
        write_empty_board(out, reason, state="empty", forensics=FORENSICS)
        return 0

    print(f"using theoddsapi · {len(rows)} quote(s)")
    board = consensus(rows)

    # ── join to the slate ────────────────────────────────────────────────────
    matched, unmatched = {}, []
    slate_path = Path(a.slate) if a.slate else (out / "nfl_week.json")
    week = season = mode = None
    if slate_path.exists():
        print(f"  joining against {slate_path}")
        try:
            slate = json.loads(slate_path.read_text())
            players = slate.get("players") if isinstance(slate, dict) else slate
            week, season, mode = (slate.get("week"), slate.get("season"), slate.get("mode")) \
                if isinstance(slate, dict) else (None, None, None)
            by_norm = {}
            for p in players or []:
                n = norm_name(p.get("name") or p.get("player_name"))
                if n:
                    by_norm.setdefault(n, p)
            for norm, mkts in board.items():
                p = by_norm.get(norm)
                if not p:
                    unmatched.append(next(iter(mkts.values()), {}).get("name") or norm)
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
        "week": week, "season": season, "mode": mode,
        # No freeze-at-kickoff yet (module docstring #4) -- every quote here
        # is whatever the provider had at fetch time, live game or not.
        "by_player_id": matched,
        "by_name": board,
        "match_rate": round(100 * len(matched) / max(1, len(board)), 1),
        "unmatched": sorted(unmatched),
        "note": ("Consensus line is the one the most books post; over/under are the "
                 "median price at that line; best_over is the best available price "
                 "for taking the over. `implied` is the break-even percentage — "
                 "compare it to a hit rate, not to a score. Not frozen at kickoff "
                 "yet — a live quote can appear here once a game starts."),
    }
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "nfl_odds_latest.json"
    dest.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {dest} ({dest.stat().st_size / 1024:.0f} KB)")

    # ── THE DATED SNAPSHOT (B1, 2026-08-28) — same reason odds_fetch.py keeps
    # one: nfl_odds_latest.json is overwritten on ~12 firings/week and again
    # next week, so a price once gone is gone. Direct port of MLB's slim
    # shape (line/over/implied per market, no book names or game strings) --
    # the one difference is the join key. MLB keys by slate_date because it
    # grades one night at a time; NFL grades a whole WEEK
    # (bots/nfl/nfl_results.py's outcomes()), so season/week ride along in the
    # snapshot body for whatever eventually joins these into nfl_odds_history.json
    # -- but the FILE is still named by the calendar date this fetch ran, not
    # by week, matching append_nfl_outcome_log()'s own precedent in
    # nfl_results.py (same file, same reasoning: NFL weeks span three-plus
    # calendar dates, so there is no single "the week's file" to accumulate
    # into the way MLB's one-file-per-night does). publish_data.sh's generic
    # accumulate-and-cap loop sorts filenames lexically to mean chronologically
    # -- an ISO date keeps that true; a week number would not sort the same
    # way once a new season starts.
    #
    # NOTE: nfl_odds_history.json itself (the join against nfl_results.json's
    # "lines" -- see that file's outcomes(), which already publishes exactly
    # {player_id: {market: actual}} for every eligible player, no separate
    # SETTLE table needed) is NOT built yet. Deliberately deferred: there is
    # nothing to join against until a few weeks of these snapshots exist, and
    # NFL's week-spans-multiple-dates shape needs its own grouping logic this
    # pass didn't have real accumulated data to verify against. This snapshot
    # writer is the prerequisite step; the joiner is the next one, once real
    # snapshots exist to test it against.
    slim: dict[str, dict] = {}
    for pid, mkts in matched.items():
        row = {m: [q.get("line"), q.get("over"), q.get("implied")]
               for m, q in mkts.items()
               if isinstance(q, dict) and q.get("over") is not None and q.get("line") is not None}
        if row:
            slim[pid] = row
    date_str = now.date().isoformat()
    snap = out / f"nfl_odds_{date_str}.json"
    snap.write_text(json.dumps({
        "date": date_str,
        "season": season, "week": week,
        "fetched_at": now.isoformat(),
        "source": "theoddsapi",
        "rows": slim,
        "note": ("Snapshot kept for a future nfl_odds_history.py. Each row is "
                 "{market: [line, over, implied]}. Keyed by NFL player_id only -- "
                 "an unjoined name has no outcome to settle against, so it would "
                 "never be usable here."),
    }, separators=(",", ":")))
    print(f"wrote {snap} — {len(slim)} players priced on {date_str} "
          f"({snap.stat().st_size / 1024:.0f} KB)")

    write_status(out, state="ok",
                 reason=f"Fetched from theoddsapi; {len(matched)} of {len(board)} priced "
                        f"players joined to the slate.",
                 provider="theoddsapi", players=len(matched), priced=len(board),
                 match_rate=payload["match_rate"], snapshot_players=len(slim),
                 forensics=FORENSICS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
