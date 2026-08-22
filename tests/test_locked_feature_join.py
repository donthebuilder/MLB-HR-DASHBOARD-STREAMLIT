"""apply_locked_features() -- roadmap step 9b task 1, the locked-run
feature join (2026-08-22).

The defect, confirmed at the code level in
claude/moonshot-sol-verdict-graded-archive-leak.md: grading reads the
CURRENTLY PUBLISHED slate, which today.yml keeps rebuilding into the
evening. A rebuild after first pitch calls the live MLB StatsAPI gamelog/
season-stats endpoints fresh -- with no as-of-date parameter -- so
last5_hr/games_since_last_hr (and hr_score, which is built from them) come
back reflecting that night's already-played games.

The fix joins por_log_<date>.jsonl (which run_id pick_lock.py locked for
each game_pk, almost always well before that night's games finish) to
prediction_log_<run_id>.jsonl (that run's own feature values) and overlays
the result onto each currently-published row.

What this file proves, all against real fixture shapes (a por_log line and
a prediction_log line, not abbreviated stand-ins):

  1. load_locked_run_ids() / load_locked_prediction_rows() parse real
     por_log/prediction_log lines correctly, including skipping the
     prediction_log header line (no player_id) without special-casing it,
     and degrade to {} on a missing file rather than raising.
  2. THE GUARD: apply_locked_features() overwrites a leaked (currently-
     published) hr_score/last5_hr/games_since_last_hr/config_hash with the
     LOCKED run's values whenever a join exists -- this is the "never
     again" test the roadmap spec calls for, proving a graded row's
     features can no longer silently diverge from the locked run's.
  3. A row with NO locked match keeps its original (still possibly leaked)
     values completely unchanged, and is honestly stamped
     feature_snapshot="unavailable" -- never silently upgraded to "locked"
     without an actual join.
  4. A player present in the locked join but absent from the currently-
     published rows comes back in dropped_locked_rows, not folded into the
     main rows list (which pick-selection logic reads).

Run: python tests/test_locked_feature_join.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bots.live_results_tracker as lrt  # noqa: E402

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


def write_por_log(d: Path, date: str, lines: list[dict]) -> None:
    p = d / f"por_log_{date}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def write_prediction_log(d: Path, run_id: str, header: dict, rows: list[dict]) -> None:
    p = d / f"prediction_log_{run_id}.jsonl"
    all_lines = [header] + rows
    p.write_text("\n".join(json.dumps(x) for x in all_lines) + "\n", encoding="utf-8")


DATE = "2026-08-22"
LOCKED_RUN = "2026-08-22.070000Z.gha-locked"  # the EARLY, locked run

# ── fixtures ─────────────────────────────────────────────────────────────

POR_LINES = [
    {
        "prediction_date": DATE, "game_pk": 900001, "run_id": LOCKED_RUN,
        "model_version": "mlb_hr_v3", "generated_at": "2026-08-22T07:00:00+00:00",
        "first_pitch": "2026-08-22T19:10:00+00:00", "locked_at": "2026-08-22T19:10:01+00:00",
        "locked_late": False,
    },
    # game 900002 never locked (still pre-first-pitch) -- deliberately no line.
]

PRED_HEADER = {
    "run_id": LOCKED_RUN, "generated_at": "2026-08-22T07:00:00+00:00",
    "slate_date": DATE, "git_sha": "1eca090ef2c06c6d9cb8ef69c8b2d06bb1caff52",
    "model_family": "moonshot-mlb", "model_versions": {"hr": "mlb_hr_v3"},
    "config_hashes": {"hr": "sha256:LOCKEDHASH"}, "schema_version": 1,
}
PRED_ROWS = [
    {
        "prediction_date": DATE, "player_id": 500100, "player": "Locked Hitter",
        "game_pk": 900001, "team": "AAA", "opp": "BBB", "run_id": LOCKED_RUN,
        "generated_at": "2026-08-22T07:00:00+00:00",
        "config_hash": "sha256:LOCKEDHASH",
        "scores": {"hr": 71.4, "overall": 60.0, "hit": 55.0, "hrr": 40.0,
                    "contact": 30.0, "hrw": 20.0, "multi_hit": 10.0},
        "components": {
            "recent_form_last5_hr": 1,       # the PRE-GAME, honest value
            "recent_form_last5_xbh": 1,
            "games_since_last_hr": 4,          # the PRE-GAME, honest value
        },
    },
    # a player who WAS in the locked run but has since been dropped from
    # the currently-published slate entirely (roadmap step 9b/23's second
    # defect).
    {
        "prediction_date": DATE, "player_id": 500999, "player": "Dropped Hitter",
        "game_pk": 900001, "team": "AAA", "opp": "BBB", "run_id": LOCKED_RUN,
        "generated_at": "2026-08-22T07:00:00+00:00",
        "config_hash": "sha256:LOCKEDHASH",
        "scores": {"hr": 12.5},
        "components": {"recent_form_last5_hr": 0, "games_since_last_hr": 20},
    },
]

# The CURRENTLY PUBLISHED slate -- as if fetched from a late-evening rebuild.
# Player 500100's hr_score/last5_hr/games_since_last_hr are LEAKED: he just
# homered in tonight's game, so the live re-fetch shows last5_hr bumped and
# games_since_last_hr reset to 0, and hr_score moved with them. Player
# 500999 (Dropped Hitter) does not appear here at all. Player 700200 is on
# a game (900002) that never locked.
CURRENT_ROWS = [
    {
        "player_id": 500100, "name": "Locked Hitter", "game_pk": 900001,
        "team": "AAA", "hr_score": 95.0, "last5_hr": 2, "last5_xbh": 2,
        "games_since_last_hr": 0, "config_hash": "sha256:POSTGAMEHASH",
    },
    {
        "player_id": 700200, "name": "Not Yet Locked", "game_pk": 900002,
        "team": "CCC", "hr_score": 33.0, "last5_hr": 1, "last5_xbh": 1,
        "games_since_last_hr": 7,
    },
]


def run():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write_por_log(d, DATE, POR_LINES)
        write_prediction_log(d, LOCKED_RUN, PRED_HEADER, PRED_ROWS)

        # ── 1. the raw loaders ──────────────────────────────────────────
        run_id_by_game = lrt.load_locked_run_ids(DATE, d)
        check("load_locked_run_ids: game 900001 -> the locked run_id",
              run_id_by_game.get(900001), LOCKED_RUN)
        check("load_locked_run_ids: game 900002 has no entry (never locked)",
              run_id_by_game.get(900002), None)

        locked_by_key = lrt.load_locked_prediction_rows({LOCKED_RUN}, d)
        checkTrue("load_locked_prediction_rows: header line (no player_id) skipped, "
                  "not mistaken for a player row",
                  all("player_id" in v for v in locked_by_key.values()))
        check("load_locked_prediction_rows: exactly the 2 player rows loaded",
              len(locked_by_key), 2)
        checkTrue("load_locked_prediction_rows: keyed (game_pk, player_id)",
                  (900001, 500100) in locked_by_key and (900001, 500999) in locked_by_key)

        # missing-file cases degrade to empty, never raise
        check("load_locked_run_ids: missing file -> {}",
              lrt.load_locked_run_ids("1999-01-01", d), {})
        check("load_locked_prediction_rows: unknown run_id -> {}",
              lrt.load_locked_prediction_rows({"no-such-run"}, d), {})

        # ── 2. THE GUARD -- locked features overwrite leaked ones ───────
        rows_in = [dict(r) for r in CURRENT_ROWS]  # apply_locked_features must not mutate the caller's dicts
        rows_out, dropped = lrt.apply_locked_features(rows_in, DATE, d)

        by_pid = {r["player_id"]: r for r in rows_out}
        locked_hitter = by_pid[500100]
        check("GUARD: hr_score is overwritten with the LOCKED value, not the leaked one",
              locked_hitter["hr_score"], 71.4)
        check("GUARD: last5_hr is overwritten with the LOCKED (pre-game) value, not the leaked one",
              locked_hitter["last5_hr"], 1)
        check("GUARD: games_since_last_hr is overwritten with the LOCKED (pre-game) value",
              locked_hitter["games_since_last_hr"], 4)
        check("GUARD: config_hash is overwritten with the locked run's provenance",
              locked_hitter["config_hash"], "sha256:LOCKEDHASH")
        check("locked row is stamped feature_snapshot=locked",
              locked_hitter["feature_snapshot"], "locked")
        check("locked row carries the run_id its overlay came from",
              locked_hitter["locked_run_id"], LOCKED_RUN)
        # the caller's original dict must be untouched -- apply_locked_features
        # returns new dicts, it does not mutate in place
        check("input row is not mutated by apply_locked_features (caller safety)",
              rows_in[0]["hr_score"], 95.0)

        # ── 3. no locked match -> untouched values, honestly stamped ────
        not_locked = by_pid[700200]
        check("unlocked row: hr_score is left exactly as currently published",
              not_locked["hr_score"], 33.0)
        check("unlocked row: last5_hr is left exactly as currently published",
              not_locked["last5_hr"], 1)
        check("unlocked row is stamped feature_snapshot=unavailable, "
              "never silently marked locked",
              not_locked["feature_snapshot"], "unavailable")
        checkTrue("unlocked row carries no locked_run_id",
                  "locked_run_id" not in not_locked)

        # ── 4. dropped-but-locked player recovered separately ───────────
        check("exactly one dropped-but-locked player recovered",
              len(dropped), 1)
        check("the dropped player is the one missing from CURRENT_ROWS",
              dropped[0]["player_id"], 500999)
        check("the dropped row carries its locked hr_score",
              dropped[0]["hr_score"], 12.5)
        check("the dropped row is stamped locked",
              dropped[0]["feature_snapshot"], "locked")
        checkTrue("the dropped player never appears in the main rows list "
                  "(pick-selection must only see the currently-published slate)",
                  500999 not in by_pid)

        # ── 5. SLOT_FIELDS whitelist actually carries the new fields through ──
        # (the failure mode this guards: trim_row() silently stripping
        # feature_snapshot/locked_run_id/config_hash before they ever reach
        # graded_results_*.json, which is exactly what missed_signals.py's
        # snapshot_split() and task-4 refusal logic depend on existing.)
        trimmed = lrt.trim_row(locked_hitter)
        checkTrue("trim_row() keeps feature_snapshot", "feature_snapshot" in trimmed)
        checkTrue("trim_row() keeps locked_run_id", "locked_run_id" in trimmed)
        checkTrue("trim_row() keeps config_hash", "config_hash" in trimmed)
        check("trim_row() output still carries the overwritten (locked) hr_score",
              trimmed["hr_score"], 71.4)


run()

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   locked-feature join (roadmap step 9b task 1): {CHECKS} assertions, "
      f"a graded row's leak-prone fields can no longer silently diverge from the "
      f"locked run's values, an unjoined row keeps its own values and is honestly "
      f"stamped unavailable, and a dropped-but-locked player is recovered for slate-"
      f"membership counting without contaminating pick-selection's row shape")
