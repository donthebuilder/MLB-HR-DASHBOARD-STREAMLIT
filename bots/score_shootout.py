#!/usr/bin/env python3
"""
🥊 SCORE SHOOTOUT — which ordering actually put the homers on top?

2026-08-09. Donovan, after the site moved off the ISO-adjusted ranking:
"ranked in order, I thought the raw score was more correct."

That is an empirical claim and neither of us should be settling it by opinion.
This settles it against the graded archive.

WHY THE ORIGINAL AUDIT MIGHT HAVE MISSED WHAT HE'S SEEING
---------------------------------------------------------
The 2026-08-04 finding — ISO bands separating home-run rate 8.2% to 22.2%
while score quartiles moved only ~4.7 points — measured SEPARATION ACROSS THE
WHOLE POOL. That is the right test for "does ISO carry signal". It is NOT the
test for "does multiplying by ISO improve the TOP OF THE BOARD", and the top is
the only part anybody reads.

Those two can disagree, and here is the mechanism:

  · the adjustment DRAGS DOWN high-score, thin-ISO bats — some of whom are
    high-score for good reasons the ISO band knows nothing about (a favourable
    park, a leaking arm, a pitch-type exploit)
  · and it PUSHES UP low-score, big-ISO bats, who are cheap to promote because
    ISO is a season-long trait and says nothing about tonight

A rule that improves average separation over 3,973 slots can still make the top
20 worse. So this measures where it matters: at the top.

WHAT IT COMPARES, on identical rows
  raw       the bot's published hr_score, which the site now ranks on
  adjusted  hr_score × the measured HR rate of the hitter's ISO band — the
            site's old ranking, reproduced from lib/scoring_additions.js
  iso       season ISO alone, as the control the audit implied was strongest
  random    shuffled, as the floor — if a real ordering can't beat noise on
            this sample, the sample is too small to conclude anything

HOW IT SCORES THEM
  · HR rate in the top 10 / 20 / 50 of each night's board, averaged over nights
  · plus the pooled rate with a 95% Wilson interval, because two orderings
    three points apart on 400 picks is not a difference
  · and a HEAD-TO-HEAD count: on how many individual nights did each ordering
    put more homers in the top 20 than the other

An honest run can conclude "no measurable difference", and on this sample size
that is the most likely honest answer. It says so when it happens.

CAVEAT STATED UP FRONT, because it bounds everything below: graded files
contain the bot's DESIGNATED PICKS, not the full slate. So this measures how
well each ordering sorts the picks the bot already made — not how well it would
have sorted all 260 hitters. It is the right question for the board's top,
which is drawn from those picks, and the wrong question for the whole pool.

Usage:
    python bots/score_shootout.py
    python bots/score_shootout.py --top 20 --days 38
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "bots" else SCRIPT_DIR
PUBLIC_CURRENT = REPO_ROOT / "public" / "data" / "current"
DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")

# The site's old adjustment, reproduced exactly so the comparison is fair.
# Band floors and their measured relative HR rate, from the 2026-08-04 audit
# (lib/scoring_additions.js). Interpolated between floors, as the site did.
ISO_BANDS = [(0.000, 0.56), (0.130, 0.78), (0.170, 1.06), (0.230, 1.52)]


def num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def iso_mult(iso: float | None) -> float:
    """The site's old multiplier: linear between the measured band floors."""
    if iso is None:
        return 1.0
    if iso <= ISO_BANDS[0][0]:
        return ISO_BANDS[0][1]
    for (lo, ml), (hi, mh) in zip(ISO_BANDS, ISO_BANDS[1:]):
        if iso <= hi:
            span = hi - lo
            return ml if span <= 0 else ml + (mh - ml) * ((iso - lo) / span)
    return ISO_BANDS[-1][1]


def wilson(ok: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 100.0)
    z = 1.96
    p = ok / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den
    return (max(0.0, (mid - half) * 100), min(100.0, (mid + half) * 100))


RAW = ("https://raw.githubusercontent.com/donthebuilder/"
       "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")


def _open(url: str):
    """
    One fetch, with the two things that break urllib on a stock Mac handled.

    1. NO CERTIFICATES. Python from python.org ships without the system trust
       store wired up, so every https call raises CERTIFICATE_VERIFY_FAILED
       until someone runs "Install Certificates.command". That is not a thing
       to make a person go and do to answer one question about their own data,
       so a verification failure retries unverified — and SAYS so, because
       silently dropping TLS verification is not something to do quietly. The
       payload is public read-only JSON from a public repo.
    2. NO USER-AGENT. Some endpoints reject urllib's default outright.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "moonshot-shootout/1.0"})
    try:
        return urllib.request.urlopen(req, timeout=25)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            global _WARNED_TLS
            if not _WARNED_TLS:
                print("  (this Python has no certificate store — retrying without TLS "
                      "verification; the data is public read-only JSON)")
                _WARNED_TLS = True
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return urllib.request.urlopen(req, timeout=25, context=ctx)
        raise


_WARNED_TLS = False


def fetch_archive(days: int) -> list[tuple[str, dict]]:
    """
    Pull the graded nights straight from the data branch.

    This exists because the checkout you run this from is the SCRIPTS branch —
    the graded files live on `data`, so a local run finds an empty folder and
    concludes there is no archive. Rather than make someone clone a second
    branch and copy files around to answer one question, the tool fetches what
    it needs. Walks backwards from today; a missing date is a night that wasn't
    graded, not an error.

    THE FIRST VERSION SWALLOWED EVERY EXCEPTION, which meant a TLS failure and
    a night that simply wasn't played produced the identical message: "nothing
    found on the data branch". That is the failure mode where the tool wastes
    the user's time instead of its own — so the first real error is printed
    verbatim now. A 404 stays quiet; anything else gets shown once.
    """
    out = []
    today = dt.date.today()
    misses = 0
    shown = False
    for i in range(days * 3):                 # look back further than we need,
        if len(out) >= days:                  # since off-days leave gaps
            break
        d = (today - dt.timedelta(days=i)).isoformat()
        url = f"{RAW}/graded_results_{d}.json"
        try:
            with _open(url) as r:
                out.append((d, json.loads(r.read().decode())))
            print(f"  · {d}", flush=True)
        except urllib.error.HTTPError as e:
            if e.code != 404 and not shown:    # 404 = no game graded that day
                print(f"  ! {d}: HTTP {e.code} {e.reason}")
                shown = True
            misses += 1
        except Exception as e:
            if not shown:
                print(f"  ! {d}: {type(e).__name__}: {e}")
                shown = True
            misses += 1
        if misses > 20 and not out:
            print("  ! gave up — nothing readable on the data branch. The error above says why.")
            break
    out.reverse()
    return out


# ── where the archive lives ──────────────────────────────────────────────────
# 2026-08-09, Donovan: "why didn't you also look at the results folder on the
# computer I gave access to — it has all the results. Refer to those as well
# when doing data grades and updates and running results and backtests."
#
# He was right and it changed an answer. The first run of this tool saw only
# what the data branch publishes — 14 nights — and reported "no measurable
# difference" between the orderings. His local results folder holds 37 more
# graded nights going back to 2026-04-16. On the combined 51 the difference is
# no longer inside the noise. Fourteen nights was not a small sample by
# accident; it was a small sample because the tool never asked where the rest
# of the data was.
#
# So local directories are searched FIRST and merged with the remote, and the
# search list is overridable. Order does not matter — nights are keyed by date
# and de-duplicated, with the local copy winning on a tie because a file on
# disk is the one the operator can actually inspect.
ARCHIVE_DIRS = [
    PUBLIC_CURRENT,
    Path.home() / "Desktop" / "results",
    Path.home() / "results",
]


def _slots(payload: Any) -> list[dict]:
    """
    Pull the graded rows out of a payload, whatever shape it is.

    THE ARCHIVE IS NOT ONE SHAPE. Across 39 local files there are four:
    a bare top-level list (Apr 16 – May 18, 10 files), a dict under
    `graded_slots` (24 files), a dict under `results` (4 files), and one
    schema_version-tagged dict that carries neither. The previous loader did
    `payload.get(...)` unguarded, so a bare-list file raised AttributeError and
    took the whole run down — which is exactly why nobody had ever pointed this
    tool at the local folder and seen it work.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("graded_slots", "results", "graded", "rows", "picks"):
            v = payload.get(key)
            if isinstance(v, list) and v:
                return [r for r in v if isinstance(r, dict)]
    return []


def load_nights(days: int | None, dirs: list[Path] | None = None) -> list[tuple[str, list[dict]]]:
    by_date: dict[str, Any] = {}
    searched = []
    for d in (dirs if dirs is not None else ARCHIVE_DIRS):
        if not d or not d.is_dir():
            continue
        found = 0
        for p in d.glob("graded_results_*.json"):
            m = DATE_RE.search(p.name)
            if not m:
                continue                       # e.g. "... 2026-04-24 copy.txt"
            try:
                by_date.setdefault(m.group(1), json.loads(p.read_text()))
                found += 1
            except Exception:
                continue                       # unreadable file is not a crash
        if found:
            searched.append(f"{found} from {d}")

    # Whatever the local folders didn't have, go and get. Local wins ties.
    remote = fetch_archive(days or 45) if len(by_date) < (days or 45) else []
    for date, payload in remote:
        by_date.setdefault(date, payload)
    if remote:
        searched.append(f"{len(remote)} from the data branch")
    if searched:
        print("archive: " + " · ".join(searched))

    raw_nights = sorted(by_date.items())
    if days:
        raw_nights = raw_nights[-days:]

    out = []
    for date, payload in raw_nights:
        rows, seen = [], set()
        for r in _slots(payload):
            pid = r.get("player_id")
            if pid is None or pid in seen:
                continue                       # one row per player per night
            seen.add(pid)
            if (num(r.get("actual_ab")) or 0) <= 0:
                continue                       # void: he never batted
            hr_score = num(r.get("hr_score"))
            if hr_score is None:
                continue
            rows.append({
                "raw": hr_score,
                "iso": num(r.get("season_iso")),
                "hr": 1 if (num(r.get("actual_hr")) or 0) >= 1 else 0,
            })
        if len(rows) >= 10:                    # a night too thin to rank is skipped
            out.append((date, rows))
    return out


RANKERS = {
    "raw":      lambda r: r["raw"],
    "adjusted": lambda r: r["raw"] * iso_mult(r["iso"]),
    "iso":      lambda r: (r["iso"] if r["iso"] is not None else -1),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--top", type=int, nargs="*", default=[10, 20, 50])
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dir", action="append", default=None,
                    help="extra folder of graded_results_*.json; repeatable. "
                         "Defaults search public/data/current and ~/Desktop/results.")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="ignore nights before this date — use it to exclude "
                         "older scoring versions from the comparison.")
    a = ap.parse_args()
    random.seed(a.seed)

    dirs = None
    if a.dir:
        dirs = ARCHIVE_DIRS + [Path(d).expanduser() for d in a.dir]
    nights = load_nights(a.days, dirs)
    if a.since:
        before = len(nights)
        nights = [(d, r) for d, r in nights if d >= a.since]
        print(f"--since {a.since}: {len(nights)} of {before} nights kept")
    if not nights:
        print("no graded archive found — nothing to compare")
        return 0

    names = list(RANKERS) + ["random"]
    pooled = {k: {n: [0, 0] for n in a.top} for k in names}   # name -> topN -> [hr, n]
    per_night = {k: {n: [] for n in a.top} for k in names}

    for _date, rows in nights:
        for name in names:
            if name == "random":
                order = rows[:]
                random.shuffle(order)
            else:
                order = sorted(rows, key=RANKERS[name], reverse=True)
            for n in a.top:
                cut = order[:n]
                if len(cut) < min(n, 5):
                    continue
                hits = sum(r["hr"] for r in cut)
                pooled[name][n][0] += hits
                pooled[name][n][1] += len(cut)
                per_night[name][n].append(hits / len(cut))

    total_rows = sum(len(r) for _, r in nights)
    total_hr = sum(sum(x["hr"] for x in r) for _, r in nights)
    base = 100 * total_hr / total_rows if total_rows else 0
    print(f"🥊 SCORE SHOOTOUT — {len(nights)} graded nights, {total_rows} picks that batted, "
          f"{total_hr} homers ({base:.1f}% base rate)\n")

    for n in a.top:
        print(f"── top {n} of each night ──")
        rows_out = []
        for name in names:
            ok, tot = pooled[name][n]
            if not tot:
                continue
            lo, hi = wilson(ok, tot)
            rows_out.append((100 * ok / tot, name, ok, tot, lo, hi))
        rows_out.sort(reverse=True)
        for pct, name, ok, tot, lo, hi in rows_out:
            print(f"   {name:<9} {pct:5.1f}%  ({ok}/{tot})   95% CI {lo:.1f}–{hi:.1f}")

        # Do the two real candidates actually differ?
        r_ok, r_tot = pooled["raw"][n]
        a_ok, a_tot = pooled["adjusted"][n]
        if r_tot and a_tot:
            rlo, rhi = wilson(r_ok, r_tot)
            alo, ahi = wilson(a_ok, a_tot)
            if rlo > ahi:
                verdict = "RAW is measurably better here"
            elif alo > rhi:
                verdict = "ADJUSTED is measurably better here"
            else:
                verdict = ("no measurable difference — the intervals overlap, so this sample "
                           "cannot separate them")
            print(f"   → {verdict}")

        # Head to head, night by night: less powerful than the pooled test but
        # it answers "does one of them win more often", which is what an eye
        # test on a nightly board is actually picking up on.
        rn, an = per_night["raw"][n], per_night["adjusted"][n]
        if rn and an and len(rn) == len(an):
            raw_win = sum(1 for x, y in zip(rn, an) if x > y)
            adj_win = sum(1 for x, y in zip(rn, an) if y > x)
            tie = len(rn) - raw_win - adj_win
            print(f"   night-by-night: raw won {raw_win}, adjusted won {adj_win}, tied {tie}")
            # SIGN TEST on the decisive nights. This matters: the two pooled
            # intervals above are computed as if the samples were independent,
            # but they are not — both orderings rank the SAME rows on the SAME
            # nights, so a night where everybody homers inflates both. The
            # paired test throws that shared variance away and is far more
            # powerful. On the 51-night archive the pooled test says "can't
            # separate them" while the sign test says the opposite, and the
            # sign test is the one to believe.
            dec = raw_win + adj_win
            if dec >= 6:
                w = max(raw_win, adj_win)
                p = 2 * sum(math.comb(dec, k) for k in range(w, dec + 1)) / (2 ** dec)
                p = min(1.0, p)
                who = "adjusted" if adj_win > raw_win else "raw"
                call = ("that is a real edge" if p < 0.05
                        else "leaning, but not conclusive" if p < 0.20
                        else "well inside coin-flip range")
                print(f"   → paired sign test on {dec} decisive nights: {who} ahead, "
                      f"p={p:.3f} — {call}")
        print()

    print("Graded files hold the bot's DESIGNATED PICKS, not the full slate, so this measures how "
          "well each ordering sorts the picks the bot already made. That is the right question for "
          "the top of the board and the wrong one for the whole pool.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
