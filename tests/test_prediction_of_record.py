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

TIMESTAMP FIX (2026-08-21). The first version of this file computed
`locked_late` by parsing a timestamp out of run_id's own
"{slate_date}.{HHMMSSZ}.{source}" shape. Donovan caught this before it
shipped: the leading segment is the Moonshot SLATE date (a Phoenix-day
boundary), not the UTC calendar date the HHMMSSZ time-of-day belongs to,
so a run generated just after UTC midnight parses 24 hours early. run_id
is opaque now — every test below drives `locked_late` off a real
`today_run_meta.json` written alongside the slate, exactly like
production, including the real cross-midnight example from Donovan's
report (section 9).
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


# WALL-CLOCK FIX (2026-08-21, cleanup pass after Sol audit #2). Every
# temporal helper in this file used to call dt.datetime.now()/dt.date.today()
# fresh on every invocation, computed relative to whatever instant the
# interpreter happened to reach that line. That's fine within a single call,
# but this file makes MANY such calls across one run, and pick_lock.main()'s
# own slate-date derivation (bots/pick_lock.py: "date = min(first_pitch
# times).date()...") independently reads real wall-clock-derived timestamps
# too -- so two "now"s captured a few lines apart could, in principle, land
# on opposite sides of a real UTC midnight if the process happened to be
# scheduled right at that instant, producing a real (if rare) flaky
# mismatch between a test's own expected date string and the date
# pick_lock.py actually derived. A single frozen reference instant, captured
# once at import time and reused everywhere below, makes every date/offset
# in this file internally consistent regardless of when the suite actually
# runs -- no live clock read anywhere past this point.
_NOW = dt.datetime.now(dt.timezone.utc)


def iso(offset_min: int) -> str:
    return (_NOW + dt.timedelta(minutes=offset_min)).isoformat().replace("+00:00", "Z")


_RID_SEQ = [0]


def opaque_run_id(tag: str = "") -> str:
    """A run_id shaped like a real one but treated purely as an opaque
    label in these tests -- nothing here is ever parsed for time. The
    number only exists so two calls never collide."""
    _RID_SEQ[0] += 1
    today = _NOW.date().isoformat()
    return f"{today}.{_RID_SEQ[0]:06d}Z.test-{tag or _RID_SEQ[0]}"


def slate_row(pid, name, gpk, game_time, run_id=None, model_version=None, role="", config_hash=None):
    row = {"player_id": pid, "name": name, "game_pk": gpk,
           "game_time": game_time, "game_pick_role": role,
           "hr_score": 50.0, "team": "AAA", "opponent": "BBB"}
    if run_id is not None:
        row["run_id"] = run_id
    if model_version is not None:
        row["model_version"] = model_version
    # PROVENANCE (2026-08-21): omitted by default so every pre-existing
    # scenario in this file (written before config_hash existed) keeps
    # exercising the "field absent entirely" shape -- exactly the real
    # legacy-row case, not a synthetic stand-in for it.
    if config_hash is not None:
        row["config_hash"] = config_hash
    return row


def run_lock(tmp: Path, slate_rows, *, apply=True, seed_ledger=None, run_meta=None, date=None):
    """Run pick_lock's main() against a throwaway public/data tree and
    return the resulting ledger. Mirrors tests/test_pick_lock_tickets.py's
    own harness exactly (same re-point-PUBLIC/CURRENT, same local-only
    fetch_lock stub) so this file exercises the real main(), not a
    reimplementation of it.

    run_meta, when given, is written to current/today_run_meta.json --
    exactly what sync_model_foundation_outputs_to_website_repo() writes in
    the real job, and the ONLY source read_current_run_meta() trusts for
    generated_at. Omit it to simulate a run with no run_meta available at
    all (registry import failure, or a run this old feature predates).

    seed_ledger lets a test start from a ledger that already exists (e.g.
    a "legacy" pick_lock.json written before this feature shipped, with
    games/cats locked but no prediction_of_record key at all).

    date, when given, is written as the payload's own "date" key so a test
    can force which slate date main() derives, instead of always falling
    back to the earliest first_pitch among slate_rows -- needed to simulate
    a slate-date rollover without needing a first_pitch that is both "in
    the past" (so the game actually locks) and "on a different calendar
    date" (so the rollover is exercised) at once.
    """
    import importlib
    import bots.pick_lock as pl
    importlib.reload(pl)

    cur = tmp / "current"
    cur.mkdir(parents=True, exist_ok=True)
    pl.PUBLIC = tmp
    pl.CURRENT = cur

    payload = {"players": slate_rows}
    if date is not None:
        payload["date"] = date
    (cur / "today.json").write_text(json.dumps(payload))
    if seed_ledger is not None:
        (cur / "pick_lock.json").write_text(json.dumps(seed_ledger))
    if run_meta is not None:
        (cur / "today_run_meta.json").write_text(json.dumps(run_meta))
    elif (cur / "today_run_meta.json").exists():
        (cur / "today_run_meta.json").unlink()

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
    rid1 = opaque_run_id("g1a")
    gen1 = iso(-40)   # generated before first pitch -- a clean pregame run
    hash1 = "sha256:" + "a" * 64
    rows = [slate_row(1, "Ann", 100, started, run_id=rid1, model_version="mlb_hr_v3", config_hash=hash1)]
    ledger = run_lock(tmp, rows, run_meta={"run_id": rid1, "generated_at": gen1})
    por = ledger["prediction_of_record"]["100"]
    check("locks to the run_id standing at first pitch", por["run_id"], rid1)
    check("model_version captured too", por["model_version"], "mlb_hr_v3")
    check("config_hash captured too (item 9)", por["config_hash"], hash1)
    check("generated_at is the canonical value from run_meta, verbatim", por["generated_at"], gen1)
    check("locked_late is False for a genuinely pregame run", por["locked_late"], False)
    checkTrue("locked_at is stamped", bool(por.get("locked_at")))

    # A later recompute (model drift, a reweight, whatever) must NOT move it,
    # even with its own, different, valid run_meta AND a different config_hash
    # sitting right there -- item 10: later runs cannot change a locked
    # game's hash, the exact same immutability run_id already gets.
    rid2 = opaque_run_id("g1b")
    hash2 = "sha256:" + "b" * 64
    rows2 = [slate_row(1, "Ann", 100, started, run_id=rid2, model_version="mlb_hr_v4", config_hash=hash2)]
    ledger2 = run_lock(tmp, rows2, run_meta={"run_id": rid2, "generated_at": iso(0)})
    por2 = ledger2["prediction_of_record"]["100"]
    check("a later run does NOT move the lock", por2["run_id"], rid1)
    check("model_version stays with the locked run too", por2["model_version"], "mlb_hr_v3")
    check("config_hash stays with the locked run too, NOT overwritten by the later run's hash (item 10)",
          por2["config_hash"], hash1)
    check("generated_at stays with the locked run too", por2["generated_at"], gen1)


# ── 2. PREGAME: NO RECORD YET ────────────────────────────────────────────────
# Before first pitch there is nothing official -- research/drift runs freely.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    not_yet = iso(120)
    rid = opaque_run_id("pregame")
    rows = [slate_row(1, "Ann", 100, not_yet, run_id=rid)]
    ledger = run_lock(tmp, rows, run_meta={"run_id": rid, "generated_at": iso(0)})
    check("no game_pk entry before first pitch", "100" in ledger["prediction_of_record"], False)


# ── 3. DOUBLEHEADERS: TWO GAME_PKS, TWO INDEPENDENT LOCKS ───────────────────
# Same two teams, same slate, two distinct game_pks with different first
# pitches -- each must lock on ITS OWN schedule to ITS OWN run_id.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    game1_started, game2_pregame = iso(-20), iso(90)
    rid_a = opaque_run_id("dh-a")
    rows = [
        slate_row(1, "Ann", 301, game1_started, run_id=rid_a, model_version="mlb_hr_v3"),
        slate_row(2, "Bob", 302, game2_pregame, run_id=rid_a, model_version="mlb_hr_v3"),
    ]
    ledger = run_lock(tmp, rows, run_meta={"run_id": rid_a, "generated_at": iso(-25)})
    check("doubleheader game 1 (started) is locked", "301" in ledger["prediction_of_record"], True)
    check("doubleheader game 2 (not started) is not locked yet", "302" in ledger["prediction_of_record"], False)
    check("game 1 locked to its own run_id", ledger["prediction_of_record"]["301"]["run_id"], rid_a)

    # Game 2 starts later, on a later run (its own run_meta) -- must lock
    # independently, to ITS run, not game 1's.
    game2_started = iso(-5)
    rid_b = opaque_run_id("dh-b")
    rows2 = [
        slate_row(1, "Ann", 301, game1_started, run_id=rid_b),   # later run, must not move game 1
        slate_row(2, "Bob", 302, game2_started, run_id=rid_b, model_version="mlb_hr_v3"),
    ]
    ledger2 = run_lock(tmp, rows2, run_meta={"run_id": rid_b, "generated_at": iso(-10)})
    check("game 1 still locked to its original run_id", ledger2["prediction_of_record"]["301"]["run_id"], rid_a)
    check("game 2 now locked to ITS OWN run_id (not game 1's)",
          ledger2["prediction_of_record"]["302"]["run_id"], rid_b)


# ── 4. GAMES LOCKING AT DIFFERENT TIMES WITHIN ONE RUN ──────────────────────
# One recompute can see some games already started and others not -- each
# game_pk's lock decision must be independent within the SAME run, not an
# all-or-nothing switch for the slate.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    early, late = iso(-15), iso(200)
    rid = opaque_run_id("mixed")
    rows = [
        slate_row(1, "Ann", 401, early, run_id=rid),
        slate_row(2, "Bob", 402, late, run_id=rid),
    ]
    ledger = run_lock(tmp, rows, run_meta={"run_id": rid, "generated_at": iso(-20)})
    check("early game locked", "401" in ledger["prediction_of_record"], True)
    check("late game not locked yet", "402" in ledger["prediction_of_record"], False)


# ── 5. LATE LINEUP ADDITIONS: A PLAYER JOINING AFTER LOCK GETS NO RETROACTIVE
#      RECORD, AND CANNOT MOVE THE GAME'S LOCK ──────────────────────────────
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    started = iso(-30)
    rid1 = opaque_run_id("late-add-1")
    rows = [slate_row(1, "Ann", 500, started, run_id=rid1, model_version="mlb_hr_v3")]
    ledger = run_lock(tmp, rows, run_meta={"run_id": rid1, "generated_at": iso(-35)})
    locked_run = ledger["prediction_of_record"]["500"]["run_id"]

    # A bench player gets added to the real lineup after the lock, scored by
    # a brand-new run.
    rid2 = opaque_run_id("late-add-2")
    rows_with_addition = [
        slate_row(1, "Ann", 500, started, run_id=rid2),
        slate_row(9, "Zed", 500, started, run_id=rid2),   # the late add
    ]
    ledger2 = run_lock(tmp, rows_with_addition, run_meta={"run_id": rid2, "generated_at": iso(0)})
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
    # No today_run_meta.json this run either -- realistic, since a registry
    # failure is exactly the condition that also disables run_meta writing.
    rows = [slate_row(1, "Ann", 600, started, run_id="", model_version="", config_hash="")]
    ledger = run_lock(tmp, rows)
    por = ledger["prediction_of_record"]["600"]
    check("locks even with a blank run_id", "600" in ledger["prediction_of_record"], True)
    check("run_id is None, not empty string or fabricated", por["run_id"], None)
    check("model_version is None too", por["model_version"], None)
    check("config_hash is None too, not empty string (item 11)", por["config_hash"], None)
    check("generated_at is None -- nothing to attribute it to", por["generated_at"], None)
    check("locked_late is None (unknown, not a guessed False)", por["locked_late"], None)

    # And it must never move once locked, even once a real run_id AND a
    # real run_meta show up.
    rid_real = opaque_run_id("after-blank")
    hash_real = "sha256:" + "c" * 64
    rows2 = [slate_row(1, "Ann", 600, started, run_id=rid_real, model_version="mlb_hr_v3", config_hash=hash_real)]
    ledger2 = run_lock(tmp, rows2, run_meta={"run_id": rid_real, "generated_at": iso(0)})
    check("a later real run_id does not retroactively fill in the gap",
          ledger2["prediction_of_record"]["600"]["run_id"], None)
    check("a later real config_hash does not retroactively fill in the gap either (item 11: never backfilled)",
          ledger2["prediction_of_record"]["600"]["config_hash"], None)
    check("generated_at stays None too, not backfilled",
          ledger2["prediction_of_record"]["600"]["generated_at"], None)


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
    check("config_hash is honestly None (pre-provenance shape, item 11)", por["config_hash"], None)
    check("generated_at is honestly None", por["generated_at"], None)
    check("locked_late is honestly None", por["locked_late"], None)

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
        "date": _NOW.date().isoformat(),
        "games": {"800": {"first_pitch": started, "cats": {
            "TOP": {"pid": 1, "name": "Ann", "at": started, "locked": True, "locked_late": False,
                    "history": [{"pid": 1, "name": "Ann", "at": started}]}}}},
        "tickets": {}, "rejected": [],
        # NOTE: no "prediction_of_record" key at all -- the pre-Task-6 shape.
    }
    rid = opaque_run_id("legacy-ledger")
    rows = [slate_row(1, "Ann", 800, started, run_id=rid, model_version="mlb_hr_v3", role="TOP")]
    ledger = run_lock(tmp, rows, seed_ledger=old_shaped_ledger,
                       run_meta={"run_id": rid, "generated_at": iso(-15)})
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
    rid1 = opaque_run_id("por-1")
    rows = [
        slate_row(1, "Ann", 900, started, run_id=rid1, model_version="mlb_hr_v3", role="TOP"),
        slate_row(2, "Bob", 900, started, run_id=rid1, model_version="mlb_hr_v3"),   # undesignated
        slate_row(3, "Cal", 900, started, run_id=rid1, model_version="mlb_hr_v3"),   # undesignated
    ]
    ledger = run_lock(tmp, rows, run_meta={"run_id": rid1, "generated_at": iso(-35)})
    check("exactly one prediction_of_record entry for this game_pk", len(ledger["prediction_of_record"]), 1)
    locked_run = ledger["prediction_of_record"]["900"]["run_id"]
    check("the locked run covers undesignated players too, same run_id",
          locked_run, rid1)

    # Ten more recomputes, different scores/players, even a thin slate with
    # only one player left -- the ONE record must never drift.
    for i in range(10):
        rid_i = opaque_run_id(f"por-drift-{i}")
        drifted = [slate_row(1, "Ann", 900, started, run_id=rid_i)]
        ledger = run_lock(tmp, drifted, run_meta={"run_id": rid_i, "generated_at": iso(i)})
        check(f"run {i}: still exactly one record for this game_pk", len(ledger["prediction_of_record"]), 1)
        check(f"run {i}: still the original run_id", ledger["prediction_of_record"]["900"]["run_id"], rid1)


# ── 9. THE REAL CROSS-UTC-MIDNIGHT CASE (Donovan's report, 2026-08-21) ──────
# slate_date=2026-08-20, generated_at=2026-08-21T05:44:56Z, run_id begins
# "2026-08-20.054456Z". The date segment of run_id is the SLATE date
# (Phoenix-day boundary); the calendar date the HHMMSSZ time-of-day
# actually falls on can be a day later in UTC. The OLD (buggy) code parsed
# run_id alone and would have reconstructed 2026-08-20T05:44:56Z -- a full
# day before the game even in this scenario's own first_pitch, so it would
# have called this run "on time" when it was in fact generated hours after
# first pitch. locked_late must be computed from the real generated_at,
# and must come out correctly late here.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    real_run_id = "2026-08-20.054456Z.gha-32451657164"          # exactly Donovan's example
    real_generated_at = "2026-08-21T05:44:56.692484+00:00"       # exactly Donovan's example
    # First pitch well before the run was actually generated -- a genuinely
    # late run by any honest measure.
    first_pitch = "2026-08-21T02:00:00+00:00"
    rows = [slate_row(1, "Ann", 999, first_pitch, run_id=real_run_id, model_version="mlb_hr_v3")]
    ledger = run_lock(tmp, rows, run_meta={"run_id": real_run_id, "generated_at": real_generated_at})
    por = ledger["prediction_of_record"]["999"]
    check("run_id is preserved verbatim, opaque, untouched", por["run_id"], real_run_id)
    check("generated_at is the real canonical value, not reconstructed from run_id",
          por["generated_at"], real_generated_at)
    check("locked_late correctly True: the real run was generated ~3h45m AFTER first pitch",
          por["locked_late"], True)
    # Prove the fix, not just the outcome: the naive parse of run_id alone
    # (slate_date + HHMMSSZ) gives 2026-08-20T05:44:56+00:00, which is
    # BEFORE first_pitch (2026-08-21T02:00:00+00:00) -- the old code would
    # have said locked_late=False here. That the real answer is True is the
    # whole point of reading generated_at from run_meta instead.
    naive_wrong_parse = dt.datetime.fromisoformat("2026-08-20T05:44:56+00:00")
    fp_dt = dt.datetime.fromisoformat(first_pitch)
    checkTrue("sanity: the naive run_id-only parse WOULD have been wrong (24h early, reads as on-time)",
              naive_wrong_parse < fp_dt)


# ── 10. por_log_<date>.jsonl — the durable copy that survives the reset ────
# pick_lock.json's own "date" gate (fetch_lock()) throws away the whole
# ledger, prediction_of_record included, the moment the slate date changes.
# append_por_log() is what makes a lock durable past that: every game_pk
# newly locked this run is also written to por_log_<date>.jsonl, once,
# forever, and eval_report.py is meant to read THAT file for history rather
# than the here-today-gone-tomorrow pick_lock.json.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    cur = tmp / "current"
    # NOT _NOW.date(). pick_lock.main() derives the slate from min(first
    # pitch) -- fp_a below is iso(-30) -- so between 00:00 and 00:30 UTC
    # _NOW.date() is already tomorrow while the bot is still writing
    # yesterday's por_log. This file was red for that half hour every night,
    # and every delivery script that runs the suite refused to push during it.
    # Anchor to the same instant the bot keys off.
    _SLATE_REF = _NOW - dt.timedelta(minutes=30)
    today_str = _SLATE_REF.date().isoformat()
    por_log_path = cur / f"por_log_{today_str}.jsonl"

    rid_a = opaque_run_id("g10a")
    gen_a = iso(-40)
    fp_a = iso(-30)
    hash_a = "sha256:" + "d" * 64
    rows = [slate_row(1, "Ann", 501, fp_a, run_id=rid_a, model_version="mlb_hr_v3", config_hash=hash_a)]
    run_lock(tmp, rows, run_meta={"run_id": rid_a, "generated_at": gen_a})

    checkTrue("por_log file is created the moment a game locks", por_log_path.exists())
    lines = [json.loads(l) for l in por_log_path.read_text().splitlines() if l.strip()]
    check("exactly one line for the one game that locked", len(lines), 1)
    check("archived line carries the game_pk", lines[0]["game_pk"], "501")
    check("archived line carries the same run_id pick_lock.json locked to", lines[0]["run_id"], rid_a)
    check("archived line carries config_hash too, past the daily reset (item 9, durable copy)",
          lines[0]["config_hash"], hash_a)
    check("archived line carries generated_at too", lines[0]["generated_at"], gen_a)
    check("archived line is tagged with the slate's prediction_date", lines[0]["prediction_date"], today_str)

    # Re-running the SAME slate (game 501 already locked, nothing new to
    # lock) must not duplicate or rewrite that line.
    run_lock(tmp, rows, run_meta={"run_id": rid_a, "generated_at": gen_a})
    lines_again = [json.loads(l) for l in por_log_path.read_text().splitlines() if l.strip()]
    check("re-running the same slate does not duplicate the archived line", len(lines_again), 1)

    # A second game locking on a LATER run must ADD a line, not replace it --
    # this is the accumulate-across-runs behavior eval_report.py depends on.
    rid_b = opaque_run_id("g10b")
    gen_b = iso(-5)
    fp_b = iso(-1)
    rows2 = [
        slate_row(1, "Ann", 501, fp_a, run_id=rid_a, model_version="mlb_hr_v3"),
        slate_row(2, "Bo", 502, fp_b, run_id=rid_b, model_version="mlb_hr_v3"),
    ]
    run_lock(tmp, rows2, run_meta={"run_id": rid_b, "generated_at": gen_b})
    lines_two = [json.loads(l) for l in por_log_path.read_text().splitlines() if l.strip()]
    check("a second game locking on a later run APPENDS a second line", len(lines_two), 2)
    gps = sorted(l["game_pk"] for l in lines_two)
    check("both game_pks are present", gps, ["501", "502"])

    # THE ACTUAL BUG THIS EXISTS TO FIX: simulate the slate date rolling
    # over. fetch_lock() would hand main() a brand-new, empty ledger for the
    # new date -- prove that por_log_<today_str>.jsonl (yesterday's file, in
    # this scenario) is completely untouched by that, because archival never
    # goes through fetch_lock()/pick_lock.json at all.
    tomorrow_str = (_SLATE_REF.date() + dt.timedelta(days=1)).isoformat()
    # First pitch a genuine 10 minutes in the past (so the game actually
    # locks this run) while the payload's own "date" is explicitly
    # tomorrow_str -- forcing main() to key this run as tomorrow's slate,
    # exactly what a real Phoenix-day rollover looks like from
    # fetch_lock()'s point of view, without needing to wait for real
    # wall-clock midnight in a test.
    fp_c = iso(-10)
    rid_c = opaque_run_id("g10c")
    rows3 = [slate_row(3, "Cy", 503, fp_c, run_id=rid_c, model_version="mlb_hr_v3")]
    run_lock(tmp, rows3, run_meta={"run_id": rid_c, "generated_at": iso(-9)}, date=tomorrow_str)
    # today's own file, from the earlier calls, must still be exactly as it was
    lines_after_rollover = [json.loads(l) for l in por_log_path.read_text().splitlines() if l.strip()]
    check("the OLD date's por_log survives a slate-date rollover untouched", len(lines_after_rollover), 2)
    new_log_path = cur / f"por_log_{tomorrow_str}.jsonl"
    checkTrue("the NEW date gets its own, separate por_log file", new_log_path.exists())


if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   prediction_of_record: {CHECKS} assertions, one run per game_pk, locked at first pitch, "
      f"forever, locked_late from real generated_at (never parsed out of run_id), "
      f"and durably archived to por_log_<date>.jsonl past pick_lock.json's own daily reset")
