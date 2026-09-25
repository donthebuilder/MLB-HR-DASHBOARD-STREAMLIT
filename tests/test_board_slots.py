"""TOP/HR slots read the board order (2026-09-25).

Measured on 21 pregame nights: the game's #1 by board_score homered 23.0%
vs 16.7% for the ISO-led power rank; board #2 as HR 16.7% vs 10.7%. So
_top_and_hr_slots ranks both slots on board_score (hr_score tiebreak) when
the slate carries one, and falls back to the old rule when it does not.
Run as a script (prints N/N checks) or under pytest.
"""
import dataclasses
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import bots.mlb_dashboard as md  # noqa: E402


def mk(**over) -> md.HitterRecord:
    kw = {}
    for f in dataclasses.fields(md.HitterRecord):
        if f.default is not dataclasses.MISSING:
            kw[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[attr-defined]
            kw[f.name] = f.default_factory()  # type: ignore[misc]
        else:
            kw[f.name] = 0
    kw.update(over)
    return md.HitterRecord(**kw)


CHECKS = []


def check(cond, msg):
    CHECKS.append(bool(cond))
    if not cond:
        print("FAIL", msg)


def test_board_slots():
    # board_score present: TOP is the game's highest board_score, HR the next,
    # even when hr_score and ISO would pick someone else.
    a = mk(player_id=1, name="Board #1", game_pk=9, season_pa=500, season_iso=0.150, hr_score=40.0, board_score=97.0, last5_hr=0)
    b = mk(player_id=2, name="Board #2", game_pk=9, season_pa=500, season_iso=0.140, hr_score=45.0, board_score=90.0, last5_hr=0)
    c = mk(player_id=3, name="Old TOP", game_pk=9, season_pa=500, season_iso=0.300, hr_score=66.0, board_score=60.0, last5_hr=3)
    top, hr = md._top_and_hr_slots([c, b, a])
    check(top.player_id == 1, "TOP should be the board #1")
    check(hr.player_id == 2, "HR should be the board #2")
    check(top.player_id != hr.player_id, "TOP and HR are two different men")
    # tie on board_score breaks on hr_score
    d = mk(player_id=4, name="Tie hi hr", game_pk=9, season_pa=500, hr_score=70.0, board_score=97.0)
    top2, _ = md._top_and_hr_slots([a, d, b])
    check(top2.player_id == 4, "tie on board_score breaks on hr_score")
    # a thin-PA man never takes the slot while a PA-eligible pool exists
    e = mk(player_id=5, name="15 PA fluke", game_pk=9, season_pa=8, hr_score=99.0, board_score=99.0)
    top3, hr3 = md._top_and_hr_slots([e, a, b])
    check(top3.player_id == 1 and hr3.player_id == 2, "PA tier still gates the board slots")
    # no board_score anywhere: the old rule stands (ISO-led power rank picks c)
    a0 = dataclasses.replace(a, board_score=0.0); b0 = dataclasses.replace(b, board_score=0.0); c0 = dataclasses.replace(c, board_score=0.0)
    top4, hr4 = md._top_and_hr_slots([a0, b0, c0])
    check(top4.player_id == 3, "without board_score the ISO-led rule still picks TOP")
    check(hr4.player_id == 2, "without board_score HR is best hr_score excluding TOP")
    assert all(CHECKS)


if __name__ == "__main__":
    test_board_slots()
    print(f"{sum(CHECKS)}/{len(CHECKS)} checks passed")
    sys.exit(0 if all(CHECKS) else 1)
