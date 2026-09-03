"""The bracket is arithmetic, so it is tested as arithmetic.

playoff_odds.py was written where statsapi.mlb.com is unreachable. That is the
reason its model half imports nothing and touches no network: the only way to
know a twelve-team bracket is wired correctly is to run it against seasons you
made up, where you already know the answer.

Each test below is a property that must hold for ANY season, not a golden
number from one run — a golden number would only prove the code still does
what it did, which is not the same as doing the right thing.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))

from playoff_odds import (  # noqa: E402
    HOME_WIN_RATE, PLAYOFF_TEAMS_PER_LEAGUE, Game, Team, simulate,
    win_probability, payload,
)

# Published shares are rounded to four decimals, so a sum over 30 teams can be
# off by up to 30 * 0.00005. Anything tighter than that is testing the rounding,
# not the bracket -- and a test that fails on rounding gets loosened by whoever
# hits it next, which is how a real assertion gets thrown away.
ROUNDING = 30 * 5e-5

DIVISIONS = {
    "AL": ["AL East", "AL Central", "AL West"],
    "NL": ["NL East", "NL Central", "NL West"],
}


def league(prefix_id, lg, records):
    """Five teams per division, records given as a flat list of (w, l)."""
    teams, i = [], 0
    for div in DIVISIONS[lg]:
        for _ in range(5):
            w, l = records[i % len(records)]
            teams.append(Team(team_id=prefix_id + i, abbr=f"{lg}{i:02d}",
                              name=f"{lg} team {i}", league=lg, division=div,
                              wins=w, losses=l))
            i += 1
    return teams


def full_season(al_records, nl_records):
    return league(100, "AL", al_records) + league(200, "NL", nl_records)


AVERAGE = [(70, 70)]


# ── one game ────────────────────────────────────────────────────────────────

def test_equal_teams_on_neutral_ground_is_a_coin_flip():
    assert win_probability(0.5, 0.5, neutral=True) == 0.5


def test_home_field_at_even_strength_is_the_league_home_win_rate():
    assert abs(win_probability(0.5, 0.5) - HOME_WIN_RATE) < 1e-9


def test_log5_is_symmetric():
    # Swapping the sides on neutral ground must give the complement.
    a, b = 0.62, 0.41
    assert abs(win_probability(a, b, neutral=True)
               + win_probability(b, a, neutral=True) - 1.0) < 1e-9


def test_better_team_is_favoured_and_home_field_only_helps():
    assert win_probability(0.60, 0.40, neutral=True) > 0.5
    assert win_probability(0.60, 0.40) > win_probability(0.60, 0.40, neutral=True)


def test_home_field_moves_a_coin_flip_more_than_a_lock():
    """The reason home field is an odds ratio and not a flat addition."""
    flip = win_probability(0.5, 0.5) - win_probability(0.5, 0.5, neutral=True)
    lock = win_probability(0.95, 0.30) - win_probability(0.95, 0.30, neutral=True)
    assert flip > lock > 0


def test_probabilities_stay_inside_zero_and_one():
    for a in (0.001, 0.25, 0.5, 0.75, 0.999):
        for b in (0.001, 0.25, 0.5, 0.75, 0.999):
            p = win_probability(a, b)
            assert 0.0 < p < 1.0


# ── the field ───────────────────────────────────────────────────────────────

def test_exactly_twelve_teams_qualify_every_single_time():
    """The one that catches a mis-seeded bracket. Shares must total 6 a league."""
    rows = simulate(full_season(AVERAGE, AVERAGE), [], sims=200, seed=1)
    for lg in ("AL", "NL"):
        total = sum(r.make_playoffs for r in rows if r.league == lg)
        assert abs(total - PLAYOFF_TEAMS_PER_LEAGUE) < ROUNDING, f"{lg} seated {total}"


def test_every_division_sends_exactly_one_winner():
    rows = simulate(full_season(AVERAGE, AVERAGE), [], sims=200, seed=2)
    by_div = {}
    for r in rows:
        by_div[r.division] = by_div.get(r.division, 0.0) + r.win_division
    assert len(by_div) == 6
    for div, share in by_div.items():
        assert abs(share - 1.0) < ROUNDING, f"{div} produced {share} winners"


def test_playoff_share_is_division_plus_wild_card():
    rows = simulate(full_season(AVERAGE, AVERAGE), [], sims=200, seed=3)
    for r in rows:
        assert abs(r.make_playoffs - (r.win_division + r.wild_card)) < 1e-9


def test_exactly_one_champion_and_two_pennants():
    rows = simulate(full_season(AVERAGE, AVERAGE), [], sims=300, seed=4)
    assert abs(sum(r.win_world_series for r in rows) - 1.0) < ROUNDING
    assert abs(sum(r.win_league for r in rows) - 2.0) < ROUNDING
    for lg in ("AL", "NL"):
        assert abs(sum(r.win_league for r in rows if r.league == lg) - 1.0) < ROUNDING


def test_a_champion_always_made_the_field():
    rows = simulate(full_season(AVERAGE, AVERAGE), [], sims=300, seed=5)
    for r in rows:
        assert r.win_world_series <= r.make_playoffs + 1e-9
        assert r.win_league <= r.make_playoffs + 1e-9


# ── does it say sensible things ─────────────────────────────────────────────

def test_a_runaway_team_is_a_lock_for_the_field():
    teams = full_season(AVERAGE, AVERAGE)
    teams[0].wins, teams[0].losses = 120, 20
    rows = {r.team_id: r for r in simulate(teams, [], sims=400, seed=6)}
    assert rows[teams[0].team_id].make_playoffs > 0.99
    assert rows[teams[0].team_id].win_world_series > rows[teams[1].team_id].win_world_series


def test_a_dreadful_team_is_not_in_the_field():
    teams = full_season(AVERAGE, AVERAGE)
    teams[0].wins, teams[0].losses = 20, 120
    rows = {r.team_id: r for r in simulate(teams, [], sims=400, seed=7)}
    assert rows[teams[0].team_id].make_playoffs < 0.02


def test_identical_teams_get_identical_odds_within_noise():
    """Nothing but the record may distinguish two teams — no id or order bias."""
    rows = simulate(full_season(AVERAGE, AVERAGE), [], sims=1500, seed=8)
    shares = [r.win_world_series for r in rows]
    assert abs(max(shares) - min(shares)) < 0.03, shares


def test_remaining_games_actually_move_the_needle():
    """A tied team handed 20 home games against the worst team must gain."""
    teams = full_season(AVERAGE, AVERAGE)
    strong, weak = teams[0], teams[1]
    weak.wins, weak.losses = 40, 100
    before = {r.team_id: r for r in simulate(teams, [], sims=400, seed=9)}
    games = [Game(home_id=strong.team_id, away_id=weak.team_id) for _ in range(20)]
    after = {r.team_id: r for r in simulate(teams, games, sims=400, seed=9)}
    assert after[strong.team_id].proj_wins > before[strong.team_id].proj_wins + 10
    assert after[strong.team_id].make_playoffs > before[strong.team_id].make_playoffs


def test_projected_wins_never_exceed_a_full_season():
    teams = full_season(AVERAGE, AVERAGE)
    games = [Game(home_id=teams[0].team_id, away_id=teams[1].team_id) for _ in range(22)]
    rows = {r.team_id: r for r in simulate(teams, games, sims=200, seed=10)}
    assert teams[0].wins < rows[teams[0].team_id].proj_wins <= teams[0].wins + 22


def test_a_game_between_unknown_teams_is_dropped_not_crashed():
    """A schedule row for a team not in standings must not take the run down."""
    teams = full_season(AVERAGE, AVERAGE)
    games = [Game(home_id=999999, away_id=teams[0].team_id)]
    rows = simulate(teams, games, sims=50, seed=11)
    assert len(rows) == len(teams)


# ── reproducibility and shape ───────────────────────────────────────────────

def test_the_same_seed_gives_the_same_answer():
    a = simulate(full_season(AVERAGE, AVERAGE), [], sims=150, seed=42)
    b = simulate(full_season(AVERAGE, AVERAGE), [], sims=150, seed=42)
    assert [r.win_world_series for r in a] == [r.win_world_series for r in b]


def test_different_seeds_do_not_give_the_same_answer():
    a = simulate(full_season(AVERAGE, AVERAGE), [], sims=150, seed=1)
    b = simulate(full_season(AVERAGE, AVERAGE), [], sims=150, seed=2)
    assert [r.team_id for r in a] != [r.team_id for r in b] or \
           [r.win_world_series for r in a] != [r.win_world_series for r in b]


def test_regression_pulls_a_small_sample_toward_five_hundred():
    hot = Team(1, "HOT", "Hot", "AL", "AL East", wins=10, losses=0)
    long_hot = Team(2, "LNG", "Long", "AL", "AL East", wins=100, losses=0)
    assert 0.5 < hot.strength < 0.65
    assert long_hot.strength > hot.strength


def test_payload_is_json_shaped_and_states_its_method():
    import json
    rows = simulate(full_season(AVERAGE, AVERAGE), [], sims=50, seed=12)
    out = payload(rows, 50, "test")
    text = json.dumps(out)
    assert "log5" in out["method"] and "coin flip" in out["method"]
    assert len(out["teams"]) == 30
    assert json.loads(text)["sims"] == 50


def test_the_model_half_imports_with_no_third_party_packages():
    """The point of the split. If this breaks, the tests stop being runnable."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "bots", "playoff_odds.py")).read()
    head = src.split("def fetch_season")[0]
    for banned in ("import requests", "import numpy", "import pandas", "import polars"):
        assert banned not in head, f"{banned} leaked into the pure half"
