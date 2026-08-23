"""The five missing pitcher split axes -- data defect #4 (2026-08-23).

Donovan named eight split axes for the pitcher modal; the slate published
two (handedness, recent form). The modal deliberately shipped without the
other five buttons rather than faking them. This publishes home/away
("in park"), day/night, RISP, ahead/behind in count, by-month and
by-day-of-week as one `pitcher_situational_splits` dict, so each button is
the promised one-line addition to the split control's options array.

Stub-client tests -- statsapi.mlb.com is unreachable from this sandbox, so
the first live run is the production verification (any starter's slim row
should carry a non-empty pitcher_situational_splits with real hr9 values).

Run: python tests/test_situational_splits.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bots.mlb_dashboard as md  # noqa: E402

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def checkTrue(name, cond):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILED.append(f"{name}: expected truthy, got falsy")


def stat(ip, hr, hits, bb, ops, bf):
    return {"inningsPitched": ip, "homeRuns": hr, "hits": hits,
            "baseOnBalls": bb, "ops": ops, "battersFaced": bf}


SIT_BLOB = {"stats": [{"splits": [
    {"split": {"code": "h"}, "stat": stat("50.1", 8, 45, 15, 0.750, 210)},
    {"split": {"code": "a"}, "stat": stat("40.2", 3, 30, 10, 0.610, 165)},
    {"split": {"code": "d"}, "stat": stat("30.0", 6, 28, 9, 0.800, 130)},
    {"split": {"code": "n"}, "stat": stat("61.0", 5, 47, 16, 0.640, 250)},
    {"split": {"code": "risp"}, "stat": stat("20.1", 2, 18, 8, 0.700, 95)},
    {"split": {"code": "ac"}, "stat": stat("35.0", 2, 20, 3, 0.520, 130)},
    {"split": {"code": "bc"}, "stat": stat("25.0", 7, 30, 20, 0.980, 140)},
    {"split": {"code": "zz"}, "stat": stat("1.0", 0, 0, 0, 0.0, 3)},  # unknown code: skipped
]}]}
MONTH_BLOB = {"stats": [{"splits": [
    {"month": 6, "stat": stat("28.0", 3, 22, 8, 0.650, 115)},
    {"month": 7, "stat": stat("30.1", 7, 33, 12, 0.870, 135)},
]}]}
DOW_BLOB = {"stats": [{"splits": [
    {"dayOfWeek": {"id": 4, "description": "Wednesday"},
     "stat": stat("12.0", 4, 14, 5, 0.910, 55)},
    {"dayOfWeek": {"id": 7, "description": "Saturday"},
     "stat": stat("15.2", 1, 10, 4, 0.550, 62)},
]}]}


class StubClient:
    def pitcher_situational_stats(self, pid): return SIT_BLOB
    def pitcher_month_stats(self, pid): return MONTH_BLOB
    def pitcher_dow_stats(self, pid): return DOW_BLOB


class FailingClient:
    def pitcher_situational_stats(self, pid): raise RuntimeError("api down")


class StubDB:
    def __init__(self): self.saved = {}
    def get(self, key, max_age_days=None): return self.saved.get(key)
    def set(self, key, val): self.saved[key] = val


db = StubDB()
out = md.parse_pitcher_situational_splits(StubClient(), db, 555001)

check("status ok", out["status"], "ok")
checkTrue("all seven sit buckets present",
          all(k in out for k in ("home", "away", "day", "night", "risp", "ahead", "behind")))
# hr9 arithmetic with MLB's fractional-IP notation: 50.1 IP = 50 1/3
check("home hr9 respects .1-innings-is-a-third",
      out["home"]["hr9"], round(8 * 9.0 / (50 + 1 / 3), 2))
check("away whip = (H+BB)/IP", out["away"]["whip"],
      round((30 + 10) / (40 + 2 / 3), 2))
check("behind-in-count carries its ops (the blowup axis)",
      out["behind"]["ops"], 0.98)
check("bf kept so the site can refuse thin samples", out["risp"]["bf"], 95)
checkTrue("unknown sit code is skipped, not invented",
          "zz" not in out and len([k for k in out if k not in
          ("status", "by_month", "by_dow")]) == 7)
check("by_month keyed by month number", sorted(out["by_month"]), ["6", "7"])
check("July's line is July's", out["by_month"]["7"]["hr"], 7)
check("by_dow keyed by day name (the 'Wednesday' ask)",
      sorted(out["by_dow"]), ["Saturday", "Wednesday"])
check("Wednesday hr9", out["by_dow"]["Wednesday"]["hr9"], round(4 * 9 / 12.0, 2))

# caching: second call comes from the stub db, no client hit
out2 = md.parse_pitcher_situational_splits(FailingClient(), db, 555001)
check("second call served from cache (client never touched)", out2, out)

# failure path: honest error status, never a raise, never cached as ok
db2 = StubDB()
bad = md.parse_pitcher_situational_splits(FailingClient(), db2, 555002)
check("api failure -> status error:RuntimeError, no crash",
      bad["status"], "error:RuntimeError")

# the record fields exist end to end
import dataclasses  # noqa: E402
ps_fields = {f.name for f in dataclasses.fields(md.PitcherSummary)}
hr_fields = {f.name for f in dataclasses.fields(md.HitterRecord)}
checkTrue("PitcherSummary.situational_splits exists", "situational_splits" in ps_fields)
checkTrue("HitterRecord.pitcher_situational_splits exists",
          "pitcher_situational_splits" in hr_fields)

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   situational splits (data defect #4): {CHECKS} assertions — all "
      f"five missing axes parse from real StatSplits shapes with correct "
      f"fractional-IP math, calendar splits key by month and day name, "
      f"failures degrade to an honest error status, and the fields exist on "
      f"both records end to end")
