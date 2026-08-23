"""The x100 pitch-usage regression guard (2026-08-22).

The fix is ALREADY LIVE (b702ebf, enrich_weather_payload_for_website):
fraction-vs-percentage used to be decided ONE VALUE AT A TIME
(`if 0 < val <= 1: val *= 100`), so a pitch thrown 1% or less of the time
was multiplied by a hundred and became the biggest number in the arsenal.
14 of 59 published starters carried a corrupt arsenal, 7 summing past
150%; PitcherProfile showed Tanner Gordon at "SI 100%" when he throws no
sinkers. The fix decides once from the dict TOTAL (percentages sum to
~100, fractions to ~1) — this file is the guard that was supposed to ride
with it and did not: a live fix with no test is one careless rewrite from
being a live bug again.

Every case runs through the real enrich_weather_payload_for_website(), on
rows shaped like published slate rows.

Run: python tests/test_pitch_usage_scaling.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bots.mlb_dashboard import enrich_weather_payload_for_website  # noqa: E402

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


def arsenal_for(raw_usage):
    row = {"player_id": 1, "name": "T", "pitcher_pitch_usage": raw_usage}
    out = enrich_weather_payload_for_website([row])[0]
    return out["pitcher_arsenal"]


# ── 1. THE BUG SHAPE: a percentage dict with one sub-1.0 entry ───────────
# Tanner Gordon's real arsenal: FF 45.1 | CH 25.3 | SL 19.7 | CU 8.9 with a
# 1.0% sinker. The old per-value rule turned SI 1.0 into SI 100.0 and the
# published dict summed to 199.
gordon = arsenal_for({"FF": 45.1, "CH": 25.3, "SL": 19.7, "CU": 8.9, "SI": 1.0})
check("Gordon: the 1% sinker stays 1%, never becomes 100", gordon.get("SI"), 1.0)
check("Gordon: the real primary is still the primary",
      list(gordon.keys())[0], "FF")
checkTrue("Gordon: arsenal sums to ~100, not ~199",
          99.0 <= sum(gordon.values()) <= 101.0)

for name, raw, code, val in [
    ("Yamamoto SL 0.9 stays 0.9", {"FF": 40.0, "FS": 30.0, "CU": 28.2, "SL": 0.9}, "SL", 0.9),
    ("Sale SI 0.6 stays 0.6", {"SL": 45.0, "FF": 30.0, "CH": 23.8, "SI": 0.6}, "SI", 0.6),
]:
    a = arsenal_for(raw)
    check(name, a.get(code), val)
    checkTrue(name + " (sum ~100)", 98.0 <= sum(a.values()) <= 101.0)

# ── 2. a genuine FRACTION dict still scales up, as a whole ───────────────
frac = arsenal_for({"FF": 0.451, "CH": 0.253, "SL": 0.197, "CU": 0.089, "SI": 0.01})
check("fraction dict: FF 0.451 -> 45.1", frac.get("FF"), 45.1)
check("fraction dict: the 1% pitch scales WITH the dict, to 1.0 not 100",
      frac.get("SI"), 1.0)
checkTrue("fraction dict sums to ~100", 99.0 <= sum(frac.values()) <= 101.0)

# ── 3. edges the total-based rule must not break ─────────────────────────
one = arsenal_for({"FF": 100.0})
check("single-pitch percentage arm stays 100", one.get("FF"), 100.0)
one_f = arsenal_for({"FF": 1.0})
check("single-pitch FRACTION arm (total 1.0 <= 1.5) scales to 100",
      one_f.get("FF"), 100.0)
mixed = arsenal_for({"FF": 55.0, "SL": 45.0, "XX": -3.0})
checkTrue("negative/garbage values are dropped, not scaled",
          "XX" not in mixed)
check("empty usage dict -> empty arsenal", arsenal_for({}), {})
check("non-dict usage -> empty arsenal", arsenal_for(None), {})
strs = arsenal_for({"FF": "45.1", "SL": "30.2", "CH": "24.7"})
check("string-valued percentages parse and keep scale", strs.get("FF"), 45.1)

# ── 4. output is sorted by usage, biggest first (what the site prints) ───
sorted_out = arsenal_for({"CU": 8.9, "FF": 45.1, "SL": 19.7, "CH": 25.3, "SI": 1.0})
check("arsenal is sorted descending by usage",
      list(sorted_out.keys()), ["FF", "CH", "SL", "CU", "SI"])

# ── 5. all three published aliases carry the same fixed dict ─────────────
row = {"player_id": 1, "name": "T",
       "pitcher_pitch_usage": {"FF": 45.1, "CH": 25.3, "SL": 19.7, "CU": 8.9, "SI": 1.0}}
out = enrich_weather_payload_for_website([row])[0]
check("pitcher_pitch_usage == pitcher_arsenal",
      out["pitcher_pitch_usage"], out["pitcher_arsenal"])
check("pitcher_pitch_usage_pct == pitcher_arsenal",
      out["pitcher_pitch_usage_pct"], out["pitcher_arsenal"])

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   x100 pitch-usage guard: {CHECKS} assertions, fraction-vs-percentage "
      f"is decided once from the dict total — a 1% pitch can never again become "
      f"a 100% pitch, whole-dict fractions still scale, and all three published "
      f"aliases carry the same corrected arsenal")
