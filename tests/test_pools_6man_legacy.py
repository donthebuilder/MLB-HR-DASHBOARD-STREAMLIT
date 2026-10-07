"""Old 6-man pools, back beside the 3-man pools (2026-10-07).

ALL DATA HERE IS TEST DATA: synthetic hitters named "TEST Hitter N" with
generated numbers. Nothing below is a real player or a real stat.

Run: python -m unittest tests.test_pools_6man_legacy
"""
import dataclasses
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bots import mlb_dashboard as m
from bots import live_results_tracker as t


def test_hitter(i):
    """A synthetic HitterRecord. Required fields zero/empty; scores spread so
    every bucket (core/hybrid/hrr/mid/wtf) has members."""
    kw = {}
    for f in dataclasses.fields(m.HitterRecord):
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            ann = str(f.type)
            kw[f.name] = "" if "str" in ann else (False if "bool" in ann else 0)
    kw.update(player_id=1000 + i, name=f"TEST Hitter {i}", team=f"T{i % 15}",
              opponent=f"T{(i + 1) % 15}", game_pk=500 + i // 4, game_time="2099-01-01T23:00:00Z",
              lineup_confirmed=True, lineup_spot=1 + i % 9, pitcher_id=7000 + i // 4)
    r = m.HitterRecord(**kw)
    for k, v in dict(hr_score=60 - i * 0.4, hrr_score=75 - i * 0.3, hrw_score=70 - (i % 17),
                     season_iso=0.25 - i * 0.001, season_hr=30 - i // 4, season_pa=500,
                     recent_ideal_hr_contact=0.20 - i * 0.001, recent_barrel_rate=0.12,
                     last5_xbh=i % 3, last5_hr=i % 2, hr_per_pa=0.05).items():
        try:
            setattr(r, k, v)
        except Exception:
            pass
    return r


def slate(n=70):
    return [test_hitter(i) for i in range(n)]


def ids(pool):
    return [r.player_id for r in pool]


class Legacy6ManRecipe(unittest.TestCase):
    def setUp(self):
        self.rows = slate()
        self.cand = m.top_pool_candidates(self.rows, 62)
        self.buckets = m.classify_pool_buckets(self.rows)
        self.tags = m.game_pick_type_map(self.rows)

    def build(self, seed=None):
        return m.build_legacy_6man_pools(self.cand, self.buckets, seed or {}, self.tags, set())

    def test_four_pools_six_distinct_names_each(self):
        pools = self.build()
        self.assertEqual([n for n, _ in pools],
                         ["Pool A — Strongest", "Pool B — Balanced", "Pool C — Mid / Var", "Pool D — Contrarian"])
        for name, p in pools:
            self.assertEqual(len(p), 6, name)
            self.assertEqual(len(set(ids(p))), 6, name)

    def test_no_name_in_two_legacy_pools(self):
        allids = [i for _, p in self.build() for i in ids(p)]
        self.assertEqual(len(allids), len(set(allids)))

    def test_recipes_are_the_6cd816e5_parent_recipes(self):
        got = {n: r for n, r, _, _ in m.LEGACY_6MAN_RECIPES}
        self.assertEqual(got["Pool A — Strongest"], {"core": 1, "hrr": 1, "hybrid": 3, "wtf": 1})
        self.assertEqual(got["Pool B — Balanced"], {"hybrid": 4, "mid": 1, "wtf": 1})
        self.assertEqual(got["Pool C — Mid / Var"], {"mid": 2, "wtf": 4})
        self.assertEqual(got["Pool D — Contrarian"], {"wtf": 4, "mid": 2})
        for n, r, _, _ in m.LEGACY_6MAN_RECIPES:
            self.assertEqual(sum(r.values()), 6)

    def test_seed_exposure_is_not_mutated(self):
        seed = {1000: 99}
        self.build(seed)
        self.assertEqual(seed, {1000: 99})

    def test_deterministic(self):
        self.assertEqual([ids(p) for _, p in self.build()], [ids(p) for _, p in self.build()])


class ExistingKeysUnchanged(unittest.TestCase):
    def test_payload_has_new_key_and_old_keys_identical_with_or_without_it(self):
        m.LAST_ALT_USED_IDS = set()
        m.LAST_TOP30_BOARD = []
        _, a = m._build_pair_sections(slate())
        for k in ("recommended_pairs", "pools_4man", "pools_3man", "pools_6man", "pools_6man_legacy"):
            self.assertIn(k, a)
        self.assertEqual(a["pools_6man"], [])
        self.assertEqual([len(p["players"]) for p in a["pools_4man"]], [4] * 4)
        self.assertEqual([len(p["players"]) for p in a["pools_3man"]], [3] * 4)
        self.assertEqual([len(p["players"]) for p in a["pools_6man_legacy"]], [6] * 4)
        for p in a["pools_6man_legacy"]:
            self.assertEqual(p["model_version"], m.LEGACY_6MAN_MODEL_VERSION)
            self.assertNotIn("model_version", a["pools_3man"][0])
        # same input twice -> the old sections are byte-identical (the legacy
        # builder uses a private exposure copy, so it can't move them)
        m.LAST_ALT_USED_IDS = set()
        _, b = m._build_pair_sections(slate())
        for k in ("recommended_pairs", "pools_4man", "pools_3man"):
            self.assertEqual(json.dumps(a[k], sort_keys=True), json.dumps(b[k], sort_keys=True))

    def test_legacy_does_not_feed_used_ids(self):
        m.LAST_ALT_USED_IDS = set()
        m.LAST_TOP30_BOARD = []
        _, a = m._build_pair_sections(slate())
        used = set(m.LAST_HR_SECTION_USED_IDS)
        three = {p["player_id"] for sec in ("pools_3man", "pools_4man") for pl in a[sec] for p in pl["players"]}
        legacy = {p["player_id"] for pl in a["pools_6man_legacy"] for p in pl["players"]}
        # legacy names that no existing section used must NOT be in used-ids
        for pid in legacy - three - {p["player_id"] for pr in a["recommended_pairs"] for p in pr["players"]}:
            self.assertNotIn(pid, used)


class LockedPregameOnly(unittest.TestCase):
    def _lock(self, tmp, first_pitch_offset_min, second_roster):
        import importlib
        import bots.pick_lock as pl
        importlib.reload(pl)
        cur = Path(tmp) / "current"
        cur.mkdir(parents=True, exist_ok=True)
        pl.PUBLIC, pl.CURRENT = Path(tmp), cur
        gt = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=first_pitch_offset_min)
              ).isoformat().replace("+00:00", "Z")
        rows = [{"player_id": i, "name": f"TEST Hitter {i}", "game_pk": 100, "game_time": gt,
                 "team": "AAA", "opponent": "BBB", "hr_score": 50.0} for i in range(1, 15)]
        (cur / "today.json").write_text(json.dumps({"players": rows}))

        def blob(pids):
            return {"name": "Pool A — Strongest", "size": 6, "model_version": "pools_6man_legacy_v1",
                    "players": [{"player_id": p, "name": f"TEST Hitter {p}", "game_pk": 100} for p in pids]}

        def run(pids):
            (cur / "pair_builder_latest.json").write_text(json.dumps({"pools_6man_legacy": [blob(pids)]}))
            def local(date):
                p = cur / "pick_lock.json"
                if p.exists():
                    j = json.loads(p.read_text())
                    if j.get("date") == date:
                        return j
                return {"date": date, "games": {}, "tickets": {}, "rejected": []}
            pl.fetch_lock = local
            argv = sys.argv
            sys.argv = ["pick_lock.py", "--apply"]
            try:
                pl.main()
            finally:
                sys.argv = argv
            return json.loads((cur / "pair_builder_latest.json").read_text())["pools_6man_legacy"][0]
        run([1, 2, 3, 4, 5, 6])
        return run(second_roster)

    def test_before_first_pitch_roster_may_change(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._lock(d, 120, [7, 8, 9, 10, 11, 12])
            self.assertEqual([p["player_id"] for p in out["players"]], [7, 8, 9, 10, 11, 12])

    def test_after_first_pitch_roster_is_frozen_and_version_kept(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._lock(d, -30, [7, 8, 9, 10, 11, 12])
            self.assertEqual([p["player_id"] for p in out["players"]], [1, 2, 3, 4, 5, 6])
            self.assertEqual(out["model_version"], "pools_6man_legacy_v1")
            self.assertTrue(out.get("locked"))


class GradedLikeOtherPools(unittest.TestCase):
    def test_grading(self):
        # TEST payload: one legacy pool of 6 TEST players; 3 homer, 1 had no AB (void)
        pb = {"date": "2099-01-01", "recommended_pairs": [], "pools_6man_legacy": [{
            "name": "Pool A — Strongest",
            "players": [{"player_id": i, "name": f"TEST Hitter {i}", "game_pk": 9} for i in range(1, 7)]}],
            "pools_3man": [{"name": "Pool A — Strongest", "players": [
                {"player_id": i, "name": f"TEST Hitter {i}", "game_pk": 9} for i in (1, 2, 3)]}],
            "pools_4man": []}
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "pair_builder_latest.json").write_text(json.dumps(pb))
            old = t.OUT_DIR
            t.OUT_DIR = Path(d)
            try:
                sections = t.load_pair_builder_sections("2099-01-01")
            finally:
                t.OUT_DIR = old
        labels = [p["label"] for p in sections["pools"]]
        self.assertIn("SIX-MAN LEGACY Pool A — Strongest", labels)
        self.assertIn("3-MAN Pool A — Strongest", labels)
        actual = {(9, 1): {"hr": 1, "ab": 4}, (9, 2): {"hr": 1, "ab": 4}, (9, 3): {"hr": 1, "ab": 3},
                  (9, 4): {"hr": 0, "ab": 4}, (9, 5): {"hr": 0, "ab": 0}, (9, 6): {"hr": 0, "ab": 4}}
        g = t.grade_pairs_pools(sections, actual)
        leg = g["pool6_legacy"]
        self.assertEqual(len(leg), 1)
        e = leg[0]
        self.assertEqual((e["hr_count"], e["total_count"]), (3, 5))   # TEST 5 voided
        self.assertEqual(e["void_names"], ["TEST Hitter 5"])
        self.assertEqual(e["primary"], 1)
        self.assertEqual(e["bar_label"], "2+ of 5")
        self.assertEqual(e["grade_3plus"], "hit")
        self.assertEqual(e["grade_perfect"], "miss")
        self.assertEqual(e["model_version"], t.LEGACY_6MAN_MODEL_VERSION)
        # existing totals unchanged: the 3-man pool is still in pool6, the legacy one is not
        self.assertEqual([p["label"] for p in g["pool6"]], ["3-MAN Pool A — Strongest"])
        self.assertEqual(g["pool4"], [])
        self.assertEqual(len(g["graded_pools"]), 2)

    def test_grader_constant_matches_builder_constant(self):
        self.assertEqual(t.LEGACY_6MAN_MODEL_VERSION, m.LEGACY_6MAN_MODEL_VERSION)


class OldRecipeHalves(unittest.TestCase):
    """pools_3man_legacy: each legacy 6-man split into X1 (first 3) / X2 (last 3). TEST data."""

    def payload(self):
        m.LAST_ALT_USED_IDS = set()
        m.LAST_TOP30_BOARD = []
        return m._build_pair_sections(slate())[1]

    def test_eight_disjoint_halves_cover_all_names_in_order(self):
        a = self.payload()
        halves, sixes = a["pools_3man_legacy"], a["pools_6man_legacy"]
        self.assertEqual([h["name"] for h in halves],
                         [f"Pool {c}{n}" for c in "ABCD" for n in "12"])
        for i, six in enumerate(sixes):
            h1, h2 = halves[2 * i], halves[2 * i + 1]
            self.assertEqual(h1["parent"], six["name"])
            self.assertEqual(h2["parent"], six["name"])
            p6 = [p["player_id"] for p in six["players"]]
            p1 = [p["player_id"] for p in h1["players"]]
            p2 = [p["player_id"] for p in h2["players"]]
            self.assertEqual((len(p1), len(p2)), (3, 3))
            self.assertFalse(set(p1) & set(p2))
            self.assertEqual(p1 + p2, p6)
            self.assertEqual(h1["size"], 3)
            self.assertEqual(h1["model_version"], "pools_6man_legacy_v1_half")

    def test_deterministic_and_existing_keys_unchanged(self):
        a, b = self.payload(), self.payload()
        for k in ("pools_3man_legacy", "pools_6man_legacy", "pools_3man", "pools_4man", "recommended_pairs"):
            self.assertEqual(json.dumps(a[k], sort_keys=True), json.dumps(b[k], sort_keys=True))
        self.assertEqual(a["pools_6man"], [])
        self.assertEqual([len(p["players"]) for p in a["pools_3man"]], [3] * 4)
        self.assertEqual([len(p["players"]) for p in a["pools_4man"]], [4] * 4)

    def test_locked_pregame_only(self):
        import importlib
        import bots.pick_lock as pl
        self.assertIn("pools_3man_legacy", pl.POOL_SECTIONS)
        for offset, want_frozen in ((120, False), (-30, True)):
            with tempfile.TemporaryDirectory() as d:
                importlib.reload(pl)
                cur = Path(d) / "current"
                cur.mkdir()
                pl.PUBLIC, pl.CURRENT = Path(d), cur
                gt = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=offset)
                      ).isoformat().replace("+00:00", "Z")
                rows = [{"player_id": i, "name": f"TEST Hitter {i}", "game_pk": 100, "game_time": gt,
                         "team": "AAA", "opponent": "BBB", "hr_score": 50.0} for i in range(1, 10)]
                (cur / "today.json").write_text(json.dumps({"players": rows}))

                def run(pids):
                    blob = {"name": "Pool A1", "parent": "Pool A — Strongest", "size": 3,
                            "model_version": "pools_6man_legacy_v1_half",
                            "players": [{"player_id": p, "name": f"TEST Hitter {p}", "game_pk": 100} for p in pids]}
                    (cur / "pair_builder_latest.json").write_text(json.dumps({"pools_3man_legacy": [blob]}))

                    def local(date):
                        f = cur / "pick_lock.json"
                        if f.exists():
                            j = json.loads(f.read_text())
                            if j.get("date") == date:
                                return j
                        return {"date": date, "games": {}, "tickets": {}, "rejected": []}
                    pl.fetch_lock = local
                    argv = sys.argv
                    sys.argv = ["pick_lock.py", "--apply"]
                    try:
                        pl.main()
                    finally:
                        sys.argv = argv
                    return json.loads((cur / "pair_builder_latest.json").read_text())["pools_3man_legacy"][0]
                run([1, 2, 3])
                out = run([4, 5, 6])
                got = [p["player_id"] for p in out["players"]]
                self.assertEqual(got, [1, 2, 3] if want_frozen else [4, 5, 6])
                if want_frozen:
                    self.assertEqual(out["parent"], "Pool A — Strongest")
                    self.assertEqual(out["model_version"], "pools_6man_legacy_v1_half")

    def test_graded_independently(self):
        mk = lambda ids: [{"player_id": i, "name": f"TEST Hitter {i}", "game_pk": 9} for i in ids]
        pb = {"date": "2099-01-01", "recommended_pairs": [], "pools_4man": [],
              "pools_3man": [{"name": "Pool A — Strongest", "players": mk((1, 2, 3))}],
              "pools_6man_legacy": [{"name": "Pool A — Strongest", "players": mk(range(1, 7))}],
              "pools_3man_legacy": [{"name": "Pool A1", "parent": "Pool A — Strongest", "players": mk((1, 2, 3))},
                                    {"name": "Pool A2", "parent": "Pool A — Strongest", "players": mk((4, 5, 6))}]}
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "pair_builder_latest.json").write_text(json.dumps(pb))
            old = t.OUT_DIR
            t.OUT_DIR = Path(d)
            try:
                sections = t.load_pair_builder_sections("2099-01-01")
            finally:
                t.OUT_DIR = old
        actual = {(9, 1): {"hr": 1, "ab": 4}, (9, 2): {"hr": 1, "ab": 4}, (9, 3): {"hr": 0, "ab": 4},
                  (9, 4): {"hr": 0, "ab": 4}, (9, 5): {"hr": 1, "ab": 4}, (9, 6): {"hr": 0, "ab": 4}}
        g = t.grade_pairs_pools(sections, actual)
        halves = {e["label"]: e for e in g["pool3_legacy"]}
        self.assertEqual(set(halves), {"OLD-RECIPE 3-MAN Pool A1", "OLD-RECIPE 3-MAN Pool A2"})
        a1, a2 = halves["OLD-RECIPE 3-MAN Pool A1"], halves["OLD-RECIPE 3-MAN Pool A2"]
        self.assertEqual((a1["hr_count"], a1["total_count"], a1["primary"]), (2, 3, 1))
        self.assertEqual((a2["hr_count"], a2["total_count"], a2["primary"]), (1, 3, 0))
        self.assertEqual(a1["model_version"], "pools_6man_legacy_v1_half")
        # the six-man grades on its own and totals elsewhere are untouched
        self.assertEqual([e["hr_count"] for e in g["pool6_legacy"]], [3])
        self.assertEqual([e["label"] for e in g["pool6"]], ["3-MAN Pool A — Strongest"])
        self.assertEqual(g["pool4"], [])
        self.assertEqual(len(g["graded_pools"]), 4)


if __name__ == "__main__":
    unittest.main()
