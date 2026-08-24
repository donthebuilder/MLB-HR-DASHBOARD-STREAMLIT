"""A study that would rather say "not yet" than quote a leaked number.

WHAT THIS PINS (2026-08-23)
===========================
The due-score question — is "he is due" real, or the gambler's fallacy wearing
a lab coat — was answerable off the graded archive right up until the first
table came back saying hitters with no home run in their last five games
homered 0 times in 217 rows. That is the archive remembering tonight, not
baseball.

hr_due_score is built from last5_hr, so every night carrying the score also
carries the leak, and the whole overlap is unusable. What matters now is that
the study REFUSES rather than reports.

Three things are asserted.

1. IT WILL NOT QUOTE A NUMBER OFF CONTAMINATED ROWS. Given a fat archive with
   no feature_snapshot, the report must reach "NOT YET" and must not print a
   due-score rate table.

2. IT COUNTS ONLY LOCKED ROWS AS USABLE. feature_snapshot="locked" is the stamp
   that the pre-game snapshot was overlaid; anything else is a post-game value
   standing in for a pre-game one.

3. IT RUNS ONCE THERE IS ENOUGH CLEAN DATA. Given enough locked rows the gate
   opens and the table appears — otherwise the refusal is unfalsifiable and the
   study would never run again no matter how many nights landed.

The fourth is the Wilson interval: on buckets this small the normal
approximation produces intervals running past 100%, and a confidence interval
that claims 104% discredits the number beside it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))

from bots.due_study import (  # noqa: E402
    MIN_CLEAN_ROWS, clean_rows, report, wilson,
)


def write_night(dirpath: Path, date: str, rows):
    (dirpath / f"graded_results_{date}.json").write_text(
        __import__("json").dumps({"date": date, "results": rows}), encoding="utf-8")


def row(hr, due, score, locked, last5=1):
    r = {"actual_hr": 1 if hr else 0, "hr_due_score": due, "hr_score": score,
         "last5_hr": last5, "player_id": 1, "game_pk": 1}
    if locked:
        r["feature_snapshot"] = "locked"
    return r


def t_refuses_on_contaminated_rows(tmp: Path):
    """A big archive with the leak in it must reach NOT YET, not a table."""
    d = tmp / "dirty"
    d.mkdir()
    rows = []
    for i in range(600):                     # last5_hr==0 never homers: the leak
        rows.append(row(False, 5.0, 50.0, locked=False, last5=0))
    for i in range(1400):
        rows.append(row(i % 3 == 0, 0.0, 60.0, locked=False, last5=2))
    write_night(d, "2026-08-01", rows)
    out = report(str(d))
    assert "VERDICT: NOT YET" in out, out[-800:]
    assert "WHY MOST OF THE ARCHIVE IS UNUSABLE" in out
    assert "last5_hr" in out
    # and it must NOT have printed the study's own headline table
    assert "on pace or ahead (0)" not in out, "quoted a number off leaked rows"


def t_only_locked_rows_count(tmp: Path):
    rows = [row(False, 1.0, 50.0, locked=True), row(True, 0.0, 60.0, locked=False),
            row(False, 2.0, 55.0, locked=True)]
    assert len(clean_rows(rows)) == 2
    assert all(r.get("feature_snapshot") == "locked" for r in clean_rows(rows))


def t_the_gate_opens_with_enough_clean_data(tmp: Path):
    """Otherwise the refusal is unfalsifiable and the study never runs again."""
    d = tmp / "clean"
    d.mkdir()
    rows = []
    for i in range(MIN_CLEAN_ROWS + 60):
        # a real spread on both axes so the buckets are not degenerate
        rows.append(row(i % 5 == 0, float(i % 17), 30.0 + (i % 50), locked=True,
                        last5=i % 3))
    write_night(d, "2026-08-22", rows)
    out = report(str(d))
    assert "VERDICT: NOT YET" not in out, out[-900:]
    assert "on pace or ahead (0)" in out, out[-900:]
    assert "Within hr_score terciles" in out


def t_wilson_never_runs_past_the_ends():
    """The reason it is Wilson and not the normal approximation."""
    for k, n in ((0, 5), (5, 5), (1, 3), (8, 584), (177, 905)):
        lo, hi = wilson(k, n)
        assert 0.0 <= lo <= hi <= 1.0, (k, n, lo, hi)
    lo, hi = wilson(0, 217)
    assert lo == 0.0 and hi < 0.02, (lo, hi)   # the shape of the leaked bucket


def t_no_archive_is_a_refusal_not_a_zero(tmp: Path):
    d = tmp / "empty"
    d.mkdir()
    out = report(str(d))
    assert "Nothing to say" in out, out
    assert "0.0%" not in out


TESTS = [
    ("refuses on contaminated rows", t_refuses_on_contaminated_rows),
    ("only locked rows count", t_only_locked_rows_count),
    ("the gate opens with enough clean data", t_the_gate_opens_with_enough_clean_data),
    ("wilson never runs past the ends", t_wilson_never_runs_past_the_ends),
    ("no archive is a refusal not a zero", t_no_archive_is_a_refusal_not_a_zero),
]

if __name__ == "__main__":
    import tempfile
    failed = []
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for name, fn in TESTS:
            try:
                fn(base) if fn.__code__.co_argcount else fn()
            except AssertionError as e:
                failed.append(f"{name}: {e}")
            except Exception as e:                  # noqa: BLE001
                failed.append(f"{name}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{len(failed)} FAILED\n" + "\n".join(f"  · {f}" for f in failed))
        sys.exit(1)
    print(f"ok   due study: {len(TESTS)} assertions — a contaminated archive reaches "
          f"NOT YET and prints no rate table, only feature_snapshot=locked rows "
          f"count as usable, the gate does open once enough clean rows exist, the "
          f"Wilson interval stays inside 0-100% on every bucket including 0 of "
          f"217, and an empty archive refuses instead of printing 0.0%")
