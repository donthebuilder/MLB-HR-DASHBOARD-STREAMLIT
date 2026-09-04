"""Comebacks are counting, so they are tested as counting.

Same discipline as test_playoff_odds.py: the counting half of
comeback_board.py imports nothing and touches no network, so it can be run
against games written by hand, where the right answer is already known.

The hand-written games below are the point. A comeback board is easy to get
subtly wrong -- off by one on the half-inning boundary, or crediting the home
team for a deficit it never faced because the away team bats first -- and no
amount of staring at real data would surface that. Two invented games do.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))

from comeback_board import GameLine, deficit_track, tally, payload  # noqa: E402


def game(away_innings, home_innings, away="AWY", home="HOM", pk=1, date="2026-05-01"):
    return GameLine(game_pk=pk, date=date, home=home, away=away,
                    away_innings=away_innings, home_innings=home_innings)


# ── the deficit walk ────────────────────────────────────────────────────────

def test_the_winner_of_a_wire_to_wire_game_never_trails():
    """Written wrong first time, which is the point of writing it.

    I asserted (0, 0) here — no deficit for EITHER side. That is nonsense: the
    away team lost 2-0, so of course they trailed. deficit_track reports the
    worst deficit each side faced, and the losing side's is almost always
    non-zero. What makes a game a comeback is the WINNER's deficit, and that
    is what this pins.
    """
    g = game([0, 0, 0], [1, 1, 0])
    home_def, away_def = deficit_track(g)
    assert home_def == 0     # the winner led throughout
    assert away_def == 2     # the loser was two down at the end


def test_the_home_team_trails_the_moment_the_away_team_scores_first():
    """The away team bats first. Down 3-0 before you hit is still down three."""
    g = game([3, 0, 0], [0, 0, 4])
    home_def, _ = deficit_track(g)
    assert home_def == 3


def test_deficits_are_measured_at_every_half_inning_boundary():
    # AWY: 3,0,0  HOM: 0,0,4  →  0-3, 0-3, 0-3, 0-3, 0-3, 4-3
    # Home's worst is 3. Away's worst is 1 (trailing 4-3 at the end).
    g = game([3, 0, 0], [0, 0, 4])
    home_def, away_def = deficit_track(g)
    assert home_def == 3
    assert away_def == 1


def test_a_one_nil_win_is_a_deficit_for_the_loser_and_none_for_the_winner():
    """The narrowest case. If the winner shows any deficit here it is a bug."""
    g = game([0, 0, 0], [0, 0, 1])
    home_def, away_def = deficit_track(g)
    assert home_def == 0
    assert away_def == 1


def test_a_home_team_that_does_not_bat_in_the_ninth_is_handled():
    """Winning home teams do not bat in the bottom of the last inning."""
    g = game([0, 0, 0, 0, 0, 0, 0, 0, 1], [2, 0, 0, 0, 0, 0, 0, 0])
    home_def, away_def = deficit_track(g)
    assert home_def == 0
    assert away_def == 2
    assert g.home_runs == 2 and g.away_runs == 1


# ── the board ───────────────────────────────────────────────────────────────

def test_a_comeback_win_is_recorded_for_the_winner_and_against_the_loser():
    rows = {r.abbr: r for r in tally([game([3, 0, 0], [0, 0, 4])])}
    assert rows["HOM"].comeback_wins == 1
    assert rows["HOM"].biggest_comeback == 3
    assert rows["AWY"].blown_leads == 1
    assert rows["AWY"].biggest_blown == 3


def test_every_comeback_win_in_the_league_has_a_matching_blown_lead():
    """The symmetry the board is built on. If these ever diverge it is a bug."""
    games = [
        game([3, 0, 0], [0, 0, 4], pk=1),
        game([0, 0, 1], [2, 0, 0], pk=2),
        game([5, 0, 0], [0, 0, 6], away="AAA", home="BBB", pk=3),
        game([1, 1, 1], [0, 0, 0], away="CCC", home="DDD", pk=4),
    ]
    rows = tally(games)
    assert sum(r.comeback_wins for r in rows) == sum(r.blown_leads for r in rows)


def test_a_wire_to_wire_win_is_not_a_comeback():
    rows = {r.abbr: r for r in tally([game([0, 0, 1], [2, 0, 0])])}
    assert rows["HOM"].comeback_wins == 0
    assert rows["HOM"].wire_to_wire_wins == 1
    assert rows["AWY"].blown_leads == 0


def test_wins_split_into_comebacks_and_wire_to_wire_and_nothing_else():
    games = [
        game([3, 0, 0], [0, 0, 4], pk=1),
        game([0, 0, 1], [2, 0, 0], pk=2),
        game([0, 0, 0], [1, 0, 0], pk=3),
    ]
    for r in tally(games):
        assert r.wins == r.comeback_wins + r.wire_to_wire_wins


def test_a_tied_line_is_skipped_rather_than_scored():
    """A tie means suspended or unfinished — it is not a result either way."""
    rows = tally([game([1, 1], [1, 1])])
    assert rows == [] or all(r.wins == 0 and r.losses == 0 for r in rows)


def test_records_add_up_across_the_league():
    games = [
        game([3, 0, 0], [0, 0, 4], pk=1),
        game([0, 0, 1], [2, 0, 0], pk=2),
        game([5, 0, 0], [0, 0, 6], away="AAA", home="BBB", pk=3),
    ]
    rows = tally(games)
    assert sum(r.wins for r in rows) == sum(r.losses for r in rows) == 3


def test_the_biggest_comeback_is_the_biggest_and_not_the_latest():
    games = [
        game([6, 0, 0], [0, 0, 7], pk=1, date="2026-04-01"),
        game([2, 0, 0], [0, 0, 3], pk=2, date="2026-09-01"),
    ]
    rows = {r.abbr: r for r in tally(games)}
    assert rows["HOM"].biggest_comeback == 6
    assert rows["HOM"].comeback_games[0]["deficit"] == 6


def test_the_board_leads_with_the_most_comebacks():
    games = [game([3, 0, 0], [0, 0, 4], away="AAA", home="BBB", pk=i) for i in range(3)]
    games.append(game([1, 0, 0], [0, 0, 2], away="CCC", home="DDD", pk=9))
    rows = tally(games)
    assert rows[0].abbr == "BBB" and rows[0].comeback_wins == 3


def test_top_lists_are_capped_so_the_file_cannot_grow_without_bound():
    games = [game([2, 0, 0], [0, 0, 3], pk=i) for i in range(40)]
    rows = {r.abbr: r for r in tally(games)}
    assert rows["HOM"].comeback_wins == 40
    assert len(rows["HOM"].comeback_games) <= 8
    assert len(rows["AWY"].led_and_lost_games) <= 8


def test_an_empty_season_produces_an_empty_board_not_a_crash():
    assert tally([]) == []


# ── shape ───────────────────────────────────────────────────────────────────

def test_payload_states_the_limitation_it_cannot_fix():
    import json
    out = payload(tally([game([3, 0, 0], [0, 0, 4])]), 1, 2026)
    assert "half-inning" in out["method"]
    assert "floor" in out["method"]
    json.dumps(out)


def test_comeback_rate_is_a_share_of_wins():
    games = [
        game([3, 0, 0], [0, 0, 4], pk=1),
        game([0, 0, 0], [1, 0, 0], pk=2),
    ]
    out = payload(tally(games), 2, 2026)
    hom = next(t for t in out["teams"] if t["abbr"] == "HOM")
    assert hom["wins"] == 2 and hom["comeback_rate"] == 0.5


def test_the_counting_half_imports_no_third_party_packages():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bots", "comeback_board.py")).read()
    head = src.split("def fetch_season")[0]
    for banned in ("import requests", "import numpy", "import pandas", "import polars"):
        assert banned not in head, f"{banned} leaked into the pure half"
