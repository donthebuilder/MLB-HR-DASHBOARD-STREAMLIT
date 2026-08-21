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
    actual_by_pid = {
        e["player_id"]: {"hits": 0, "hr": e.get("hr", 0), "runs": 0, "rbi": 0, "tb": 0,
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
        {555: {"hits": 0, "hr": 0, "runs": 0, "rbi": 0, "tb": 0, "ab": 0, "bb": 0, "k": 0}},
        {900005: {"detailed_state": "Suspended", "abstract_state": "Suspended"}}, "2026-08-14T10:00:00Z")
    lines_r1, _ = lrt.append_outcome_log(cands_r1, None)
    p1.write_text("\n".join(lines_r1) + "\n", encoding="utf-8")
    cands_r2 = lrt.build_outcome_candidates(
        "2026-08-16", [{"player_id": 555, "game_pk": 900005}],
        {555: {"hits": 2, "hr": 1, "runs": 1, "rbi": 2, "tb": 5, "ab": 4, "bb": 0, "k": 1}},
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


if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   regrade-stale (suspended/resumed-game fix): {CHECKS} assertions, unresolved-date detection "
      f"(latest-revision-only, void/final/out-of-window all correctly excluded) + the live re-fetch "
      f"resolve path + the no-op safety property a recheck run depends on")
