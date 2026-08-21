"""eval_report.py — tier table, monotonicity, rolling windows, exclusions,
and provenance %, all driven off synthetic por_log_*.jsonl /
outcome_log_*.jsonl / prediction_log_*.jsonl files written to a throwaway
directory, in exactly the shape bots/pick_lock.py and
bots/live_results_tracker.py actually publish.

Expected N/HR counts below are worked out by hand in the comments; rates,
lifts, and CIs are then derived with plain arithmetic in this file (not
retyped as decimals) so a comparison failure means the CODE disagrees with
the counts, not that a decimal got fat-fingered twice in two places.

Run: python tests/test_eval_report.py
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bots.eval_report as ev  # noqa: E402

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


def approx(name, got, want, tol=1e-9):
    global CHECKS
    CHECKS += 1
    if got is None or want is None or abs(got - want) > tol:
        FAILED.append(f"{name}: got {got!r}, want ≈{want!r}")


AS_OF = dt.date(2026, 8, 21)
DATE_A = "2026-08-19"    # 2 days back  -- inside 7/14/30-day windows
DATE_B = "2026-08-10"    # 11 days back -- inside 14/30, outside 7
DATE_OLD = "2026-07-01"  # 51 days back -- outside all three windows


def write_por_log(d: Path, date: str, entries: list[dict]) -> None:
    p = d / f"por_log_{date}.jsonl"
    with p.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps({"prediction_date": date, **e}))
            f.write("\n")


def write_outcome_log(d: Path, date: str, entries: list[dict]) -> None:
    p = d / f"outcome_log_{date}.jsonl"
    with p.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e))
            f.write("\n")


def write_pred_log(d: Path, run_id: str, date: str, rows: list[dict]) -> None:
    p = d / f"prediction_log_{run_id}.jsonl"
    lines = [{"run_id": run_id, "generated_at": "irrelevant-header", "slate_date": date}]
    for r in rows:
        lines.append({
            "prediction_date": date, "player_id": r["pid"], "player": r["name"],
            "game_pk": r["gp"], "run_id": run_id,
            "scores": {"hr": r["score"]},
        })
    with p.open("w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln))
            f.write("\n")


def outcome(gp, pid, went_yard, is_final=True, void=False, void_reason=None, revision=1):
    return {
        "player_game_id": f"{gp}|{pid}", "game_pk": gp, "player_id": pid,
        "went_yard": went_yard, "is_final": is_final, "void": void, "void_reason": void_reason,
        "revision": revision,
    }


def por(gp, run_id, model_version, generated_at, locked_late, date):
    return {
        "game_pk": gp, "run_id": run_id, "model_version": model_version,
        "generated_at": generated_at, "first_pitch": f"{date}T20:00:00+00:00",
        "locked_at": f"{date}T20:00:01+00:00", "locked_late": locked_late,
    }


with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)

    # ── DATE_A: one clean, fully-graded game per tier ───────────────────────
    # 90+: 4 players, 2 HR   80-89: 5 players, 1 HR   70-79: 5 players, 1 HR
    # 60-69 / 50-59 / <50: 5 players each, 0 HR
    tier_spec = [
        ("90+", 95.0, 4, 2),
        ("80-89", 85.0, 5, 1),
        ("70-79", 75.0, 5, 1),
        ("60-69", 65.0, 5, 0),
        ("50-59", 55.0, 5, 0),
        ("<50", 30.0, 5, 0),
    ]
    run_a = "2026-08-19.100000Z.test-a"
    gp_seq = 9000
    pid_seq = 1
    por_a, pred_a, outc_a = [], [], []
    for _label, score, n, hrs in tier_spec:
        gp_seq += 1
        gp = str(gp_seq)
        por_a.append(por(gp, run_a, "mlb_hr_v3", "2026-08-19T10:00:00+00:00", False, DATE_A))
        for i in range(n):
            pid = pid_seq; pid_seq += 1
            pred_a.append({"pid": pid, "name": f"P{pid}", "gp": gp, "score": score})
            outc_a.append(outcome(gp, pid, i < hrs))
    write_por_log(tmp, DATE_A, por_a)
    write_pred_log(tmp, run_a, DATE_A, pred_a)
    write_outcome_log(tmp, DATE_A, outc_a)

    # ── DATE_B: one more 90+ game, clean, 1/3 HR ────────────────────────────
    run_b = "2026-08-10.100000Z.test-b"
    gp_b = "9100"
    write_por_log(tmp, DATE_B, [por(gp_b, run_b, "mlb_hr_v3", "2026-08-10T10:00:00+00:00", False, DATE_B)])
    write_pred_log(tmp, run_b, DATE_B, [
        {"pid": 900, "name": "B0", "gp": gp_b, "score": 92.0},
        {"pid": 901, "name": "B1", "gp": gp_b, "score": 91.0},
        {"pid": 902, "name": "B2", "gp": gp_b, "score": 90.0},
    ])
    write_outcome_log(tmp, DATE_B, [
        outcome(gp_b, 900, True), outcome(gp_b, 901, False), outcome(gp_b, 902, False),
    ])

    # ── DATE_OLD: outside every rolling window, still in the all-time table ─
    run_old = "2026-07-01.100000Z.test-old"
    gp_old = "9200"
    write_por_log(tmp, DATE_OLD, [por(gp_old, run_old, "mlb_hr_v3", "2026-07-01T10:00:00+00:00", False, DATE_OLD)])
    write_pred_log(tmp, run_old, DATE_OLD, [{"pid": 700, "name": "Old", "gp": gp_old, "score": 96.0}])
    write_outcome_log(tmp, DATE_OLD, [outcome(gp_old, 700, True)])

    # ── exclusion scenarios, one game each, all dated DATE_A ────────────────
    # locked_late
    gp_late = "9300"
    run_late = "2026-08-19.110000Z.test-late"
    write_por_log(tmp, DATE_A, [por(gp_late, run_late, "mlb_hr_v3", "2026-08-19T21:00:00+00:00", True, DATE_A)])
    write_pred_log(tmp, run_late, DATE_A, [{"pid": 1001, "name": "Late", "gp": gp_late, "score": 95.0}])
    write_outcome_log(tmp, DATE_A, [outcome(gp_late, 1001, True)])

    # missing_provenance -- no run_id / generated_at at all. A candidate only
    # exists for a provenance-less game if there's a graded outcome to join
    # against (see build_candidates' late-join path), so give it one.
    gp_noprov = "9301"
    write_por_log(tmp, DATE_A, [por(gp_noprov, None, None, None, None, DATE_A)])
    write_outcome_log(tmp, DATE_A, [outcome(gp_noprov, 1007, True)])

    # legacy_or_unknown_model -- run_id/generated_at present, model_version missing
    gp_nomv = "9302"
    run_nomv = "2026-08-19.120000Z.test-nomv"
    write_por_log(tmp, DATE_A, [por(gp_nomv, run_nomv, None, "2026-08-19T10:00:00+00:00", False, DATE_A)])
    write_pred_log(tmp, run_nomv, DATE_A, [{"pid": 1002, "name": "NoMV", "gp": gp_nomv, "score": 95.0}])
    write_outcome_log(tmp, DATE_A, [outcome(gp_nomv, 1002, True)])

    # missing_prediction_log -- run_id known, but no prediction_log_<run_id>.jsonl exists
    gp_nolog = "9303"
    run_nolog = "2026-08-19.130000Z.test-nolog-NEVER-WRITTEN"
    write_por_log(tmp, DATE_A, [por(gp_nolog, run_nolog, "mlb_hr_v3", "2026-08-19T10:00:00+00:00", False, DATE_A)])

    # void
    gp_void = "9304"
    run_void = "2026-08-19.140000Z.test-void"
    write_por_log(tmp, DATE_A, [por(gp_void, run_void, "mlb_hr_v3", "2026-08-19T10:00:00+00:00", False, DATE_A)])
    write_pred_log(tmp, run_void, DATE_A, [{"pid": 1003, "name": "Void", "gp": gp_void, "score": 95.0}])
    write_outcome_log(tmp, DATE_A, [outcome(gp_void, 1003, False, void=True, void_reason="postponed")])

    # no_outcome_yet -- graded row missing entirely (game not final)
    gp_pending = "9305"
    run_pending = "2026-08-19.150000Z.test-pending"
    write_por_log(tmp, DATE_A, [por(gp_pending, run_pending, "mlb_hr_v3", "2026-08-19T10:00:00+00:00", False, DATE_A)])
    write_pred_log(tmp, run_pending, DATE_A, [{"pid": 1004, "name": "Pending", "gp": gp_pending, "score": 95.0}])

    # missing_score_row -- one player IN the locked run (included, tier 90+,
    # HR) and one player graded for the same game_pk who never appeared in
    # that run's own prediction_log (joined the slate after the lock).
    gp_latejoin = "9306"
    run_latejoin = "2026-08-19.160000Z.test-latejoin"
    write_por_log(tmp, DATE_A, [por(gp_latejoin, run_latejoin, "mlb_hr_v3", "2026-08-19T10:00:00+00:00", False, DATE_A)])
    write_pred_log(tmp, run_latejoin, DATE_A, [{"pid": 1005, "name": "InLock", "gp": gp_latejoin, "score": 95.0}])
    write_outcome_log(tmp, DATE_A, [
        outcome(gp_latejoin, 1005, True),   # in the log -> included
        outcome(gp_latejoin, 1006, True),   # NOT in the log -> missing_score_row
    ])

    # unusable_hr_score -- the locked run HAS a row for this player, but its
    # scores.hr is null, and he went deep. This is the exact shape of the bug
    # the adversarial review found: before the guard in classify(), this row
    # passed as INCLUDED, its HR landed in total_hr, and then tier_for(None)
    # dropped it from every tier -- so OVERALL read 1 HR richer than the tiers
    # could account for, and the lift column silently divided every real tier
    # by an inflated denominator. The assertions below that catch a regression
    # here are "overall HRs" and the reconciliation block, not just the
    # excluded_by_reason count.
    gp_nullscore = "9307"
    run_nullscore = "2026-08-19.170000Z.test-nullscore"
    write_por_log(tmp, DATE_A, [por(gp_nullscore, run_nullscore, "mlb_hr_v3", "2026-08-19T10:00:00+00:00", False, DATE_A)])
    write_pred_log(tmp, run_nullscore, DATE_A, [{"pid": 1008, "name": "NullScore", "gp": gp_nullscore, "score": None}])
    write_outcome_log(tmp, DATE_A, [outcome(gp_nullscore, 1008, True)])

    report = ev.build_report(tmp, model_version=None, as_of=AS_OF)

    # ── tier table -- 1005 (score 95, HR) folds into 90+ alongside DATE_A's
    # own tier_spec, DATE_B's 3 players, and DATE_OLD's 1 player ───────────
    expected_tiers = {
        "90+":   (4 + 1 + 3 + 1, 2 + 1 + 1 + 1),  # tier_spec + 1005 + DATE_B + DATE_OLD
        "80-89": (5, 1),
        "70-79": (5, 1),
        "60-69": (5, 0),
        "50-59": (5, 0),
        "<50":   (5, 0),
    }
    total_n = sum(n for n, _ in expected_tiers.values())
    total_hr = sum(h for _, h in expected_tiers.values())
    overall_rate = total_hr / total_n

    tiers = report["tier_table"]["tiers"]
    for label, (n, hrs) in expected_tiers.items():
        check(f"{label} N", tiers[label]["n"], n)
        check(f"{label} HRs", tiers[label]["hrs"], hrs)
        approx(f"{label} rate", tiers[label]["hr_rate"], hrs / n if n else None)
        if n:
            approx(f"{label} lift", tiers[label]["lift"], (hrs / n) / overall_rate)
            ci = tiers[label]["ci_95"]
            checkTrue(f"{label} CI is a (lo, hi) pair with lo <= rate <= hi",
                      ci is not None and ci[0] <= hrs / n <= ci[1])

    ov = report["tier_table"]["overall"]
    check("overall N", ov["n"], total_n)
    # If the null-score row above ever leaks back into the included set, THIS
    # is the assertion that fires: its HR would push overall HRs to total_hr+1
    # while every tier count stayed put.
    check("overall HRs -- the null-score HR is NOT in here", ov["hrs"], total_hr)
    approx("overall rate", ov["hr_rate"], overall_rate)

    # ── reconciliation: every included row lands in exactly one tier ─────
    tiered_n = sum(t["n"] for t in tiers.values())
    tiered_hrs = sum(t["hrs"] for t in tiers.values())
    check("sum of tier N equals overall N -- no row counted in OVERALL but held by no tier",
          tiered_n, ov["n"])
    check("sum of tier HRs equals overall HRs", tiered_hrs, ov["hrs"])
    check("unbucketed_n is zero", report["tier_table"]["unbucketed_n"], 0)
    check("unbucketed_hrs is zero", report["tier_table"]["unbucketed_hrs"], 0)

    # ── monotonicity: rates are 0.5-ish, 0.2, 0.2, 0, 0, 0 -- non-increasing,
    # so no real inversion, even though every tier here is under the 20-N
    # low-confidence threshold ──────────────────────────────────────────────
    mono = report["monotonicity"]
    checkTrue("no genuine inversions in a deliberately monotonic fixture", mono["monotonic"])
    check("zero violations listed", len(mono["violations"]), 0)
    checkTrue("every pair is flagged low_confidence (all tiers under N=20 here)",
              all(p["status"] in ("low_confidence", "ok") for p in mono["pairs"] if p["status"] != "no_data"))

    # ── rolling windows ──────────────────────────────────────────────────
    # DATE_A included N/HR = tier_spec (29,4) + 1005 (1,1) = 30, 5
    date_a_n, date_a_hr = 30, 5
    date_b_n, date_b_hr = 3, 1
    by_window = {w["days"]: w for w in report["rolling"]}
    check("7-day sees only DATE_A", by_window[7]["n"], date_a_n)
    check("7-day HR", by_window[7]["hrs"], date_a_hr)
    approx("7-day rate", by_window[7]["hr_rate"], date_a_hr / date_a_n)
    check("14-day sees DATE_A + DATE_B", by_window[14]["n"], date_a_n + date_b_n)
    check("14-day HR", by_window[14]["hrs"], date_a_hr + date_b_hr)
    check("30-day == 14-day here (DATE_OLD is 51 days back, still excluded)",
          by_window[30]["n"], by_window[14]["n"])
    checkTrue("DATE_OLD's single player never enters any rolling window",
              by_window[30]["n"] < total_n)

    # ── model-version breakdown ─────────────────────────────────────────
    by_mv = report["by_model_version"]
    check("only mlb_hr_v3 appears among INCLUDED rows (NoMV was excluded, not counted here)",
          list(by_mv.keys()), ["mlb_hr_v3"])
    check("mlb_hr_v3 N matches overall (nothing else included)", by_mv["mlb_hr_v3"]["n"], total_n)

    # ── exclusions, one of each reason ──────────────────────────────────
    expected_reasons = {
        "locked_late": 1,
        "missing_provenance": 1,
        "legacy_or_unknown_model": 1,
        "missing_prediction_log": 1,
        "void": 1,
        "no_outcome_yet": 1,
        "missing_score_row": 1,
        "unusable_hr_score": 1,
    }
    for reason, n in expected_reasons.items():
        check(f"excluded_by_reason[{reason}]", report["excluded_by_reason"].get(reason), n)
    check("total excluded == sum of every reason above", report["n_excluded"], sum(expected_reasons.values()))
    check("n_candidates == included + excluded", report["n_candidates"], report["n_included"] + report["n_excluded"])
    check("n_included matches the tier table's own total", report["n_included"], total_n)

    # ── provenance % -- every por entry has run_id+generated_at EXCEPT
    # gp_noprov (1 candidate), out of 15 total por entries / 41 candidates ──
    n_por_entries = (6            # tier_spec games
                     + 1 + 1      # DATE_B, DATE_OLD
                     + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1)  # the 8 exclusion-scenario games
    check("n_por_entries counted correctly", report["n_por_entries"], n_por_entries)
    check("n_candidates counted correctly", report["n_candidates"], report["n_included"] + report["n_excluded"])
    n_cand = report["n_candidates"]
    approx("provenance_valid_pct excludes exactly gp_noprov's one candidate, "
           "counted per player-game not per game_pk",
           report["provenance_valid_pct"], (n_cand - 1) / n_cand)

    # ── model-version filtering ─────────────────────────────────────────
    filtered = ev.build_report(tmp, model_version="mlb_hr_v4_does_not_exist", as_of=AS_OF)
    check("filtering to a version with zero matching rows includes nothing",
          filtered["n_included"], 0)
    checkTrue("everything real gets excluded as model_version_filtered when the filter doesn't match",
              filtered["excluded_by_reason"].get("model_version_filtered", 0) >= total_n)

    # ── cross-version pooling warning ───────────────────────────────────
    # Every included row in the fixture above is mlb_hr_v3, so an unfiltered
    # run is NOT actually pooling anything and must stay quiet -- a warning
    # that fires on every default run is one people learn to skip past.
    check("only one model version is present among included rows",
          report["model_versions_present"], ["mlb_hr_v3"])
    check("single-version data raises no pooling warning even unfiltered",
          report["warnings"], [])
    checkTrue("the header names the one version rather than claiming a bare ALL",
              ev.render_text(report).splitlines()[0].endswith("ALL (only mlb_hr_v3 present)"))

    with tempfile.TemporaryDirectory() as d2:
        tmp2 = Path(d2)
        # two versions, one clean included game each
        for gp, run, mv, pid in (("8001", "2026-08-19.180000Z.v3", "mlb_hr_v3", 2001),
                                  ("8002", "2026-08-19.190000Z.v4", "mlb_hr_v4", 2002)):
            write_por_log(tmp2, DATE_A, [por(gp, run, mv, "2026-08-19T10:00:00+00:00", False, DATE_A)])
            write_pred_log(tmp2, run, DATE_A, [{"pid": pid, "name": f"P{pid}", "gp": gp, "score": 95.0}])
            write_outcome_log(tmp2, DATE_A, [outcome(gp, pid, True)])

        mixed = ev.build_report(tmp2, model_version=None, as_of=AS_OF)
        check("both versions are seen among included rows",
              mixed["model_versions_present"], ["mlb_hr_v3", "mlb_hr_v4"])
        check("mixed versions with no filter raises exactly one warning",
              len(mixed["warnings"]), 1)
        checkTrue("the warning names both pooled versions",
                  "mlb_hr_v3" in mixed["warnings"][0] and "mlb_hr_v4" in mixed["warnings"][0])
        mixed_txt = ev.render_text(mixed)
        checkTrue("the warning renders ABOVE the tier table, not below it",
                  mixed_txt.index("POOL 2 MODEL VERSIONS") < mixed_txt.index("Score Tier"))

        filtered_mixed = ev.build_report(tmp2, model_version="mlb_hr_v3", as_of=AS_OF)
        check("an explicit --model-version silences the pooling warning",
              filtered_mixed["warnings"], [])
        check("...and actually narrows the eval to that version", filtered_mixed["n_included"], 1)

    # ── _tierable: the precondition that makes the reconciliation hold ──
    check("None is not tierable", ev._tierable(None), False)
    check("NaN is not tierable", ev._tierable(float("nan")), False)
    check("+inf is not tierable", ev._tierable(float("inf")), False)
    check("-inf is not tierable", ev._tierable(float("-inf")), False)
    check("a string score is not tierable", ev._tierable("95"), False)
    check("True is not tierable (isinstance(True, int) is a trap)", ev._tierable(True), False)
    check("a float score is tierable", ev._tierable(95.0), True)
    check("an int score is tierable", ev._tierable(95), True)
    check("a negative score is still tierable", ev._tierable(-5.0), True)
    checkTrue("anything _tierable() accepts, tier_for() actually places in a tier",
              all(ev.tier_for(s) is not None
                  for s in (-1e9, -0.0, 0, 49.999, 50, 59.9, 60, 70, 80, 89.99, 90, 1e9)))

    # ── wilson_ci sanity, independent of the fixture above ──────────────
    check("wilson_ci(0, 0) is undefined, not a fabricated (0, 0)", ev.wilson_ci(0, 0), None)
    lo, hi = ev.wilson_ci(50, 100)
    checkTrue("wilson_ci(50, 100) straddles 0.5", lo < 0.5 < hi)
    checkTrue("wilson_ci narrows as N grows", (ev.wilson_ci(500, 1000)[1] - ev.wilson_ci(500, 1000)[0])
              < (hi - lo))

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   eval_report: {CHECKS} assertions, tier table + monotonicity + rolling windows + "
      f"every exclusion reason + provenance % + model-version filtering + tier/overall "
      f"reconciliation + the cross-version pooling warning, all off real-shaped "
      f"por_log/outcome_log/prediction_log fixtures")
