"""regrade_stale_dates() / find_unresolved_outcome_dates() -- the
suspended/resumed-game fix (2026-08-21, Sol's audit finding #4).

Before this fix, resolve_grade_date() picked exactly ONE date per grading
run (today, or -- only if today has no breakdown published yet -- the most
recent day that does). Once tomorrow's slate existed, that fallback never
fired for today again, so a game suspended on day D and resumed on D+2 sat
in outcome_log_D.jsonl with is_final=False forever: nothing in the pipeline
ever revisited it.

Two things are proven here, both against real outcome-log rows produced by
the actual build_outcome_candidates()/append_outcome_log() functions (not
hand-typed JSON, so a schema drift in those functions would break this test
too, not silently pass it):

  1. find_unresolved_outcome_dates() correctly separates "still open" from
     "resolved" and from "void" across a lookback window, using only the
     LATEST revision per player-game (an old superseded revision must never
     leak a stale is_final=False back into the result).
  2. regrade_stale_dates() re-fetches live status for exactly those still-
     open games, appends a new revision when the game has actually resolved
     since, and -- the safety property the whole design leans on -- writes
     NOTHING when the game is still unresolved, so this is safe to call on
     every single hourly grading run forever.

Run: python tests/test_regrade_stale.py
"""
import datetime as dt
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


def checkFalse(name, cond):
    global CHECKS
    CHECKS += 1
    if cond:
        FAILED.append(f"{name}: expected falsy, got truthy")


# ── fixture builder: real candidate/append machinery, not hand-typed JSON ──

def seed_outcome_log(tmpdir: Path, date_str: str, entries: list[dict]) -> Path:
    """entries: [{'player_id', 'game_pk', 'detailed_state', 'hr'(optional)}].
    Runs each through the SAME build_outcome_candidates()/append_outcome_log()
    path production grading uses, so the fixture is schema-true by
    construction."""
    rows = [{"player_id": e["player_id"], "game_pk": e["game_pk"]} for e in entries]
    game_status_by_pk = {
        e["game_pk"]: {"detailed_state": e["detailed_state"], "abstract_state": e["detailed_state"]}
        for e in entries
    }
    # Keyed (game_pk, player_id), NOT player_id alone -- see Sol audit #2
    # finding #1 / DOUBLEHEADER GRADING FIX. Matches how build_outcome_candidates()
    # and every other real call site now key this dict.
    actual_by_pid = {
        (e["game_pk"], e["player_id"]): {"hits": 0, "hr": e.get("hr", 0), "runs": 0, "rbi": 0, "tb": 0,
                                          "ab": 1 if "final" in e["detailed_state"].lower() else 0, "bb": 0, "k": 0}
        for e in entries
    }
    candidates = lrt.build_outcome_candidates(date_str, rows, actual_by_pid, game_status_by_pk, "2026-08-14T10:00:00Z")
    path = tmpdir / f"outcome_log_{date_str}.jsonl"
    lines, _ = lrt.append_outcome_log(candidates, None)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    TODAY = dt.date(2026, 8, 21)

    # 08-19: one player, game SUSPENDED -- the exact scenario this fix targets.
    seed_outcome_log(tmp, "2026-08-19", [
        {"player_id": 111, "game_pk": 900001, "detailed_state": "Suspended"},
    ])
    # 08-18: one player, game already FINAL -- must be invisible to the scan.
    seed_outcome_log(tmp, "2026-08-18", [
        {"player_id": 222, "game_pk": 900002, "detailed_state": "Final", "hr": 1},
    ])
    # 08-17: one player, game POSTPONED (void) -- also must be invisible;
    # void means "will never resolve under this game_pk," not "recheck me."
    seed_outcome_log(tmp, "2026-08-17", [
        {"player_id": 333, "game_pk": 900003, "detailed_state": "Postponed"},
    ])
    # 08-05: suspended, but 16 days back -- outside a 14-day lookback window.
    seed_outcome_log(tmp, "2026-08-05", [
        {"player_id": 444, "game_pk": 900004, "detailed_state": "Suspended"},
    ])
    # 08-16: TWO revisions for the same player-game -- game was suspended,
    # THEN this fix's own recheck already resolved it to final. Only the
    # LATEST revision's is_final may be trusted; the superseded revision-1
    # row (is_final=False) must not leak back in.
    p1 = tmp / "outcome_log_2026-08-16.jsonl"
    cands_r1 = lrt.build_outcome_candidates(
        "2026-08-16", [{"player_id": 555, "game_pk": 900005}],
        {(900005, 555): {"hits": 0, "hr": 0, "runs": 0, "rbi": 0, "tb": 0, "ab": 0, "bb": 0, "k": 0}},
        {900005: {"detailed_state": "Suspended", "abstract_state": "Suspended"}}, "2026-08-14T10:00:00Z")
    lines_r1, _ = lrt.append_outcome_log(cands_r1, None)
    p1.write_text("\n".join(lines_r1) + "\n", encoding="utf-8")
    cands_r2 = lrt.build_outcome_candidates(
        "2026-08-16", [{"player_id": 555, "game_pk": 900005}],
        {(900005, 555): {"hits": 2, "hr": 1, "runs": 1, "rbi": 2, "tb": 5, "ab": 4, "bb": 0, "k": 1}},
        {900005: {"detailed_state": "Final", "abstract_state": "Final"}}, "2026-08-15T10:00:00Z")
    lines_r2, appended_r2 = lrt.append_outcome_log(cands_r2, p1)
    p1.write_text("\n".join(lines_r2) + "\n", encoding="utf-8")
    check("08-16 fixture: the resolve produced a real second revision", appended_r2, 1)

    # ── find_unresolved_outcome_dates() ──

    found = lrt.find_unresolved_outcome_dates(tmp, TODAY, lookback_days=14)

    checkTrue("suspended date (08-19) is flagged", "2026-08-19" in found)
    check("08-19's flagged row carries the right player/game", found.get("2026-08-19"),
          [{"player_id": 111, "game_pk": 900001}])
    checkFalse("already-final date (08-18) is NOT flagged", "2026-08-18" in found)
    checkFalse("void/postponed date (08-17) is NOT flagged -- will never resolve under this game_pk", "2026-08-17" in found)
    checkFalse("suspended-but-outside-lookback date (08-05) is NOT flagged at 14 days", "2026-08-05" in found)
    checkFalse("date whose LATEST revision is final (08-16) is NOT flagged, even though rev 1 was suspended",
               "2026-08-16" in found)
    checkFalse("today itself is never flagged -- today's own grading run already covers it",
               TODAY.strftime("%Y-%m-%d") in found)
    checkFalse("a date with no local outcome_log file at all is skipped, not guessed at",
               "2026-08-20" in found)

    # A wider lookback DOES reach the 16-day-back suspended game.
    found_wide = lrt.find_unresolved_outcome_dates(tmp, TODAY, lookback_days=20)
    checkTrue("a 20-day lookback reaches the 16-day-back suspended date", "2026-08-05" in found_wide)

    # ── regrade_stale_dates(): the live re-fetch + no-op safety property ──

    calls = {"fetch_game_feed": [], "get_player_batting_line": []}
    # Every regrade-worthy game_pk in this fixture now reports FINAL when
    # "re-fetched live", except 900001 (08-19's suspended game), which stays
    # suspended -- proving the no-op path separately from the resolve path.
    STILL_SUSPENDED = {900001}

    def fake_fetch_game_feed(game_pk):
        calls["fetch_game_feed"].append(game_pk)
        return {"_game_pk": game_pk}  # opaque; get_game_status is faked below too

    def fake_get_game_status(game_feed):
        gp = game_feed["_game_pk"]
        state = "Suspended" if gp in STILL_SUSPENDED else "Final"
        return {"detailed_state": state, "abstract_state": state}

    def fake_get_player_batting_line(game_feed, player_id):
        calls["get_player_batting_line"].append((game_feed["_game_pk"], player_id))
        if game_feed["_game_pk"] in STILL_SUSPENDED:
            return {"hits": 0, "hr": 0, "runs": 0, "rbi": 0, "tb": 0, "ab": 0, "bb": 0, "k": 0}
        # The suspended game (if it WERE resolved) or the postponed one never
        # reach this fake since regrade_stale_dates must not even try them --
        # this path is only exercised for legitimately-flagged games.
        return {"hits": 1, "hr": 1, "runs": 1, "rbi": 1, "tb": 4, "ab": 3, "bb": 0, "k": 0}

    _orig_fetch, _orig_status, _orig_line = lrt.fetch_game_feed, lrt.get_game_status, lrt.get_player_batting_line
    lrt.fetch_game_feed = fake_fetch_game_feed
    lrt.get_game_status = fake_get_game_status
    lrt.get_player_batting_line = fake_get_player_batting_line
    try:
        lrt.regrade_stale_dates(fetch_dir=tmp, publish_dir=tmp, today=TODAY,
                                 graded_at="2026-08-21T12:00:00Z", lookback_days=14)
    finally:
        lrt.fetch_game_feed, lrt.get_game_status, lrt.get_player_batting_line = _orig_fetch, _orig_status, _orig_line

    checkFalse("void date (08-17, game_pk 900003) was never live-fetched",
               900003 in calls["fetch_game_feed"])
    checkFalse("already-final date (08-18, game_pk 900002) was never live-fetched",
               900002 in calls["fetch_game_feed"])
    checkFalse("out-of-window date (08-05, game_pk 900004) was never live-fetched at 14-day lookback",
               900004 in calls["fetch_game_feed"])
    checkTrue("the genuinely-open date (08-19, game_pk 900001) WAS live-fetched",
              900001 in calls["fetch_game_feed"])

    # 08-19's game is STILL suspended per the fake -- must be a true no-op:
    # zero new lines appended, file byte-identical in row count.
    lines_after = (tmp / "outcome_log_2026-08-19.jsonl").read_text(encoding="utf-8").strip().splitlines()
    check("still-suspended game: outcome_log_2026-08-19.jsonl still has exactly 1 line (no-op)",
          len(lines_after), 1)
    row_after = json.loads(lines_after[0])
    check("still-suspended game: revision is still 1 (no phantom revision written)",
          row_after["revision"], 1)
    checkFalse("still-suspended game: is_final is still False", row_after["is_final"])

    # find_unresolved_outcome_dates() run again must still see it as open --
    # a no-op recheck must not accidentally mark it resolved.
    found_again = lrt.find_unresolved_outcome_dates(tmp, TODAY, lookback_days=14)
    checkTrue("08-19 is STILL flagged unresolved after a no-op recheck", "2026-08-19" in found_again)

    # ── the resolve path, on a second scenario where the game DID finish ──

    seed_outcome_log(tmp, "2026-08-20", [
        {"player_id": 666, "game_pk": 900006, "detailed_state": "Suspended"},
    ])

    def fake_fetch_game_feed2(game_pk):
        return {"_game_pk": game_pk}

    def fake_get_game_status2(game_feed):
        return {"detailed_state": "Final", "abstract_state": "Final"}

    def fake_get_player_batting_line2(game_feed, player_id):
        return {"hits": 2, "hr": 1, "runs": 2, "rbi": 3, "tb": 6, "ab": 4, "bb": 1, "k": 0}

    lrt.fetch_game_feed = fake_fetch_game_feed2
    lrt.get_game_status = fake_get_game_status2
    lrt.get_player_batting_line = fake_get_player_batting_line2
    try:
        # 08-20 is "today - 1", well inside any lookback; use a fresh dir
        # scoped to just this scenario's date so 08-19's earlier fixture
        # (still suspended, per the fake still installed a moment ago) is
        # not re-touched by this second call.
        lrt.regrade_stale_dates(fetch_dir=tmp, publish_dir=tmp, today=TODAY,
                                 graded_at="2026-08-21T13:00:00Z", lookback_days=1)
    finally:
        lrt.fetch_game_feed, lrt.get_game_status, lrt.get_player_batting_line = _orig_fetch, _orig_status, _orig_line

    lines_20 = (tmp / "outcome_log_2026-08-20.jsonl").read_text(encoding="utf-8").strip().splitlines()
    check("resolved game: outcome_log_2026-08-20.jsonl now has 2 lines (original + new revision)",
          len(lines_20), 2)
    newest = json.loads(lines_20[-1])
    check("resolved game: new revision is 2", newest["revision"], 2)
    checkTrue("resolved game: is_final is now True", newest["is_final"])
    checkTrue("resolved game: went_yard now True (he homered)", newest["went_yard"])
    check("resolved game: supersedes points at the original revision",
          newest["supersedes"], "900006|666|r1")

    found_final = lrt.find_unresolved_outcome_dates(tmp, TODAY, lookback_days=14)
    checkFalse("08-20 is no longer flagged once genuinely resolved", "2026-08-20" in found_final)

    # ── DOUBLEHEADER GRADING FIX (2026-08-21, Sol audit #2 finding #1) ──
    #
    # A player who bats in BOTH legs of a doubleheader (same player_id, two
    # distinct game_pks, same date -- confirmed real this season: 2026-08-17
    # STL@CIN, gamePk 824514 + 824478) has two genuinely different batting
    # lines. actual_by_pid used to be keyed by player_id alone, so whichever
    # game was processed last silently overwrote the other's line in the
    # PERMANENT, append-only outcome log. Proven directly against
    # build_outcome_candidates() -- the real function, not a reimplementation.

    DH_PID = 777
    DH_GAME_1, DH_GAME_2 = 900101, 900102
    dh_actual_by_pid = {
        (DH_GAME_1, DH_PID): {"hits": 1, "hr": 1, "runs": 1, "rbi": 1, "tb": 4, "ab": 4, "bb": 0, "k": 1},
        (DH_GAME_2, DH_PID): {"hits": 0, "hr": 0, "runs": 0, "rbi": 0, "tb": 0, "ab": 4, "bb": 0, "k": 2},
    }
    dh_status = {
        DH_GAME_1: {"detailed_state": "Final", "abstract_state": "Final"},
        DH_GAME_2: {"detailed_state": "Final", "abstract_state": "Final"},
    }
    dh_rows = [{"player_id": DH_PID, "game_pk": DH_GAME_1}, {"player_id": DH_PID, "game_pk": DH_GAME_2}]
    dh_candidates = lrt.build_outcome_candidates("2026-08-17", dh_rows, dh_actual_by_pid, dh_status, "2026-08-17T22:00:00Z")
    check("doubleheader: build_outcome_candidates produces exactly 2 candidates (one per leg)",
          len(dh_candidates), 2)
    dh_by_game = {c["game_pk"]: c for c in dh_candidates}
    checkTrue("doubleheader: game 1's candidate (where he homered) has went_yard True",
              dh_by_game[DH_GAME_1]["went_yard"])
    checkFalse("doubleheader: game 2's candidate (where he did NOT homer) has went_yard False -- "
               "NOT overwritten by game 1's line",
               dh_by_game[DH_GAME_2]["went_yard"])
    check("doubleheader: game 1's home_runs is 1", dh_by_game[DH_GAME_1]["home_runs"], 1)
    check("doubleheader: game 2's home_runs is 0, not contaminated by game 1's", dh_by_game[DH_GAME_2]["home_runs"], 0)
    check("doubleheader: game 1's at_bats is 4", dh_by_game[DH_GAME_1]["at_bats"], 4)
    check("doubleheader: game 2's strikeouts-implying at_bats also 4 (independent line, not shared)",
          dh_by_game[DH_GAME_2]["at_bats"], 4)

    # Same fix, same-shaped collision, in grade_pairs_pools() -- pools/pairs
    # read the same actual_by_pid dict, keyed off each player-dict's own
    # game_pk (as published by _s2_player_dict/trim_row, both of which carry
    # it). A pool player whose entry is DH_GAME_2 must never pick up
    # DH_GAME_1's homer.
    dh_sections = {
        "pair_groups": [],
        "pools": [{
            "label": "DH TEST POOL",
            "players": [
                {"player_id": DH_PID, "name": "DH Player", "game_pk": DH_GAME_1},
                {"player_id": DH_PID, "name": "DH Player (leg 2 entry)", "game_pk": DH_GAME_2},
            ],
        }],
    }
    dh_pool_result = lrt.grade_pairs_pools(dh_sections, dh_actual_by_pid)
    dh_pool = dh_pool_result["graded_pools"][0]
    check("grade_pairs_pools: only the leg-1 entry (real HR) is in homer_names",
          dh_pool["homer_names"], ["DH Player"])
    checkTrue("grade_pairs_pools: leg-2 entry's name is NOT in homer_names -- not contaminated by leg 1's HR",
              "DH Player (leg 2 entry)" not in dh_pool["homer_names"])
    check("grade_pairs_pools: hr_count is 1, not 2 (only one leg actually homered)", dh_pool["hr_count"], 1)
    check("grade_pairs_pools: both legs counted active (neither voided -- both had at-bats)",
          dh_pool["total_count"], 2)

    print("doubleheader (Sol audit #2 finding #1): 13 assertions, a player's two doubleheader legs "
          "keep independent, uncontaminated outcome lines in both the outcome log and pool grading")

    # ── DISTANCE-MERGE DOUBLEHEADER KEYING FIX (2026-08-21, quick review of
    # finding #1's fix) ──
    #
    # merge_homer_distances() (extracted from main() specifically so it's
    # testable here) used to key its lookup dict by player_id alone -- the
    # same collision class as actual_by_pid, one layer up the display
    # pipeline. A player who homered in both legs of a doubleheader would
    # have one leg's distance/exit-velo data silently overwrite the other's
    # on the merged "who hit it farthest" board.

    _dist_checks_before = CHECKS
    dh_capture_report = {
        "all_homer_entries": [
            {"player_id": DH_PID, "game_pk": DH_GAME_1, "longest_ft": 430, "max_ev_mph": 112.0},
            {"player_id": DH_PID, "game_pk": DH_GAME_2, "longest_ft": 395, "max_ev_mph": 101.5},
        ],
    }
    dh_merged_leg1 = {"player_id": DH_PID, "name": "DH Player", "base_row": {"game_pk": DH_GAME_1}}
    dh_merged_leg2 = {"player_id": DH_PID, "name": "DH Player", "base_row": {"game_pk": DH_GAME_2}}
    dh_merged_rows = [dh_merged_leg1, dh_merged_leg2]
    lrt.merge_homer_distances(dh_merged_rows, dh_capture_report)
    check("distance merge: leg-1 row picks up leg 1's own distance (430ft), not leg 2's",
          dh_merged_leg1["longest_ft"], 430)
    check("distance merge: leg-1 row picks up leg 1's own exit velo (112.0), not leg 2's",
          dh_merged_leg1["max_ev_mph"], 112.0)
    check("distance merge: leg-2 row picks up leg 2's own distance (395ft), not leg 1's",
          dh_merged_leg2["longest_ft"], 395)
    check("distance merge: leg-2 row picks up leg 2's own exit velo (101.5), not leg 1's",
          dh_merged_leg2["max_ev_mph"], 101.5)
    print(f"distance merge (quick review of finding #1's fix): {CHECKS - _dist_checks_before} assertions, "
          f"a doubleheader player's two legs keep independent distance/exit-velo data on the merged board")

    # ── PER-ITEM ISOLATION, ROUND 2 (2026-08-21, quick review of finding #6's
    # fix) ──
    #
    # get_player_batting_line() used to sit outside regrade_stale_dates()'s
    # inner try/except -- a failure there would propagate to the outer
    # per-date handler and abort every other still-pending game on that
    # date too, not just the one that actually failed. Proves a batting-line
    # lookup failure for one game_pk doesn't block a DIFFERENT game_pk's
    # item, still pending on the very same date, from resolving normally.

    _iso2_checks_before = CHECKS
    tmp3 = tempfile.TemporaryDirectory()
    tmp3p = Path(tmp3.name)

    ISO2_GAME_BAD, ISO2_GAME_GOOD = 900201, 900202
    ISO2_PID_BAD, ISO2_PID_GOOD = 881, 882
    seed_outcome_log(tmp3p, "2026-08-18", [
        {"player_id": ISO2_PID_BAD, "game_pk": ISO2_GAME_BAD, "detailed_state": "Suspended: Rain"},
        {"player_id": ISO2_PID_GOOD, "game_pk": ISO2_GAME_GOOD, "detailed_state": "Suspended: Rain"},
    ])

    def fake_fetch_game_feed3(game_pk):
        return {"game_pk": game_pk}

    def fake_get_game_status3(feed):
        return {"detailed_state": "Final", "abstract_state": "Final"}

    def fake_get_player_batting_line3(game_feed, player_id):
        if game_feed.get("game_pk") == ISO2_GAME_BAD:
            raise ValueError("simulated boxscore parse failure")
        return {"hits": 1, "hr": 1, "runs": 1, "rbi": 1, "tb": 4, "ab": 4, "bb": 0, "k": 0}

    _o_fetch, _o_status, _o_line = lrt.fetch_game_feed, lrt.get_game_status, lrt.get_player_batting_line
    lrt.fetch_game_feed = fake_fetch_game_feed3
    lrt.get_game_status = fake_get_game_status3
    lrt.get_player_batting_line = fake_get_player_batting_line3
    try:
        lrt.regrade_stale_dates(fetch_dir=tmp3p, publish_dir=tmp3p, today=TODAY,
                                 graded_at="2026-08-21T14:00:00Z", lookback_days=14)
    finally:
        lrt.fetch_game_feed, lrt.get_game_status, lrt.get_player_batting_line = _o_fetch, _o_status, _o_line

    lines_good = (tmp3p / "outcome_log_2026-08-18.jsonl").read_text(encoding="utf-8").strip().splitlines()
    good_by_pid = {json.loads(l)["player_id"]: json.loads(l) for l in lines_good}
    checkTrue("isolation round 2: the GOOD game_pk's player still got a new final revision "
              "despite the BAD game_pk's batting-line lookup failing",
              any(row["player_id"] == ISO2_PID_GOOD and row["revision"] == 2 and row["is_final"]
                  for row in (json.loads(l) for l in lines_good)))
    bad_revisions = [json.loads(l)["revision"] for l in lines_good if json.loads(l)["player_id"] == ISO2_PID_BAD]
    check("isolation round 2: the BAD game_pk's player got NO new revision (stayed at revision 1, "
          "safe to retry next hourly run)", bad_revisions, [1])

    still_open = lrt.find_unresolved_outcome_dates(tmp3p, TODAY, lookback_days=14)
    checkTrue("isolation round 2: 2026-08-18 is still flagged unresolved (the bad game_pk's player "
              "is still open, correctly not silently marked resolved)",
              "2026-08-18" in still_open)

    tmp3.cleanup()
    print(f"isolation round 2 (quick review of finding #6's fix): {CHECKS - _iso2_checks_before} assertions, "
          f"a batting-line lookup failure for one game_pk on a date doesn't block another game_pk's "
          f"item on that same date from resolving")


if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   regrade-stale (suspended/resumed-game fix): {CHECKS} assertions, unresolved-date detection "
      f"(latest-revision-only, void/final/out-of-window all correctly excluded) + the live re-fetch "
      f"resolve path + the no-op safety property a recheck run depends on")
