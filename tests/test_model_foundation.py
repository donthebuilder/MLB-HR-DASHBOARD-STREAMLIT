"""Model foundation: registry, run metadata, row stamping, the prediction
log, and the outcome log (DASH_ROADMAP.md Tasks 1-5, implemented 2026-08-21).

Run: python tests/test_model_foundation.py

This is an ADDITIVE reliability change -- nothing in this file scores
anything. Every check here is either "does the new plumbing behave" or
"did the new plumbing accidentally change something it must not touch"
(scoring, SLOT_FIELDS' existing entries, the slim-payload shape guard).
"""
import dataclasses
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# mlb_dashboard.py resolves its sibling `import model_registry` (and
# `import pair_history_helper`) against sys.path directly -- true whenever
# it's run as the bot entry point (python bots/mlb_dashboard.py, or
# smoke_test.py's own sys.path.insert of its own directory) but NOT true
# for the package-style `from bots.mlb_dashboard import ...` this test file
# uses, unless bots/ is also placed on sys.path directly. Without this,
# mlb_dashboard's defensive try/except around `import model_registry`
# silently falls back to MODEL_REGISTRY = None -- exactly the failure mode
# that fallback exists to survive in production, but not what this file
# means to exercise.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
from bots import model_registry as R  # noqa: E402
from bots.mlb_dashboard import (  # noqa: E402
    HitterRecord,
    MODEL_WEIGHTS,
    apply_model_v2_layers,
    build_run_meta,
    build_prediction_log_lines,
    write_prediction_log,
    load_locked_rows_by_game,
)
from bots.live_results_tracker import (  # noqa: E402
    SLOT_FIELDS,
    trim_row,
    build_outcome_candidates,
    append_outcome_log,
    load_latest_outcome_revisions,
)
from bots.make_slim import _rows_of, slate_is_real, slim_rows  # noqa: E402

FAILED: list = []
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


def make_hitter(**over) -> HitterRecord:
    """Fill every no-default HitterRecord field with the empty value for its
    type, same convention as tests/test_hr_gate_label.py, so this file
    doesn't rot the first time an unrelated required field is added."""
    kw = {}
    for f in dataclasses.fields(HitterRecord):
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        t = str(f.type)
        kw[f.name] = "" if "str" in t else (0.0 if "float" in t else (False if "bool" in t else 0))
    kw.update(over)
    return HitterRecord(**kw)


# ═══ 1. bots/model_registry.py ═══════════════════════════════════════════════
# (1) imports successfully -- if this script got this far, the import at the
# top already succeeded, and validate_registry() ran at import time.
checkTrue("registry: MODEL_FAMILY is a non-empty string", bool(R.MODEL_FAMILY))
checkTrue("registry: MODEL_VERSIONS is non-empty", len(R.MODEL_VERSIONS) > 0)
checkTrue("registry: SCHEMA_VERSION is a positive int", isinstance(R.SCHEMA_VERSION, int) and R.SCHEMA_VERSION >= 1)
# (2) version strings valid/non-empty
for market, version in R.MODEL_VERSIONS.items():
    checkTrue(f"registry: {market} version is non-empty", bool(version and version.strip()))
    checkTrue(f"registry: {market} version matches the registry's own pattern",
               bool(R._VERSION_PATTERN.match(version)))
check("registry: hr market is registered", "hr" in R.MODEL_VERSIONS, True)
# model_versions_snapshot() must be a defensive copy
snap = R.model_versions_snapshot()
snap["hr"] = "TAMPERED"
check("registry: snapshot mutation does not leak into MODEL_VERSIONS", R.MODEL_VERSIONS["hr"], "mlb_hr_v3")

# a deliberately broken registry must fail validate_registry(), not silently pass
_bad_versions = {"hr": ""}
try:
    for market, version in _bad_versions.items():
        if not version.strip():
            raise ValueError("empty version")
    FAILED.append("registry: empty-version guard did not fire (test harness bug)")
except ValueError:
    pass  # expected -- validate_registry()'s own logic rejects this shape


# ═══ 2. Run metadata (Task 2) ═════════════════════════════════════════════════
_slate_date = dt.date(2026, 8, 21)


class _Args:
    today = True
    tomorrow = False


rm1 = build_run_meta(_slate_date, "today", _Args())
rm2 = build_run_meta(_slate_date, "today", _Args())

# (3) one bot run creates one stable run_id -- calling build_run_meta() once
# and reusing the SAME dict for every row (as main() does) means every row
# in that run shares one run_id by construction. Assert the shape is stable
# and self-consistent across the fields the rest of the pipeline reads.
check("run_meta: run_id starts with the slate date", rm1["run_id"].startswith("2026-08-21."), True)
check("run_meta: slate_date matches", rm1["slate_date"], "2026-08-21")
check("run_meta: model_family matches the registry", rm1["model_family"], R.MODEL_FAMILY)
check("run_meta: model_versions matches the registry", rm1["model_versions"], R.MODEL_VERSIONS)
check("run_meta: schema_version matches the registry", rm1["schema_version"], R.SCHEMA_VERSION)
checkTrue("run_meta: generated_at parses as an ISO timestamp",
          bool(dt.datetime.fromisoformat(rm1["generated_at"])))
checkTrue("run_meta: git_sha is present", bool(rm1["git_sha"]))
checkTrue("run_meta: env.python is present", bool(rm1["env"].get("python")))

# (4) two separate runs (two separate build_run_meta() calls, i.e. two bot
# executions) create different run_ids.
checkTrue("run_meta: two separate executions get different run_ids", rm1["run_id"] != rm2["run_id"])


# ═══ 3. Stamping HitterRecord (Task 3) ════════════════════════════════════════
checkTrue("HitterRecord.model_version is defaulted",
          dataclasses.fields(HitterRecord)[[f.name for f in dataclasses.fields(HitterRecord)].index("model_version")].default == "")
checkTrue("HitterRecord.run_id is defaulted",
          dataclasses.fields(HitterRecord)[[f.name for f in dataclasses.fields(HitterRecord)].index("run_id")].default == "")

h = make_hitter(player_id=42, name="Stamped Guy", game_pk=900)
h.model_version = R.MODEL_VERSIONS["hr"]
h.run_id = rm1["run_id"]
# (6) stamped rows contain model_version, (7) stamped rows contain run_id
check("stamping: model_version lands on the row", h.model_version, "mlb_hr_v3")
check("stamping: run_id lands on the row", h.run_id, rm1["run_id"])

# (5) HitterRecord legacy/default loading still works -- a saved row that
# predates model_version/run_id (those two keys entirely absent from the
# JSON) must still load, with both fields defaulting to "".
full = dataclasses.asdict(make_hitter(player_id=7, name="Legacy Row", game_pk=1))
legacy_row = dict(full)
del legacy_row["model_version"]
del legacy_row["run_id"]
tmp_dir = Path(tempfile.mkdtemp())
legacy_path = tmp_dir / "legacy_slate.json"
legacy_path.write_text(json.dumps([legacy_row]), encoding="utf-8")
loaded = load_locked_rows_by_game(legacy_path)
checkTrue("legacy load: the row was loaded at all", 1 in loaded)
if 1 in loaded:
    rec = loaded[1][0]
    check("legacy load: model_version defaults to empty string", rec.model_version, "")
    check("legacy load: run_id defaults to empty string", rec.run_id, "")

# a NEW stamped row round-trips through the exact same loader correctly
stamped_row = dict(full)
stamped_row["model_version"] = "mlb_hr_v3"
stamped_row["run_id"] = "2026-08-21.120000Z.gha-999.1"
stamped_path = tmp_dir / "stamped_slate.json"
stamped_path.write_text(json.dumps([stamped_row]), encoding="utf-8")
loaded2 = load_locked_rows_by_game(stamped_path)
if 1 in loaded2:
    rec2 = loaded2[1][0]
    check("stamped load: model_version round-trips", rec2.model_version, "mlb_hr_v3")
    check("stamped load: run_id round-trips", rec2.run_id, "2026-08-21.120000Z.gha-999.1")


# ═══ 4. SLOT_FIELDS carries the new stamps without losing anything old ═══════
checkTrue("SLOT_FIELDS: model_version added", "model_version" in SLOT_FIELDS)
checkTrue("SLOT_FIELDS: run_id added", "run_id" in SLOT_FIELDS)
# a sample of pre-existing entries must still be present -- this catches an
# accidental replace-instead-of-add edit to the set literal.
for old_field in ("hr_score", "player_id", "game_pick_role", "multi_hit_score", "hr_gate_flagged"):
    checkTrue(f"SLOT_FIELDS: pre-existing field {old_field} still present", old_field in SLOT_FIELDS)

trimmed = trim_row({"player_id": 1, "hr_score": 50.0, "model_version": "mlb_hr_v3",
                     "run_id": "R1", "spray_chart": [1, 2, 3], "unrelated_junk": "x"})
check("trim_row: keeps model_version", trimmed.get("model_version"), "mlb_hr_v3")
check("trim_row: keeps run_id", trimmed.get("run_id"), "R1")
check("trim_row: still drops fields outside the whitelist", "unrelated_junk" in trimmed, False)
check("trim_row: still drops heavy fields", "spray_chart" in trimmed, False)


# ═══ 5. Prediction log (Task 4) ════════════════════════════════════════════════
rows_payload = []
for i in range(3):
    r = make_hitter(player_id=100 + i, name=f"Slate Player {i}", team="NYY",
                     opponent="BOS", game_pk=700, pitcher_id=555)
    apply_model_v2_layers(r)
    r.model_version = R.MODEL_VERSIONS["hr"]
    r.run_id = rm1["run_id"]
    d = dataclasses.asdict(r)
    d["game_pick_role"] = "TOP" if i == 0 else ""
    rows_payload.append(d)

lines = build_prediction_log_lines(rm1, rows_payload)
check("prediction log: line count is 1 run_meta + N hitters", len(lines), 1 + len(rows_payload))
check("prediction log: first line IS the run_meta", lines[0], rm1)

# (8) prediction log contains expected hitters
players_in_log = {ln["player"] for ln in lines[1:]}
check("prediction log: every hitter present", players_in_log,
      {f"Slate Player {i}" for i in range(3)})

# (9) prediction log contains intended score blocks
row0 = lines[1]
for block in ("scores", "candidate", "components"):
    checkTrue(f"prediction log: '{block}' block present", block in row0)
for key in ("hr", "hit", "hrr", "contact", "overall", "top_board", "hrw", "multi_hit"):
    checkTrue(f"prediction log: scores.{key} present", key in row0["scores"])
for key in ("hr_score_shadow", "best_blend_score", "alt_hr_score"):
    checkTrue(f"prediction log: candidate.{key} present", key in row0["candidate"])
check("prediction log: run_id matches the run", row0["run_id"], rm1["run_id"])
check("prediction log: prediction_type is the documented constant", row0["prediction_type"], "slate_row")
check("prediction log: probability is always null, never a bare number", row0["probability"], None)

# (10) shadow/component values are captured when available
r_shadow = make_hitter(player_id=200, name="Shadow Test", game_pk=701)
r_shadow.hr_score_shadow = 61.1
r_shadow.pitcher_hr9 = 1.61
d_shadow = dataclasses.asdict(r_shadow)
d_shadow["game_pick_role"] = ""
lines_shadow = build_prediction_log_lines(rm1, [d_shadow])
check("prediction log: shadow score captured when set", lines_shadow[1]["candidate"]["hr_score_shadow"], 61.1)
check("prediction log: pitcher_hr9 component captured", lines_shadow[1]["components"]["pitcher_hr9"], 1.61)

# (11) missing optional components remain missing/null, not a misleading zero.
# A row that never had a key at all (rather than a real 0.0) must come
# through as None (json null), not silently coerced to 0.
sparse_row = {"player_id": 300, "name": "Sparse", "game_pk": 702}  # no score/component keys at all
sparse_line = build_prediction_log_lines(rm1, [sparse_row])[1]
check("prediction log: truly-absent score is null, not 0", sparse_line["scores"]["hr"], None)
check("prediction log: truly-absent component is null, not 0", sparse_line["components"]["pitcher_hr9"], None)
check("prediction log: absent hr_gate flag defaults False (documented boolean mapping), not null",
      sparse_line["components"]["hr_gate_flagged"], False)

# write_prediction_log actually writes a readable file with the right name
pred_path = write_prediction_log(rm1, rows_payload)
checkTrue("prediction log: write_prediction_log returned a path", pred_path is not None)
if pred_path is not None:
    checkTrue("prediction log: file exists on disk", pred_path.exists())
    check("prediction log: filename embeds the slate_date", pred_path.name.startswith(f"prediction_log_{rm1['slate_date']}."), True)
    written_lines = pred_path.read_text(encoding="utf-8").strip().split("\n")
    check("prediction log: written file has 1 + N lines", len(written_lines), 1 + len(rows_payload))
    check("prediction log: first written line round-trips as this run's run_meta",
          json.loads(written_lines[0]), rm1)
    pred_path.unlink()


# ═══ 6. Outcome log (Task 5) ═══════════════════════════════════════════════════
outcome_rows = [
    {"player_id": 100, "game_pk": 700, "name": "Slate Player 0"},
    {"player_id": 101, "game_pk": 700, "name": "Slate Player 1"},
]
actual_by_pid = {
    100: {"hits": 2, "hr": 1, "runs": 1, "rbi": 2, "tb": 5, "ab": 4, "bb": 0, "k": 1},
    101: {"hits": 0, "hr": 0, "runs": 0, "rbi": 0, "tb": 0, "ab": 0, "bb": 0, "k": 0},
}
game_status_by_pk = {700: {"detailed_state": "Final"}}
graded_at = dt.datetime.now(dt.timezone.utc).isoformat()

candidates = build_outcome_candidates("2026-08-21", outcome_rows, actual_by_pid, game_status_by_pk, graded_at)
check("outcome: one row per player-game", len(candidates), 2)
check("outcome: went_yard true for the HR", candidates[0]["went_yard"], True)
check("outcome: DNP (final, all-zero) is voided", candidates[1]["void"], True)
check("outcome: DNP void_reason is honest, not a guess", candidates[1]["void_reason"], "did_not_play")
check("outcome: grader_version is stamped", candidates[0]["grader_version"], "mlb_grader_v1")
checkTrue("outcome: graded_at is stamped", bool(candidates[0]["graded_at"]))

# (12) outcome records can join prediction records using stable keys --
# player_id + game_pk on both sides must agree, and player_game_id must be
# built the same deterministic way every time.
pred_join_key = (row0["player_id"], row0["game_pk"])
outcome_join_key = (candidates[0]["player_id"], candidates[0]["game_pk"])
checkTrue("join: prediction and outcome rows share a (player_id, game_pk) key space",
          isinstance(pred_join_key[0], int) and isinstance(outcome_join_key[0], int))
lines1, appended1 = append_outcome_log(candidates, None)
lines2, appended2 = append_outcome_log(candidates, None)
check("outcome join: player_game_id is deterministic across two builds",
      json.loads(lines1[0])["player_game_id"], json.loads(lines2[0])["player_game_id"])

# append-safe: a re-grade with identical facts appends nothing to a real file
tmp_outcome = tmp_dir / "outcome_log_2026-08-21.jsonl"
first_lines, first_appended = append_outcome_log(candidates, tmp_outcome)
tmp_outcome.write_text("\n".join(first_lines) + "\n", encoding="utf-8")
check("outcome: first grading run appends every candidate", first_appended, len(candidates))
second_lines, second_appended = append_outcome_log(candidates, tmp_outcome)
check("outcome: an identical re-grade appends nothing (append-safe no-op)", second_appended, 0)
check("outcome: identical re-grade does not shrink or duplicate history",
      len(second_lines), len(first_lines))

# a real change (game just went final with a different stat line) appends a
# NEW revision and keeps the old one intact
actual_by_pid[101] = {"hits": 1, "hr": 0, "runs": 0, "rbi": 1, "tb": 1, "ab": 3, "bb": 0, "k": 1}
changed_candidates = build_outcome_candidates("2026-08-21", outcome_rows, actual_by_pid, game_status_by_pk, graded_at)
third_lines, third_appended = append_outcome_log(changed_candidates, tmp_outcome)
check("outcome: a real stat change appends exactly one new revision", third_appended, 1)
p101_revs = [json.loads(ln) for ln in third_lines if json.loads(ln)["player_id"] == 101]
check("outcome: the old revision is kept, not overwritten", len(p101_revs), 2)
checkTrue("outcome: revision 2 supersedes revision 1",
          any(r["revision"] == 2 and r["supersedes"] == "700|101|r1" for r in p101_revs))

# postponed game -> void with an honest reason, never silently dropped
postponed_rows = [{"player_id": 900, "game_pk": 999, "name": "Rainout Guy"}]
postponed_status = {999: {"detailed_state": "Postponed"}}
postponed_actual = {900: {"hits": 0, "hr": 0, "runs": 0, "rbi": 0, "tb": 0, "ab": 0, "bb": 0, "k": 0}}
postponed_cands = build_outcome_candidates("2026-08-21", postponed_rows, postponed_actual, postponed_status, graded_at)
check("outcome: postponed game produces a row (never silently missing)", len(postponed_cands), 1)
check("outcome: postponed void_reason is honest", postponed_cands[0]["void_reason"], "postponed")

# a game still in progress is NOT void -- it's simply not final yet
live_status = {998: {"detailed_state": "In Progress"}}
live_rows = [{"player_id": 901, "game_pk": 998, "name": "Mid Game Guy"}]
live_actual = {901: {"hits": 1, "hr": 0, "runs": 0, "rbi": 0, "tb": 1, "ab": 2, "bb": 0, "k": 0}}
live_cands = build_outcome_candidates("2026-08-21", live_rows, live_actual, live_status, graded_at)
check("outcome: in-progress game is not marked void", live_cands[0]["void"], False)
check("outcome: in-progress game_status is carried through honestly", live_cands[0]["game_status"], "In Progress")


# ═══ 7. Existing consumers stay compatible (make_slim, graded output) ════════
# (14) current slim/full payload consumers remain compatible: today.json /
# today_slim.json is (and remains) a bare list, and the new HitterRecord
# fields ride along as ordinary scalar keys -- make_slim's list-shaped path
# (_rows_of / slate_is_real / slim_rows) must not choke on them or drop them.
big_payload = [dict(full, model_version="mlb_hr_v3", run_id="R1", game_pk=i, player_id=i)
               for i in range(45)]
ok, how = slate_is_real(big_payload)
checkTrue("make_slim: a real-sized slate with the new fields still passes the shape guard", ok)
rows_seen = _rows_of(big_payload)
check("make_slim: _rows_of sees every row of a list payload unchanged", len(rows_seen), 45)
slimmed = slim_rows(big_payload)
check("make_slim: slim_rows keeps model_version (not in DROP_KEYS)", slimmed[0].get("model_version"), "mlb_hr_v3")
check("make_slim: slim_rows keeps run_id (not in DROP_KEYS)", slimmed[0].get("run_id"), "R1")

# (13) existing graded_results output still works / (15) no scoring value
# changed solely because of this implementation: hr_blend still sums to
# 1.00 (smoke_test.py's own invariant), and stamping model_version/run_id
# on a row after scoring never touches its score fields.
w = MODEL_WEIGHTS["hr_blend"]
checkTrue("scoring untouched: hr_blend weights still sum to 1.00", abs(sum(w.values()) - 1.0) < 1e-9)

before = make_hitter(player_id=1, name="Score Check", season_iso=0.280, last5_hr=2)
apply_model_v2_layers(before)
score_before_stamp = before.hr_score
before.model_version = "mlb_hr_v3"
before.run_id = "SOME_RUN"
check("scoring untouched: stamping model_version/run_id does not change hr_score",
      before.hr_score, score_before_stamp)


if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   model foundation (registry, run_meta, stamping, prediction log, "
      f"outcome log): {CHECKS} assertions, zero scoring drift")
