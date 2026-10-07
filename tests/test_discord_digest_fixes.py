"""Discord digest fixes (2026-10-07).

ALL DATA HERE IS TEST DATA: players are "TEST Hitter N" and webhook URLs are
example.invalid placeholders. Nothing is sent anywhere: urlopen is replaced.

Covers: the digest budget never drops pool/pair lines silently; one cash event
is one alert; a 429 is retried a bounded number of times; every sender sets a
User-Agent and failures are loud.

Run: PYTHONPATH=. python3 tests/test_discord_digest_fixes.py
"""
import email.message
import io
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import discord_post  # noqa: E402
import digest_budget  # noqa: E402
import live_results_tracker as t  # noqa: E402

FAN = "https://example.invalid/api/webhooks/TEST-FAN/TEST"
OPS = "https://example.invalid/api/webhooks/TEST-OPS/TEST"


class FakeResp:
    status = 204

    def read(self):
        return b""


def http_err(code, retry_after=None):
    m = email.message.Message()
    if retry_after is not None:
        m["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://example.invalid", code, "x", m, io.BytesIO(b"{}"))


def pool(label, names, homers):
    return {"label": label, "players": [{"name": n} for n in names], "hr_count": len(homers),
            "total_count": len(names), "homer_names": list(homers)}


SIX = [f"TEST Hitter {i}" for i in range(1, 7)]


def legacy_payload(homers):
    return {"pair_pool_results": {"graded_pools": [
        pool("SIX-MAN LEGACY Pool A — Strongest", SIX, [n for n in SIX if n in homers]),
        pool("OLD-RECIPE 3-MAN Pool A1", SIX[:3], [n for n in SIX[:3] if n in homers]),
        pool("OLD-RECIPE 3-MAN Pool A2", SIX[3:], [n for n in SIX[3:] if n in homers]),
    ], "all_pairs": []}}


def body_lines(descs):
    out = []
    for d in descs:
        for x in d.split("\n"):
            if x and not x.startswith("**") and not x.startswith("+"):
                out.append(x)
    return out


class Budget(unittest.TestCase):
    def sections(self, n_hr=60, n_tix=12):
        tix = [f"💰 **POOL CASHED — TEST pool {i}**: all 3 went deep" for i in range(n_tix)]
        return [("🧾 THE RECORD, FIRST", ["TEST record line"]),
                ("🎫 TICKETS", tix),
                ("💥 WENT DEEP", [f"💥 **TEST Slugger {i}** went deep " + "x" * 60 for i in range(n_hr)])], tix

    def test_every_ticket_line_is_shown_and_first(self):
        secs, tix = self.sections()
        descs, left = digest_budget.pack_sections(secs)
        self.assertTrue(all(len(d) <= digest_budget.LIMIT for d in descs))
        joined = "\n".join(descs)
        for ln in tix:
            self.assertIn(ln, joined)
        self.assertLess(joined.index("TICKETS"), joined.index("WENT DEEP"))

    def test_split_not_cut(self):
        secs, _ = self.sections(n_hr=60)
        descs, left = digest_budget.pack_sections(secs)
        self.assertEqual(left, 0)
        self.assertGreater(len(descs), 1)
        self.assertIn("(cont.)", descs[1])
        self.assertEqual("\n".join(descs).count("TEST Slugger"), 60)

    def test_overflow_is_counted_honestly(self):
        secs, tix = self.sections(n_hr=400)
        total = sum(len(ls) for _, ls in secs)
        descs, left = digest_budget.pack_sections(secs)
        self.assertEqual(len(descs), digest_budget.MAX_MESSAGES)
        self.assertGreater(left, 0)
        self.assertIn(f"+{left} more line", descs[-1])
        self.assertEqual(len(body_lines(descs)) + left, total)     # nothing vanished unannounced
        for ln in tix:                                              # tickets never the ones lost
            self.assertIn(ln, "\n".join(descs))

    def test_under_limit_is_one_message(self):
        descs, left = digest_budget.pack_sections([("A", ["one", "two"])])
        self.assertEqual((len(descs), left), (1, 0))


class CashEvents(unittest.TestCase):
    def test_one_swing_completing_six_and_a_half_is_one_alert(self):
        lines = t.pool_ticket_lines(legacy_payload(set(SIX[:5])), legacy_payload(set(SIX)))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("POOL CASHED", lines[0])
        self.assertIn("SIX-MAN LEGACY Pool A", lines[0])
        self.assertIn("Pool A1", lines[0])
        self.assertIn("Pool A2", lines[0])

    def test_half_only_cash_names_the_six_man_pool(self):
        lines = t.pool_ticket_lines(legacy_payload({SIX[0], SIX[1]}), legacy_payload(set(SIX[:3])))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("SIX-MAN LEGACY Pool A", lines[0])
        self.assertIn("Pool A1", lines[0])
        self.assertNotIn("Pool A2", lines[0])

    def test_one_away_is_one_line_per_family(self):
        lines = t.pool_ticket_lines(legacy_payload({SIX[0]}),
                                    legacy_payload({SIX[0], SIX[1], SIX[3], SIX[4]}))
        self.assertEqual(len(lines), 1, lines)

    def test_two_plus_names_rule_kept(self):
        one = {"pair_pool_results": {"graded_pools": [pool("3-MAN TEST solo", ["TEST Solo"], ["TEST Solo"])]}}
        self.assertEqual(t.pool_ticket_lines({}, one), [])
        two = {"pair_pool_results": {"graded_pools": [
            pool("3-MAN TEST duo", ["TEST A", "TEST B"], ["TEST A", "TEST B"])]}}
        self.assertEqual(len(t.pool_ticket_lines({}, two)), 1)

    def test_digest_ticket_lines_ride_first(self):
        posted = []
        orig = t._post_discord_payload
        t._post_discord_payload = lambda p: (posted.append(p) or (1, 0))
        try:
            old = legacy_payload(set(SIX[:5]))
            new = legacy_payload(set(SIX))
            new["graded_slots"] = [{"name": f"TEST Slugger {i}", "game_pick_role": "HR", "player_id": i,
                                    "game_pk": 1, "actual_hr": 1, "actual_ab": 4, "actual_hits": 1,
                                    "actual_tb": 4} for i in range(25)]
            old["graded_slots"] = [dict(s, actual_hr=0, actual_hits=0, actual_tb=0) for s in new["graded_slots"]]
            t._webhook_transitions(old, new, "2099-01-01")
        finally:
            t._post_discord_payload = orig
        self.assertTrue(posted)
        desc = "\n".join(e["embeds"][0]["description"] for e in posted)
        self.assertEqual(desc.count("POOL CASHED"), 1)
        first = posted[0]["embeds"][0]["description"]
        self.assertIn("POOL CASHED", first)
        if "WENT DEEP" in first:
            self.assertLess(first.index("POOL CASHED"), first.index("WENT DEEP"))


class Poster(unittest.TestCase):
    def setUp(self):
        self._orig = urllib.request.urlopen
        self._env = {k: os.environ.get(k) for k in ("DISCORD_WEBHOOK", "DISCORD_OPS_WEBHOOK", "DISCORD_LIVE_WEBHOOK")}
        os.environ.pop("DISCORD_LIVE_WEBHOOK", None)

    def tearDown(self):
        urllib.request.urlopen = self._orig
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_429_retries_then_succeeds_with_retry_after(self):
        outcomes = [http_err(429, 2), http_err(429, 3), FakeResp()]
        sleeps, calls = [], []

        def opener(req, timeout=0):
            calls.append(req)
            o = outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            return o
        self.assertEqual(discord_post.send(FAN, b"{}", opener=opener, sleep=sleeps.append), 204)
        self.assertEqual(sleeps, [2.0, 3.0])
        self.assertEqual(len(calls), 3)

    def test_429_is_bounded(self):
        calls, sleeps = [], []

        def opener(req, timeout=0):
            calls.append(1)
            raise http_err(429, 1)
        with self.assertRaises(urllib.error.HTTPError):
            discord_post.send(FAN, b"{}", opener=opener, sleep=sleeps.append)
        self.assertEqual(len(calls), discord_post.MAX_RETRIES + 1)
        self.assertEqual(len(sleeps), discord_post.MAX_RETRIES)

    def test_retry_after_is_clamped(self):
        sleeps = []
        outcomes = [http_err(429, 9999), FakeResp()]

        def opener(req, timeout=0):
            o = outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            return o
        discord_post.send(FAN, b"{}", opener=opener, sleep=sleeps.append)
        self.assertEqual(sleeps, [discord_post.MAX_WAIT_SECONDS])

    def test_other_errors_are_not_retried(self):
        calls = []

        def opener(req, timeout=0):
            calls.append(1)
            raise http_err(403)
        with self.assertRaises(urllib.error.HTTPError):
            discord_post.send(FAN, b"{}", opener=opener, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_user_agent_on_every_sender_and_ops_routing(self):
        seen = []

        def fake(req, timeout=0):
            seen.append((req.full_url, req.get_header("User-agent")))
            return FakeResp()
        urllib.request.urlopen = fake
        os.environ["DISCORD_WEBHOOK"] = FAN
        os.environ["DISCORD_OPS_WEBHOOK"] = OPS
        import make_slim
        import validate_context
        import staleness_watch
        import pick_lock
        make_slim._alert_bad_slate("TEST slate", "TEST reason")
        self.assertTrue(validate_context.post_discord({"nights": 1, "through": "2099-01-01", "stats": []}))
        self.assertTrue(staleness_watch.post_discord(["TEST alert"]))
        pick_lock.post_discord(["TEST alert"])
        t._post_discord("TEST fan message")
        t._post_discord_payload({"content": "TEST"})
        t._post_discord_file(b"PNG", "x.png", "TEST")
        self.assertEqual(len(seen), 7)
        for url, ua in seen:
            self.assertEqual(ua, discord_post.USER_AGENT, url)
        self.assertTrue(all(u == OPS for u, _ in seen[:4]), seen[:4])
        self.assertTrue(all(u == FAN for u, _ in seen[4:]), seen[4:])

    def test_ops_falls_back_to_fan_room_only_when_unset(self):
        os.environ["DISCORD_WEBHOOK"] = FAN
        os.environ.pop("DISCORD_OPS_WEBHOOK", None)
        self.assertEqual(discord_post.ops_secret(), FAN)

    def test_failures_are_loud(self):
        import validate_context
        os.environ["DISCORD_OPS_WEBHOOK"] = OPS

        def boom(req, timeout=0):
            raise http_err(403)
        urllib.request.urlopen = boom
        err, old = io.StringIO(), sys.stderr
        sys.stderr = err
        try:
            ok = validate_context.post_discord({"nights": 1, "through": "2099-01-01", "stats": []})
        finally:
            sys.stderr = old
        self.assertFalse(ok)
        self.assertIn("0 webhook", err.getvalue())

    def test_no_webhook_is_reported_not_skipped(self):
        import validate_context
        os.environ.pop("DISCORD_WEBHOOK", None)
        os.environ.pop("DISCORD_OPS_WEBHOOK", None)
        err, old = io.StringIO(), sys.stderr
        sys.stderr = err
        try:
            ok = validate_context.post_discord({"nights": 1, "through": "2099-01-01", "stats": []})
        finally:
            sys.stderr = old
        self.assertFalse(ok)
        self.assertIn("NOT delivered", err.getvalue())


if __name__ == "__main__":
    unittest.main()
