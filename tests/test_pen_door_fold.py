"""PEN DOOR FOLD (PEN_DOOR_FOLD_ON, 2026-10-16). ALL DATA HERE IS TEST DATA: players are "Test ...", clubs TST / EXA,
ids are made up, get_json and every Discord call are replaced by recorders. Nothing is fetched and nothing is sent.

Run: PYTHONPATH=. python3 tests/test_pen_door_fold.py
"""
import datetime as dt
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

from bots import pen_door_watch as pdw
# The SAME module objects the watcher uses. With bots/ on the path (how production runs it: python bots/pen_door_watch.py),
# pen_door_watch imports pen_door_fold as a top-level module; a separate `from bots import pen_door_fold` would be a second
# copy, and patches on it would never reach the watcher (2026-10-20).
words = pdw.words
fold = pdw.fold

HOOK = "https://discord.com/api/webhooks/111/TEST-TOKEN"
OWN = "https://discord.com/api/webhooks/222/TEST-OWN"


class FakeResp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b


class Recorder:
    """An opener that records requests; `fail_patch` makes an edit answer 404 (message deleted)."""
    def __init__(self, fail_patch=False):
        self.calls = []
        self.fail_patch = fail_patch
        self.n = 0

    def __call__(self, req, timeout=15):
        self.calls.append((req.get_method(), req.full_url, json.loads(req.data.decode())))
        if req.get_method() == "PATCH" and self.fail_patch:
            raise urllib.error.HTTPError(req.full_url, 404, "gone", {}, io.BytesIO(b"{}"))
        self.n += 1
        return FakeResp({"id": f"msg{self.n}"})


PICKS = {101: [
    {"pid": 1, "name": "Test Alpha", "team": "TST", "role": "HRR"},
    {"pid": 2, "name": "Test Bravo", "team": "TST", "role": ""},      # rides for names: NOT called
    {"pid": 3, "name": "Test Charlie", "team": "EXA", "role": "HRR"},
]}


class Switch(unittest.TestCase):
    def test_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PEN_DOOR_FOLD_ON", None)
            self.assertFalse(fold.fold_on())
        with mock.patch.dict(os.environ, {"PEN_DOOR_FOLD_ON": "on"}):
            self.assertTrue(fold.fold_on())

    def test_own_webhook_unset_is_empty(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PEN_DOOR_OWN_WEBHOOK", None)
            self.assertEqual(fold.own_urls(), [])
        with mock.patch.dict(os.environ, {"PEN_DOOR_OWN_WEBHOOK": OWN}):
            self.assertEqual(fold.own_urls(), [OWN])


class Classify(unittest.TestCase):
    def test_pinch_hit_for_called_pick_by_ids(self):
        det = {"eventType": "offensive_substitution", "description": "Offensive Substitution: Pinch-hitter Test Zulu replaces Test Alpha."}
        ev = {"player": {"id": 9, "fullName": "Test Zulu"}, "replacedPlayer": {"id": 1, "fullName": "Test Alpha"}}
        c = fold.classify_substitution(det, ev, fold.called_only(PICKS[101]))
        self.assertEqual((c["kind"], c["label"], c["pick_out"]), ("pinch", "pinch-hits for", True))
        self.assertEqual((c["incoming"], c["outgoing"]), ("Test Zulu", "Test Alpha"))

    def test_description_fallback_and_not_called(self):
        det = {"eventType": "offensive_substitution", "description": "Offensive Substitution: Pinch-hitter Test Charlie replaces Test Yankee."}
        c = fold.classify_substitution(det, {}, fold.called_only(PICKS[101]))
        self.assertFalse(c["pick_out"])                      # the called pick came IN
        self.assertEqual(c["pick"]["pid"], 3)
        det2 = {"eventType": "offensive_substitution", "description": "Offensive Substitution: Pinch-hitter Test Zulu replaces Test Bravo."}
        self.assertIsNone(fold.classify_substitution(det2, {}, fold.called_only(PICKS[101])))   # Bravo is not called
        self.assertIsNone(fold.classify_substitution({"eventType": "pitching_substitution", "description": "x"}, {}, fold.called_only(PICKS[101])))

    def test_pregame_and_ingame_lines_are_different(self):
        pre = words.pregame_change_line("Test Alpha", "HRR", "TST")
        inn = words.ingame_change_line("pinch-hits for", "Test Zulu", "Test Alpha", "HRR", True, "bottom", 7)
        self.assertIn("Pregame lineup change", pre)
        self.assertNotIn("In-game", pre)
        self.assertIn("In-game change", inn)
        self.assertIn("Bottom 7th", inn)
        self.assertNotIn("Pregame", inn)

    def test_pitching_line_carries_new_pitcher_and_stats(self):
        stats = fold.pitcher_stats_text({"era": "3.45", "inningsPitched": "60.1", "strikeOuts": 70, "baseOnBalls": 20})
        self.assertEqual(stats, "3.45 ERA · 60.1 IP · 70 K · 20 BB")
        line = words.pitching_change_status_line("TST", "bottom", 7, "Test Reliever", "Test Starter", stats, ["Test Charlie"])
        self.assertIn("Test Reliever in for Test Starter", line)
        self.assertIn("3.45 ERA", line)
        self.assertEqual(fold.pitcher_stats_text(None), "")
        self.assertNotIn("()", words.pitching_change_status_line("TST", "top", 2, "Test Reliever", "", "", ["Test Charlie"]))


class EditInPlace(unittest.TestCase):
    def test_one_message_per_game_edited(self):
        st, rec = {}, Recorder()
        a = fold.publish_game_status(st, 101, "2026-10-16", "T", ["first"], "f", [HOOK], opener=rec)
        b = fold.publish_game_status(st, 101, "2026-10-16", "T", ["second"], "f", [HOOK], opener=rec)
        self.assertEqual((a, b), ((1, 0), (1, 0)))
        self.assertEqual([c[0] for c in rec.calls], ["POST", "PATCH"])
        self.assertTrue(rec.calls[0][1].endswith("?wait=true"))
        self.assertTrue(rec.calls[1][1].endswith("/messages/msg1"))
        desc = rec.calls[1][2]["embeds"][0]["description"]
        self.assertIn("first", desc)
        self.assertIn("second", desc)                       # the edit carries the whole game's lines
        self.assertNotIn("TEST-TOKEN", json.dumps(st))      # the state never stores the webhook secret

    def test_deleted_message_is_posted_again(self):
        st, rec = {}, Recorder()
        fold.publish_game_status(st, 101, "2026-10-16", "T", ["first"], "f", [HOOK], opener=rec)
        rec.fail_patch = True
        ok, bad = fold.publish_game_status(st, 101, "2026-10-16", "T", ["second"], "f", [HOOK], opener=rec)
        self.assertEqual((ok, bad), (1, 0))
        self.assertEqual([c[0] for c in rec.calls], ["POST", "PATCH", "POST"])

    def test_lines_capped_and_pruned(self):
        st, rec = {}, Recorder()
        for i in range(12):
            fold.publish_game_status(st, 101, "2026-10-01", "T", [f"line {i}"], "f", [HOOK], opener=rec)
        self.assertEqual(len(st["gs"]["101"]["lines"]), fold.MAX_LINES)
        fold.prune_state(st, "2026-10-16")
        self.assertEqual(st["gs"], {})


GAMES = [
    {"gamePk": 101, "_date": "2026-10-16", "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
     "teams": {"home": {"team": {"id": 1}}, "away": {"team": {"id": 2}}}},
    {"gamePk": 202, "_date": "2026-10-16", "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
     "teams": {"home": {"team": {"id": 1}}, "away": {"team": {"id": 2}}}},
]
ABBRS = {1: "TST", 2: "EXA"}


def fake_get_json(url):
    if "boxscore" in url:
        return {"teams": {"home": {"team": {"id": 1}, "battingOrder": [9, 2]}, "away": {"team": {"id": 2}, "battingOrder": [3]}}}
    return None


class Gather(unittest.TestCase):
    def test_pregame_change_only_for_called_pick(self):
        with mock.patch.object(pdw, "get_json", fake_get_json):
            out = pdw.gather_fold({}, GAMES, {**PICKS, 202: [{"pid": 2, "name": "Test Bravo", "team": "TST", "role": ""}]}, ABBRS)
        self.assertEqual(list(out), [101])                   # game 202 has no called pick: no post
        self.assertEqual(len(out[101]["lines"]), 1)          # Alpha (called, TST) is not in the TST order; Bravo is not called
        self.assertIn("Pregame lineup change", out[101]["lines"][0])
        self.assertIn("Test Alpha", out[101]["lines"][0])

    def test_delay_for_game_without_called_pick_is_silent(self):
        g = [{**GAMES[1], "status": {"abstractGameState": "Preview", "detailedState": "Delayed: RAIN"}}]
        with mock.patch.object(pdw, "get_json", fake_get_json):
            self.assertEqual(pdw.gather_fold({}, g, {202: [{"pid": 2, "name": "Test Bravo", "team": "TST", "role": ""}]}, ABBRS), {})

    def test_finals_wait_for_the_whole_night_and_post_once(self):
        fin = lambda pk, ab: {"gamePk": pk, "_date": "2026-10-16", "status": {"abstractGameState": ab, "detailedState": ab},  # noqa: E731
                              "teams": {"home": {"team": {"id": 1}, "score": 3}, "away": {"team": {"id": 2}, "score": 2}}}
        picks = {101: PICKS[101], 202: [{"pid": 3, "name": "Test Charlie", "team": "EXA", "role": "HRR"}]}
        box = {"teams": {"home": {"players": {}}, "away": {"players": {"ID3": {"stats": {"batting": {"hits": 2, "atBats": 4, "homeRuns": 1}}}}}}}
        with mock.patch.object(pdw, "get_json", lambda url: box):
            self.assertEqual(pdw.gather_finals({}, [fin(101, "Final"), fin(202, "Live")], picks, ABBRS), {})   # one game still on
            out = pdw.gather_finals({}, [fin(101, "Final"), fin(202, "Final")], picks, ABBRS)
            self.assertEqual(list(out), ["2026-10-16"])
            self.assertEqual(len(out["2026-10-16"]["lines"]), 2)                                             # both games, ONE post
            st = {"seen": {"finalnight:2026-10-16": "x"}}
            self.assertEqual(pdw.gather_finals(st, [fin(101, "Final"), fin(202, "Final")], picks, ABBRS), {})


def feed(with_bases=True, with_pc=True):
    now = dt.datetime.now(dt.UTC).isoformat()
    events = []
    if with_pc:
        events.append({"details": {"eventType": "pitching_substitution", "description": "Pitching Change: Old Text replaces Old Text."},
                       "startTime": now, "player": {"id": 77, "fullName": "Test Reliever"}, "replacedPlayer": {"id": 78, "fullName": "Test Starter"}})
    events.append({"details": {"eventType": "offensive_substitution", "description": "Offensive Substitution: Pinch-hitter Test Zulu replaces Test Charlie."},
                   "startTime": now, "player": {"id": 9, "fullName": "Test Zulu"}, "replacedPlayer": {"id": 3, "fullName": "Test Charlie"}})
    return {"gameData": {"teams": {"home": {"abbreviation": "TST"}, "away": {"abbreviation": "EXA"}}},
            "liveData": {"plays": {"allPlays": [{"about": {"halfInning": "top", "inning": 7}, "playEvents": events}],
                                   "currentPlay": {"matchup": {"batter": {"id": 3}}}},
                         "linescore": {"currentInning": 7, "isTopInning": True, "offense": {"first": {"id": 5}, "second": {"id": 6}}}}}


def live_get_json(url):
    if "/schedule" in url:
        return {"dates": [{"games": [{"gamePk": 101, "status": {"abstractGameState": "Live"}}]}]}
    if "/feed/live" in url:
        return feed()
    if "boxscore" in url:
        return {"teams": {"home": {"battingOrder": [1, 2]}, "away": {"battingOrder": [3]}}}
    if "/people/77" in url:
        return {"people": [{"id": 77, "stats": [{"splits": [{"stat": {"era": "2.10", "inningsPitched": "40.0", "strikeOuts": 55, "baseOnBalls": 10}}]}]}]}
    return None


class LiveFold(unittest.TestCase):
    def run_sweep(self, own):
        rec = Recorder()
        env = {"DISCORD_PENDOOR_WEBHOOK": HOOK, "PEN_DOOR_OWN_WEBHOOK": own}
        st = {}
        with mock.patch.dict(os.environ, env), mock.patch.object(pdw, "get_json", live_get_json), \
                mock.patch.object(pdw, "slate_picks", lambda: PICKS), mock.patch.object(pdw, "MAX_LIVE_SWEEPS", 1), \
                mock.patch.object(fold, "_call", lambda m, u, p, o=None, s=None, max_retries=2: rec(type("R", (), {"get_method": lambda self: m, "full_url": u, "data": json.dumps(p).encode()})()) and {"id": "m1"}):
            pdw._PITCHER_MEMO.clear()
            sent, failed = pdw.live_sweep(st, fold_mode=True)
        return rec.calls, st, (sent, failed)

    def test_one_game_status_with_new_pitcher_and_pinch_hit(self):
        calls, st, counts = self.run_sweep(own="")
        game_calls = [c for c in calls if HOOK.split("/")[-2] in c[1]]
        self.assertEqual(len(game_calls), 1)                 # ONE message for the game, not one per event
        desc = game_calls[0][2]["embeds"][0]["description"]
        self.assertIn("Test Reliever in for Test Starter", desc)      # the NEW pitcher, not the feed's stale sentence
        self.assertNotIn("Old Text", desc)
        self.assertIn("2.10 ERA", desc)                               # his stats, read now
        self.assertIn("In-game change", desc)                         # the pinch-hit is labelled as in-game
        self.assertIn("pinch-hits for", desc)
        self.assertEqual(counts, (1, 0))

    def test_pick_at_plate_goes_only_to_own_webhook_or_nowhere(self):
        calls, _, _ = self.run_sweep(own="")
        self.assertFalse([c for c in calls if "222" in c[1]])
        self.assertFalse([c for c in calls if "up with" in json.dumps(c[2])])    # unset: not sent anywhere
        calls, _, _ = self.run_sweep(own=OWN)
        plate = [c for c in calls if "up with" in json.dumps(c[2])]
        self.assertEqual(len(plate), 1)
        self.assertIn("222", plate[0][1])                                         # the own server, never the pen-door channel


class OffIntact(unittest.TestCase):
    def test_old_main_path_chosen_when_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PEN_DOOR_FOLD_ON", None)
            with mock.patch.object(pdw, "main_fold", side_effect=AssertionError("fold ran while off")), \
                    mock.patch.object(pdw, "load_state", return_value={}), mock.patch.object(pdw, "slate_picks", return_value={}), \
                    mock.patch.object(pdw, "team_abbrs", return_value={}), mock.patch.object(pdw, "get_json", return_value=None), \
                    mock.patch.object(pdw, "live_sweep", return_value=(0, 0)), mock.patch.object(pdw, "save_state"):
                self.assertEqual(pdw.main(), 0)


if __name__ == "__main__":
    unittest.main()
