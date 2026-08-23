"""Meatball% is not one number, and the model now knows it.

WHAT THIS PINS (2026-08-23)
===========================
Donovan: "meat ball percent needs to be used in hr for sure hand splits and
everything. wtf ."

He was right to be annoyed. `pitcher_meatball_pct` — the share of pitches an
arm leaves middle-middle — has been published on every slate row for months,
and the only thing that read it was a 0.12 slice of the pitcher_damage
sub-score, as ONE number, identical against a lefty and against a righty.

An arm with a slider he can bury against same-side bats and nothing but a
straight fastball to the other side does not leave the heart of the plate open
equally to both. Averaging the two hides exactly the matchup the whole site
exists to find.

THREE THINGS ARE ASSERTED HERE
------------------------------
1. meatball_vs_hand() resolves the side correctly, INCLUDING switch hitters.
   A switch hitter bats opposite the arm, so he takes the platoon side every
   time; comparing `bats` directly against "L"/"R" is what dropped ~11% of a
   slate out of every side-specific term for a season (see effective_side).

2. It refuses to invent a split it does not have. Under the 150-pitch floor,
   with no Statcast pull, or with a bat whose side cannot be resolved, it
   returns the arm's OVERALL rate and says so — a fabricated 10% meatball rate
   built on 1-of-10 pitches has the shape of a finding and the content of noise.

3. The GAP is stricter than the rate. One real side is enough to use that
   side's number; it is NOT enough to publish "he gives 3pp more to lefties",
   because the other half of that subtraction would be a fallback value.

WHAT IS DELIBERATELY NOT TESTED
-------------------------------
That the change improves the HR board. It has not been measured and it does not
claim to be: meatball_fit_score, the new term this work produced, is worth ZERO
points in hr_raw and ships as an archived column so the question can be
answered off real graded nights in a few weeks. See the SLOT_FIELDS note in
live_results_tracker.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bots.mlb_dashboard import effective_side, meatball_vs_hand  # noqa: E402


class Row:
    """The four fields the resolver reads. Nothing else is needed, which is
    itself the point — this is a pure function of the row."""

    def __init__(self, bats, throws, lhb=0.094, rhb=0.061, overall=0.070, status="ok"):
        self.bats = bats
        self.pitcher_throws = throws
        self.pitcher_meatball_pct = overall
        self.pitcher_meatball_pct_vs_lhb = lhb
        self.pitcher_meatball_pct_vs_rhb = rhb
        self.pitcher_meatball_side_status = status


def test_lefty_gets_the_lefty_rate():
    rate, other, both = meatball_vs_hand(Row("L", "R"))
    assert rate == 0.094, rate
    assert other == 0.061, other
    assert both is True


def test_righty_gets_the_righty_rate():
    rate, other, both = meatball_vs_hand(Row("R", "R"))
    assert rate == 0.061, rate
    assert other == 0.094, other


def test_switch_hitter_takes_the_platoon_side():
    """The bug this repo already paid for once. A switch hitter bats opposite
    the arm, so against a righty he is a LEFTY and takes the LHB rate."""
    assert effective_side("S", "R") == "L"
    assert meatball_vs_hand(Row("S", "R"))[0] == 0.094
    assert effective_side("S", "L") == "R"
    assert meatball_vs_hand(Row("S", "L"))[0] == 0.061


def test_no_split_falls_back_to_overall_and_says_so():
    for status in ("missing", "low_sample"):
        rate, other, both = meatball_vs_hand(Row("L", "R", status=status))
        assert rate == 0.070, (status, rate)
        assert other == 0.070, (status, other)
        assert both is False, status


def test_one_real_side_is_usable_but_ungappable():
    """The lefty rate cleared the floor; the righty rate did not.

    A lefty may use his side's number. Nobody may subtract the two — the
    righty value in the row is the arm's overall rate wearing a split's
    clothes, and a gap computed against it would be pure artifact.
    """
    rate, other, both = meatball_vs_hand(Row("L", "R", status="one_side:L"))
    assert rate == 0.094, rate
    assert other == rate, "the gap must collapse to zero, not to a fallback"
    assert both is False

    # The righty facing the same arm gets nothing extra at all.
    rate, other, both = meatball_vs_hand(Row("R", "R", status="one_side:L"))
    assert rate == 0.070, rate
    assert both is False


def test_unresolvable_bat_side_is_neutral():
    """An empty or junk `bats` must not silently pick a side."""
    for bats in ("", None, "?"):
        rate, other, both = meatball_vs_hand(Row(bats, "R"))
        assert rate == 0.070, (bats, rate)
        assert both is False


def test_the_blend_actually_reads_the_hand_version():
    """A source guard, and it is the load-bearing assertion in this file.

    Every test above can pass while the model still feeds the OVERALL rate into
    pitcher_damage — which was the state of the world this morning. This reads
    the scorer and checks the 0.12 slice takes meatball_hand.
    """
    src = (Path(__file__).resolve().parents[1] / "bots" / "mlb_dashboard.py").read_text(encoding="utf-8")
    assert "0.12 * minmax_norm(meatball_hand, 0.040, 0.105)" in src, \
        "the pitcher_damage meatball slice is not reading the hand-split rate"
    assert "0.12 * minmax_norm(meatball, 0.040, 0.105)" not in src, \
        "the old overall-rate slice is still in the blend"
    # And the flag that reads the same signal per hitter.
    assert "meatball_hand >= 0.080" in src, "mistake_pitch_setup_flag still on the overall rate"


def test_the_new_column_is_worth_zero_points():
    """The standing rule: no hr_blend weight moves before 9c (~2026-09-22),
    because the graded archive still manufactures tuning signals out of its own
    leak. meatball_fit_score is therefore a COLUMN, not a term."""
    src = (Path(__file__).resolve().parents[1] / "bots" / "mlb_dashboard.py").read_text(encoding="utf-8")
    assert "h.meatball_fit_score = round(" in src, "the column is not being computed"
    for forbidden in ("* meatball_fit_score", "meatball_fit_score *", "+ meatball_fit_score",
                      '"meatball_fit"'):
        assert forbidden not in src, f"meatball_fit_score has leaked into the blend ({forbidden})"
    # hr_blend must be untouched — same keys, still summing to 1.00.
    from bots.mlb_dashboard import MODEL_WEIGHTS  # noqa: PLC0415
    assert abs(sum(MODEL_WEIGHTS["hr_blend"].values()) - 1.0) < 1e-6
    assert "meatball" not in " ".join(MODEL_WEIGHTS["hr_blend"].keys())


def test_the_column_survives_the_archive():
    """Archived UNSCORED or it is unmeasurable, and an unmeasurable column can
    never earn its way in. trim_row() drops anything not on this whitelist —
    the exact way longest_hr_score went 5,766 graded rows without ever being
    written down."""
    src = (Path(__file__).resolve().parents[1] / "bots" / "live_results_tracker.py").read_text(encoding="utf-8")
    for field in ("meatball_pct_vs_hand", "meatball_edge_pp", "meatball_fit_score",
                  "meatball_fit_status", "pitcher_meatball_side_status"):
        assert f'"{field}"' in src, f"{field} would be dropped by trim_row()"


def test_the_model_version_was_bumped():
    """The registry's rule is about NUMBERS, not weights: bump if the change
    could alter the numeric output for any historical input. Feeding a
    different rate into an unchanged weight does exactly that."""
    from bots.model_registry import MODEL_VERSIONS  # noqa: PLC0415
    assert MODEL_VERSIONS["hr"] == "mlb_hr_v4", MODEL_VERSIONS["hr"]


if __name__ == "__main__":
    failed, checks = [], 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            checks += 1
        except AssertionError as e:
            failed.append(f"{name}: {e}")
        except Exception as e:                      # noqa: BLE001
            failed.append(f"{name}: {type(e).__name__}: {e}")

    if failed:
        print(f"\n{len(failed)} FAILED\n" + "\n".join(f"  · {f}" for f in failed))
        sys.exit(1)
    print(f"ok   meatball hand split: {checks} assertions — the side resolves "
          f"(switch hitters included), a thin sample falls back to the overall "
          f"rate instead of inventing one, the gap needs BOTH sides real, the "
          f"blend reads the hand version, and the new fit column is worth zero "
          f"points and lands in the graded archive")
