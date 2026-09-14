#!/usr/bin/env python3
"""prediction_of_record is the last run written BEFORE first pitch (2026-09-14).

Measured before this existed: the record was a post-first-pitch run for ~95%
of games since 08-21, and on 09-13 ten of thirty homer hitters' "locked"
hr_score already contained the homer. These checks pin the selector:

  · the LATEST pregame log wins, not the first and not the one on the rows
  · a log generated after first pitch is never chosen, however recent
  · a log that does not carry the game is skipped
  · only this slate date's logs are read
  · with no pregame log at all, None -- so main() falls back to the old
    rows-standing path and flags it, rather than inventing a record

Runnable as a script or under pytest. No network.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bots"))
import pick_lock as pl  # noqa: E402

FAILS: list[str] = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r}, want {want!r}")
        if "pytest" in sys.modules:
            raise AssertionError(FAILS[-1])


def write_log(d: Path, run_id: str, generated_at: str, games: list[str], slate="2026-09-13"):
    lines = [json.dumps({"run_id": run_id, "generated_at": generated_at, "slate_date": slate,
                         "model_versions": {"hr": "mlb_hr_v4"}, "config_hashes": {"hr": "sha256:X"}})]
    for gp in games:
        lines.append(json.dumps({"prediction_date": slate, "player_id": 1, "game_pk": int(gp), "run_id": run_id, "scores": {"hr": 1}}))
    (d / f"prediction_log_{run_id}.jsonl").write_text("\n".join(lines) + "\n")


def test_selector():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        fp = dt.datetime(2026, 9, 13, 17, 35, tzinfo=dt.timezone.utc)
        write_log(d, "2026-09-13.124120Z.gha-1", "2026-09-13T12:41:20+00:00", ["1001", "1002"])   # early pregame
        write_log(d, "2026-09-13.153037Z.gha-2", "2026-09-13T15:30:37+00:00", ["1001", "1002"])   # LAST pregame
        write_log(d, "2026-09-13.182956Z.gha-3", "2026-09-13T18:29:56+00:00", ["1001", "1002"])   # after the pitch
        write_log(d, "2026-09-13.160000Z.gha-4", "2026-09-13T16:00:00+00:00", ["1002"])           # pregame, other game only
        write_log(d, "2026-09-12.230000Z.gha-5", "2026-09-12T23:00:00+00:00", ["1001"], slate="2026-09-12")  # yesterday's slate
        pl._PL_HEADER_CACHE.clear()

        got = pl.pregame_run_for_game("1001", fp, d, "2026-09-13")
        check("the LAST pregame run is the record", got and got["run_id"], "2026-09-13.153037Z.gha-2")
        check("generated_at comes from the header, verbatim", got and got["generated_at"], "2026-09-13T15:30:37+00:00")
        check("model_version read from the header", got and got["model_version"], "mlb_hr_v4")
        check("config_hash read from the header", got and got["config_hash"], "sha256:X")

        got2 = pl.pregame_run_for_game("1002", fp, d, "2026-09-13")
        check("a later pregame run that carries the game beats an earlier one", got2 and got2["run_id"], "2026-09-13.160000Z.gha-4")

        early = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.timezone.utc)
        check("no pregame log before first pitch -> None (fallback path)", pl.pregame_run_for_game("1001", early, d, "2026-09-13"), None)
        check("a game no log carries -> None", pl.pregame_run_for_game("9999", fp, d, "2026-09-13"), None)

        pl._PL_HEADER_CACHE.clear()
        idx = pl._prediction_log_index(d, "2026-09-13")
        check("only this slate date's logs are indexed", len(idx), 4)


def main() -> int:
    test_selector()
    if FAILS:
        print("\n".join("  RED  " + f for f in FAILS))
        print(f"\n{len(FAILS)} RED")
        return 1
    print("ok   pick_lock pregame record: the latest run written before first pitch is the record; "
          "post-pitch runs, other games and other slate dates are never chosen; no pregame log -> None")
    return 0


if __name__ == "__main__":
    sys.exit(main())
