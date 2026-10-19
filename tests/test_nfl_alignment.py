"""bots/nfl/nfl_alignment.py: club-level run front / personnel / formation. No per-man data.

TEST DATA, MADE UP: clubs AAA (offence) and BBB (defence), 60 plays each.

Run: python3 -m pytest tests/test_nfl_alignment.py -v
"""
import os
import sys

import polars as pl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import nfl_alignment as A  # noqa: E402

P11 = "1 C, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR"
P12 = "1 C, 2 G, 1 QB, 1 RB, 2 T, 2 TE, 2 WR"
P21 = "1 C, 1 FB, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 2 WR"


def plays():
    rows = []
    for i in range(60):
        rows.append(dict(posteam="AAA", defteam="BBB", rush_attempt=1, pass_attempt=0,
                         defenders_in_box=6 if i < 30 else 7 if i < 50 else 8,
                         offense_personnel=[P11, P12, P21][i % 3],
                         offense_formation="SHOTGUN" if i % 2 else "UNDER CENTER"))
    # a special-teams row: box 0, no QB in the personnel -- counted nowhere
    rows.append(dict(posteam="AAA", defteam="BBB", rush_attempt=1, pass_attempt=0, defenders_in_box=0,
                     offense_personnel="2 CB, 1 FB, 1 FS", offense_formation=None))
    return pl.DataFrame(rows)


def test_skill_personnel_counts_fb_as_a_back():
    assert A._skill(P11) == "11" and A._skill(P12) == "12" and A._skill(P21) == "21"
    assert A._skill("2 CB, 1 FB, 1 FS") is None and A._skill(None) is None


def test_run_front_shares():
    f = A.club_tables(plays())["run_front"]["BBB"]
    assert f["plays"] == 60
    assert (f["box_6_or_fewer"], f["box_7"], f["box_8_plus"]) == (0.5, 0.333, 0.167)


def test_personnel_and_formation():
    t = A.club_tables(plays())
    p = t["personnel"]["AAA"]
    assert p["plays"] == 60 and p["11"] == 0.333 and p["12"] == 0.333 and p["21"] == 0.333
    assert abs(p["other"]) <= 0.002   # rounding only: three equal thirds
    assert t["formation"]["AAA"]["shotgun"] == 0.5


def test_thin_clubs_get_nothing():
    assert A.club_tables(plays().head(10))["run_front"] == {}
