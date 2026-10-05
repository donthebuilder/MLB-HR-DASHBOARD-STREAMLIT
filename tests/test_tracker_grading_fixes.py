"""Bot-audit 10-05 fixes in the results tracker: WIN bars = the digest's bars,
postponed = void (not a miss), doubleheader keys, board posts once / never after
first pitch, shared Discord helper. Run: PYTHONPATH=. python3 tests/test_tracker_grading_fixes.py"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))
import live_results_tracker as t  # noqa: E402
import discord_post  # noqa: E402
import mlb_record_repair as rr  # noqa: E402

# ---- win bars (same as the digest's bar_cleared)
base = {"got_hr": 0, "got_base_hit": 1, "actual_hits": 1, "actual_runs": 0, "actual_rbi": 0, "actual_tb": 1, "actual_ab": 4}
assert not t.win_bar_cleared({**base, "pick_type": "CONTACT"})           # a single is not 2+ TB
assert t.win_bar_cleared({**base, "pick_type": "CONTACT", "actual_tb": 2})
assert not t.win_bar_cleared({**base, "pick_type": "HRR"})               # H+R+RBI = 1
assert t.win_bar_cleared({**base, "pick_type": "HRR", "actual_runs": 1})
assert not t.win_bar_cleared({**base, "pick_type": "TOP"})               # TOP needs a homer
assert not t.win_bar_cleared({**base, "pick_type": "TOP15"})
assert t.win_bar_cleared({**base, "pick_type": "HIT"})
assert t.win_bar_cleared({**base, "pick_type": "TOP", "got_hr": 1})
assert rr._grade_for_row({**base, "pick_type": "CONTACT"}) == "LOSS"
assert rr._grade_for_row({**base, "pick_type": "HIT"}) == "WIN"
assert rr._grade_for_row({**base, "pick_type": "HIT", "void": True}) == "DNP"

# ---- postponed = void
assert t.game_is_void({"detailed_state": "Postponed", "abstract_state": "Final"})
assert t.game_is_void({"detailed_state": "Cancelled"})
assert not t.game_is_void({"detailed_state": "Final"})
assert not t.game_is_void({"detailed_state": "Suspended"})

def slot(pt, hr, void=False, gp=1, pid=1):
    return {"pick_type": pt, "got_hr": hr, "got_base_hit": hr, "tb_2_plus": 0, "tb_3_plus": 0, "got_xbh": 0,
            "hrr_2_plus": 0, "hrr_3_plus": 0, "game_pk": gp, "player_id": pid, "name": f"P{pid}", "team": "AAA", "is_final": 1,
            "rank": 1, "designed_hit": hr, "void": void}
txt = t.build_summary_text("2026-10-05", [slot("HR", 1, pid=1), slot("HR", 0, pid=2), slot("HR", 0, void=True, pid=3, gp=2)],
                           [], {"pairs": [], "pools": []} if False else {}, None, None)
assert "HR Picks: 1/2 (50.0%)" in txt, txt
assert "Void (postponed/cancelled, not counted): 1" in txt

# ---- doubleheader keys
g1, g2 = slot("HR", 1, gp=10, pid=5), slot("HR", 0, gp=11, pid=5)
assert len(t.merge_homer_entries([g1, g2])) == 1                 # only the leg he homered in
g2h = slot("HRR", 1, gp=11, pid=5)
assert len(t.merge_homer_entries([g1, g2h])) == 2                # homered in both legs = two entries
u = t.build_unique_player_hr_report([g1, g2])
assert u["unique_players_tracked"] == 2 and u["unique_players_with_hr"] == 1

orig = t.get_all_homers_from_game
t.get_all_homers_from_game = lambda feed: [{"player_id": 5, "name": "P5", "hr": 1}]
try:
    rows = [{"player_id": 5, "game_pk": 11, "hr_score": 50.0}]       # on the sheet for game 2 only
    rep = t.build_hr_capture_report(rows, {10: {}, 11: {}}, {})
    assert rep["total_hrs_on_slate"] == 2
    assert rep["caught_hrs_on_sheet"] == 1 and rep["missed_hrs_not_on_sheet"] == 1, rep
finally:
    t.get_all_homers_from_game = orig

# ---- board: once, never after first pitch, memory survives a fresh runner
now = dt.datetime(2026, 10, 5, 18, 0, tzinfo=dt.timezone.utc)
future = [{"game_time": "2026-10-05T23:05:00Z"}]
past = [{"game_time": "2026-10-05T17:05:00Z"}, {"game_time": "2026-10-05T23:05:00Z"}]
assert not t._board_started(future, now) and t._board_started(past, now)
posted = []
t._load_board_state = lambda: {}
t._save_board_state = lambda st: None
t._post_discord_payload = lambda p: (posted.append(p) or (1, 0))
t.pregame_board_sections = lambda slots, proven: [("TOP", ["x"])]
t._proven_b2b_ids = lambda d: set()
assert t.post_pregame_board(past, "2026-10-05", None, now) is None and not posted        # after first pitch
assert t.post_pregame_board(future, "2026-10-05", "2026-10-05", now) == "2026-10-05" and not posted   # remembered
assert t.post_pregame_board(future, "2026-10-05", None, now) == "2026-10-05" and len(posted) == 1

# ---- shared discord helper
assert discord_post.webhook_urls("https://a/1, https://b/2\nhttps://c/3  junk") == ["https://a/1", "https://b/2", "https://c/3"]
print("test_tracker_grading_fixes: ok")
