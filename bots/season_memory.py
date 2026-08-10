#!/usr/bin/env python3
"""
🧠 SEASON MEMORY — the thing the site has never had.

2026-08-09, Donovan: "maybe we need to figure out the memory thing and how
that would work and/or slow down the site."

THE PROBLEM. moonshot is amnesiac. Every night it rebuilds from that night's
payload and knows nothing about last Tuesday. That is why three separate
requests — "storylines need a tracker", "something that builds and runs for
each slate", "a tracker on the watchlist" — all got per-slate answers: the
data to answer them properly did not exist. Everything people actually talk
about is longitudinal. Homered in three straight. Hasn't gone deep in twelve.
This park has played hotter than its factor all month. The HIT picks have been
cold for nine days. None of that was knowable.

WOULD IT SLOW THE SITE DOWN? No, and the shape of this file is the reason.

The naive version computes memory IN THE BROWSER: fetch 40 graded files, fold
them together on every page load. That's ~3 MB down the wire and a second of
main-thread work, per visit, per tab — on a phone, on cellular. It would be
the slowest thing on the site by an order of magnitude.

So the fold happens HERE, once a night, on a runner that is already awake for
the grading run. The site fetches ONE small file and reads it like any other
payload. Concretely:

  · one HTTP request, alongside the five it already makes
  · ~45 KB for a full season of memory (measured below, printed on every run)
  · zero client computation — every number is already final
  · no new dependency: it reads the graded archive that already exists

The size discipline is deliberate and enforced: SIX fields per player, none of
them lists. A per-day history per player would be a megabyte and nobody would
ever read row 340. If a number cannot be summarised into a single value it
does not belong in this file.

WHAT IT KNOWS
  players.{id}   streak / drought / last7,14,30 HR / games seen / last date
  venues.{name}  actual HR per game this season vs the park's own factor
  roles.{CAT}    the bot's rolling hit rate at 7 / 14 / 30 days
  slate.{date}   one line per graded night: picks, cleared, homers

GRADING RULES ARE THE SITE'S RULES, not new ones:
  · one row per player per day (the file publishes one row per pick CATEGORY,
    so a two-category hitter appears twice — deduped on max, lib/graded.js)
  · a player only counts on a day he actually batted (actual_ab > 0);
    scratched and never-used are VOID, not misses
  · streaks and droughts are measured in GAMES HE PLAYED, never calendar days

Usage:
    python bots/season_memory.py                     # full rebuild
    python bots/season_memory.py --days 45           # window the archive
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "bots" else SCRIPT_DIR
PUBLIC_CURRENT = REPO_ROOT / "public" / "data" / "current"
OUT = PUBLIC_CURRENT / "season_memory.json"

DATE_RE = re.compile(r"graded_results_(\d{4}-\d{2}-\d{2})\.json$")
CATEGORIES = ("TOP", "HR", "HIT", "HRR", "CONTACT")


def num(v: Any, d: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else d          # NaN guard
    except (TypeError, ValueError):
        return d


# One shape assumption used to live in each of these three scripts, and all
# three were wrong the same way: the archive also contains bare top-level lists
# (every graded night from 2026-04-16 to 2026-05-18) and a payload.get() on a
# list raises AttributeError. Shared in bots/archive.py now.
from archive import rows_of  # noqa: E402


def dedupe(rows: list[dict]) -> dict[int, dict]:
    """
    One row per player_id, taking the MAX of every actual_* field.

    The graded file publishes one row per pick CATEGORY, so a hitter the bot
    designated twice appears twice — and mid-grading those two rows can be a
    step apart. Taking the max means the answer can't depend on which category
    happens to be walked last. Same rule as lib/graded.js on the site; if the
    two ever disagree, one of them is a bug.
    """
    out: dict[int, dict] = {}
    for r in rows:
        try:
            pid = int(r.get("player_id") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        cur = out.get(pid)
        if cur is None:
            out[pid] = dict(r)
            continue
        for k, v in r.items():
            if k.startswith("actual_") or k.startswith("got_"):
                if num(v) > num(cur.get(k)):
                    cur[k] = v
    return out


def cleared(role: str, r: dict) -> bool | None:
    """Did this pick clear ITS OWN bar? None when he never batted."""
    if num(r.get("actual_ab")) <= 0:
        return None
    hits = num(r.get("actual_hits"))
    combo = hits + num(r.get("actual_runs")) + num(r.get("actual_rbi"))
    role = (role or "").split("/")[0].strip().upper()
    if role in ("HR", "TOP"):
        return num(r.get("actual_hr")) >= 1
    if role == "HIT":
        return hits >= 1
    if role == "HRR":
        return combo >= 2
    if role in ("CONTACT", "TB"):
        return num(r.get("actual_tb")) >= 2
    return None


def load_archive(days: int | None) -> list[tuple[str, dict]]:
    # 2026-08-09: was globbing PUBLIC_CURRENT and nothing else, which on a
    # laptop is an empty folder — the graded files live on the data branch —
    # and on CI holds only the last few published runs. bots/archive.py knows
    # every place a graded night can be, including Donovan's results folder.
    # See the note at the top of that module for why this was a class of bug
    # rather than one bug.
    from archive import describe, load_local
    found, notes = load_local(REPO_ROOT)
    print("  " + describe(found, notes))
    out = sorted(found.items())
    return out[-days:] if days else out


def build(days: int | None = None) -> dict:
    archive = load_archive(days)
    if not archive:
        return {}

    # player -> chronological list of (date, hr, hits, ab)
    seen: dict[int, list[tuple[str, float, float, float]]] = defaultdict(list)
    names: dict[int, str] = {}
    venue_hr: dict[str, list[float]] = defaultdict(list)
    venue_factor: dict[str, float] = {}
    role_days: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    slate_log: list[dict] = []

    for date, payload in archive:
        rows = rows_of(payload)
        if not rows:
            continue
        by_pid = dedupe(rows)

        # ── players ──────────────────────────────────────────────────────
        for pid, r in by_pid.items():
            ab = num(r.get("actual_ab"))
            if ab <= 0:
                continue                              # void: he wasn't asked
            names[pid] = str(r.get("name") or r.get("player_name") or "")
            seen[pid].append((date, num(r.get("actual_hr")), num(r.get("actual_hits")), ab))

        # ── venues: what the building ACTUALLY did, vs what it promised ──
        by_game: dict[Any, dict] = {}
        for r in rows:
            gp = r.get("game_pk")
            if gp is None:
                continue
            g = by_game.setdefault(gp, {"venue": str(r.get("venue_name") or ""), "hr": 0.0, "pids": set()})
            pid = r.get("player_id")
            if pid in g["pids"]:
                continue
            g["pids"].add(pid)
            g["hr"] += num(r.get("actual_hr"))
            f = num(r.get("park_hr_factor")) or (num(r.get("park_factor")) / 100.0 if num(r.get("park_factor")) > 5 else 0)
            if f and g["venue"]:
                venue_factor[g["venue"]] = f
        for g in by_game.values():
            if g["venue"]:
                venue_hr[g["venue"]].append(g["hr"])

        # ── the bot's own categories, per night ──────────────────────────
        night = {"date": date, "picks": 0, "cleared": 0, "hr": 0}
        for r in rows:
            role = str(r.get("game_pick_role") or "").split("/")[0].strip().upper()
            if role not in CATEGORIES:
                continue
            v = cleared(role, r)
            if v is None:
                continue                              # void legs never count
            night["picks"] += 1
            night["cleared"] += 1 if v else 0
        for cat in CATEGORIES:
            ok = tot = 0
            for r in rows:
                role = str(r.get("game_pick_role") or "").split("/")[0].strip().upper()
                if role != cat:
                    continue
                v = cleared(cat, r)
                if v is None:
                    continue
                tot += 1
                ok += 1 if v else 0
            if tot:
                role_days[cat].append((date, ok, tot))
        night["hr"] = int(sum(num(r.get("actual_hr")) for r in dedupe(rows).values()))
        slate_log.append(night)

    today = archive[-1][0]

    def days_ago(d: str, n: int) -> bool:
        try:
            return (dt.date.fromisoformat(today) - dt.date.fromisoformat(d)).days < n
        except ValueError:
            return False

    # ── fold each player down to six numbers ────────────────────────────
    players: dict[str, dict] = {}
    for pid, log in seen.items():
        log.sort(key=lambda x: x[0])
        # Streak and drought are counted in GAMES HE PLAYED, walking backwards
        # from his most recent one — calendar days would call an off-day a
        # drought, which is how "12 games without a homer" becomes a lie.
        streak = 0
        for _, hr, _, _ in reversed(log):
            if hr > 0:
                streak += 1
            else:
                break
        drought = 0
        if streak == 0:
            for _, hr, _, _ in reversed(log):
                if hr > 0:
                    break
                drought += 1
        hit_streak = 0
        for _, _, h, _ in reversed(log):
            if h > 0:
                hit_streak += 1
            else:
                break
        players[str(pid)] = {
            "n": names.get(pid, ""),
            "g": len(log),                                            # games seen
            "s": streak,                                              # HR games in a row
            "d": drought,                                             # games since
            "h": hit_streak,                                          # hit streak
            "hr7": int(sum(hr for d, hr, _, _ in log if days_ago(d, 7))),
            "hr14": int(sum(hr for d, hr, _, _ in log if days_ago(d, 14))),
            "hr30": int(sum(hr for d, hr, _, _ in log if days_ago(d, 30))),
            "last": next((d for d, hr, _, _ in reversed(log) if hr > 0), None),
        }

    # ── venues: measured vs promised ────────────────────────────────────
    venues: dict[str, dict] = {}
    for v, per_game in venue_hr.items():
        if len(per_game) < 3:
            continue                                   # three games is not a park read
        rate = sum(per_game) / len(per_game)
        venues[v] = {
            "g": len(per_game),
            "hrpg": round(rate, 2),
            "factor": round(venue_factor.get(v, 0), 3) or None,
        }
    # League average HR per game across every graded night — the only fair
    # yardstick, and it comes from this same archive rather than an outside
    # number that might be measuring something else.
    all_games = [x for lst in venue_hr.values() for x in lst]
    league_hrpg = round(sum(all_games) / len(all_games), 3) if all_games else None
    for v in venues.values():
        v["vs_league"] = round(v["hrpg"] / league_hrpg, 2) if league_hrpg else None

    roles = {}
    for cat, log in role_days.items():
        log.sort(key=lambda x: x[0])

        def window(n: int) -> dict | None:
            w = [(ok, tot) for d, ok, tot in log if days_ago(d, n)]
            t = sum(t for _, t in w)
            return {"ok": sum(o for o, _ in w), "n": t, "pct": round(100 * sum(o for o, _ in w) / t, 1)} if t else None

        roles[cat] = {"d7": window(7), "d14": window(14), "d30": window(30),
                      "all": {"ok": sum(o for _, o, _ in log), "n": sum(t for _, _, t in log)}}

    return {
        "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "through": today,
        "nights": len(archive),
        "league_hrpg": league_hrpg,
        "players": players,
        "venues": venues,
        "roles": roles,
        # Newest last; the site charts the tail of this.
        "slates": slate_log[-45:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="use only the last N graded files")
    a = ap.parse_args()

    mem = build(a.days)
    if not mem:
        print("no graded archive found — nothing written (this is not an error on a fresh repo)")
        return 0

    PUBLIC_CURRENT.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(mem, separators=(",", ":")))
    kb = OUT.stat().st_size / 1024
    print(f"season_memory.json  {kb:.1f} KB")
    print(f"  {mem['nights']} nights through {mem['through']}")
    print(f"  {len(mem['players'])} players · {len(mem['venues'])} venues")
    print(f"  league {mem['league_hrpg']} HR per game")
    # The size discipline is the whole design; shout if it slips.
    if kb > 250:
        print(f"  !! {kb:.0f} KB is past the budget — this file must stay small enough "
              f"to fetch on a phone. Trim fields before adding more.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
