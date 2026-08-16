#!/usr/bin/env python3
"""
📌 PICK LOCK — make the claim on the receipt card true.

REPORTED 2026-08-09, Donovan: "I noticed two people who were on the bot that
went, then later in the day it says the bot didn't have them at all."

He is right, and the cause is structural.

WHAT WAS HAPPENING
------------------
`build_game_pick_role_map()` in mlb_dashboard.py recomputes every designation
FROM SCRATCH, and today.yml runs it THIRTEEN TIMES A DAY. The inputs it ranks
on are not stable across those runs:

  · `last5_hr` changes the moment an early game finishes
  · `season_iso` ticks with every at-bat
  · the hitter pool itself changes when projected lineups become real ones

So the 11am TOP pick for a game can be a different player by 4pm. Nothing
anywhere recorded who it was at 11am. The site only ever reads the CURRENT
sheet, so a hitter who was designated in the morning and went deep at 1pm can
show up at 4pm with no 🤖 tag at all — exactly what was reported.

WHY THIS IS THE WORST BUG IN THE PROJECT
----------------------------------------
Two reasons, and the second is the one that matters.

1. It loses credit for picks that hit, and — the same mechanism, opposite
   direction — it can quietly drop picks that missed.

2. THE RECEIPTS CARD ALREADY CLAIMED THIS WAS IMPOSSIBLE. Both the nightly
   Discord image and its caption say "locked at first pitch, never edited".
   That text has been shipping for weeks against code that locks nothing. A
   feature being incomplete is a bug; a published accountability claim that
   isn't implemented is something worse, and on a slate with staggered starts
   it means the model could re-designate a hitter at 4pm who homered at 1pm
   and grade itself a winner.

THE RULE THIS ENFORCES
----------------------
For each (game, category):

  · BEFORE that game's first pitch, re-picking is legitimate — lineups post,
    scratches happen, and a pick made against a projected lineup SHOULD be
    revisited. Every change is recorded with a timestamp so the history is
    inspectable, but the latest one stands.

  · AT FIRST PITCH the designation FREEZES. From then on the published sheet
    is rewritten to match the lock, and any attempt to change it is recorded
    in `rejected[]` and announced. This is the line that makes the claim on
    the card true: after the first pitch of his game, a pick cannot change,
    in either direction.

  · If the first run for a game happens AFTER its first pitch (a late build,
    a failed earlier run), the lock is taken then and flagged `locked_late` —
    it is stamped honestly rather than pretending it was locked on time, and
    those games are visible in the ledger.

State lives on the data branch and is fetched over HTTPS, because the runner
checks out `main` and the previous run's output is only on `data`.

Usage (in today.yml, AFTER mlb_dashboard and BEFORE publish):
    python bots/pick_lock.py --apply
    python bots/pick_lock.py --apply --dry-run      # report, change nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "bots" else SCRIPT_DIR
PUBLIC = REPO_ROOT / "public" / "data"
CURRENT = PUBLIC / "current"

RAW = ("https://raw.githubusercontent.com/donthebuilder/"
       "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")

CATEGORIES = ("TOP", "HR", "HIT", "HRR", "CONTACT")

# Files whose rows carry game_pick_role and are published to the site.
SLATE_FILES = ["current/today.json", "current/today_slim.json",
               "today.json", "today_slim.json"]

# ── POOLS AND PAIRS ──────────────────────────────────────────────────────────
#
# REPORTED 2026-08-15, Donovan: "WHY DID THE pools changsed thru ought the day
# that not cool someone just called ann ask about it."
#
# Same bug as the one above, in a file this script never opened. `SLATE_FILES`
# covers today.json and today_slim.json — the per-game designations — and
# nothing else. `pair_builder_latest.json`, which is where Pools and Pairs
# come from, is rebuilt from scratch by _build_pair_sections() on every one of
# today.yml's thirteen daily runs, ranked on the same drifting inputs. So
# "Pool A — Strongest" is four names at 11am and a different four at 4pm, and
# a user who wrote his ticket down in the morning finds it gone by the
# afternoon. That is worse here than on a single pick: a pool is a TICKET, and
# a ticket whose legs change is not a ticket.
#
# THE SLOT IS THE THING THAT PERSISTS. Pools carry a stable `label`
# ("Pool A — Strongest"); pairs carry a stable `lane_key` ("TOP30"). Those are
# slots that exist every run while their occupants change — so they are what
# the lock keys on.
#
# WHEN A TICKET FREEZES: when its EARLIEST leg's game starts. A parlay goes
# live with its first leg, and from that moment its composition has to be
# fixed or nothing about it can be graded or trusted. Before that, re-rostering
# is legitimate for exactly the reasons re-picking is — lineups post, hitters
# scratch — and every change is kept in history[].
#
# A locked ticket is NOT re-rostered when a leg scratches. That leg is a void
# leg, which is the honest outcome and what a real ticket would do; swapping in
# a replacement after the ticket is live would be inventing a bet nobody made.
POOL_FILES = ["current/pair_builder_latest.json", "pair_builder_latest.json"]
POOL_SECTIONS = ("recommended_pairs", "recommended_3mans",
                 "pools_4man", "pools_3man", "pools_6man")

# Restored verbatim onto a locked ticket, because they were computed FOR that
# roster. Leaving today's score on yesterday's names is how you get a pool
# labelled "Strongest" that is arithmetically nothing of the sort.
TICKET_FIELDS = ("name", "label", "type", "lane_key", "pair_key", "pair_score",
                 "score", "pool_score", "size", "risk", "tags", "reason")

# Enough to render a locked man who has since vanished from the candidate pool.
STUB_FIELDS = ("player_id", "name", "team", "opponent", "game_pk", "lineup_spot")

PLAYER_KEYS = ("players", "all_players", "player_pool", "slate_players", "rows",
               "picks", "top_picks", "hr_picks", "hit_picks", "hrr_picks",
               "contact_picks", "top_board", "hr_board", "alt_looks", "results")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(v: Any) -> dt.datetime | None:
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def iter_rows(payload: Any):
    """Every player row in a slate payload, wherever the builder put it."""
    if isinstance(payload, list):
        for r in payload:
            if isinstance(r, dict):
                yield r
        return
    if not isinstance(payload, dict):
        return
    for k in PLAYER_KEYS:
        for r in payload.get(k) or []:
            if isinstance(r, dict):
                yield r
    payload = payload if isinstance(payload, dict) else {}   # shape guard, see bots/check_shapes.py
    for g in payload.get("games") or []:
        if not isinstance(g, dict):
            continue
        for k in ("players", "away_players", "home_players"):
            for r in g.get(k) or []:
                if isinstance(r, dict):
                    yield r


def fetch_lock(date: str) -> dict:
    """Last run's ledger. A miss is a fresh start.

    LOCAL FIRST. In CI the runner checks out `main`, which never carries this
    file — it's published to `data` — so the network fetch below is the real
    path. But if a ledger for THIS SLATE is already sitting in the checkout,
    it is by definition fresher than anything on the branch: it was written by
    an earlier invocation in this same job. Reading the branch instead would
    silently discard a lock taken minutes ago.
    """
    local = CURRENT / "pick_lock.json"
    if local.exists():
        try:
            j = json.loads(local.read_text())
            if isinstance(j, dict) and j.get("date") == date:
                return j
        except Exception as e:
            print(f"  · local ledger unreadable ({e}) — falling back to the branch")

    url = f"{RAW}/pick_lock.json?t={int(now_utc().timestamp())}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            j = json.loads(r.read().decode())
        if isinstance(j, dict) and j.get("date") == date:
            return j
        # A ledger for a different slate is not this slate's ledger.
        return {}
    except Exception as e:
        print(f"  · no previous lock fetched ({e}) — starting fresh for {date}")
        return {}


def current_designations(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """(game_pk -> category -> {pid, name}) as the sheet currently says."""
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        raw = str(r.get("game_pick_role") or "").strip().upper()
        if not raw:
            continue
        gp = str(r.get("game_pk") or "")
        if not gp:
            continue
        try:
            pid = int(r.get("player_id") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        for cat in [c.strip() for c in raw.split("/") if c.strip()]:
            if cat in CATEGORIES:
                out.setdefault(gp, {})[cat] = {
                    "pid": pid,
                    "name": str(r.get("name") or r.get("player_name") or ""),
                }
    return out


def first_pitch_of(rows: list[dict]) -> dict[str, dt.datetime]:
    out: dict[str, dt.datetime] = {}
    for r in rows:
        gp = str(r.get("game_pk") or "")
        t = parse_ts(r.get("game_time"))
        if gp and t and gp not in out:
            out[gp] = t
    return out


# ── ticket helpers ───────────────────────────────────────────────────────────

def ticket_slots(section: str, blobs: list) -> list[str]:
    """Stable slot keys for one section's blobs, in order.

    ── TWO BUGS FOUND BY WRITING THE TEST (2026-08-16) ──────────────────────
    This used to be `blob.get("label") or blob.get("lane_key")` and it was
    wrong on BOTH of the shapes the bot actually publishes. Neither failed
    loudly; both quietly locked the wrong thing.

    1. POOLS DO NOT HAVE `label`. The published field is `name`
       ("Pool A — Strongest"). The old comment even quoted that exact string
       as an example of `label`, which is how the mistake survived — the field
       name was assumed and never checked against pair_builder_latest.json.
       With `label` missing the key fell through to the ARRAY INDEX, so the
       lock froze "whatever is sitting in slot 0" rather than Pool A. Pools are
       ranked, so the day the recompute reorders them the lock restores one
       ticket's legs onto a different ticket.

    2. `lane_key` IS NOT UNIQUE. On the live file two pairs share lane "TOP30"
       and two more share lane "A". Same key for two blobs means the second
       overwrote the first in the ledger dict, and on restore BOTH pairs got
       the same frozen roster — the lock actively duplicating a ticket, which
       is worse than not locking at all.

    So: `name` first (pools), then `label`, then `lane_key` — and because a
    lane legitimately holds several pairs, every key carries its ORDINAL
    WITHIN ITS OWN SLOT NAME. "the second pair in lane TOP30" is a real,
    stable slot; "lane TOP30" is not. Index remains the last resort for a blob
    that identifies itself no other way.

    Keys are computed in ONE place and consumed by both the ledger pass and
    the restore pass, because the two drifting apart would mean writing a lock
    under one key and reading it under another — silently never restoring.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for idx, blob in enumerate(blobs):
        base = ""
        if isinstance(blob, dict):
            base = str(blob.get("name") or blob.get("label") or blob.get("lane_key") or "").strip()
        if not base:
            base = f"#{idx}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(f"{section}|{base}|{n}")
    return out


def ticket_pids(blob: dict) -> list[int]:
    out = []
    for p in blob.get("players") or []:
        try:
            pid = int(p.get("player_id") or 0)
        except (TypeError, ValueError):
            continue
        if pid:
            out.append(pid)
    return out


def ticket_first_pitch(blob: dict, fp: dict[str, dt.datetime]) -> dt.datetime | None:
    """A ticket goes live with its earliest leg."""
    times = [fp[str(p.get("game_pk"))] for p in blob.get("players") or []
             if str(p.get("game_pk") or "") in fp]
    return min(times) if times else None


def index_players(payload: dict) -> dict[int, dict]:
    """Every player dict anywhere in the pair payload, by id.

    Used to rebuild a locked roster from TODAY's dicts, so the names are frozen
    but their stats stay current — which is what you want. The ledger only has
    to remember WHO, not carry a copy of every field.
    """
    out: dict[int, dict] = {}
    buckets = [payload.get("available_pool") or []]
    for sec in POOL_SECTIONS:
        for t in payload.get(sec) or []:
            if isinstance(t, dict):
                buckets.append(t.get("players") or [])
    for b in buckets:
        for p in b:
            if not isinstance(p, dict):
                continue
            try:
                pid = int(p.get("player_id") or 0)
            except (TypeError, ValueError):
                continue
            if pid and pid not in out:
                out[pid] = p
    return out


def post_discord(lines: list[str]) -> None:
    hook = os.environ.get("DISCORD_WEBHOOK", "")
    if not hook or not lines:
        return
    body = json.dumps({"content": "\n".join(lines)[:1900]}).encode()
    for url in [u.strip() for u in hook.split(",") if u.strip()]:
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            print(f"  ! discord post failed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="rewrite game_pick_role on the slate files")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    a = ap.parse_args()

    # Load whichever slate file exists, to read designations and first pitches.
    slate_paths = [PUBLIC / p for p in SLATE_FILES]
    slate_paths = [p for p in slate_paths if p.exists()]
    if not slate_paths:
        print("no slate file found — nothing to lock")
        return 0

    base = json.loads(slate_paths[0].read_text())
    rows = list(iter_rows(base))
    if not rows:
        print("slate has no player rows — nothing to lock")
        return 0

    # ⚠️ THE LINE THAT KILLED THE 2026-08-10 RUN. `base` is a bare LIST on
    # some slate files, and a list has no .get. iter_rows above already
    # handled both shapes, which is what made this so easy to miss — the
    # rows came back fine and the very next line exploded.
    #
    # meta_of() returns {} for a list, so the fallback below (derive the date
    # from the earliest first pitch) takes over, which was always the right
    # answer for a headerless payload.
    meta = base if isinstance(base, dict) else {}
    date = str(meta.get("date") or meta.get("slate_date") or "")
    if not date:
        t = first_pitch_of(rows)
        date = min(t.values()).date().isoformat() if t else now_utc().date().isoformat()

    lock = fetch_lock(date)
    games: dict[str, dict] = lock.get("games") or {}
    rejected: list[dict] = lock.get("rejected") or []

    now = now_utc()
    fp = first_pitch_of(rows)
    cur = current_designations(rows)
    stamp = now.isoformat(timespec="seconds")

    changed_before, froze_now, rejects = 0, 0, []

    for gp, cats in cur.items():
        started = bool(fp.get(gp) and now >= fp[gp])
        g = games.setdefault(gp, {"first_pitch": fp[gp].isoformat() if fp.get(gp) else None, "cats": {}})
        for cat, who in cats.items():
            slot = g["cats"].get(cat)
            if slot is None:
                # First time we've ever seen this game/category designated.
                g["cats"][cat] = {
                    "pid": who["pid"], "name": who["name"],
                    "at": stamp, "locked": started,
                    # Honest flag: we never saw this game before it started, so
                    # the lock is late and says so rather than implying it was
                    # taken on time.
                    "locked_late": started,
                    "history": [{"pid": who["pid"], "name": who["name"], "at": stamp}],
                }
                if started:
                    froze_now += 1
                continue

            if slot.get("locked"):
                if int(slot["pid"]) != int(who["pid"]):
                    # THE EVENT THIS FILE EXISTS FOR.
                    rejects.append({
                        "game_pk": gp, "cat": cat, "at": stamp,
                        "locked": {"pid": slot["pid"], "name": slot.get("name", "")},
                        "attempted": {"pid": who["pid"], "name": who["name"]},
                    })
                continue

            if started:
                # THE MOMENT OF THE LOCK, and the one place it is easy to get
                # backwards — my first version did.
                #
                # We already have a designation for this game from a PRE-GAME
                # run. That is the pick that was standing when the first pitch
                # was thrown, so that is the one that freezes. Freezing
                # "whatever the sheet says now" would lock in a re-pick the
                # recompute made after the game was underway, which is exactly
                # the behaviour this file exists to stop — and the test caught
                # it doing precisely that.
                slot["locked"] = True
                slot["locked_at"] = stamp
                froze_now += 1
                if int(slot["pid"]) != int(who["pid"]):
                    rejects.append({
                        "game_pk": gp, "cat": cat, "at": stamp,
                        "locked": {"pid": slot["pid"], "name": slot.get("name", "")},
                        "attempted": {"pid": who["pid"], "name": who["name"]},
                    })
                continue

            # Pre-game: a re-pick is legitimate. Record it and move on.
            if int(slot["pid"]) != int(who["pid"]):
                slot.update(pid=who["pid"], name=who["name"], at=stamp)
                slot.setdefault("history", []).append({"pid": who["pid"], "name": who["name"], "at": stamp})
                changed_before += 1

    rejected.extend(rejects)

    # ── tickets: pools and pairs ────────────────────────────────────────────
    tickets: dict[str, dict] = lock.get("tickets") or {}
    t_changed, t_froze = 0, 0
    t_rejects: list[dict] = []
    pool_paths = [PUBLIC / p for p in POOL_FILES]
    pool_paths = [p for p in pool_paths if p.exists()]

    pool_payloads: list[tuple[Path, dict]] = []
    for p in pool_paths:
        try:
            j = json.loads(p.read_text())
            if isinstance(j, dict):
                pool_payloads.append((p, j))
        except Exception as e:
            print(f"  ! could not read {p.name}: {e}")

    if pool_payloads:
        # One payload decides the ledger; the rest are the same file under
        # another name and get the same restoration applied.
        _, lead = pool_payloads[0]
        for section in POOL_SECTIONS:
            blobs = lead.get(section) or []
            keys = ticket_slots(section, blobs)
            for idx, blob in enumerate(blobs):
                if not isinstance(blob, dict):
                    continue
                pids = ticket_pids(blob)
                if not pids:
                    continue
                key = keys[idx]
                tfp = ticket_first_pitch(blob, fp)
                started = bool(tfp and now >= tfp)
                slot = tickets.get(key)

                snap = {
                    "pids": pids,
                    "meta": {k: blob[k] for k in TICKET_FIELDS if k in blob},
                    "stubs": {str(p.get("player_id")): {k: p.get(k) for k in STUB_FIELDS}
                              for p in (blob.get("players") or [])
                              if isinstance(p, dict) and p.get("player_id")},
                }

                if slot is None:
                    tickets[key] = {
                        "section": section,
                        "first_pitch": tfp.isoformat() if tfp else None,
                        **snap,
                        "at": stamp, "locked": started, "locked_late": started,
                        "history": [{"pids": pids, "at": stamp}],
                    }
                    if started:
                        t_froze += 1
                    continue

                if slot.get("locked"):
                    if slot.get("pids") != pids:
                        t_rejects.append({"ticket": key, "at": stamp,
                                          "locked": slot.get("pids"), "attempted": pids})
                    continue

                if started:
                    # Same ordering trap as the designation lock above: freeze
                    # what was standing at first pitch, NOT what this post-game
                    # recompute just produced.
                    slot["locked"] = True
                    slot["locked_at"] = stamp
                    t_froze += 1
                    if slot.get("pids") != pids:
                        t_rejects.append({"ticket": key, "at": stamp,
                                          "locked": slot.get("pids"), "attempted": pids})
                    continue

                if slot.get("pids") != pids:
                    slot.update(**snap, at=stamp)
                    slot.setdefault("history", []).append({"pids": pids, "at": stamp})
                    t_changed += 1

    rejected.extend(t_rejects)

    # ── restore locked tickets onto every pool payload ──────────────────────
    t_restored = 0
    if a.apply and not a.dry_run:
        for path, payload in pool_payloads:
            idx_players = index_players(payload)
            touched = False
            for section in POOL_SECTIONS:
                blobs = payload.get(section) or []
                keys = ticket_slots(section, blobs)
                for i, blob in enumerate(blobs):
                    if not isinstance(blob, dict):
                        continue
                    slot = tickets.get(keys[i])
                    if not slot or not slot.get("locked"):
                        continue
                    if ticket_pids(blob) == slot.get("pids"):
                        continue
                    # Rebuild from today's dicts where possible so the frozen
                    # names carry live stats; fall back to the stub for a man
                    # who has dropped out of the candidate pool entirely.
                    rebuilt = []
                    for pid in slot.get("pids") or []:
                        rebuilt.append(idx_players.get(pid)
                                       or (slot.get("stubs") or {}).get(str(pid))
                                       or {"player_id": pid, "name": "(scratched)"})
                    blob["players"] = rebuilt
                    blob.update(slot.get("meta") or {})
                    blob["locked"] = True
                    blob["locked_at"] = slot.get("locked_at")
                    touched = True
                    t_restored += 1
            if touched:
                path.write_text(json.dumps(payload, indent=2))

    # ── apply the lock back onto the published rows ─────────────────────────
    # Only locked games are touched. For those, the role is stripped from
    # whoever the recompute handed it to and restored to the locked hitter.
    restored, stripped, missing = 0, 0, []
    if a.apply and not a.dry_run:
        for path in slate_paths:
            try:
                payload = json.loads(path.read_text())
                if not isinstance(payload, dict):
                    continue          # a bare list carries no lock metadata
            except Exception as e:
                print(f"  ! could not read {path.name}: {e}")
                continue
            prows = list(iter_rows(payload))
            by_key: dict[tuple[str, int], list[dict]] = {}
            for r in prows:
                gp = str(r.get("game_pk") or "")
                try:
                    pid = int(r.get("player_id") or 0)
                except (TypeError, ValueError):
                    continue
                if gp and pid:
                    by_key.setdefault((gp, pid), []).append(r)

            for gp, g in games.items():
                locked_cats = {c: s for c, s in g["cats"].items() if s.get("locked")}
                if not locked_cats:
                    continue
                want: dict[int, set[str]] = {}
                for cat, slot in locked_cats.items():
                    want.setdefault(int(slot["pid"]), set()).add(cat)
                for r in prows:
                    if str(r.get("game_pk") or "") != gp:
                        continue
                    try:
                        pid = int(r.get("player_id") or 0)
                    except (TypeError, ValueError):
                        continue
                    have = {c.strip().upper() for c in str(r.get("game_pick_role") or "").split("/") if c.strip()}
                    # keep any category that isn't locked for this game
                    keep = {c for c in have if c not in locked_cats}
                    new = keep | want.get(pid, set())
                    if new != have:
                        if want.get(pid) and not (have & want[pid]):
                            restored += 1
                        if have - new:
                            stripped += 1
                        r["game_pick_role"] = "/".join(sorted(new, key=lambda c: CATEGORIES.index(c) if c in CATEGORIES else 9))
                for pid in want:
                    if (gp, pid) not in by_key:
                        missing.append({"game_pk": gp, "pid": pid,
                                        "cats": sorted(want[pid])})
            path.write_text(json.dumps(payload, separators=(",", ":")))

    ledger = {
        "date": date,
        "updated": stamp,
        "runs": int(lock.get("runs") or 0) + 1,
        "games": games,
        "rejected": rejected,
        # A locked hitter whose row has left the slate entirely — scratched
        # after the lock. He stays in the ledger; grading voids him for having
        # no at-bat, which is the correct outcome and not a miss.
        "gone_from_slate": missing,
        # Pools and pairs, keyed by SLOT (label / lane_key). Same rule, applied
        # to tickets instead of designations — added 2026-08-15 after a user
        # rang Donovan about pools changing during the day.
        "tickets": tickets,
        "rule": ("Before a game's first pitch a designation may change and every change is kept in "
                 "history[]. At first pitch it freezes: the published sheet is rewritten to match the "
                 "lock and any later change is recorded in rejected[] instead of being applied. "
                 "locked_late marks games first seen after they had already started. "
                 "Pools and pairs follow the same rule keyed on their slot (label / lane_key), "
                 "freezing when their EARLIEST leg starts; a locked ticket is never re-rostered "
                 "when a leg scratches — that leg voids, which is what a real ticket does."),
    }

    if not a.dry_run:
        CURRENT.mkdir(parents=True, exist_ok=True)
        (CURRENT / "pick_lock.json").write_text(json.dumps(ledger, indent=1))

    n_locked = sum(1 for g in games.values() for s in g["cats"].values() if s.get("locked"))
    n_late = sum(1 for g in games.values() for s in g["cats"].values() if s.get("locked_late"))
    print(f"pick lock — {date}, run #{ledger['runs']}")
    print(f"  {len(games)} games · {n_locked} designations locked ({n_late} locked late)")
    print(f"  pre-game re-picks recorded: {changed_before} · froze this run: {froze_now}")
    n_tlock = sum(1 for t in tickets.values() if t.get("locked"))
    print(f"  {len(tickets)} tickets · {n_tlock} locked · "
          f"pre-lock re-rosters: {t_changed} · froze this run: {t_froze}")
    if t_rejects:
        print(f"  ! {len(t_rejects)} ticket re-roster(s) REJECTED after lock")
    if a.apply and not a.dry_run and t_restored:
        print(f"  restored {t_restored} locked ticket(s) onto the published payload")
    if a.apply and not a.dry_run:
        print(f"  applied to sheet: {restored} restored, {stripped} stripped")
    if missing:
        print(f"  {len(missing)} locked hitters no longer on the slate (scratched after lock)")
    if rejects:
        print(f"  !! {len(rejects)} POST-LOCK CHANGES REJECTED:")
        for r in rejects:
            print(f"     {r['cat']} game {r['game_pk']}: kept {r['locked']['name']}, "
                  f"refused {r['attempted']['name']}")
        post_discord(
            [f"**📌 Pick lock held** — {len(rejects)} designation change{'s' if len(rejects) > 1 else ''} "
             f"refused after first pitch ({date})", ""]
            + [f"· **{r['cat']}** — kept **{r['locked']['name']}**, refused {r['attempted']['name']}"
               for r in rejects[:8]]
            + ["", "_Picks freeze when their game starts. This is the rule the receipts card has always "
               "claimed; it is enforced now._"]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
