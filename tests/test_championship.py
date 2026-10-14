"""Championship odds (NFL / NHL / NBA): properties that must hold for ANY season, not golden numbers.

TEST DATA, MADE UP. Teams T00.. and every record here are invented for these checks and describe no real club.
The feed-parser fixtures are shaped after the documented NHL (api-web) and ESPN payloads; they prove the parsers read
that shape, not that the live feeds still look like it (the live check is the first real run).

Run: python tests/test_championship.py     (or python -m pytest tests/test_championship.py)
"""
import copy
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "championship"))

import common as C  # noqa: E402
import nba  # noqa: E402
import nfl  # noqa: E402
import nhl  # noqa: E402

SIMS = 1500


def _pairs(n_teams: int, rounds: int):
    """Circle-method round robin: (home, away) pairs, each team once per round."""
    ids = list(range(n_teams))
    out = []
    for r in range(rounds):
        for k in range(n_teams // 2):
            a, b = ids[k], ids[n_teams - 1 - k]
            out.append((a, b) if (r + k) % 2 == 0 else (b, a))
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]
    return out


# ── common ───────────────────────────────────────────────────────────────────

def test_win_prob_basics():
    hwr = 0.54
    assert abs(C.win_prob(0.5, 0.5, hwr) - hwr) < 1e-9, "equal teams reproduce the home win rate"
    assert abs(C.win_prob(0.5, 0.5, hwr, neutral=True) - 0.5) < 1e-9, "no home edge at a neutral site"
    assert C.win_prob(0.7, 0.4, hwr) > C.win_prob(0.6, 0.4, hwr)
    assert C.win_prob(0.99, 0.01, hwr) <= 0.999


def test_strength_regression():
    assert C.strength(0, 0, 0, 0, 8, 0, 2.0) == 0.5
    assert abs(C.strength(5, 5, 0, 0, 8, 0, 2.0) - (5 + 4) / 13) < 1e-9
    py = (C.pythag(100, 50, 2.0) * 10 + 4) / 18
    assert abs(C.strength(10, 10, 100, 50, 8, 1.0, 2.0) - py) < 1e-9, "blend 1 is the regressed Pythagorean rate"


def test_home_rate_sanity_bound_raises():
    try:
        C.fit_home_win_rate([(3, 1)] * 10, 0.52, 0.56, "x")
    except ValueError:
        return
    raise AssertionError("a home win rate of 1.0 must raise, not publish")


def _good_doc(n=4):
    teams = [{"abbr": f"T{i}", "make_playoffs": 0.5, "win_division": 0.25, "top_seed": 0.1,
              "win_round1": 0.5, "win_round2": 0.4, "win_round3": 0.3, "win_round4": 1.0 / n, "win_champion": 1.0 / n,
              "seeds": {"1": 0.1}} for i in range(n)]
    return {"method": "m", "sims": 100, "teams": teams}


def test_validator_accepts_good_and_rejects_bad():
    assert C.validate(_good_doc(), 4, 2) == []
    bad = _good_doc(); bad["teams"][0]["make_playoffs"] = 31.5          # a percent written as a share
    assert any("outside [0,1]" in b for b in C.validate(bad, 4, 2))
    bad = _good_doc(); bad["method"] = ""
    assert any("method" in b for b in C.validate(bad, 4, 2))
    bad = _good_doc(); bad["teams"][0]["win_champion"] = 0.9; bad["teams"][0]["win_round4"] = 0.9
    assert any("sums to" in b for b in C.validate(bad, 4, 2))
    bad = _good_doc(); bad["teams"][0]["win_round2"] = 0.9               # round 2 > round 1
    assert any("monotone" in b for b in C.validate(bad, 4, 2))
    assert any("expected" in b for b in C.validate(_good_doc(3), 4, 2))   # fewer than the full league


# ── NFL ──────────────────────────────────────────────────────────────────────

def _nfl_teams():
    divs = [f"{c} {d}" for c in ("AFC", "NFC") for d in ("East", "North", "South", "West")]
    teams = []
    for i in range(32):
        d = divs[i // 4]
        teams.append({"team_id": 100 + i, "abbr": f"T{i:02d}", "name": f"Team {i}", "conference": d[:3], "division": d})
    return teams


def _nfl_state(played=15, strong=None):
    teams = _nfl_teams()
    wu = [7.0 + (i % 3) * 0.5 for i in range(32)]
    if strong is not None:
        wu[strong] = float(played); wu[31 if strong != 31 else 30] = 0.0
    pairs = _pairs(32, 2)                        # 2 games left for everyone
    home = [h for h, a in pairs]; away = [a for h, a in pairs]
    return nfl.State.from_arrays(teams, wu, [played] * 32, [20.0 * played] * 32, [20.0 * played] * 32, home, away)


def test_nfl_invariants():
    rows = nfl.simulate(_nfl_state(), SIMS, seed=1, home_win_rate=0.54)
    assert len(rows) == 32
    assert abs(sum(r["make_playoffs"] for r in rows) - 14) < 32 * 5e-5 + 1e-6, "exactly 14 teams reach the bracket"
    assert abs(sum(r["win_champion"] for r in rows) - 1) < 32 * 5e-5 + 1e-6
    for r in rows:
        assert r["win_round1"] >= r["win_round2"] >= r["win_round3"] >= r["win_round4"] - 1e-9
        assert r["win_round4"] == r["win_champion"]
        assert abs(sum(r["seeds"].values()) - r["make_playoffs"]) < 8e-4, r["abbr"]
        assert 0.0 <= r["top_seed"] <= r["make_playoffs"] + 1e-9
    byes = sum(r["top_seed"] for r in rows)
    assert abs(byes - 2) < 32 * 5e-5 + 1e-6, "one bye per conference"
    assert C.validate(C.payload(sport_label="nfl", season=2026, sims=SIMS, model_version="x", method="m",
                                home_win_rate=0.54, teams=rows), 32, 14) == []


def test_nfl_clinched_and_eliminated():
    rows = {r["abbr"]: r for r in nfl.simulate(_nfl_state(strong=0), SIMS, seed=2, home_win_rate=0.54)}
    assert rows["T00"]["make_playoffs"] == 1.0 and rows["T00"]["win_division"] == 1.0, "unbeatable record is always in"
    assert rows["T00"]["top_seed"] == 1.0
    assert rows["T31"]["make_playoffs"] == 0.0 and rows["T31"]["win_champion"] == 0.0, "winless team cannot get in"
    assert rows["T00"]["win_champion"] > max(r["win_champion"] for k, r in rows.items() if k != "T00")


def test_nfl_deterministic():
    a = nfl.simulate(_nfl_state(), 800, seed=7, home_win_rate=0.54)
    b = nfl.simulate(_nfl_state(), 800, seed=7, home_win_rate=0.54)
    assert a == b, "same seed, same file"


# ── NHL ──────────────────────────────────────────────────────────────────────

def _nhl_rows(played=60, strong=None):
    divs = [("East", "Atlantic"), ("East", "Metropolitan"), ("West", "Central"), ("West", "Pacific")]
    rows = []
    for i in range(32):
        conf, div = divs[i // 8]
        w = 28 + (i % 5)
        rows.append({"abbr": f"H{i:02d}", "name": f"Club {i}", "conference": conf, "division": div, "gp": played,
                     "w": w, "l": played - w - 6, "otl": 6, "pts": 2 * w + 6, "rw": w - 6, "gf": 180, "ga": 180, "clinch": ""})
    if strong is not None:
        rows[strong].update({"w": played, "l": 0, "otl": 0, "pts": 2 * played, "rw": played})
        rows[31 if strong != 31 else 30].update({"w": 0, "l": played - 0, "otl": 0, "pts": 0, "rw": 0})
    return rows


def _nhl_games(left=22):
    return [{"home": f"H{h:02d}", "away": f"H{a:02d}"} for h, a in _pairs(32, left)]


def test_nhl_invariants():
    st = nhl.State(_nhl_rows(), _nhl_games())
    rows = nhl.simulate(st, 600, seed=3, home_win_rate=0.54, ot_rate=0.23)
    assert len(rows) == 32
    assert abs(sum(r["make_playoffs"] for r in rows) - 16) < 32 * 5e-5 + 1e-6, "exactly 16 teams reach the bracket"
    assert abs(sum(r["win_champion"] for r in rows) - 1) < 32 * 5e-5 + 1e-6
    for r in rows:
        assert r["win_round1"] >= r["win_round2"] >= r["win_round3"] >= r["win_round4"] - 1e-9
        assert abs(sum(r["seeds"].values()) - r["make_playoffs"]) < 8e-4
    assert abs(sum(r["win_division"] for r in rows) - 4) < 32 * 5e-5 + 1e-6, "four division winners"
    assert abs(sum(r["top_seed"] for r in rows) - 2) < 32 * 5e-5 + 1e-6, "one top seed per conference"
    doc = C.payload(sport_label="nhl", season=2026, sims=600, model_version="x", method="m", home_win_rate=0.54, teams=rows)
    assert C.validate(doc, 32, 16) == []


def test_nhl_clinched_and_eliminated():
    st = nhl.State(_nhl_rows(strong=0), _nhl_games(left=4))
    rows = {r["abbr"]: r for r in nhl.simulate(st, 400, seed=4, home_win_rate=0.54, ot_rate=0.23)}
    assert rows["H00"]["make_playoffs"] == 1.0 and rows["H00"]["top_seed"] == 1.0
    assert rows["H31"]["make_playoffs"] == 0.0


def test_nhl_deterministic_and_parsers():
    st = nhl.State(_nhl_rows(), _nhl_games(left=6))
    assert nhl.simulate(st, 300, seed=9, home_win_rate=0.54, ot_rate=0.2) == nhl.simulate(st, 300, seed=9, home_win_rate=0.54, ot_rate=0.2)
    standings = {"standings": [{"teamAbbrev": {"default": "AAA"}, "teamName": {"default": "Alpha"}, "conferenceAbbrev": "E",
                                "divisionName": "Atlantic", "gamesPlayed": 10, "wins": 6, "losses": 3, "otLosses": 1, "points": 13,
                                "regulationWins": 5, "goalFor": 30, "goalAgainst": 25, "clinchIndicator": "x"}]}
    r = nhl.parse_standings(standings)[0]
    assert (r["abbr"], r["conference"], r["w"], r["otl"], r["pts"], r["rw"]) == ("AAA", "East", 6, 1, 13, 5)
    sched = {"gameWeek": [{"date": "2026-10-10", "games": [
        {"id": 1, "gameType": 2, "gameState": "OFF", "homeTeam": {"abbrev": "AAA", "id": 1, "score": 3},
         "awayTeam": {"abbrev": "BBB", "id": 2, "score": 2}, "gameOutcome": {"lastPeriodType": "OT"}},
        {"id": 2, "gameType": 2, "gameState": "FUT", "homeTeam": {"abbrev": "BBB", "id": 2}, "awayTeam": {"abbrev": "AAA", "id": 1}}]}]}
    g = nhl.parse_games(sched)
    assert g[0]["hs"] == 3 and g[0]["ot"] is True and g[1]["hs"] is None and g[1]["ot"] is None


# ── NBA ──────────────────────────────────────────────────────────────────────

def _nba_rows(played=50, strong=None):
    rows = []
    for i in range(30):
        w = 20 + (i % 7)
        rows.append({"team_id": 200 + i, "abbr": f"B{i:02d}", "name": f"Club {i}", "conference": "East" if i < 15 else "West",
                     "division": None, "w": w, "l": played - w, "pf": 5000.0, "pa": 5000.0, "clinch": ""})
    if strong is not None:
        rows[strong].update({"w": played, "l": 0})
        rows[29 if strong != 29 else 28].update({"w": 0, "l": played})
    return rows


def _nba_games(left=32):
    return [{"home": f"B{h:02d}", "away": f"B{a:02d}"} for h, a in _pairs(30, left)]


def test_nba_invariants():
    st = nba.State(_nba_rows(), _nba_games())
    rows = nba.simulate(st, 600, seed=5, home_win_rate=0.58)
    assert len(rows) == 30
    assert abs(sum(r["make_playoffs"] for r in rows) - 16) < 30 * 5e-5 + 1e-6, "exactly 16 teams reach the bracket after the play-in"
    assert abs(sum(r["win_champion"] for r in rows) - 1) < 30 * 5e-5 + 1e-6
    for r in rows:
        assert "win_division" not in r
        assert r["win_round1"] >= r["win_round2"] >= r["win_round3"] >= r["win_round4"] - 1e-9
        bracket_seeds = sum(v for k, v in r["seeds"].items() if int(k) <= 8)
        assert abs(bracket_seeds - r["make_playoffs"]) < 8e-4, r["abbr"]
        assert r["win_round1"] <= r["make_playoffs"] + 1e-9
    assert abs(sum(r["top_seed"] for r in rows) - 2) < 30 * 5e-5 + 1e-6
    doc = C.payload(sport_label="nba", season=2026, sims=600, model_version="x", method="m", home_win_rate=0.58, teams=rows)
    assert C.validate(doc, 30, 16) == []


def test_nba_clinched_and_eliminated():
    st = nba.State(_nba_rows(strong=0), _nba_games(left=2))
    rows = {r["abbr"]: r for r in nba.simulate(st, 400, seed=6, home_win_rate=0.58)}
    assert rows["B00"]["make_playoffs"] == 1.0 and rows["B00"]["top_seed"] == 1.0
    assert rows["B29"]["make_playoffs"] == 0.0 and rows["B29"]["win_champion"] == 0.0


def test_nba_parsers():
    standings = {"children": [{"name": "Eastern Conference", "standings": {"entries": [
        {"team": {"id": "1", "abbreviation": "AAA", "displayName": "Alpha"},
         "stats": [{"name": "wins", "value": 7}, {"name": "losses", "value": 3}, {"name": "pointsFor", "value": 1100},
                   {"name": "pointsAgainst", "value": 1000}]}]}}]}
    r = nba.parse_standings(standings)[0]
    assert (r["abbr"], r["conference"], r["w"], r["l"], r["team_id"]) == ("AAA", "East", 7, 3, 1)
    sb = {"events": [{"id": "9", "date": "2026-11-01T00:00Z", "season": {"type": 2}, "competitions": [{
        "status": {"type": {"completed": True}},
        "competitors": [{"homeAway": "home", "team": {"abbreviation": "AAA"}, "score": "101"},
                        {"homeAway": "away", "team": {"abbreviation": "BBB"}, "score": "99"}]}]},
        {"id": "10", "date": "2026-11-02T00:00Z", "season": {"type": 2}, "competitions": [{
            "status": {"type": {"completed": False}},
            "competitors": [{"homeAway": "home", "team": {"abbreviation": "BBB"}, "score": "0"},
                            {"homeAway": "away", "team": {"abbreviation": "AAA"}, "score": "0"}]}]}]}
    g = nba.parse_events(sb)
    assert g[0]["hs"] == 101 and g[0]["as"] == 99 and g[1]["hs"] is None


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                failed += 1; print("FAIL", name, "->", type(e).__name__, e)
    print("\nall ok" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
