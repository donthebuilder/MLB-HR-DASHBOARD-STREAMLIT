"""nfl_features._roll() -- "Trailing mean over the previous FORM_W weeks.
Never includes week w" (that function's own docstring). Flagged urgent in
the 2026-08-24 NFL-vs-MLB parity audit: this invariant had no regression
test at all before this file, only the docstring's own claim.

A real regression test, not an import smoke test: plants a known,
deliberately extreme value at week w and asserts week w's OWN trailing
feature does not reflect it -- then, as a sanity check on the test itself
(so a bug that just always returns the baseline value can't pass by
accident), asserts the SAME value legitimately DOES flow into a LATER
week's trailing window once it is genuinely history.

Run: python3 -m pytest tests/test_nfl_leak_safety.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import polars as pl  # noqa: E402
import nfl_features as nf  # noqa: E402

NORMAL_VALUE = 10.0
LEAK_VALUE = 999_999.0  # deliberately absurd -- if this ever leaks into its
                          # own week's average, the test fails loudly, not
                          # by a couple of decimal places.


def _synthetic_df(player_id="P1", weeks=None, leak_week=None):
    """One player, `stat` = NORMAL_VALUE every week except `leak_week`,
    where it's LEAK_VALUE."""
    rows = []
    for w in weeks:
        val = LEAK_VALUE if w == leak_week else NORMAL_VALUE
        rows.append((player_id, w, val))
    return pl.DataFrame(rows, schema=["player_id", "week", "stat"], orient="row")


def test_roll_never_includes_the_current_week_own_value():
    """The exact invariant the docstring claims: week w's own planted
    value must not appear in week w's own f_stat."""
    form_w = nf.FORM_W
    leak_week = form_w + 1  # first week with a full, real trailing window
    weeks = list(range(1, leak_week + 2))  # ... plus one week after it
    df = _synthetic_df(weeks=weeks, leak_week=leak_week)

    result = nf._roll(df, ["stat"])

    row = result.filter((pl.col("week") == leak_week) & (pl.col("player_id") == "P1"))
    assert row.height == 1, f"expected exactly one row for week {leak_week}"
    assert row["f_stat"][0] == NORMAL_VALUE, (
        f"week {leak_week}'s own planted LEAK_VALUE leaked into its own f_stat: "
        f"got {row['f_stat'][0]!r}, expected {NORMAL_VALUE!r}"
    )


def test_roll_correctly_includes_the_value_once_it_is_real_history():
    """Sanity check on the test above: prove _roll() isn't just always
    returning NORMAL_VALUE regardless of window contents (which would make
    the first test pass for the wrong reason). Once `leak_week` itself
    becomes a PAST week relative to a later week, its value must legitimately
    enter that later week's trailing average."""
    form_w = nf.FORM_W
    leak_week = form_w + 1
    next_week = leak_week + 1
    weeks = list(range(1, next_week + 1))
    df = _synthetic_df(weeks=weeks, leak_week=leak_week)

    result = nf._roll(df, ["stat"])

    row = result.filter((pl.col("week") == next_week) & (pl.col("player_id") == "P1"))
    assert row.height == 1, f"expected exactly one row for week {next_week}"
    # window for next_week is [next_week - FORM_W, next_week - 1], which
    # includes leak_week -- so the average must be pulled well above
    # NORMAL_VALUE by the huge planted value now sitting inside it.
    assert row["f_stat"][0] > NORMAL_VALUE, (
        "the planted value never flowed forward into a later week's trailing "
        "window at all -- _roll() may be broken in a way the first test can't "
        "distinguish from correct (e.g. always returning a constant)"
    )
    # And it must equal the exact arithmetic mean of the window contents --
    # a real number, not just "bigger than baseline".
    expected = ((form_w - 1) * NORMAL_VALUE + LEAK_VALUE) / form_w
    assert abs(row["f_stat"][0] - expected) < 1e-6


def test_roll_window_is_exactly_form_w_weeks_wide():
    """The window is [w - FORM_W, w - 1] -- exactly FORM_W weeks, neither
    off-by-one direction. A week planted one slot OUTSIDE that window (at
    w - FORM_W - 1) must not affect the average."""
    form_w = nf.FORM_W
    w = form_w + 5  # comfortably past the ramp-up so the full window exists
    weeks = list(range(1, w + 1))
    just_outside = w - form_w - 1
    assert just_outside >= 1, "test setup assumption violated: need room before the window"

    rows = [("P1", wk, LEAK_VALUE if wk == just_outside else NORMAL_VALUE) for wk in weeks]
    df = pl.DataFrame(rows, schema=["player_id", "week", "stat"], orient="row")

    result = nf._roll(df, ["stat"])
    row = result.filter((pl.col("week") == w) & (pl.col("player_id") == "P1"))
    assert row.height == 1
    assert row["f_stat"][0] == NORMAL_VALUE, (
        f"a value planted at week {just_outside} (one week OUTSIDE the "
        f"FORM_W={form_w}-week window for week {w}) leaked into f_stat"
    )


def test_roll_respects_the_player_id_grouping():
    """Two players in the same weeks; one player's planted leak value must
    never bleed into the other player's average."""
    form_w = nf.FORM_W
    leak_week = form_w + 1
    weeks = list(range(1, leak_week + 1))
    rows = []
    for wk in weeks:
        rows.append(("P1", wk, LEAK_VALUE if wk == leak_week else NORMAL_VALUE))
        rows.append(("P2", wk, NORMAL_VALUE))
    df = pl.DataFrame(rows, schema=["player_id", "week", "stat"], orient="row")

    result = nf._roll(df, ["stat"])
    p2_row = result.filter((pl.col("week") == leak_week) & (pl.col("player_id") == "P2"))
    assert p2_row.height == 1
    assert p2_row["f_stat"][0] == NORMAL_VALUE, "P1's planted leak value bled into P2's average"
