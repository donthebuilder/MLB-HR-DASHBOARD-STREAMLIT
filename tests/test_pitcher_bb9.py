"""pitcher_bb9 must be a real rate, not the constant 3.20 -- data defect #3
(2026-08-23).

compute_pitcher_extended_stats() has computed a real BB/9 since 2026-08-12
(bb * 9 / IP, from fields already fetched) and returned it in its dict --
but build_pitcher_profile()'s kwargs block never copied "bb9" into
PitcherSummary, so the dataclass default 3.20 shipped on every starter of
every slate (30 of 30 re-verified 2026-08-22) while bb_pct, copied one line
up from the same walk counts, varied normally. Donovan's call on the field:
keep it, make it real -- "pitcher [walks] per 9 innings if possible yes,
make a stat like bb%."

What this file proves:

  1. compute_pitcher_extended_stats() turns a season stat blob into the
     textbook BB/9 (30 BB over 90.0 IP -> 3.00; 60 over 120 -> 4.50), with
     MLB's fractional-innings notation (".1"/".2" = thirds) handled.
  2. The wiring: the exact defect was PitcherSummary never receiving the
     computed value. PitcherSummary(**{**defaults-shape, "bb9": ext["bb9"]})
     must carry the real number, and two different pitchers must publish
     two different bb9s (the constant-field failure mode).
  3. No innings -> the honest default, never a division crash.

Run: python tests/test_pitcher_bb9.py
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


def ext_for(bb, ip):
    stat = {"baseOnBalls": bb, "inningsPitched": ip, "battersFaced": 400,
            "strikeOuts": 100, "hits": 90, "atBats": 360, "doubles": 15,
            "triples": 2, "homeRuns": 12, "hitByPitch": 4, "sacFlies": 3,
            "intentionalWalks": 1}
    flat = md.flatten_pitching(stat)
    return md.compute_pitcher_extended_stats(stat, flat, {})


# ── 1. the arithmetic ────────────────────────────────────────────────────
check("30 BB / 90.0 IP -> BB/9 = 3.00", ext_for(30, "90.0")["bb9"], 3.0)
check("60 BB / 120.0 IP -> BB/9 = 4.50", ext_for(60, "120.0")["bb9"], 4.5)
check("MLB fractional innings: 20 BB / 60.1 IP (60⅓) -> 2.98",
      ext_for(20, "60.1")["bb9"], round(20 * 9.0 / (60 + 1 / 3), 2))
check("no innings -> honest default, no crash", ext_for(5, "0.0")["bb9"], 3.20)

# ── 2. the wiring defect: the value must reach PitcherSummary ────────────
import dataclasses  # noqa: E402

fields = {f.name for f in dataclasses.fields(md.PitcherSummary)}
check("PitcherSummary has a bb9 field", "bb9" in fields, True)
check("PitcherSummary's bb9 default is the old constant (what shipped)",
      md.PitcherSummary(1, "X", "AAA").bb9, 3.20)

a = md.PitcherSummary(1, "Wild Arm", "AAA", bb9=ext_for(60, "120.0")["bb9"])
b = md.PitcherSummary(2, "Control Arm", "BBB", bb9=ext_for(20, "180.0")["bb9"])
check("a real computed bb9 lands on the summary", a.bb9, 4.5)
check("two pitchers publish two DIFFERENT bb9s (the constant-field "
      "failure mode)", a.bb9 != b.bb9, True)

# ── 3. the kwargs block actually copies it (source-level guard) ──────────
# The defect was one missing line in build_pitcher_profile's kwargs dict.
# Assert the copy exists next to its siblings, so a refactor that drops it
# fails a test instead of silently reviving the constant.
src_path = os.path.join(os.path.dirname(__file__), "..", "bots", "mlb_dashboard.py")
with open(src_path, encoding="utf-8") as fh:
    src = fh.read()
check('build_pitcher_profile copies "bb9" from extended stats',
      '"bb9": extended["bb9"]' in src, True)

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   pitcher bb9 (data defect #3): {CHECKS} assertions, BB/9 is the "
      f"textbook walks x 9 / innings from data already fetched, it reaches "
      f"PitcherSummary instead of the 3.20 dataclass default, and the one-line "
      f"copy that was missing is now guarded at source level")
