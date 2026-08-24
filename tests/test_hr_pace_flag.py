"""due_score() is gone. This is what replaced it.

WHAT THIS PINS (2026-08-24)
============================
Donovan: "maybe just remove it and use it as flag or tag somewhere if player
is over their hr/pa ... and match with high hr9 pitcher recent."

due_score() (see bots/mlb_dashboard.py, where it used to live -- now just a
comment) blended six recent-shape/contact-quality terms into one continuous
0-1 number and fed it into every pick-ranking formula, pool tag and pair tag
in the file. The archive leak investigation (bots/leak_scan.py, 2026-08-23)
found that one of its inputs, last5_hr, is refreshed AFTER the game in graded
rows -- so "no HR in his last 5" was never a real cold-streak read on graded
nights, it was the archive remembering the outcome. Decomposed, only 0.15 of
due_score() was the honest expected-value gap and 0.07 was that contaminated
last5_hr term; the other 0.78 was recent contact quality wearing a dueness
name.

hr_pace_flag is what is left once the honest part is kept and the rest is
dropped: a boolean, not a score, that fires ONLY when BOTH hold —

  1. the hitter has a real EV gap over a real recent sample (expected HRs at
     his own season rate, over at least 15 recent PA, minus actual HRs hit,
     >= 0.75), and
  2. the opposing pitcher is CURRENTLY (his last 3 starts, gated on
     pitcher_l3_starts_found >= 2) allowing home runs at an elevated rate.

Neither half alone is the point. A hitter overdue against a cold pitcher is
not tonight's edge, and a hot pitcher against a hitter on pace is just a hot
pitcher -- pitcher_damage already has him. It is the MATCH that is new.

There were actually TWO copies of due_score() -- mlb_dashboard.py's original
(dataclass-attribute-keyed) and an independent dict-keyed twin inside
live_results_tracker.py, used only as that file's own pair/pool fallback
(build_pair_pool_sections, exercised when a date has no saved pair-builder
sections). Both are gone; both now read hr_pace_flag off the row directly
(a dataclass attribute in the first file, a dict key already present on
every archived row in the second, via SLOT_FIELDS).

WHAT IS DELIBERATELY NOT TESTED
--------------------------------
That hr_pace_flag nights actually homer more. It has not been measured and
does not claim to be — like meatball_fit_score and steal_risk_score before
it, it is archived UNSCORED (see live_results_tracker.SLOT_FIELDS) so that
question is answerable off real graded nights in a few weeks. If the answer
is no, it gets deleted the same way due_score() itself was.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dataclasses  # noqa: E402

from bots.mlb_dashboard import HitterRecord, apply_model_v2_layers  # noqa: E402


def make_hitter(**over) -> HitterRecord:
    """Every no-default field filled with the empty value for its type. Same
    local helper as test_running_game.py — importing from test_model_foundation
    drags in its whole 176-assertion suite at import time."""
    kw = {}
    for f in dataclasses.fields(HitterRecord):
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        t = str(f.type)
        kw[f.name] = "" if "str" in t else (0.0 if "float" in t else (False if "bool" in t else 0))
    kw.update(over)
    return HitterRecord(**kw)


# A hitter on a 30-HR, 500-PA season pace (0.06 HR/PA) who has gone homerless
# over his last 18 PA — an honest 1.08-HR gap over a real sample — matched
# with a pitcher three starts into a stretch of allowing 1.8 HR/9.
DUE_HITTER = dict(season_hr=30, season_pa=500, l20pa_pa=18, l20pa_hr=0, last5_hr=0)
HOT_PITCHER = dict(pitcher_l3_hr9=1.8, pitcher_l3_starts_found=3)


def score(**over):
    h = make_hitter(**{**DUE_HITTER, **HOT_PITCHER, **over})
    apply_model_v2_layers(h)
    return h


def test_the_match_fires():
    h = score()
    assert h.hr_pace_flag is True
    assert h.hr_pace_gap >= 0.75, h.hr_pace_gap
    assert "1.8" in h.hr_pace_note and "3 starts" in h.hr_pace_note


def test_on_pace_does_not_fire_even_against_a_hot_pitcher():
    """Half the match without the other half is not the point."""
    h = score(l20pa_hr=2)  # 2 HRs over 18 PA at a 0.06 rate is ahead of pace
    assert h.hr_pace_gap < 0.75, h.hr_pace_gap
    assert h.hr_pace_flag is False
    assert "pace" in h.hr_pace_note


def test_a_due_hitter_against_a_cold_pitcher_does_not_fire():
    """The other half missing is just as disqualifying — an overdue bat means
    nothing if the arm he's facing isn't currently giving anything up."""
    h = score(pitcher_l3_hr9=0.8)
    assert h.hr_pace_gap >= 0.75
    assert h.hr_pace_flag is False
    assert "not currently HR-prone" in h.hr_pace_note


def test_a_thin_pitcher_sample_refuses_rather_than_falling_back_to_season():
    """Under 2 recent starts, pitcher_l3_hr9 is too thin to trust — the flag
    must not quietly fall back to a season number that isn't 'recent' at all."""
    h = score(pitcher_l3_starts_found=1)
    assert h.hr_pace_flag is False
    assert "need 2+ starts" in h.hr_pace_note and "have 1" in h.hr_pace_note

    zero = score(pitcher_l3_starts_found=0)
    assert zero.hr_pace_flag is False
    assert "have 0" in zero.hr_pace_note


def test_a_thin_hitter_sample_refuses_even_with_a_large_raw_gap():
    """A 'gap' computed over a handful of PA is noise wearing the shape of a
    finding — a 50-HR-pace hitter over just 8 PA can post a gap above 0.75
    on arithmetic alone, and that must not be enough."""
    h = score(season_hr=50, l20pa_pa=8, l20pa_hr=0)
    assert h.hr_pace_gap >= 0.75, h.hr_pace_gap   # the raw number clears the bar
    assert h.hr_pace_flag is False                # the sample floor still blocks it
    assert "too thin" in h.hr_pace_note and "8 PA" in h.hr_pace_note


def test_l20pa_falls_back_to_last5_when_unpublished():
    """Same fallback due_score() used to lean on — l20pa fields are not
    published for every slate, and this must not silently zero the gap out
    when that happens."""
    h = score(l20pa_pa=0, l20pa_hr=0, last5_hr=0, recent_350_den=18)
    assert h.hr_pace_gap >= 0.75, h.hr_pace_gap
    assert h.hr_pace_flag is True


def test_due_score_is_gone_from_scoring():
    """Neither copy of the function may still exist or be called anywhere --
    mlb_dashboard.py had the dataclass-keyed original, live_results_tracker.py
    had an independent dict-keyed twin used as its own pair/pool fallback
    (build_pair_pool_sections). A stray reference to either reintroduces the
    contaminated blend."""
    import re
    for relpath in ("bots/mlb_dashboard.py", "bots/live_results_tracker.py"):
        src = (ROOT / relpath).read_text(encoding="utf-8")
        assert "def due_score(" not in src, relpath
        # Strip comment lines first -- the function's name is still mentioned
        # in prose explaining what got removed and why; only a real call is a bug.
        code_only = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("#")
        )
        calls = re.findall(r"\bdue_score\s*\(", code_only)
        assert not calls, (relpath, calls)


def test_the_columns_survive_the_archive():
    """trim_row() drops anything not on the whitelist — the way
    longest_hr_score went 5,766 graded rows without ever being written down."""
    src = (ROOT / "bots" / "live_results_tracker.py").read_text(encoding="utf-8")
    for field in ("hr_pace_flag", "hr_pace_gap", "hr_pace_note"):
        assert f'"{field}"' in src, f"{field} would be dropped by trim_row()"


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
    print(f"ok   hr pace flag: {checks} assertions — the honest EV-gap half of due_score() "
          f"survives matched with a currently HR-prone pitcher, neither half alone is enough, "
          f"a thin pitcher or hitter sample refuses rather than guessing, due_score() itself is "
          f"gone from the file, and the new fields are archived unscored")
