"""A real zero shows for a stat that is his; a stat that isn't his stays out (2026-10-04).
Run: PYTHONPATH=. python3 tests/test_nfl_keeps_stat.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))
import nfl_bot as b  # noqa: E402
cases = [
    ("WR", "TD", 0, True), ("WR", "20+", 0.0, True), ("RB", "GL", 0, True), ("TE", "REC", 0, True),
    ("RB", "PAYD", 0, False), ("RB", "FGM", 0, False), ("WR", "CAR", 0, False), ("K", "TD", 0, False),
    ("QB", "PAYD", 0, True), ("RB", "PAYD", 12.0, True), ("WR", "TD", None, False), (None, "TD", 0, False),
]
bad = [c for c in cases if b.keeps_stat(c[0], c[1], c[2]) is not c[3]]
print(f"{len(cases) - len(bad)}/{len(cases)} checks passed")
for c in bad: print("  FAIL", c)
sys.exit(1 if bad else 0)
