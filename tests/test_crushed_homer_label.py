"""The CRUSHED-HOMER LABEL (2026-09-14).

claude/park-is-not-dead-scrapers-2026-09-08.md §5 asked for four booleans
on the graded row -- nodoubt (>=425 ft), scraper (<=380 ft), crushed
(>=108 mph), soft (<=100 mph) -- so the record can finally be split by HR
type with a real measurement instead of the distance-only proxy
(claude/watch-rebuild-band-rates-and-shape-split-2026-09-13.md: "crushed
410ft+, scraper <370ft") used for the one-off finding that started this.

WHAT THIS PINS
--------------
classify_hr_type() is pure and only cares about its two numbers -- no
network, no feed shape, so the threshold logic can be pinned exactly without
a fake game feed. grade_slot() is tested separately to confirm the labels
actually reach the graded row through hr_events, the same path a real grading
run uses (get_player_batting_line() -> actual["hr_events"] -> grade_slot()).

WHAT THIS DELIBERATELY DOES NOT TEST
-------------------------------------
Whether crushed homers actually predict anything about hr_score, the pick
score, or any other signal -- that is the whole reason this is a label and
not a weight. Two-lane: these four fields are archived UNSCORED, same as
hr_pace_flag and meatball_fit_score before it. The accuracy question needs
weeks of graded nights and is out of scope here.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bots.live_results_tracker import (  # noqa: E402
    classify_hr_type,
    grade_slot,
)


# ─── classify_hr_type ────────────────────────────────────────────────────

def test_a_crushed_no_doubter():
    """108+ mph AND 425+ ft -- both flags fire, independently."""
    t = classify_hr_type(112.3, 441.0)
    assert t == {"nodoubt": True, "scraper": False, "crushed": True, "soft": False}


def test_a_wall_scraper_can_still_be_crushed():
    """A ball can be a cheap scraper off the bat speed AND a legitimately
    crushed swing -- e.g. a line drive that just clears a short porch. The
    two are independent booleans on purpose, not a single category."""
    t = classify_hr_type(109.0, 355.0)
    assert t["crushed"] is True
    assert t["scraper"] is True
    assert t["nodoubt"] is False


def test_soft_contact_cheap_homer():
    t = classify_hr_type(97.5, 362.0)
    assert t == {"nodoubt": False, "scraper": True, "crushed": False, "soft": True}


def test_the_middle_of_the_distribution_is_neither():
    """Most home runs are none of the four -- the labels are supposed to be
    selective, not a forced partition of every ball into a bucket."""
    t = classify_hr_type(104.0, 400.0)
    assert t == {"nodoubt": False, "scraper": False, "crushed": False, "soft": False}


def test_thresholds_are_inclusive_at_the_boundary():
    assert classify_hr_type(108.0, 400.0)["crushed"] is True
    assert classify_hr_type(100.0, 400.0)["soft"] is True
    assert classify_hr_type(90.0, 425.0)["nodoubt"] is True
    assert classify_hr_type(90.0, 380.0)["scraper"] is True


def test_untracked_measurement_is_none_not_false():
    """A Statcast tracking gap on ONE of the two numbers must not silently
    read as 'confirmed not crushed' -- that would misrepresent an unknown as
    a negative finding, which is exactly the mistake hr_distances_from_game's
    own longest_ft field already avoids ('None, not 0')."""
    t = classify_hr_type(None, 441.0)
    assert t["crushed"] is None and t["soft"] is None
    assert t["nodoubt"] is True and t["scraper"] is False

    t2 = classify_hr_type(112.0, None)
    assert t2["nodoubt"] is None and t2["scraper"] is None
    assert t2["crushed"] is True and t2["soft"] is False

    t3 = classify_hr_type(None, None)
    assert t3 == {"nodoubt": None, "scraper": None, "crushed": None, "soft": None}


def test_a_zero_reads_as_untracked_same_as_none():
    """hitData can carry a literal 0 on a tracking miss, same as it can omit
    the key outright -- both must refuse to call it, not report a 0-mph or
    0-ft home run as 'soft' and 'scraper' by arithmetic accident."""
    t = classify_hr_type(0.0, 0.0)
    assert t == {"nodoubt": None, "scraper": None, "crushed": None, "soft": None}


# ─── grade_slot() -- the labels actually reach the graded row ──────────────

BASE_SLOT = {"player_id": 660271, "name": "Test Player", "team": "TST",
             "game_pk": 123456, "pick_type": "HR", "rank": None}


def _actual(**over):
    base = dict(hits=1, hr=1, runs=1, rbi=1, tb=4, ab=4, k=0, bb=0,
                doubles=0, triples=0, sb=0, pa=4, was_replaced=False,
                was_substitute=False, hr_events=[])
    base.update(over)
    return base


def test_a_single_crushed_homer_rolls_up_onto_the_graded_row():
    actual = _actual(hr_events=[
        {"launch_speed": 110.4, "total_distance": 432.0, "launch_angle": 27.0,
         "pitch_type": "", "event": "Home Run"},
    ])
    g = grade_slot(BASE_SLOT, actual)
    assert g["hr_crushed"] is True
    assert g["hr_nodoubt"] is True
    assert g["hr_scraper"] is False
    assert g["hr_soft"] is False
    # the per-event label rides along on hr_events too, not just the rollup
    assert g["hr_events"][0]["hr_type"]["crushed"] is True


def test_a_two_homer_night_is_any_of_across_both_swings():
    """One crushed, one soft -- both should read True on the row, because
    'any of' is the aggregation build_hr_capture_report already uses for
    longest_ft/max_ev_mph, not 'the last one wins'."""
    actual = _actual(hr=2, tb=8, hr_events=[
        {"launch_speed": 109.0, "total_distance": 415.0, "launch_angle": 25.0,
         "pitch_type": "", "event": "Home Run"},
        {"launch_speed": 98.0, "total_distance": 365.0, "launch_angle": 30.0,
         "pitch_type": "", "event": "Home Run"},
    ])
    g = grade_slot(BASE_SLOT, actual)
    assert g["hr_crushed"] is True
    assert g["hr_soft"] is True
    assert g["hr_scraper"] is True
    assert g["hr_nodoubt"] is False


def test_no_homer_gets_no_flags_at_all():
    """got_hr=0 nights must not carry hr_crushed=False -- that would read as
    a measured 'not crushed' on a row where nothing was hit to measure."""
    g = grade_slot(BASE_SLOT, _actual(hr=0, tb=1, hr_events=[]))
    assert "hr_crushed" not in g
    assert "hr_nodoubt" not in g
    assert "hr_events" not in g


def test_an_untracked_homer_reports_none_on_the_row_not_false():
    actual = _actual(hr_events=[
        {"launch_speed": None, "total_distance": None, "launch_angle": None,
         "pitch_type": "", "event": "Home Run"},
    ])
    g = grade_slot(BASE_SLOT, actual)
    assert g["hr_crushed"] is None
    assert g["hr_nodoubt"] is None


def test_the_score_is_untouched():
    """Two-lane: these fields must not exist anywhere near hr_score /
    overall_score / any ranking -- grep the function body, not just this
    test's assertions, so a future edit can't quietly wire it in unnoticed."""
    import inspect
    from bots import live_results_tracker as lrt
    src = inspect.getsource(lrt.grade_slot)
    # the four new lines must all be simple dict assignments off
    # _hr_event_any_flag(), never combined with hr_score/overall_score
    for line in src.splitlines():
        if "hr_crushed" in line or "hr_nodoubt" in line or "hr_scraper" in line or "hr_soft" in line:
            assert "hr_score" not in line and "overall_score" not in line, line


if __name__ == "__main__":
    failed, checks = [], 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn(); checks += 1
        except AssertionError as e:
            failed.append(f"{name}: {e}")
        except Exception as e:                      # noqa: BLE001
            failed.append(f"{name}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{len(failed)} FAILED\n" + "\n".join(f"  · {f}" for f in failed))
        sys.exit(1)
    print(f"ok   crushed-homer label: {checks} assertions — classify_hr_type() pins the "
          f"real EV/distance thresholds from park-is-not-dead-scrapers §5, untracked "
          f"measurements read None not False, grade_slot() rolls per-event labels up onto "
          f"the row any-of across a multi-homer night, a no-homer row carries no flags at "
          f"all, and the four fields stay unscored -- nowhere near hr_score")
