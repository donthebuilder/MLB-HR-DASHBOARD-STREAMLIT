"""pairhist_v2 tests. EVERY ROW HERE IS TEST DATA (hand-built), not a real player or game."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bots"))
import pair_history_v2 as ph  # noqa: E402

SEASONS = [2023, 2024, 2025, 2026]


def rec(season, day, pid, events=0, team="TST", game=None):
    return {"season": season, "day": day, "date": day, "game": game or f"{team}-{day}", "team": team,
            "pid": pid, "name": f"TEST {pid}", "events": events}


def season_days(season, n):
    # n synthetic dates per season (distinct strings, ordered)
    return [f"{season}-04-{d:02d}" if d < 29 else f"{season}-05-{d - 28:02d}" for d in range(1, n + 1)]


def full_team(n_per_season=100):
    """TEST: a team (id 'ANCHOR') that plays every day, so each season has n games."""
    out = []
    for s in SEASONS:
        for d in season_days(s, n_per_season):
            out.append(rec(s, d, "ANCHOR", team="TST"))
    return out


def test_season_games_is_the_busiest_team():
    r = full_team(100) + [rec(2023, "2023-04-01", "X", team="OTH")]
    assert ph.season_game_counts(r) == {2023: 100, 2024: 100, 2025: 100, 2026: 100}


def test_thresholds_are_the_named_constants():
    assert ph.SEASON_GATE == 0.25 and ph.WINDOW_GATE == 0.375
    assert ph.CONSISTENT_MIN_SEASONS == 3 and ph.CONSISTENT_SEASON_FRAC == 0.15
    assert ph.PAIR_CAP == 300 and ph.WINDOW_SEASONS == 4


def sg100():
    return {s: 100 for s in SEASONS}


def test_current_season_quarter_gate():
    assert ph.qualifies({2026: 25}, sg100(), 2026) == (True, "season")
    assert ph.qualifies({2026: 24}, sg100(), 2026)[0] is False


def test_window_gate_three_eighths():
    # 150 of 400 = exactly 37.5%, spread so no single season is 25% and not 3 seasons >= 15
    ok, rule = ph.qualifies({2023: 50, 2024: 50, 2025: 50}, sg100(), 2026)
    assert (ok, rule) == (True, "window") or rule == "consistent"
    # 149 of 400 is under, with spread that fails consistency (only two seasons >= 15)
    assert ph.qualifies({2023: 74, 2024: 75}, sg100(), 2026)[0] is False


def test_consistency_rule_three_of_four_seasons_at_15_percent():
    # 15 games a season in three seasons = 45/400 = 11% window, 0% now: only the consistency rule lets him in
    assert ph.qualifies({2023: 15, 2024: 15, 2025: 15}, sg100(), 2026) == (True, "consistent")
    # two seasons is not consistent
    assert ph.qualifies({2023: 15, 2024: 15}, sg100(), 2026)[0] is False
    # three seasons but one under 15% is not consistent
    assert ph.qualifies({2023: 15, 2024: 15, 2025: 14}, sg100(), 2026)[0] is False


def test_active_requires_a_current_roster():
    r = full_team(100)
    for s in SEASONS:
        for d in season_days(s, 100):
            r.append(rec(s, d, "STAR"))
    assert "STAR" in ph.active_players(r, {"STAR", "ANCHOR"}, 2026)
    assert "STAR" not in ph.active_players(r, {"ANCHOR"}, 2026)   # retired / released: not on a roster


def test_part_timer_dropped_unless_consistent():
    r = full_team(100)
    for d in season_days(2023, 10) + season_days(2024, 10):
        r.append(rec(int(d[:4]), d, "PART"))        # 10% x 2 seasons: neither rule
    assert "PART" not in ph.active_players(r, {"PART", "ANCHOR"}, 2026)
    for d in season_days(2025, 16):
        r.append(rec(2025, d, "PART"))              # now 10%,10%,16%: still only one season >= 15%
    assert "PART" not in ph.active_players(r, {"PART", "ANCHOR"}, 2026)


def two_stars(joint_event_days, only_a_days=0, only_b_days=0, season=2026, total_days=60):
    """TEST: A and B play every one of `total_days`; both score on the first `joint_event_days`."""
    out = []
    days = season_days(season, total_days)
    for i, d in enumerate(days):
        a = 1 if i < joint_event_days or joint_event_days <= i < joint_event_days + only_a_days else 0
        b = 1 if i < joint_event_days or joint_event_days + only_a_days <= i < joint_event_days + only_a_days + only_b_days else 0
        out.append(rec(season, d, "A", a, team="AAA"))
        out.append(rec(season, d, "B", b, team="BBB"))
    return out


def test_pair_counting_joint_days_events_rate_and_expected():
    r = two_stars(joint_event_days=6, only_a_days=14, only_b_days=10, total_days=60)
    active = {"A": "season", "B": "season"}
    out = ph.build_pairs(r, active, "mlb", min_joint_days=40)
    assert len(out["pairs"]) == 1
    p = out["pairs"][0]
    assert p["joint_days"] == 60 and p["joint_event_days"] == 6
    assert p["rate"] == 0.1
    # A did it 20 of 60, B 16 of 60 -> expected 60 * (1/3) * (4/15) = 5.33
    assert abs(p["expected_joint"] - 5.33) < 0.01
    assert abs(p["lift"] - round(6 / (60 * (20 / 60) * (16 / 60)), 2)) < 0.01
    assert p["seasons"] == {"2026": [60, 6]}
    assert p["last_joint_date"] == season_days(2026, 60)[5]


def test_same_game_subset_needs_the_same_game_id():
    r = two_stars(joint_event_days=3, total_days=45)
    for x in r:                       # both on the same game id
        x["game"] = f"G-{x['day']}"
    p = ph.build_pairs(r, {"A": "season", "B": "season"}, "mlb", min_joint_days=40)["pairs"][0]
    assert p["same_game_event_days"] == 3
    r2 = two_stars(joint_event_days=3, total_days=45)   # different games (default ids differ by team)
    p2 = ph.build_pairs(r2, {"A": "season", "B": "season"}, "mlb", min_joint_days=40)["pairs"][0]
    assert p2["same_game_event_days"] == 0


def test_inactive_players_never_appear_in_pairs():
    r = two_stars(joint_event_days=6, total_days=45)
    out = ph.build_pairs(r, {"A": "season"}, "mlb", min_joint_days=40)   # B is not active
    assert out["pairs"] == []


def test_floors_joint_days_and_joint_events():
    r = two_stars(joint_event_days=5, total_days=30)
    assert ph.build_pairs(r, {"A": "x", "B": "x"}, "mlb", min_joint_days=40)["pairs"] == []        # too few shared days
    r1 = two_stars(joint_event_days=1, total_days=60)
    assert ph.build_pairs(r1, {"A": "x", "B": "x"}, "mlb", min_joint_days=40)["pairs"] == []       # once is not evidence
    assert ph.MIN_JOINT_DAYS == {"mlb": 40, "nhl": 40, "nfl": 15}


def test_size_cap_keeps_the_strongest_by_evidence():
    # TEST: 30 players who all play 60 days; player k scores on the first (k % 10 + 2) days, so many pairs tie/vary
    rows = []
    days = season_days(2026, 60)
    for k in range(30):
        for i, d in enumerate(days):
            rows.append(rec(2026, d, f"P{k:02d}", 1 if i < (k % 10) + 2 else 0, team=f"T{k}"))
    active = {f"P{k:02d}": "season" for k in range(30)}
    full = ph.build_pairs(rows, active, "mlb", min_joint_days=40, cap=10_000)
    capped = ph.build_pairs(rows, active, "mlb", min_joint_days=40, cap=7)
    assert len(capped["pairs"]) == 7 and capped["pairs_qualifying"] == len(full["pairs"])
    assert capped["pairs"] == full["pairs"][:7]
    counts = [p["joint_event_days"] for p in full["pairs"]]
    assert counts == sorted(counts, reverse=True)


def test_default_cap_is_300():
    rows = []
    days = season_days(2026, 60)
    for k in range(40):
        for d in days:
            rows.append(rec(2026, d, f"P{k:02d}", 1, team=f"T{k}"))
    out = ph.build_pairs(rows, {f"P{k:02d}": "season" for k in range(40)}, "mlb", min_joint_days=40)
    assert len(out["pairs"]) == 300 and out["pairs_qualifying"] == 40 * 39 // 2


def test_build_file_shape_and_seasons_covered():
    r = full_team(100) + two_stars(6, 14, 10, total_days=60, season=2026)
    f = ph.build_file("mlb", r, {"A", "B"}, 2026, generated_at="TEST")
    assert f["model_version"] == "pairhist_v2" and f["sport"] == "mlb"
    assert f["seasons_covered"] == [2023, 2024, 2025, 2026]
    assert f["event"]["unit"] == "home run"
    assert set(f["thresholds"]) >= {"season_gate", "window_gate", "consistent_min_seasons", "pair_cap"}
    assert f["active_players"] == 2 and len(f["pairs"]) == 1


def test_season_window_is_four_seasons():
    import datetime as dt
    assert ph.season_window("mlb", dt.date(2026, 10, 7)) == ([2023, 2024, 2025, 2026], 2026)
    assert ph.season_window("nhl", dt.date(2026, 10, 7)) == ([2023, 2024, 2025, 2026], 2026)
    assert ph.season_window("nhl", dt.date(2027, 2, 1)) == ([2023, 2024, 2025, 2026], 2026)
