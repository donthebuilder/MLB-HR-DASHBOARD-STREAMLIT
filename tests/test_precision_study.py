"""Four markets, four scales — and a short board flatters itself twice.

WHAT THIS PINS (2026-08-23)
===========================
Donovan: "i was thinking what about the 4 best bets then from dividing up the
picks top hit hrr bases whatever, what would the socring look like if we did
that over the time — if bad or not good just forget that idea."

bots/precision_study.py answers that off the graded archive. Two things in it
are easy to get wrong in a way that produces a confident, wrong answer, and
both are asserted here.

1. CROSS-MARKET RANKING MUST NOT USE RAW SCORES. hit_score runs hotter than
   hr_score — the same fact the site's verdict registry already carries a
   warning about — so ranking a mixed board on raw score puts a mediocre HIT
   pick above the night's best HR pick and calls it an ordering. The study
   ranks on PERCENTILE WITHIN EACH MARKET ON EACH NIGHT. The test builds a
   night where the raw ordering and the percentile ordering DISAGREE and
   checks which one wins.

2. A SHORT BOARD FLATTERS ITSELF TWICE. It drops the model's least-confident
   calls (that is the thing being measured) and it usually holds less HR, the
   hardest bar on the site at 21.8% against 74.3% for 1+ hit. The second one is
   not skill. MIX BASE re-scores every board using the full board's own
   per-lane rates weighted by the lanes THAT board selected, so a board that
   simply avoided home runs shows a skill of zero.

The third assertion is the refusal: no archive means no number, not a zero.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))

from bots.precision_study import night_board, quota_board, report, study  # noqa: E402


def row(pick_type, score, hit, name="X", pid=1):
    field = {"TOP": "top_board_score_v2", "HR": "hr_score", "HIT": "hit_score",
             "HRR": "hrr_score", "CONTACT": "contact_score"}[pick_type]
    return {"pick_type": pick_type, field: score, "designed_hit": hit,
            "name": name, "player_id": pid, "game_pk": pid}


def test_ranking_is_by_percentile_not_raw_score():
    """The night where the two orderings disagree.

    Three HIT picks at 80/78/76 and three HR picks at 60/40/20. On RAW score
    every HIT pick outranks every HR pick. On percentile-within-market the top
    of each lane ties at 100, and the board's first two entries are one of
    each — which is the correct reading, because 80 is merely the best of a hot
    scale while 60 is the best home-run bat on the card.
    """
    rows = [row("HIT", 80, 1, "H1", 1), row("HIT", 78, 0, "H2", 2), row("HIT", 76, 0, "H3", 3),
            row("HR", 60, 1, "R1", 4), row("HR", 40, 0, "R2", 5), row("HR", 20, 0, "R3", 6)]
    board = night_board(rows)
    assert len(board) == 6
    top2 = {e["name"] for e in board[:2]}
    assert top2 == {"H1", "R1"}, f"raw-score ordering leaked in: {[e['name'] for e in board]}"
    # And the bottom is the bottom of each lane, not "all the HR picks".
    assert {e["name"] for e in board[-2:]} == {"H3", "R3"}


def test_quota_board_is_one_per_lane_not_top_n():
    """Donovan described DIVIDING UP the picks by type, which is a different
    board from 'the best four overall' — it guarantees one of each market
    rather than letting a hot lane take every slot. Conflating the two is how
    a study ends up measuring the one nobody asked about."""
    rows = [row("HIT", 90, 1, "H1", 1), row("HIT", 89, 1, "H2", 2),
            row("HIT", 88, 1, "H3", 3), row("HIT", 87, 1, "H4", 4),
            row("HR", 50, 0, "R1", 5), row("HRR", 50, 0, "P1", 6)]
    q = quota_board(night_board(rows))
    lanes = [e["lane"] for e in q]
    assert len(lanes) == len(set(lanes)), f"a lane appears twice: {lanes}"
    assert "HIT" in lanes and "HR" in lanes and "HRR" in lanes


def test_mix_baseline_kills_a_board_that_only_dodged_home_runs():
    """A board with NO ordering skill that simply holds proportionally less HR.

    This is the real mechanism, and it is not "the short board is all base
    hits" — percentile ranking is per-market, so a short board comes out
    roughly lane-BALANCED by construction. The skew is on the other side: the
    FULL board is HR-heavy (an HR pick in every game plus the TOP15 slate
    board — 708 of 2048 picks on the archive this was written against), and HR
    is the hardest bar on the site. So a balanced short board out-scores an
    HR-heavy full board before a single thing is said about the ordering.

    Here every HIT pick hits and every HR pick misses, with no ordering signal
    inside either lane. The naive comparison is a huge lift; SKILL must be
    exactly zero, because the board scored precisely what its own lane shape
    scores by construction. This is the assertion that stops "precision works"
    from being a restatement of "we published less home run."
    """
    nights = {}
    for d in range(1, 13):
        rows = [row("HIT", 90 - i, 1, f"H{i}", 100 + i) for i in range(4)]
        rows += [row("HR", 90 - i, 0, f"R{i}", 200 + i) for i in range(20)]
        nights[f"2026-07-{d:02d}"] = {"graded_slots": rows}
    res = study(nights)
    top4 = res["sizes"]["4"]
    # The short board holds proportionally more HIT than the full board does.
    assert set(top4["mix"]) == {"HIT", "HR"}, top4["mix"]
    assert top4["mix"]["HIT"] / sum(top4["mix"].values()) > 4 / 24
    # It looks good on the naive comparison...
    assert top4["pct"] > res["full"]["pct"] + 5, (top4["pct"], res["full"]["pct"])
    # ...and has no skill at all once its own lane mix is priced in.
    assert abs(top4["skill"]) < 1e-6, f"mix baseline did not absorb it: {top4}"


def test_real_ordering_survives_the_mix_adjustment():
    """The other direction: a board where the ordering DOES carry signal keeps
    a positive skill after the adjustment. Otherwise the guard above would be
    satisfied by a study that always reports zero."""
    nights = {}
    for d in range(1, 13):
        rows = []
        for i in range(8):
            # within HIT, the better score really does hit more often
            rows.append(row("HIT", 90 - i * 8, 1 if i < 3 else 0, f"H{i}", 100 + i))
        for i in range(8):
            rows.append(row("HR", 90 - i * 8, 1 if i < 2 else 0, f"R{i}", 200 + i))
        nights[f"2026-07-{d:02d}"] = {"graded_slots": rows}
    res = study(nights)
    assert res["sizes"]["4"]["skill"] > 5, res["sizes"]["4"]


def test_ungraded_and_unscored_rows_are_dropped_not_zeroed():
    """A row with no designed_hit is not evidence either way, and a row whose
    score cannot be recovered would sort to the bottom of the percentile
    ranking and quietly make every short board look better."""
    rows = [row("HIT", 80, 1, "H1", 1),
            {"pick_type": "HR", "hr_score": 70, "name": "no grade", "player_id": 2},
            {"pick_type": "HR", "designed_hit": 1, "name": "no score", "player_id": 3}]
    board = night_board(rows)
    assert [e["name"] for e in board] == ["H1"], [e["name"] for e in board]


def test_no_archive_is_a_refusal_not_a_zero():
    res = study({})
    assert res == {"nights": 0}
    text = report(res, "archive: nothing on disk")
    assert "NO GRADED NIGHTS FOUND" in text
    assert "refusal, not a" in text
    assert "0.0%" not in text, "an empty archive must not print a rate"


def test_json_output_round_trips():
    """The workflow writes this to the data branch; a payload that will not
    serialise makes the whole run a log nobody can query."""
    nights = {"2026-07-01": {"graded_slots": [row("HIT", 80, 1), row("HR", 60, 0, "R", 2)]}}
    json.dumps(study(nights), default=str)


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
    print(f"ok   precision study: {checks} assertions — cross-market ranking is by "
          f"percentile and not raw score, the quota board is one per lane, a board "
          f"that only dodged home runs scores zero SKILL, a board with real "
          f"ordering does not, ungraded rows are dropped rather than zeroed, and "
          f"an empty archive refuses instead of printing 0.0%")
