"""prediction_of_record — the run standing at a game's first pitch is the
official one, for every player in that game, forever.

Run: python tests/test_prediction_of_record.py

WHY THIS EXISTS. today.yml scores a game up to ~13 times before its first
pitch, and every row on every one of those runs already carries a
`run_id`/`model_version` (model foundation, Tasks 2-3). Nothing recorded
which run's numbers a designation was actually graded against, which means
a 6pm recompute (model drift, a late-breaking injury reweight, whatever)
could retroactively change what "the prediction" for an 11am pick was —
the same failure `pick_lock.py`'s designation lock already exists to stop,
one level up: not "who got picked" but "what number did the pick get
graded against."

`run_id_by_game()` / the `prediction_of_record` block in `pick_lock.py`'s
`main()` lock each game_pk to whichever run_id is standing the instant its
first pitch arrives — independent of designations, so it covers every
player in the game, not just the 5 picked categories.
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


def checkTrue(name, cond):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILED.append(f"{name}: expected truthy, got falsy")


def iso(offset_min: int) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(minutes=offset_min)).isoformat().replace("+00:00", "Z")


def run_id_at(offset_min: int, source="gha-1") -> str:
    """A real-shaped run_id whose embedded timestamp is `offset_min` minutes
    from now, so _run_id_generated_at() has something to parse -- same
    "{slate_date}.{HHMMSSZ}.{source}" shape build_run_meta() produces."""
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=offset_min)
    return f"{t.date().isoformat()}.{t.strftime('%H%M%S')}Z.{source}"


def slate_row(pid, name, gpk, game_time, run_id=None, model_version=None, role=""):
    row = {"player_id": pid, "name": name, "game_pk": gpk,
           "game_time": game_time, "game_pick_role": role,
           "hr_score": 50.0, "team": "AAA", "opponent": "BBB"}
    if run_id is not None:
        row["run_id"] = run_id
    if model_version is not None:
        row["model_version"] = model_version
    return row


def run_lock(tmp: Path, slate_rows, *, apply=True, seed_ledger=None):
    """Run pick_lock's main() against a throwaway public/data tree and
    return the resulting ledger. Mirrors tests/test_pick_lock_tickets.py's
    own harness exactly (same re-point-PUBLIC/CURRENT, same local-only
    fetch_lock stub) so this file exercises the real main(), not a
    reimplementation of it.

    seed_ledger lets a test start from a ledger that already exists (e.g.
    a "legacy" pick_lock.json written before this feature shipped, with
    games/cats locked but no prediction_of_record key at all).
    """
    import importlib
    import bots.pick_lock as pl
    importlib.reload(pl)

    cur = tmp / "current"
    cur.mkdir(parents=True, exist_ok=True)
    pl.PUBLIC = tmp
    pl.CURRENT = cur

    (cur / "today.json").write_text(json.dumps({"players": slate_rows}))
    if seed_ledger is not None:
        (cur / "pick_lock.json").write_text(json.dumps(seed_ledger))

    def local_lock(date):
        p = cur / "pick_lock.json"
        if p.exists():
            j = json.loads(p.read_text())
            if j.get("date") == date:
                return j
        return {"date": date, "games": {}, "tickets": {}, "rejected": [],
                "prediction_of_record": {}}
    pl.fetch_lock = local_lock

    argv = sys.argv
    sys.argv = ["pick_lock.py"] + (["--apply"] if apply else ["--dry-run"])
    try:
        pl.main()
    finally:
        sys.argv = argv

    return json.loads((cur / "pick_lock.json").read_text()) if (cur / "pick_lock.json").exists() else {}


# ── 1. THE RUN STANDING AT FIRST PITCH IS LOCKED, AND STAYS LOCKED ──────────
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-30)
    rid1 = run_id_at(-40)
    rows = [slate_row(1, "Ann", 100, started, run_id=rid1, model_version="mlb_hr_v3")]
    ledger = run_lock(tmp, rows)
    por = ledger["prediction_of_record"]["100"]
    check("locks to the run_id standing at first pitch", por["run_id"], rid1)
    check("model_version captured too", por["model_version"], "mlb_hr_v3")
    checkTrue("locked_at is stamped", bool(por.get("locked_at")))

    # A later recompute (model drift, a reweight, whatever) must NOT move it.
    rid2 = run_id_at(0)
    rows2 = [slate_row(1, "Ann", 100, started, run_id=rid2, model_version="mlb_hr_v4")]
    ledger2 = run_lock(tmp, rows2)
    por2 = ledger2["prediction_of_record"]["100"]
    check("a later run does NOT move the lock", por2["run_id"], rid1)
    check("model_version stays with the locked run too", por2["model_version"], "mlb_hr_v3")


# ── 2. PREGAME: NO RECORD YET ────────────────────────────────────────────────
# Before first pitch there is nothing official -- research/drift runs freely.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    not_yet = iso(120)
    rows = [slate_row(1, "Ann", 100, not_yet, run_id=run_id_at(0))]
    ledger = run_lock(tmp, rows)
    check("no game_pk entry before first pitch", "100" in ledger["prediction_of_record"], False)


# ── 3. DOUBLEHEADERS: TWO GAME_PKS, TWO INDEPENDENT LOCKS ───────────────────
# Same two teams, same slate, two distinct game_pks with different first
# pitches -- each must lock on ITS OWN schedule to ITS OWN run_id.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    game1_started, game2_pregame = iso(-20), iso(90)
    rid_g1 = run_id_at(-25)
    rows = [
        slate_row(1, "Ann", 301, game1_started, run_id=rid_g1, model_version="mlb_hr_v3"),
        slate_row(2, "Bob", 302, game2_pregame, run_id=run_id_at(-25), model_version="mlb_hr_v3"),
    ]
    ledger = run_lock(tmp, rows)
    check("doubleheader game 1 (started) is locked", "301" in ledger["prediction_of_record"], True)
    check("doubleheader game 2 (not started) is not locked yet", "302" in ledger["prediction_of_record"], False)
    check("game 1 locked to its own run_id", ledger["prediction_of_record"]["301"]["run_id"], rid_g1)

    # Game 2 starts later, on a later run with a different run_id -- must
    # lock independently, to ITS run, not game 1's.
    game2_started = iso(-5)
    rid_g2 = run_id_at(-10)
    rows2 = [
        slate_row(1, "Ann", 301, game1_started, run_id=run_id_at(0)),   # later run, must not move game 1
        slate_row(2, "Bob", 302, game2_started, run_id=rid_g2, model_version="mlb_hr_v3"),
    ]
    ledger2 = run_lock(tmp, rows2)
    check("game 1 still locked to its original run_id", ledger2["prediction_of_record"]["301"]["run_id"], rid_g1)
    check("game 2 now locked to ITS OWN run_id (not game 1's)",
          ledger2["prediction_of_record"]["302"]["run_id"], rid_g2)


# ── 4. GAMES LOCKING AT DIFFERENT TIMES WITHIN ONE RUN ──────────────────────
# One recompute can see some games already started and others not -- each
# game_pk's lock decision must be independent within the SAME run, not an
# all-or-nothing switch for the slate.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    early, late = iso(-15), iso(200)
    rid = run_id_at(-20)
    rows = [
        slate_row(1, "Ann", 401, early, run_id=rid),
        slate_row(2, "Bob", 402, late, run_id=rid),
    ]
    ledger = run_lock(tmp, rows)
    check("early game locked", "401" in ledger["prediction_of_record"], True)
    check("late game not locked yet", "402" in ledger["prediction_of_record"], False)


# ── 5. LATE LINEUP ADDITIONS: A PLAYER JOINING AFTER LOCK GETS NO RETROACTIVE
#      RECORD, AND CANNOT MOVE THE GAME'S LOCK ──────────────────────────────
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-30)
    rid1 = run_id_at(-35)
    rows = [slate_row(1, "Ann", 500, started, run_id=rid1, model_version="mlb_hr_v3")]
    ledger = run_lock(tmp, rows)
    locked_run = ledger["prediction_of_record"]["500"]["run_id"]

    # A bench player gets added to the real lineup after the lock, scored by
    # a brand-new run_id.
    rows_with_addition = [
        slate_row(1, "Ann", 500, started, run_id=run_id_at(0)),
        slate_row(9, "Zed", 500, started, run_id=run_id_at(0)),   # the late add
    ]
    ledger2 = run_lock(tmp, rows_with_addition)
    check("the game's lock does not move for a late addition",
          ledger2["prediction_of_record"]["500"]["run_id"], locked_run)
    # pick_lock.py itself has nothing per-player to assert here (prediction_
    # of_record is a game-level lock, by design -- see the block comment in
    # pick_lock.py). The honest consequence, left for eval_report.py to
    # observe later, is that Zed will not appear in the locked run's own
    # prediction_log, so he correctly gets no official row -- not a bug.


# ── 6. MISSING RUN IDS: LOCKS ANYWAY, HONESTLY, NOT FABRICATED ──────────────
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-10)
    # model_registry import failed this run (or any other reason a row
    # ships with a blank run_id) -- the row still has the KEY, just empty.
    rows = [slate_row(1, "Ann", 600, started, run_id="", model_version="")]
    ledger = run_lock(tmp, rows)
    por = ledger["prediction_of_record"]["600"]
    check("locks even with a blank run_id", "600" in ledger["prediction_of_record"], True)
    check("run_id is None, not empty string or fabricated", por["run_id"], None)
    check("model_version is None too", por["model_version"], None)
    check("locked_late is None (unknown, since there is no timestamp to compare)",
          por["locked_late"], None)

    # And it must never move once locked, even once a real run_id shows up.
    rows2 = [slate_row(1, "Ann", 600, started, run_id=run_id_at(0), model_version="mlb_hr_v3")]
    ledger2 = run_lock(tmp, rows2)
    check("a later real run_id does not retroactively fill in the gap",
          ledger2["prediction_of_record"]["600"]["run_id"], None)


# ── 7. LEGACY PREDICTIONS: NO run_id KEY AT ALL (PRE-REGISTRY SHAPE) ────────
# Rows from before run_id/model_version existed as a concept carry neither
# key at all, not even blank ones. Must behave exactly like case 6, not
# raise on the missing key.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-10)
    legacy_row = {"player_id": 1, "name": "Ann", "game_pk": 700,
                  "game_time": started, "game_pick_role": "", "hr_score": 50.0,
                  "team": "AAA", "opponent": "BBB"}   # no run_id / model_version key at all
    ledger = run_lock(tmp, [legacy_row])
    por = ledger["prediction_of_record"]["700"]
    check("legacy row (no run_id key at all) still locks", "700" in ledger["prediction_of_record"], True)
    check("run_id is honestly None", por["run_id"], None)
    check("model_version is honestly None", por["model_version"], None)

# A SEPARATE legacy scenario: a ledger fetched from the data branch that was
# already locking designations before this feature existed at all -- games[]
# has locked cats, but there is no prediction_of_record key in the fetched
# ledger whatsoever (an old-shaped pick_lock.json). Must not crash on the
# missing top-level key, and must lock this run's game going forward exactly
# like a fresh ledger would (there is no way to recover what "the pregame
# run" was for a game whose lock predates this feature -- going forward from
# here is the honest thing pick_lock.py itself can do; see the module-level
# rationale for why this is a forward-looking mechanism, not retroactive).
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-10)
    old_shaped_ledger = {
        "date": dt.datetime.now(dt.timezone.utc).date().isoformat(),
        "games": {"800": {"first_pitch": started, "cats": {
            "TOP": {"pid": 1, "name": "Ann", "at": started, "locked": True, "locked_late": False,
                    "history": [{"pid": 1, "name": "Ann", "at": started}]}}}},
        "tickets": {}, "rejected": [],
        # NOTE: no "prediction_of_record" key at all -- the pre-Task-6 shape.
    }
    rows = [slate_row(1, "Ann", 800, started, run_id=run_id_at(-15), model_version="mlb_hr_v3", role="TOP")]
    ledger = run_lock(tmp, rows, seed_ledger=old_shaped_ledger)
    checkTrue("does not crash loading an old-shaped ledger with no prediction_of_record key",
              "prediction_of_record" in ledger)
    check("locks the game going forward under the new code",
          "800" in ledger["prediction_of_record"], True)
    check("designation lock from the old ledger is untouched",
          ledger["games"]["800"]["cats"]["TOP"]["pid"], 1)


# ── 8. ONE PLAYER-GAME = ONE OFFICIAL EVALUATION ROW ─────────────────────────
# Once a game_pk is locked, EVERY player in that game shares the same one
# run_id to join against -- not just the designated picks, and never a
# second, different run_id no matter how many more times the slate recomputes.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-30)
    rid1 = run_id_at(-35)
    rows = [
        slate_row(1, "Ann", 900, started, run_id=rid1, model_version="mlb_hr_v3", role="TOP"),
        slate_row(2, "Bob", 900, started, run_id=rid1, model_version="mlb_hr_v3"),   # undesignated
        slate_row(3, "Cal", 900, started, run_id=rid1, model_version="mlb_hr_v3"),   # undesignated
    ]
    ledger = run_lock(tmp, rows)
    check("exactly one prediction_of_record entry for this game_pk", len(ledger["prediction_of_record"]), 1)
    locked_run = ledger["prediction_of_record"]["900"]["run_id"]
    check("the locked run covers undesignated players too, same run_id",
          locked_run, rid1)

    # Ten more recomputes, different scores/players, even a thin slate with
    # only one player left -- the ONE record must never drift.
    for i in range(10):
        drifted = [slate_row(1, "Ann", 900, started, run_id=run_id_at(i))]
        ledger = run_lock(tmp, drifted)
        check(f"run {i}: still exactly one record for this game_pk", len(ledger["prediction_of_record"]), 1)
        check(f"run {i}: still the original run_id", ledger["prediction_of_record"]["900"]["run_id"], rid1)


if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   prediction_of_record: {CHECKS} assertions, one run per game_pk, locked at first pitch, forever")
