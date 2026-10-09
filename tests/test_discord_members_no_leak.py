"""LEAK CHECK for the free Discord server (Stage 1 audit, 2026-10-09): "members post + no members
webhook = sends nothing", for the BOT senders.

ALL DATA HERE IS TEST DATA. Every webhook is an example.invalid placeholder and urlopen is replaced
by a recorder that raises: nothing is ever sent.

The bot repo has NO members channel at all (no DISCORD_MEMBERS_* anywhere in bots/ or the
workflows), so members content cannot leave from here. This test pins that:

  1. static: no bot file or workflow reads a members webhook variable.
  2. with ONLY a members URL in the environment (every bot variable unset), every bot sender
     sends nothing.
  3. with every bot variable set AND a members URL, no bot sender ever calls the members URL.
  4. documented behaviour (not a members leak): ops alerts fall back to DISCORD_WEBHOOK, the fan
     room, while DISCORD_OPS_WEBHOOK is unset.

Run: PYTHONPATH=. python3 tests/test_discord_members_no_leak.py
"""
import os
import re
import sys
import unittest
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "bots"))
sys.path.insert(0, ROOT)

import discord_post  # noqa: E402

MEMBERS = "https://example.invalid/api/webhooks/TEST-MEMBERS/TEST"
FAN = "https://example.invalid/api/webhooks/TEST-FAN/TEST"
LIVE = "https://example.invalid/api/webhooks/TEST-LIVE/TEST"
PEN = "https://example.invalid/api/webhooks/TEST-PEN/TEST"
OPS = "https://example.invalid/api/webhooks/TEST-OPS/TEST"
BOT_KEYS = ["DISCORD_WEBHOOK", "DISCORD_LIVE_WEBHOOK", "DISCORD_PENDOOR_WEBHOOK", "DISCORD_OPS_WEBHOOK",
            "DISCORD_MEMBERS_WEBHOOK", "DISCORD_HOMER_WEBHOOK"]


class Resp:
    status = 204

    def read(self):
        return b""


class Net:
    """Replaces urllib.request.urlopen; records every URL it is asked for."""

    def __init__(self):
        self.urls = []
        self._real = None

    def __enter__(self):
        self._real = urllib.request.urlopen

        def fake(req, timeout=None):
            self.urls.append(req.full_url if hasattr(req, "full_url") else str(req))
            return Resp()
        urllib.request.urlopen = fake
        return self

    def __exit__(self, *a):
        urllib.request.urlopen = self._real


def set_env(**kw):
    for k in BOT_KEYS:
        os.environ.pop(k, None)
    os.environ.update(kw)


def senders():
    """(name, callable) for every bot function that can put text in Discord. Modules that cannot be
    imported in this environment are reported, not silently skipped."""
    out, missing = [], []

    def add(name, fn):
        out.append((name, fn))
    add("discord_post.post", lambda: discord_post.post(os.environ.get("DISCORD_WEBHOOK", ""), {"content": "TEST"}))
    add("discord_post.post_ops", lambda: discord_post.post_ops({"content": "TEST"}))
    for modname in ("live_results_tracker", "pen_door_watch", "staleness_watch", "pick_lock", "validate_context", "make_slim"):
        try:
            __import__(modname)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{modname}: {type(exc).__name__}: {exc}")
    m = sys.modules
    if "live_results_tracker" in m:
        t = m["live_results_tracker"]
        add("tracker._post_discord_payload", lambda: t._post_discord_payload({"content": "TEST"}))
        add("tracker._post_discord", lambda: t._post_discord("TEST"))
        add("tracker._post_discord_file", lambda: t._post_discord_file(b"\x89PNG TEST", "t.png", "TEST"))
    if "pen_door_watch" in m:
        p = m["pen_door_watch"]
        add("pen_door._deliver", lambda: p._deliver({"content": "TEST"}))
        add("pen_door.post", lambda: p.post("TEST"))
    if "staleness_watch" in m:
        add("staleness.post_discord", lambda: m["staleness_watch"].post_discord(["TEST alert"]))
    if "pick_lock" in m:
        add("pick_lock.post_discord", lambda: m["pick_lock"].post_discord(["TEST alert"]))
    if "make_slim" in m:
        add("make_slim._alert_bad_slate", lambda: m["make_slim"]._alert_bad_slate("TEST", "test"))
    if "validate_context" in m:
        add("validate_context.post_discord", lambda: m["validate_context"].post_discord({"verdict": "TEST"}))
    return out, missing


class StaticScan(unittest.TestCase):
    def test_no_bot_file_or_workflow_has_a_members_webhook(self):
        hits = []
        for base in ("bots", ".github/workflows", "scripts"):
            for dp, _dn, files in os.walk(os.path.join(ROOT, base)):
                for f in files:
                    if f.endswith((".py", ".yml", ".yaml", ".sh")):
                        p = os.path.join(dp, f)
                        try:
                            with open(p, encoding="utf-8", errors="ignore") as fh:
                                txt = fh.read()
                        except OSError:
                            continue
                        if re.search(r"MEMBERS?_WEBHOOK|DISCORD_MEMBERS", txt) and not p.endswith("test_discord_members_no_leak.py"):
                            hits.append(os.path.relpath(p, ROOT))
        self.assertEqual(hits, [], f"a bot file reads a members webhook: {hits}")


class MembersUrlOnly(unittest.TestCase):
    def test_every_sender_sends_nothing_when_only_a_members_url_exists(self):
        set_env(DISCORD_MEMBERS_WEBHOOK=MEMBERS)
        fns, missing = senders()
        self.assertGreaterEqual(len(fns), 2)
        for name, fn in fns:
            with Net() as net:
                try:
                    fn()
                except Exception:  # noqa: BLE001 - a sender may print and raise; only the network matters here
                    pass
                self.assertEqual(net.urls, [], f"{name} sent to {net.urls}")
        print(f"  checked {len(fns)} senders; not importable here: {missing or 'none'}")


class AllBotUrlsSet(unittest.TestCase):
    def test_no_sender_ever_calls_the_members_url(self):
        set_env(DISCORD_WEBHOOK=FAN, DISCORD_LIVE_WEBHOOK=LIVE, DISCORD_PENDOOR_WEBHOOK=PEN, DISCORD_OPS_WEBHOOK=OPS,
                DISCORD_MEMBERS_WEBHOOK=MEMBERS)
        fns, _missing = senders()
        for name, fn in fns:
            with Net() as net:
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
                self.assertNotIn(MEMBERS, net.urls, f"{name} called the members URL")


class OpsFallback(unittest.TestCase):
    """Documented: while DISCORD_OPS_WEBHOOK is unset, ops alerts post to the fan room."""

    def test_ops_alert_lands_in_the_fan_room_when_no_ops_channel(self):
        set_env(DISCORD_WEBHOOK=FAN, DISCORD_MEMBERS_WEBHOOK=MEMBERS)
        with Net() as net:
            ok, bad, used_ops = discord_post.post_ops({"content": "TEST ops"})
        self.assertEqual(net.urls, [FAN])
        self.assertFalse(used_ops)

    def test_ops_alert_uses_the_ops_channel_when_set_and_not_the_fan_room(self):
        set_env(DISCORD_WEBHOOK=FAN, DISCORD_OPS_WEBHOOK=OPS)
        with Net() as net:
            discord_post.post_ops({"content": "TEST ops"})
        self.assertEqual(net.urls, [OPS])

    def test_ops_alert_with_nothing_set_sends_nothing(self):
        set_env()
        with Net() as net:
            discord_post.post_ops({"content": "TEST ops"})
        self.assertEqual(net.urls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
