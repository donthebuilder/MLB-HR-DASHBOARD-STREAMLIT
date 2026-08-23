"""A steal needs a runner, and the first version of this model forgot that.

WHAT THIS PINS (2026-08-23)
===========================
Donovan asked for a model built on wild pitches, pickoffs, pitcher
stolen-bases-against, catcher CS% and team defence. steal_risk_score is that
model. It is worth ZERO points in hr_raw or any other blend — a steal model has
no business inside a home-run score, and the standing no-hr_blend-weights-
before-9c rule covers the rest.

TWO BUGS THIS FILE EXISTS BECAUSE OF, both caught by running the thing rather
than reading it:

1. A hitter with NO stolen-base attempt all season scored 78.7 — the HIGHEST on
   the test slate. With both runner terms absent, the renormalisation handed
   his entire score to the arm and the catcher. A soft arm and a weak-throwing
   catcher are a wonderful steal spot for somebody who runs; for a man who has
   never gone they are a fact about two other people. The runner is a GATE now,
   not a term.

2. A 42-steal man with a .262 on-base scored 75.5 against 75.6 for a .352
   runner — the model called them the same bet while its own comment said one
   of them "is not tonight's steal". You cannot steal first base. Reaching base
   is a MULTIPLIER on the whole score, not a tenth of it.

Both failures share a shape worth naming: a model that is silent about a
missing input will hand its weight to whatever else is present, and the result
looks like a confident finding. Every term here contributes only if its input
exists, the weights renormalise over what landed, and the status field says
"thin" the moment the pitcher or catcher half is absent.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dataclasses  # noqa: E402

from bots.mlb_dashboard import (  # noqa: E402
    HitterRecord, apply_model_v2_layers, find_catcher,
)


def make_hitter(**over) -> HitterRecord:
    """Every no-default field filled with the empty value for its type.

    Local rather than imported from test_model_foundation, which runs its whole
    check suite at import time — importing one helper from it dragged in 176
    unrelated assertions and printed their failures as though they were this
    file's. Same convention, four lines, no side effects.
    """
    kw = {}
    for f in dataclasses.fields(HitterRecord):
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        t = str(f.type)
        kw[f.name] = "" if "str" in t else (0.0 if "float" in t else (False if "bool" in t else 0))
    kw.update(over)
    return HitterRecord(**kw)

RUNNER = dict(season_obp=0.352, season_sb=28, season_cs=5, season_sb_attempt_rate=0.19,
              pitcher_sb_attempts_against=22, pitcher_pickoff_rate=0.004, pitcher_wp9=0.62,
              opp_catcher_cs_rate=0.13, opp_catcher_cs_rate_expected=0.19)


def score(**over):
    h = make_hitter(**dict(RUNNER, **over))
    apply_model_v2_layers(h)
    return h


def test_a_man_who_never_runs_is_not_a_steal_spot():
    h = score(season_sb=0, season_cs=0, season_sb_attempt_rate=0.0)
    assert h.steal_risk_score == 0.0, h.steal_risk_score
    assert h.steal_risk_status == "no_runner"
    assert "no stolen-base attempt" in h.steal_risk_note
    # and he must not outrank an actual runner in a WORSE matchup
    tough = score(opp_catcher_cs_rate=0.33, pitcher_sb_attempts_against=3,
                  pitcher_pickoff_rate=0.05, pitcher_wp9=0.05)
    assert tough.steal_risk_score > h.steal_risk_score


def test_you_cannot_steal_first_base():
    """The 42-steal man batting .262 must sit clearly below the .352 runner in
    the SAME matchup, not tie him."""
    reaches = score()
    doesnt = score(season_obp=0.262, season_sb=42, season_cs=14, season_sb_attempt_rate=0.31)
    assert doesnt.steal_risk_score < reaches.steal_risk_score - 10, \
        (doesnt.steal_risk_score, reaches.steal_risk_score)


def test_the_catcher_moves_it_and_says_so():
    weak = score(opp_catcher_cs_rate=0.13, opp_catcher_cs_rate_expected=0.19)
    strong = score(opp_catcher_cs_rate=0.33, opp_catcher_cs_rate_expected=0.26)
    assert strong.steal_risk_score < weak.steal_risk_score - 5, \
        (strong.steal_risk_score, weak.steal_risk_score)
    assert "13% CS" in weak.steal_risk_note
    assert "33% CS" in strong.steal_risk_note


def test_an_arm_that_holds_runners_lowers_it():
    loose = score()
    tight = score(pitcher_sb_attempts_against=3, pitcher_pickoff_rate=0.045, pitcher_wp9=0.10)
    assert tight.steal_risk_score < loose.steal_risk_score
    assert "no run history against this arm" in tight.steal_risk_note


def test_a_missing_catcher_is_thin_not_average():
    """The Savant feed going down must not quietly score every catcher as
    league-average — that is how a matchup against the best thrower in baseball
    reads neutral. It scores on what is left and LABELS itself."""
    h = score(opp_catcher_cs_rate=None, opp_catcher_cs_rate_expected=None)
    assert h.steal_risk_status == "thin", h.steal_risk_status
    assert "catcher unmeasured" in h.steal_risk_note
    assert h.steal_risk_score > 0


def test_nothing_published_refuses():
    h = make_hitter(season_obp=0.0, season_sb=0, season_cs=0, season_sb_attempt_rate=0.0,
                    pitcher_sb_attempts_against=0, pitcher_pickoff_rate=None,
                    pitcher_wp9=None, opp_catcher_cs_rate=None)
    apply_model_v2_layers(h)
    assert h.steal_risk_score == 0.0
    assert h.steal_risk_status in ("no_runner", "missing")


def test_the_score_is_worth_zero_points_anywhere_else():
    """The standing rule: no hr_blend weight moves before 9c, and a steal model
    does not belong in a home-run score at any date."""
    src = (ROOT / "bots" / "mlb_dashboard.py").read_text(encoding="utf-8")
    for forbidden in ("* steal_risk_score", "steal_risk_score *", "+ steal_risk_score",
                      '"steal_risk"'):
        assert forbidden not in src, f"steal_risk_score has leaked into a blend ({forbidden})"
    from bots.mlb_dashboard import MODEL_WEIGHTS  # noqa: PLC0415
    assert abs(sum(MODEL_WEIGHTS["hr_blend"].values()) - 1.0) < 1e-6
    assert "steal" not in " ".join(MODEL_WEIGHTS["hr_blend"].keys())


def test_find_catcher_walks_the_posted_order_not_the_bench():
    """The players dict includes the bench, so the first "C" in it is as likely
    to be the backup as the starter. The posted batting order is the answer;
    everything else is labelled as a guess."""
    box = {
        "battingOrder": [111, 222],
        "players": {
            "ID999": {"person": {"id": 999, "fullName": "Bench Backup"},
                      "position": {"abbreviation": "C"},
                      "seasonStats": {"batting": {"plateAppearances": 40}}},
            "ID111": {"person": {"id": 111, "fullName": "Lead Off"},
                      "position": {"abbreviation": "CF"}},
            "ID222": {"person": {"id": 222, "fullName": "Starting Catcher"},
                      "position": {"abbreviation": "C"},
                      "seasonStats": {"batting": {"plateAppearances": 400}}},
        },
    }
    cid, name, src = find_catcher(box)
    assert (cid, name, src) == (222, "Starting Catcher", "lineup"), (cid, name, src)

    # No posted order: fall back to the most-used catcher, and SAY it is a
    # fallback rather than presenting a guess as a fact.
    no_order = dict(box, battingOrder=[])
    cid, name, src = find_catcher(no_order)
    assert src == "roster" and cid == 222, (cid, name, src)

    # Nobody at all: refuse. A league-average catcher invented here is how a
    # matchup against the best thrower in baseball ends up scored neutral.
    assert find_catcher({"players": {}}) == (0, "", "")


def test_the_columns_survive_the_archive():
    """trim_row() drops anything not on the whitelist — the way
    longest_hr_score went 5,766 graded rows without ever being written down."""
    src = (ROOT / "bots" / "live_results_tracker.py").read_text(encoding="utf-8")
    for field in ("steal_risk_score", "steal_risk_status",
                  "pitcher_wild_pitches", "pitcher_pickoffs", "pitcher_sb_against",
                  "pitcher_cs_rate_against", "pitcher_wp9", "pitcher_pickoff_rate",
                  "opp_catcher_cs_rate", "opp_catcher_source", "opp_catcher_status"):
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
    print(f"ok   running game: {checks} assertions — a man who never runs scores 0 "
          f"instead of topping the board, a 42-steal .262 on-base sits well below a "
          f".352 runner in the same matchup, the catcher and the arm both move it, a "
          f"dead feed reads THIN rather than average, the catcher comes off the posted "
          f"order and says when it did not, and the score is worth zero points in any blend")
