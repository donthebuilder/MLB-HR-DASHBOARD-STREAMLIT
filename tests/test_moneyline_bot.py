"""Prices are arithmetic, and arithmetic on money is worth testing twice.

Same pure/IO split as the other two new bots, for the same reason: the odds
provider is unreachable from where this was written, so everything that turns
a price into a decision is tested against prices typed by hand.

The numbers below are not invented. -110/-110 is the standard pair every
sportsbook quotes and its hold is famously about 4.5%; +100/-120 and
-200/+170 are ordinary MLB moneylines. If the de-vig ever stops reproducing
those, something real has broken.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))

from moneyline_bot import (  # noqa: E402
    EDGE_FLOOR, FALLBACK_LEAGUE_FIP, STARTER_INNINGS, STARTER_WEIGHT,
    WORST_PRICE, Line, Pick, devig, game_probability, implied, league_baseline,
    make_picks, payout, payload, record, settle, starter_shift,
)

# Two teams, so a "model" is just a dict of strengths.
EVEN = {"HOM": 0.500, "AWY": 0.500}


def line(hp, ap, pk=1, home="HOM", away="AWY", date="2026-09-01",
         home_fip=None, away_fip=None):
    return Line(game_pk=pk, date=date, home=home, away=away,
                home_price=hp, away_price=ap, book="test",
                home_fip=home_fip, away_fip=away_fip)


# ── prices ──────────────────────────────────────────────────────────────────

def test_even_money_is_a_half():
    assert implied(100) == 0.5
    assert implied(-100) == 0.5


def test_a_favourite_implies_more_than_half_and_a_dog_less():
    assert implied(-200) > 0.5 > implied(+200)


def test_the_standard_minus_110_pair_holds_about_four_and_a_half_points():
    _, _, hold = devig(-110, -110)
    assert 0.04 < hold < 0.05


def test_de_vigged_probabilities_sum_to_one():
    for pair in ((-110, -110), (+100, -120), (-200, +170), (-150, +130)):
        h, a, _ = devig(*pair)
        assert abs(h + a - 1.0) < 1e-9, pair


def test_de_vig_preserves_who_the_favourite_is():
    h, a, _ = devig(-200, +170)
    assert h > a


def test_a_missing_price_is_not_a_pick():
    assert devig(None, -110) == (None, None, None)
    assert implied(None) is None
    assert implied(0) is None


def test_payout_matches_the_price():
    assert abs(payout(+150) - 1.5) < 1e-9
    assert abs(payout(-200) - 0.5) < 1e-9
    assert abs(payout(100) - 1.0) < 1e-9


# ── picking ─────────────────────────────────────────────────────────────────

def test_a_fairly_priced_game_produces_no_pick():
    """Two even teams at even money. There is nothing here and it says so."""
    assert make_picks([line(-110, -110)], EVEN) == []


def test_a_pick_needs_to_clear_the_floor():
    """A disagreement smaller than the model's own error is not a pick."""
    # Home is a slight dog by price but the model has them even: a small edge.
    picks = make_picks([line(+105, -115)], EVEN)
    for p in picks:
        assert p.edge >= EDGE_FLOOR


def test_a_big_disagreement_is_picked_on_the_right_side():
    strong = {"HOM": 0.700, "AWY": 0.400}
    picks = make_picks([line(+150, -170)], strong)
    assert len(picks) == 1
    assert picks[0].side == "HOM"      # model loves the home team, price does not
    assert picks[0].price == 150
    assert picks[0].edge >= EDGE_FLOOR


def test_a_price_worse_than_the_floor_is_declined_however_good_it_looks():
    strong = {"HOM": 0.950, "AWY": 0.200}
    picks = make_picks([line(WORST_PRICE - 50, +400)], strong)
    assert picks == []


def test_only_one_side_of_a_game_is_ever_picked():
    strong = {"HOM": 0.700, "AWY": 0.400}
    picks = make_picks([line(+150, -170)], strong)
    assert len({p.game_pk for p in picks}) == len(picks)


def test_a_game_with_an_unknown_team_is_skipped_not_crashed():
    assert make_picks([line(+150, -170, home="ZZZ")], EVEN) == []


def test_picks_come_back_biggest_edge_first():
    strong = {"A": 0.700, "B": 0.350, "C": 0.620, "D": 0.400}
    picks = make_picks([
        line(+150, -170, pk=1, home="A", away="B"),
        line(+110, -130, pk=2, home="C", away="D"),
    ], strong)
    assert [p.edge for p in picks] == sorted((p.edge for p in picks), reverse=True)


# ── grading ─────────────────────────────────────────────────────────────────

def _pick(price, side="HOM", pk=1):
    return Pick(game_pk=pk, date="2026-09-01", home="HOM", away="AWY", side=side,
                price=price, model_p=0.6, market_p=0.5, edge=0.1, hold=0.04)


def test_a_winner_pays_the_price_it_was_taken_at():
    p = settle([_pick(+150)], {1: "HOM"})[0]
    assert p.result == "win" and abs(p.profit - 1.5) < 1e-9


def test_a_loser_costs_exactly_one_unit():
    p = settle([_pick(+150)], {1: "AWY"})[0]
    assert p.result == "loss" and p.profit == -1.0


def test_an_ungraded_pick_stays_ungraded_rather_than_counting_as_a_loss():
    p = settle([_pick(+150)], {})[0]
    assert p.result == "" and p.profit == 0.0
    assert record([p])["graded"] == 0


def test_the_record_adds_up():
    picks = settle([_pick(+150, pk=1), _pick(-200, pk=2), _pick(+120, pk=3)],
                   {1: "HOM", 2: "AWY", 3: "HOM"})
    r = record(picks)
    assert r["graded"] == 3 and r["wins"] == 2 and r["losses"] == 1
    # +1.5 - 1.0 + 1.2
    assert abs(r["units_profit"] - 1.7) < 1e-9
    # ROI is published rounded to four decimals, so this compares at that
    # precision. Asserting tighter would be testing the rounding, not the
    # arithmetic — and a test that fails on rounding is one the next person
    # quietly deletes.
    assert abs(r["roi"] - 1.7 / 3) < 5e-5


def test_an_empty_record_does_not_divide_by_zero():
    r = record([])
    assert r["graded"] == 0 and r["roi"] == 0.0 and r["win_rate"] == 0.0


def test_breakeven_rate_is_what_the_prices_demanded():
    picks = settle([_pick(+100, pk=1)], {1: "HOM"})
    picks[0].market_p = 0.5
    assert abs(record(picks)["breakeven_rate"] - 0.5) < 1e-9


# ── shape ───────────────────────────────────────────────────────────────────

def test_payload_still_admits_what_the_model_cannot_see():
    """It sees the starters now. It still cannot see the bullpen or the injuries.

    This assertion used to check for "record and nothing else", which stopped
    being true the day the starters went in — and a claim that quietly becomes
    false while its test keeps passing is the failure mode this whole file is
    trying to prevent. So it pins the CURRENT limitation instead.
    """
    out = payload([], settle([_pick(+150)], {1: "HOM"}))
    assert "blind" in out["method"]
    assert "bullpen" in out["method"] and "injuries" in out["method"]
    assert out["starter_weight"] and out["starter_innings"]
    import json
    json.dumps(out)


def test_history_is_capped_so_the_file_cannot_grow_forever():
    many = [_pick(+120, pk=i) for i in range(900)]
    out = payload([], settle(many, {i: "HOM" for i in range(900)}))
    assert len(out["history"]) <= 400
    # but the RECORD is over everything, not just what is kept
    assert out["record"]["graded"] == 900


def test_the_deciding_half_imports_no_third_party_packages():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bots", "moneyline_bot.py")).read()
    head = src.split("def fetch_lines")[0]
    for banned in ("import requests", "import numpy", "import pandas", "urllib.request"):
        assert banned not in head, f"{banned} leaked into the deciding half"


# ── the test that decided whether this bot ships at all ─────────────────────

def test_the_edge_floor_cannot_rescue_a_blind_model():
    """Written after simulating it, and kept because the answer was no.

    The question a moneyline bot has to survive is the null hypothesis: what
    if the market is simply right and the model is blind? The model here sees
    records and nothing else, while the market sees the starting pitchers, the
    bullpen and the injury report. So the model's disagreement with a price is
    mostly the model's ignorance, not an edge.

    The simulation below builds exactly that world — a true win probability
    that differs from the records-only model by the amount the market can see
    and the model cannot, priced with an ordinary hold — and plays it out.

    THE ANSWER, at every edge floor tried (5, 8 and 12 points) and every level
    of market knowledge (3, 6 and 9 points): the bot loses roughly 4-8% flat,
    and RAISING THE FLOOR DOES NOT HELP. It picks fewer bets and loses the
    same rate, because selectivity concentrates the vig instead of finding an
    edge. That is the finding this board has to be published with, and it is
    why the board is framed as a disagreement log rather than a bet list.

    The assertion is deliberately loose. It is not pinning a number; it is
    pinning the DIRECTION, so that if somebody later gives this model eyes —
    starting-pitcher quality is the obvious one — this test starts failing and
    that is the signal the bot became real.
    """
    import random
    from playoff_odds import win_probability

    def price_from(p, hold_side=0.0225):
        q = min(0.98, max(0.02, p + hold_side))
        return int(round(-100 * q / (1 - q))) if q >= 0.5 else int(round(100 * (1 - q) / q))

    rng = random.Random(4)
    picks, winners, seen = [], {}, 0
    while len(picks) < 1200 and seen < 60000:
        seen += 1
        sh, sa = rng.uniform(.42, .60), rng.uniform(.42, .60)
        model_home = win_probability(sh, sa)
        # what the market knows and the record does not: six points of it.
        true_home = min(.92, max(.08, model_home + rng.gauss(0, 0.06)))
        ln = Line(game_pk=seen, date="d", home="H", away="A",
                  home_price=price_from(true_home), away_price=price_from(1 - true_home))
        got = make_picks([ln], {"H": sh, "A": sa})
        if not got:
            continue
        winners[seen] = "H" if rng.random() < true_home else "A"
        picks.append(got[0])

    r = record(settle(picks, winners))
    assert r["graded"] > 500, "not enough simulated bets to say anything"
    # Losing. If this ever passes zero the model has gained real information
    # and this test is the place to celebrate it.
    assert r["roi"] < 0.0, (
        f"the blind model now shows a positive ROI ({r['roi']:.1%}) under the "
        "null hypothesis — either the model gained eyes or the simulation broke")


# ── the starters, which is what the model was missing ───────────────────────

def test_two_identical_starters_move_nothing():
    assert starter_shift(3.80, 3.80, 3.80) == 0.0
    assert starter_shift(5.00, 5.00, 3.80) == 0.0


def test_the_better_starter_moves_the_game_his_way():
    assert starter_shift(2.90, 4.90, 3.90) > 0      # home has the ace
    assert starter_shift(4.90, 2.90, 3.90) < 0      # away has the ace


def test_the_shift_is_antisymmetric():
    """Swapping the two starters must flip the sign and nothing else."""
    a = starter_shift(2.90, 4.90, 3.90)
    b = starter_shift(4.90, 2.90, 3.90)
    assert abs(a + b) < 1e-12


def test_an_ace_against_a_replacement_arm_is_worth_a_sensible_amount():
    """Size check against reality, not just sign.

    A 2.80 FIP against a 5.20 over five and a half innings is about the widest
    starting-pitcher gap a real slate produces. If this model priced that at
    two points it would be useless, and if it priced it at thirty it would be
    dangerous. The honest range is roughly a tenth of a game.
    """
    shift = starter_shift(2.80, 5.20, 4.00)
    assert 0.06 < shift < 0.16, shift


def test_a_missing_starter_moves_nothing_rather_than_guessing_average():
    assert starter_shift(None, 3.90, 3.90) == 0.0
    assert starter_shift(3.90, None, 3.90) == 0.0
    assert starter_shift("", 3.90, 3.90) == 0.0


def test_an_absurd_fip_is_ignored_rather_than_trusted():
    """One start with a 40.00 FIP must not price a game."""
    assert starter_shift(40.0, 3.90, 3.90) == 0.0
    assert starter_shift(0.0, 3.90, 3.90) == 0.0


def test_the_baseline_comes_from_the_slate_and_falls_back_when_it_is_thin():
    assert league_baseline([3.0, 4.0, 5.0, 4.0]) == 4.0
    assert league_baseline([3.0, 4.0]) == FALLBACK_LEAGUE_FIP
    assert league_baseline([]) == FALLBACK_LEAGUE_FIP
    # junk is filtered, not averaged in
    assert league_baseline([3.0, 4.0, 5.0, 4.0, None, "x", 99.0]) == 4.0


def test_the_baseline_cannot_inflate_the_whole_slate():
    """Written wrong first time, and the failure was the useful part.

    I asserted the per-GAME shifts sum to zero. They do not, and they should
    not: if the better arm is at home in every game on a slate, every shift
    points at the home side and the total is large. That is the model working.

    What the slate baseline actually guarantees is weaker and still worth
    having — the STARTERS' deviations sum to zero, so the bot can never decide
    that everybody pitching tonight is above average. It can only rank tonight's
    arms against each other.
    """
    fips = [3.0, 5.0, 3.5, 4.5, 4.0, 4.0]
    base = league_baseline(fips)
    assert abs(sum(base - f for f in fips)) < 1e-9

    # And the thing I wrongly asserted, pinned as the truth instead: stacking
    # the good arms at home really does push every game the same way.
    same_way = [starter_shift(fips[i], fips[i + 1], base) for i in (0, 2, 4)]
    assert all(v >= 0 for v in same_way) and sum(same_way) > 0


def test_game_probability_stays_a_probability_at_the_extremes():
    p = game_probability(0.95, 0.30, 1.50, 9.00, 4.00)
    assert 0.05 <= p <= 0.95


def test_the_starters_actually_reach_the_picks():
    """The wiring test. The model can be right and the caller still ignore it."""
    even = {"HOM": 0.500, "AWY": 0.500}
    fair = line(-110, -110, home_fip=4.0, away_fip=4.0)
    assert make_picks([fair], even) == []          # equal arms, no opinion
    # Same teams, same prices, but the home man is an ace: now there is one.
    lop = [line(-110, -110, pk=1, home_fip=2.60, away_fip=5.40),
           line(-110, -110, pk=2, home_fip=4.00, away_fip=4.00),
           line(-110, -110, pk=3, home_fip=4.20, away_fip=3.80),
           line(-110, -110, pk=4, home_fip=3.90, away_fip=4.10)]
    picks = make_picks(lop, even)
    assert [p.game_pk for p in picks] == [1]
    assert picks[0].side == "HOM"
    assert picks[0].starter_shift > 0


def test_giving_the_model_eyes_reduces_both_the_picks_and_the_losses():
    """The counterpart to the blind-model test, and the reason this shipped.

    Same simulated world: the market prices the truth, and part of that truth
    is the starting pitcher. The blind model treats the pitching as noise and
    bets into it. The seeing model has the same information the market used, so
    it should DISAGREE LESS — fewer picks — and lose less on the ones it takes.

    Note what is NOT claimed. Neither version turns a profit here, and it would
    be a broken simulation if one did: the market is right by construction in
    this world. What the test pins is that eyes are worth something, which is
    the only thing that can be established without real settled bets.
    """
    import random
    from playoff_odds import win_probability

    def price_from(p, hold_side=0.0225):
        q = min(0.98, max(0.02, p + hold_side))
        return int(round(-100 * q / (1 - q))) if q >= 0.5 else int(round(100 * (1 - q) / q))

    def run(with_eyes):
        rng = random.Random(11)
        picks, winners, seen = [], {}, 0
        while seen < 20000:
            seen += 1
            sh, sa = rng.uniform(.42, .60), rng.uniform(.42, .60)
            hf, af = rng.uniform(2.7, 5.3), rng.uniform(2.7, 5.3)
            base = win_probability(sh, sa)
            # The market prices records AND the starters, plus a little it
            # knows that nothing here does (bullpen, injuries, travel).
            truth = min(.92, max(.08,
                        base + starter_shift(hf, af, 4.0) + rng.gauss(0, 0.03)))
            ln = Line(game_pk=seen, date="d", home="H", away="A",
                      home_price=price_from(truth), away_price=price_from(1 - truth),
                      home_fip=hf if with_eyes else None,
                      away_fip=af if with_eyes else None)
            got = make_picks([ln], {"H": sh, "A": sa})
            if not got:
                continue
            winners[seen] = "H" if rng.random() < truth else "A"
            picks.append(got[0])
        return record(settle(picks, winners))

    blind, seeing = run(False), run(True)
    assert blind["graded"] > 200 and seeing["graded"] > 20
    # Fewer opinions, because most of what it used to "disagree" about was the
    # pitching it could not see.
    assert seeing["graded"] < blind["graded"] * 0.6, (blind["graded"], seeing["graded"])
    # And better ones.
    assert seeing["roi"] > blind["roi"], (blind["roi"], seeing["roi"])
