"""The Moneyball base, tested where statsapi is unreachable."""
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))

from moneyball import (  # noqa: E402
    PRIOR, Coef, FinishedGame, League, TeamRates, backtest, calibration, fit_runs,
    game_probability, home_field, league_of, matchup_rate, pythagenpat, score,
)


def team(tid, obp, slg, obpa, slga, games=100, rpg=4.5, w=50, l=50):
    return TeamRates(team_id=tid, abbr=f"T{tid}", obp=obp, slg=slg, obp_allowed=obpa,
                     slg_allowed=slga, games=games, runs=int(rpg * games), wins=w, losses=l)


# ── the regression ──────────────────────────────────────────────────────────

def test_ols_recovers_known_coefficients_with_no_prior():
    rng = random.Random(3)
    rows = []
    for _ in range(60):
        o, s = rng.uniform(0.29, 0.36), rng.uniform(0.35, 0.47)
        rows.append((o, s, -4.0 + 15.0 * o + 10.0 * s))
    c = fit_runs(rows, prior_weight=0)
    assert abs(c.intercept + 4.0) < 1e-6 and abs(c.obp - 15.0) < 1e-6 and abs(c.slg - 10.0) < 1e-6
    assert c.shrink == 0


def test_shrinkage_lands_between_data_and_prior():
    rows = [(o, s, -4.0 + 15.0 * o + 10.0 * s) for o, s in
            [(0.30 + i * 0.002, 0.38 + (i % 7) * 0.01) for i in range(30)]]
    c = fit_runs(rows, prior_weight=30)
    assert min(15.0, PRIOR.obp) <= c.obp <= max(15.0, PRIOR.obp)
    assert abs(c.shrink - 0.5) < 1e-9


def test_too_few_rows_returns_the_prior_and_says_so():
    c = fit_runs([(0.32, 0.40, 4.4)] * 3)
    assert (c.intercept, c.obp, c.slg) == (PRIOR.intercept, PRIOR.obp, PRIOR.slg)
    assert c.shrink == 1.0


def test_collinear_rows_do_not_crash():
    rows = [(0.30 + i * 0.001, 0.40 + i * 0.001, 4.0 + i * 0.02) for i in range(20)]
    c = fit_runs(rows)
    assert math.isfinite(c.obp) and math.isfinite(c.slg)


def test_the_prior_prices_a_league_average_line_at_about_four_and_a_half():
    assert abs(PRIOR.runs(0.315, 0.400) - 4.40) < 0.01


def test_obp_is_worth_more_than_slg_per_point_in_the_prior():
    assert PRIOR.obp_per_slg > 1.0


# ── the matchup ─────────────────────────────────────────────────────────────

def test_matchup_rate_is_the_offence_when_the_defence_is_league_average():
    assert abs(matchup_rate(0.340, 0.315, 0.315) - 0.340) < 1e-9


def test_a_better_staff_lowers_the_rate_and_a_worse_one_raises_it():
    assert matchup_rate(0.340, 0.290, 0.315) < 0.340 < matchup_rate(0.340, 0.340, 0.315)


def test_pythagenpat_is_a_coin_flip_at_equal_runs_and_symmetric():
    assert abs(pythagenpat(4.5, 4.5) - 0.5) < 1e-12
    assert abs(pythagenpat(5.5, 3.5) + pythagenpat(3.5, 5.5) - 1.0) < 1e-12
    assert pythagenpat(5.5, 3.5) > 0.6


def test_home_field_reproduces_the_league_rate_at_a_coin_flip_and_barely_moves_a_lock():
    assert abs(home_field(0.5) - 0.535) < 1e-9
    assert home_field(0.95) - 0.95 < 0.01
    assert home_field(0.535) - 0.535 < home_field(0.5) - 0.5


def test_better_obp_and_slg_wins_more_often():
    lg = League(0.315, 0.400)
    good = team(1, 0.340, 0.440, 0.300, 0.380)
    bad = team(2, 0.300, 0.370, 0.330, 0.430)
    p = game_probability(good, bad, lg, PRIOR, neutral=True)
    assert p is not None and p > 0.6
    assert abs(p + game_probability(bad, good, lg, PRIOR, neutral=True) - 1.0) < 1e-9


def test_a_walk_counts_the_same_as_a_single_here():
    """The Moneyball point: two lineups with the same OBP and SLG price the
    same, however they got there. Batting average is not an input."""
    lg = League(0.315, 0.400)
    opp = team(9, 0.315, 0.400, 0.315, 0.400)
    walks = team(1, 0.350, 0.420, 0.315, 0.400)
    singles = team(2, 0.350, 0.420, 0.315, 0.400)
    assert game_probability(walks, opp, lg, PRIOR) == game_probability(singles, opp, lg, PRIOR)


def test_rates_from_too_few_games_are_not_priced():
    lg = League(0.315, 0.400)
    early = team(1, 0.400, 0.500, 0.250, 0.300, games=5)
    assert game_probability(early, team(2, 0.315, 0.400, 0.315, 0.400), lg, PRIOR) is None


def test_league_rates_are_games_weighted():
    lg = league_of([team(1, 0.300, 0.400, 0.3, 0.4, games=100), team(2, 0.400, 0.400, 0.3, 0.4, games=0)])
    assert abs(lg.obp - 0.300) < 1e-9


# ── scoring ─────────────────────────────────────────────────────────────────

def test_a_coin_scores_ln2_and_a_perfect_model_near_zero():
    pairs = [(0.5, i % 2 == 0) for i in range(100)]
    assert abs(score(pairs)["log_loss"] - math.log(2)) < 1e-3  # rounded to 4 places
    sharp = [(0.99 if i % 2 == 0 else 0.01, i % 2 == 0) for i in range(100)]
    assert score(sharp)["log_loss"] < 0.02 and score(sharp)["fav_accuracy"] == 1.0


def test_empty_score_does_not_divide_by_zero():
    assert score([])["n"] == 0 and calibration([]) == []


# ── the walk-forward harness ────────────────────────────────────────────────

def poisson(rng, mean):
    """Knuth. Small means, so the loop is short."""
    L, k, p = math.exp(-mean), 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


def synthetic_season(seed=11, n_teams=12, games_per_month=120):
    """A league whose runs are Poisson around the Moneyball expectation, so
    the true win probability comes FROM OBP and SLG and a model reading them
    should beat one reading only the record."""
    rng = random.Random(seed)
    truth = {}
    for tid in range(1, n_teams + 1):
        truth[tid] = dict(obp=rng.uniform(0.285, 0.355), slg=rng.uniform(0.345, 0.475),
                          obpa=rng.uniform(0.285, 0.355), slga=rng.uniform(0.345, 0.475))
    lg = League(sum(t["obp"] for t in truth.values()) / n_teams,
                sum(t["slg"] for t in truth.values()) / n_teams)
    wins = {t: 0 for t in truth}; losses = {t: 0 for t in truth}
    runs = {t: 0 for t in truth}; played = {t: 0 for t in truth}
    games, snapshots = [], {}
    for month in range(4, 10):
        for i in range(games_per_month):
            h, a = rng.sample(list(truth), 2)
            th, ta = truth[h], truth[a]
            rh = PRIOR.runs(matchup_rate(th["obp"], ta["obpa"], lg.obp), matchup_rate(th["slg"], ta["slga"], lg.slg))
            ra = PRIOR.runs(matchup_rate(ta["obp"], th["obpa"], lg.obp), matchup_rate(ta["slg"], th["slga"], lg.slg))
            hr, ar = poisson(rng, rh * 1.04), poisson(rng, ra * 0.96)   # a little home field
            while hr == ar:
                hr, ar = hr + poisson(rng, 0.5), ar + poisson(rng, 0.5)
            games.append(FinishedGame(game_pk=len(games), date=f"2026-{month:02d}-{1 + i % 28:02d}",
                                      home_id=h, away_id=a, home_runs=hr, away_runs=ar))
            w, l = (h, a) if hr > ar else (a, h)
            wins[w] += 1; losses[l] += 1
            for t, r in ((h, hr), (a, ar)):
                runs[t] += r; played[t] += 1
        # month-end snapshot: true rates plus a little sampling noise
        snapshots[month] = [
            TeamRates(team_id=t, abbr=f"T{t}", obp=truth[t]["obp"] + rng.gauss(0, 0.004),
                      slg=truth[t]["slg"] + rng.gauss(0, 0.006),
                      obp_allowed=truth[t]["obpa"] + rng.gauss(0, 0.004),
                      slg_allowed=truth[t]["slga"] + rng.gauss(0, 0.006),
                      games=played[t], runs=runs[t], wins=wins[t], losses=losses[t])
            for t in truth]
    return games, snapshots


def test_walk_forward_scores_only_months_with_a_prior_snapshot():
    games, snaps = synthetic_season()
    rep = backtest(games, snaps)
    assert rep["months"] == [5, 6, 7, 8, 9]
    assert rep["games"] > 0 and rep["moneyball"]["n"] == rep["records_only"]["n"] == rep["coin"]["n"]


def test_in_a_world_run_on_obp_and_slg_the_model_beats_the_coin_and_the_record():
    games, snaps = synthetic_season()
    rep = backtest(games, snaps)
    assert rep["moneyball"]["log_loss"] < rep["coin"]["log_loss"]
    assert rep["moneyball"]["log_loss"] < rep["home_always"]["log_loss"]
    assert rep["moneyball"]["log_loss"] < rep["records_only"]["log_loss"]
    assert rep["verdict"].startswith("Out of sample, OBP/SLG beats the record-only model")


def test_a_game_never_sees_its_own_month():
    """Poison the snapshot for month 7 with inverted rates. Games in month 7
    must price identically to a run where month 7's snapshot is absent from
    the past, because only month 6's snapshot is allowed to see them."""
    games, snaps = synthetic_season()
    july = [g for g in games if g.month == 7]
    clean = backtest(july, {6: snaps[6], 5: snaps[5], 4: snaps[4]})
    poisoned = dict(snaps)
    poisoned[7] = [TeamRates(team_id=t.team_id, abbr=t.abbr, obp=0.44, slg=0.59, obp_allowed=0.21,
                             slg_allowed=0.26, games=t.games, runs=t.runs) for t in snaps[7]]
    dirty = backtest(july, {k: poisoned[k] for k in (4, 5, 6, 7)})
    assert clean["moneyball"] == dirty["moneyball"]


def test_verdict_is_negative_when_the_model_is_worse_than_home_always():
    from moneyball import verdict
    v = verdict({"n": 100, "log_loss": 0.70}, {"n": 100, "log_loss": 0.69}, {"n": 100, "log_loss": 0.69})
    assert "did not beat" in v


def test_the_model_half_imports_with_no_third_party_packages():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bots", "moneyball.py")).read()
    head = src.split("def fetch_rates")[0]
    for banned in ("import requests", "import numpy", "import pandas", "import sklearn"):
        assert banned not in head, f"{banned} leaked into the pure half"


def test_the_blend_sweep_lands_on_the_rates_when_the_world_runs_on_them():
    from moneyball import blend_sweep
    games, snaps = synthetic_season()
    rep = backtest(games, snaps)
    assert rep["blend"]["weight"] >= 0.5
    assert rep["blend"]["score"]["log_loss"] <= min(rep["moneyball"]["log_loss"], rep["records_only"]["log_loss"]) + 1e-9
    assert "The live base is" in rep["verdict"]


def test_the_blend_sweep_lands_on_the_record_when_the_rates_are_noise():
    from moneyball import blend_sweep
    rng = random.Random(5)
    truth = [rng.random() < 0.6 for _ in range(600)]
    record = [(0.6, w) for w in truth]
    noise = [(rng.uniform(0.2, 0.8), w) for w in truth]
    assert blend_sweep(noise, record)["best"] == 0.0
