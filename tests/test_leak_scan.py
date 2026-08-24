"""A column that remembers tonight is not a column that predicts it.

WHAT THIS PINS (2026-08-23)
===========================
The due-score study produced a table saying hitters with no home run in their
last five games homered 0 times in 217 rows. That is not a cold streak, it is a
tautology: last5_hr counts his last five GAMES, the archived value was written
after tonight finished, so a man who homered tonight has last5_hr >= 1 by
construction and the zero bucket excludes him automatically.

bots/leak_scan.py is the general form of that check. Four things about it are
easy to get wrong in a way that produces a confident, wrong answer.

1. IT MUST SURVIVE A BIG SAMPLE. The first version used math.comb and died with
   OverflowError on a 584-row bucket — a detector that only works on small
   samples is a detector that never sees the leak worth finding. The tail is
   summed in log space and the test runs it at n=5000.

2. IT MUST NOT FLAG THE OUTCOME COLUMNS. got_hr, tb_2_plus and friends sit in
   the same file and correlate with actual_hr perfectly, because they ARE the
   result. They are excluded by name.

3. IT MUST NOT FLAG AN ORDINARY PREDICTIVE FIELD. A real signal makes its zero
   bucket worse than base, not impossible. The test feeds one that halves the
   rate and asserts silence.

4. "NOTHING TESTED" IS NOT "NOTHING FOUND". A subset too small to test anything
   must say so, because a scan that never ran gets quoted as a pass.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))

from bots.leak_scan import (  # noqa: E402
    LOG_P_ALARM, MIN_BUCKET, MIN_ROWS, OUTCOME_FIELDS, STRONG_SIGNAL_FLOOR,
    log10_binom_tail_le, scan, smallest_detectable,
)


def rows_with(field, zero_n, zero_hits, rest_n, rest_hits, extra=None):
    """A synthetic archive: `zero_n` rows where `field` is 0 (of which
    `zero_hits` homered) and `rest_n` rows where it is 1."""
    out = []
    for i in range(zero_n):
        r = {field: 0, "actual_hr": 1 if i < zero_hits else 0}
        if extra:
            r.update(extra(i, True))
        out.append(r)
    for i in range(rest_n):
        r = {field: 1, "actual_hr": 1 if i < rest_hits else 0}
        if extra:
            r.update(extra(i, False))
        out.append(r)
    return out


def t_log_tail_matches_exact_on_small_n():
    """Log space must agree with the direct computation where both work."""
    for n, p in ((20, 0.2), (30, 0.05), (12, 0.5)):
        for k in range(0, 6):
            exact = sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))
            got = 10 ** log10_binom_tail_le(k, n, p)
            assert abs(got - exact) < 1e-9, f"n={n} k={k} p={p}: {got} vs {exact}"


def t_log_tail_survives_a_big_sample():
    """The bug that shipped in the first draft: OverflowError at n in the
    hundreds. Nothing here may raise, and the answer must stay a real number."""
    v = log10_binom_tail_le(8, 5000, 0.18)
    assert math.isfinite(v), v
    assert v < -100, v


def t_finds_a_planted_leak():
    """584 rows, 8 homers, against an 18% base — the real last5_hr numbers."""
    rows = rows_with("last5_hr", 584, 8, 1555, 377)
    res = scan(rows)
    fields = {s["field"] for s in res["suspects"]}
    assert "last5_hr" in fields, res["suspects"]
    s = next(x for x in res["suspects"] if x["field"] == "last5_hr")
    assert s["log10p"] < LOG_P_ALARM, s
    assert s["expected"] > 90, s          # ~105 expected against 8 observed


def t_ignores_the_outcome_columns():
    """got_hr is 0 on every non-homer by definition. Flagging it would bury the
    real finding under a list of columns doing their job."""
    assert "got_hr" in OUTCOME_FIELDS
    rows = rows_with("got_hr", 900, 0, 300, 300)
    res = scan(rows)
    assert not res["suspects"], res["suspects"]


def t_leaves_an_honest_signal_alone():
    """A genuinely predictive field halves its zero bucket.

    This is the false positive that mattered. Tested against the raw base rate
    it came out at log10 p = -9.9 and was reported as a leak — a discovery
    filed as a defect. A leak is not "far below base", it is "impossible", so
    the leak tier is judged against a floor a very strong honest signal could
    reach. Halving the rate must land in WATCH and never in LEAK.
    """
    rows = rows_with("cold_bat", 600, 54, 1500, 330)      # 9% against a ~18% base
    res = scan(rows)
    assert not res["suspects"], res["suspects"]
    assert {w["field"] for w in res["watch"]} == {"cold_bat"}, res["watch"]


def t_a_leak_outranks_a_strong_signal():
    """Both are far below base; only one is impossible. Same bucket size, same
    board, so the tiers can only differ on the rate itself."""
    leak = scan(rows_with("leaked", 600, 8, 1500, 330))
    signal = scan(rows_with("strong", 600, 54, 1500, 330))
    assert {s["field"] for s in leak["suspects"]} == {"leaked"}, leak
    assert not signal["suspects"], signal["suspects"]


def t_nothing_tested_is_not_nothing_found():
    """Too few rows to test must report zero TESTED, so the report can say so
    rather than printing the same line a clean scan prints."""
    rows = rows_with("thin", 30, 0, 60, 12)
    res = scan(rows)
    assert res["tested"] == 0, res
    assert not res["suspects"], res
    assert len(rows) < MIN_ROWS


def t_smallest_detectable_is_honest():
    """The floor a clean result must be read against.

    A SMALL sample can only catch an EXTREME leak, so its detectable rate sits
    far below base; a big sample catches subtler ones, so its floor rises
    toward base. The first version of this test asserted that backwards, which
    is worth keeping a note of: the quantity is "the worst leak that could
    still have hidden", and more data makes that number LARGER, not smaller.
    """
    small = smallest_detectable(173, 0.139)
    big = smallest_detectable(2000, 0.18)
    assert small is not None and big is not None
    assert small < big, (small, big)
    assert 0.0 < small < 0.139, small
    assert big < 0.18, big


def t_bucket_floor_is_respected():
    """A field with a big row count but a tiny zero bucket must not be tested —
    three of three homering proves nothing."""
    rows = rows_with("rare_zero", MIN_BUCKET - 1, 0, 400, 72)
    res = scan(rows)
    assert not res["suspects"], res["suspects"]


TESTS = [
    ("log tail matches exact on small n", t_log_tail_matches_exact_on_small_n),
    ("log tail survives a big sample", t_log_tail_survives_a_big_sample),
    ("finds a planted leak", t_finds_a_planted_leak),
    ("ignores the outcome columns", t_ignores_the_outcome_columns),
    ("leaves an honest signal alone", t_leaves_an_honest_signal_alone),
    ("a leak outranks a strong signal", t_a_leak_outranks_a_strong_signal),
    ("nothing tested is not nothing found", t_nothing_tested_is_not_nothing_found),
    ("smallest detectable is honest", t_smallest_detectable_is_honest),
    ("bucket floor is respected", t_bucket_floor_is_respected),
]

if __name__ == "__main__":
    failed = []
    for name, fn in TESTS:
        try:
            fn()
        except AssertionError as e:
            failed.append(f"{name}: {e}")
        except Exception as e:                      # noqa: BLE001
            failed.append(f"{name}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{len(failed)} FAILED\n" + "\n".join(f"  · {f}" for f in failed))
        sys.exit(1)
    print(f"ok   leak scan: {len(TESTS)} assertions — the binomial tail is exact on "
          f"small n and finite at n=5000, a planted last5_hr leak is found, the "
          f"outcome columns are not flagged, an honestly predictive field is left "
          f"alone in a WATCH tier, a subset too small reports nothing TESTED "
          f"rather than nothing found, and the detectable floor rises with the "
          f"sample rather than falling")
