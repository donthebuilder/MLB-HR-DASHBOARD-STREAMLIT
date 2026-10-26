#!/usr/bin/env python3
"""Tests for bots/lineup_first_seen.py (TEST data only). Runs as a script or under pytest.
    python3 tests/test_lineup_first_seen.py"""
from __future__ import annotations
import json, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))
import lineup_first_seen as L  # noqa: E402

FAILS: list[str] = []
def check(ok, msg):
    print(("ok   " if ok else "FAIL ") + msg)
    if not ok: FAILS.append(msg)

T = lambda h, m=0: datetime(2026, 10, 26, h, m, tzinfo=timezone.utc)
START = "2026-10-26T23:00:00Z"
mlb = lambda posted: [{"game_id": 1, "start_utc": START, "sides": {"away": {"team": "TST", "posted": posted, "order": {"11": 1, "12": 2}}, "home": {"team": "TSU", "posted": False, "order": {}}}}]
nhl = lambda g: [{"game_id": "9", "start_utc": START, "sides": {"home": {"team": "TST", "probable_goalie": g}, "away": {"team": "TSU", "probable_goalie": None}}}]
FETCH = lambda m, n: {"mlb": lambda d: m, "nhl": lambda d: n, "nba": lambda d: [], "nba_starters": lambda d: {}}

with tempfile.TemporaryDirectory() as d:
    a = L.observe("2026-10-26", d, T(20), FETCH(mlb(False), nhl("Test Goalie")))
    check(len(a) == 1 and a[0]["what"] == "goalie" and a[0]["mins_before_start"] == 180, "an unposted lineup is not logged; a goalie is, 180 min before start")
    b = L.observe("2026-10-26", d, T(21), FETCH(mlb(True), nhl("Test Goalie")))
    check(len(b) == 1 and b[0]["what"] == "lineup" and b[0]["mins_before_start"] == 120, "the lineup posting is logged the first time it is seen (120 min before)")
    c = L.observe("2026-10-26", d, T(22), FETCH(mlb(True), nhl("Test Goalie")))
    check(c == [], "nothing new: nothing written")
    e = L.observe("2026-10-26", d, T(22, 30), FETCH(mlb(True), nhl("Other Goalie")))
    check(len(e) == 1 and e[0]["value"] == "Other Goalie", "a goalie swap is a NEW event")
    ev = json.loads((Path(d) / "2026-10-26.json").read_text())["events"]
    check(len(ev) == 3 and ev[0]["first_seen_utc"] == "2026-10-26T20:00:00Z", "append only: the first event keeps its first-seen time")
    g = L.observe("2026-10-26", d, T(23, 45), FETCH(mlb(True), nhl("Third Goalie")))
    check(g == [], "a game long under way logs nothing new")
    bad = {"mlb": lambda d: 1 / 0, "nhl": lambda d: nhl("New Goalie"), "nba": lambda d: [], "nba_starters": lambda d: {}}
    h = L.observe("2026-10-26", d, T(19), bad)
    check(len(h) == 1 and h[0]["sport"] == "nhl", "a dead feed costs only its own sport")

print("\nFAILED" if FAILS else "\nall ok")
sys.exit(1 if FAILS else 0)
