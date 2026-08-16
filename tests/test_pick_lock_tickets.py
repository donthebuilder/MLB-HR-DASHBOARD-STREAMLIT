"""pick_lock — a POOL IS A TICKET, and a ticket's legs must not change.

Run: python tests/test_pick_lock_tickets.py

WHY THIS EXISTS. Donovan, 2026-08-15: "WHY DID THE pools changsed thru ought
the day that not cool someone just called ann ask about it." pair_builder is
rebuilt from scratch on every one of today.yml's thirteen daily runs, so
"Pool A — Strongest" was four names at 11am and four different names at 4pm.
The ticket lock in bots/pick_lock.py fixes that — and shipped with NO TEST,
which is the wrong way round for logic whose failure mode is silent: a
re-rostered ticket still renders perfectly, it is just a different bet from
the one somebody wrote down.

The assertions below ARE the contract. Each one is a sentence from the
module's own header turned into something that can fail.
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def iso(offset_min: int) -> str:
    """A game time offset from now, in the shape the slate publishes."""
    return (dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(minutes=offset_min)).isoformat().replace("+00:00", "Z")


def slate_row(pid, name, gpk, game_time, role=""):
    return {"player_id": pid, "name": name, "game_pk": gpk,
            "game_time": game_time, "game_pick_role": role,
            "hr_score": 50.0, "team": "AAA", "opponent": "BBB"}


def pool_blob(name, pids, names, gpk=100):
    """Shaped like the REAL pair_builder_latest.json, which is the whole point.

    Two fields here were wrong in the first draft of this test and each one
    hid a production bug: pool blobs identify themselves with `name` (not
    `label`), and their player dicts carry `game_pk` — without which
    ticket_first_pitch() finds no time and the ticket can never freeze.
    """
    return {"name": name, "type": "pool", "pool_score": 71.5, "size": len(pids),
            "players": [{"player_id": p, "name": n, "hr_score": 55.0, "game_pk": gpk}
                        for p, n in zip(pids, names)]}


def run_lock(tmp: Path, slate_rows, pools, *, apply=True):
    """Run pick_lock's main() against a throwaway public/data tree.

    The module resolves PUBLIC/CURRENT at import time from its own location,
    so they are re-pointed at the temp tree before main() runs. fetch_lock is
    stubbed to read the local ledger only — the real one falls back to a
    network pull of pick_lock.json off the data branch, which a test must not
    depend on.
    """
    import importlib
    import bots.pick_lock as pl
    importlib.reload(pl)

    cur = tmp / "current"
    cur.mkdir(parents=True, exist_ok=True)
    pl.PUBLIC = tmp
    pl.CURRENT = cur

    (cur / "today.json").write_text(json.dumps({"players": slate_rows}))
    (cur / "pair_builder_latest.json").write_text(json.dumps(pools))

    def local_lock(date):
        p = cur / "pick_lock.json"
        if p.exists():
            j = json.loads(p.read_text())
            if j.get("date") == date:
                return j
        return {"date": date, "games": {}, "tickets": {}, "rejected": []}
    pl.fetch_lock = local_lock

    argv = sys.argv
    sys.argv = ["pick_lock.py"] + (["--apply"] if apply else ["--dry-run"])
    try:
        pl.main()
    finally:
        sys.argv = argv

    return (json.loads((cur / "pair_builder_latest.json").read_text()),
            json.loads((cur / "pick_lock.json").read_text()) if (cur / "pick_lock.json").exists() else {})


# ── 1. BEFORE FIRST PITCH, RE-ROSTERING IS LEGITIMATE ───────────────────────
# Lineups post and hitters scratch; a ticket whose game has not started is
# allowed to change, and every change is kept in history[].
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    later = iso(120)
    rows = [slate_row(1, "Ann", 100, later), slate_row(2, "Bob", 100, later),
            slate_row(3, "Cal", 100, later), slate_row(4, "Dee", 100, later)]
    run_lock(tmp, rows, {"pools_4man": [pool_blob("Pool A — Strongest", [1, 2], ["Ann", "Bob"])]})
    # the recompute swaps a leg while the game is still two hours away
    out, ledger = run_lock(
        tmp, rows, {"pools_4man": [pool_blob("Pool A — Strongest", [3, 4], ["Cal", "Dee"])]})
    got = [p["player_id"] for p in out["pools_4man"][0]["players"]]
    check("pre-first-pitch: swap is allowed", got, [3, 4])
    slot = list(ledger["tickets"].values())[0]
    check("pre-first-pitch: not locked", bool(slot.get("locked")), False)
    check("pre-first-pitch: change kept in history", len(slot.get("history") or []), 2)

# ── 2. ONCE THE EARLIEST LEG'S GAME STARTS, THE TICKET FREEZES ──────────────
# "A parlay goes live with its first leg, and from that moment its composition
# has to be fixed or nothing about it can be graded or trusted."
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-30)          # first pitch was half an hour ago
    rows = [slate_row(1, "Ann", 100, started), slate_row(2, "Bob", 100, started),
            slate_row(3, "Cal", 100, started), slate_row(4, "Dee", 100, started)]
    run_lock(tmp, rows, {"pools_4man": [pool_blob("Pool A — Strongest", [1, 2], ["Ann", "Bob"])]})
    out, ledger = run_lock(
        tmp, rows, {"pools_4man": [pool_blob("Pool A — Strongest", [3, 4], ["Cal", "Dee"])]})
    got = [p["player_id"] for p in out["pools_4man"][0]["players"]]
    check("post-first-pitch: legs restored", got, [1, 2])
    check("post-first-pitch: names restored", [p["name"] for p in out["pools_4man"][0]["players"]],
          ["Ann", "Bob"])
    check("post-first-pitch: blob marked locked", out["pools_4man"][0].get("locked"), True)
    slot = list(ledger["tickets"].values())[0]
    check("post-first-pitch: slot locked", bool(slot.get("locked")), True)
    # THE REJECTION IS RECORDED. A silently-refused change is as bad as an
    # accepted one — the ledger has to say the recompute tried.
    check("post-first-pitch: rejection logged", len(ledger.get("rejected") or []) >= 1, True)

# ── 3. THE SLOT IS THE NAME, NOT THE ROSTER ─────────────────────────────────
# Pools carry a stable `name` that exists every run while its occupants
# change. If the lock keyed on the players instead, every re-roster would look
# like a brand-new ticket and nothing would ever be frozen.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-10)
    rows = [slate_row(i, n, 100, started) for i, n in [(1, "Ann"), (2, "Bob"), (5, "Eve")]]
    run_lock(tmp, rows, {"pools_4man": [pool_blob("Pool A — Strongest", [1, 2], ["Ann", "Bob"])]})
    out, ledger = run_lock(
        tmp, rows, {"pools_4man": [pool_blob("Pool A — Strongest", [5], ["Eve"])]})
    check("slot keyed on label, not roster", len(ledger["tickets"]), 1)
    check("slot held its original legs", [p["player_id"] for p in out["pools_4man"][0]["players"]], [1, 2])

# ── 4. A LOCKED TICKET IS NOT RE-ROSTERED WHEN A LEG SCRATCHES ──────────────
# "That leg is a void leg, which is the honest outcome and what a real ticket
# would do; swapping in a replacement after the ticket is live would be
# inventing a bet nobody made." Here Bob disappears from the slate entirely —
# the stub has to carry him, not a substitute.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-45)
    full = [slate_row(1, "Ann", 100, started), slate_row(2, "Bob", 100, started)]
    run_lock(tmp, full, {"pools_4man": [pool_blob("Pool A — Strongest", [1, 2], ["Ann", "Bob"])]})
    # Bob is gone from the candidate pool and the recompute offers a stand-in
    without_bob = [slate_row(1, "Ann", 100, started), slate_row(9, "Zed", 100, started)]
    out, _ = run_lock(tmp, without_bob,
                      {"pools_4man": [pool_blob("Pool A — Strongest", [1, 9], ["Ann", "Zed"])]})
    got = [p["player_id"] for p in out["pools_4man"][0]["players"]]
    check("scratched leg is NOT replaced", got, [1, 2])
    names = [p.get("name") for p in out["pools_4man"][0]["players"]]
    check("scratched leg carried by its stub", names[1], "Bob")

# ── 5. THE META TRAVELS WITH THE ROSTER IT WAS COMPUTED FOR ─────────────────
# "Leaving today's score on yesterday's names is how you get a pool labelled
# 'Strongest' that is arithmetically nothing of the sort."
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-20)
    rows = [slate_row(i, n, 100, started) for i, n in [(1, "Ann"), (2, "Bob"), (3, "Cal")]]
    first = pool_blob("Pool A — Strongest", [1, 2], ["Ann", "Bob"])
    first["pool_score"] = 71.5
    run_lock(tmp, rows, {"pools_4man": [first]})
    second = pool_blob("Pool A — Strongest", [3], ["Cal"])
    second["pool_score"] = 99.9          # today's score, for a roster that lost
    out, _ = run_lock(tmp, rows, {"pools_4man": [second]})
    check("locked ticket keeps its OWN score", out["pools_4man"][0]["pool_score"], 71.5)

# ── 6. AN UNSTARTED TICKET IN A LATER GAME IS UNAFFECTED ────────────────────
# The freeze is per-ticket, off its own earliest leg — not a global switch
# thrown when the night's first game starts.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    early, late = iso(-30), iso(180)
    rows = [slate_row(1, "Ann", 100, early), slate_row(2, "Bob", 100, early),
            slate_row(3, "Cal", 200, late), slate_row(4, "Dee", 200, late),
            slate_row(5, "Eve", 200, late)]
    pools = {"pools_4man": [pool_blob("Pool A — Strongest", [1, 2], ["Ann", "Bob"]),
                            pool_blob("Pool B — Balanced", [3, 4], ["Cal", "Dee"], gpk=200)]}
    run_lock(tmp, rows, pools)
    moved = {"pools_4man": [pool_blob("Pool A — Strongest", [3, 4], ["Cal", "Dee"]),
                            pool_blob("Pool B — Balanced", [4, 5], ["Dee", "Eve"], gpk=200)]}
    out, _ = run_lock(tmp, rows, moved)
    check("started ticket frozen", [p["player_id"] for p in out["pools_4man"][0]["players"]], [1, 2])
    check("unstarted ticket still free", [p["player_id"] for p in out["pools_4man"][1]["players"]], [4, 5])


def pair_blob(lane, pids, names, gpk=100):
    """A recommended_pairs blob — identified by lane_key, which is NOT unique."""
    return {"lane_key": lane, "pair_key": "|".join(str(p) for p in pids),
            "type": "pair", "pair_score": 60.0,
            "players": [{"player_id": p, "name": n, "hr_score": 55.0, "game_pk": gpk}
                        for p, n in zip(pids, names)]}


# ── 7. THE POOLS ARE RANKED, SO THEY REORDER — AND THE LOCK MUST FOLLOW THE
#      POOL, NOT THE POSITION ─────────────────────────────────────────────────
# This is bug #1, encoded. ticket_key read `label`, pools publish `name`, so
# the key fell through to the ARRAY INDEX. Both pools below are live; the
# recompute then swaps their order. Index-keyed, Pool A would be handed Pool
# B's frozen legs and vice versa — each ticket silently becoming the other one.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-25)
    rows = [slate_row(i, n, 100, started) for i, n in
            [(1, "Ann"), (2, "Bob"), (3, "Cal"), (4, "Dee")]]
    first = {"pools_4man": [pool_blob("Pool A — Strongest", [1, 2], ["Ann", "Bob"]),
                            pool_blob("Pool B — Balanced", [3, 4], ["Cal", "Dee"])]}
    run_lock(tmp, rows, first)
    # same two pools, opposite order, each also trying to re-roster
    reordered = {"pools_4man": [pool_blob("Pool B — Balanced", [1, 2], ["Ann", "Bob"]),
                                pool_blob("Pool A — Strongest", [3, 4], ["Cal", "Dee"])]}
    out, _ = run_lock(tmp, rows, reordered)
    by_name = {b["name"]: [p["player_id"] for p in b["players"]] for b in out["pools_4man"]}
    check("reorder: Pool A keeps ITS legs", by_name["Pool A — Strongest"], [1, 2])
    check("reorder: Pool B keeps ITS legs", by_name["Pool B — Balanced"], [3, 4])

# ── 8. TWO PAIRS IN ONE LANE ARE TWO TICKETS ────────────────────────────────
# This is bug #2, encoded. On the live file two pairs share lane "TOP30" and
# two more share lane "A". Keyed on lane_key alone they collide: the second
# overwrites the first in the ledger and BOTH blobs get restored to the same
# roster — the lock duplicating a ticket, which is worse than not locking.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-15)
    rows = [slate_row(i, n, 100, started) for i, n in
            [(1, "Ann"), (2, "Bob"), (3, "Cal"), (4, "Dee"), (5, "Eve"), (6, "Fay")]]
    first = {"recommended_pairs": [pair_blob("TOP30", [1, 2], ["Ann", "Bob"]),
                                   pair_blob("TOP30", [3, 4], ["Cal", "Dee"])]}
    run_lock(tmp, rows, first)
    moved = {"recommended_pairs": [pair_blob("TOP30", [5, 6], ["Eve", "Fay"]),
                                   pair_blob("TOP30", [5, 6], ["Eve", "Fay"])]}
    out, ledger = run_lock(tmp, rows, moved)
    legs = [[p["player_id"] for p in b["players"]] for b in out["recommended_pairs"]]
    check("same-lane pairs are two ledger slots", len(ledger["tickets"]), 2)
    check("same-lane pair 1 keeps its own legs", legs[0], [1, 2])
    check("same-lane pair 2 keeps its own legs", legs[1], [3, 4])

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   pick_lock tickets: {CHECKS} assertions, a ticket's legs hold")
