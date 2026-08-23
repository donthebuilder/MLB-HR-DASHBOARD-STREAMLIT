#!/usr/bin/env python3
"""
Tests for what the archive KEEPS — bots/mlb_dashboard.PREGAME_SNAPSHOT_FIELDS
and bots/live_results_tracker.SLOT_FIELDS.

Runnable BOTH as a pytest module and as a plain script: every other file in
tests/ is a script and pytest is not installed everywhere this repo runs, so a
pytest-only file would be a test that quietly never executes.

    python3 tests/test_capture_completeness.py
    pytest  tests/test_capture_completeness.py

WHY THIS FILE EXISTS
--------------------
The slate publishes 424 fields a row. Before 2026-08-23 the archive kept 149,
and of the 52 fields the site's own filter menu exposes only 22 existed on 26+
of the 28 graded nights. Max EV, Avg dist, Max dist, Air %, Pull %, Sweet-spot %,
BBE, SLG, BABIP, HR per PA, Hard-hit % and Arm WHIP had ZERO archived rows.

Two failure modes produced that, and this file asserts against both:

  1. A field is computed and published and simply never added to either
     collection. Caught by the surface tests below, which name the fields
     out loud so adding a column to the site without archiving it goes red.

  2. A field is added to PREGAME_SNAPSHOT_FIELDS and NOT to SLOT_FIELDS, so it
     is snapshotted at generation time and then dropped before the graded file.
     Seven fields were in exactly that state (l20pa_hr, l20pa_xbh, last10_hr,
     recent_ideal_hr_contact, recent_pull_rate, season_ab, season_tb). Caught by
     test_no_drift.

These are read out of the SOURCE rather than by importing the modules --
mlb_dashboard.py pulls pandas and a stack of optional deps at import time, and
a test that cannot run is not a test. Same technique check-rank-lock.mjs uses.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def check(cond, msg):
    if cond:
        return
    FAILS.append(msg)
    print("  RED  " + msg)


def _pregame() -> list[str]:
    text = (ROOT / "bots" / "mlb_dashboard.py").read_text()
    i = text.index("PREGAME_SNAPSHOT_FIELDS = (")
    j = text.index("\n)\n", i)
    body = re.sub(r"#.*", "", text[i + len("PREGAME_SNAPSHOT_FIELDS = ("):j])
    return list(ast.literal_eval("[" + body + "]"))


def _slot() -> list[str]:
    text = (ROOT / "bots" / "live_results_tracker.py").read_text()
    i = text.index("\nSLOT_FIELDS = {")
    j = text.index("\n}\n", i)
    body = re.sub(r"#.*", "", text[i + len("\nSLOT_FIELDS = {"):j])
    return list(ast.literal_eval("[" + body + "]"))


PITCHER_SURFACE = """pitcher_meatball_pct pitcher_barrel_allowed pitcher_ev_allowed
pitcher_hardhit_allowed pitcher_hr_fb_pct pitcher_xhr_bbe pitcher_hr9_vs_lhb
pitcher_hr9_vs_rhb pitcher_attack_score pitcher_era pitcher_fip pitcher_whip
pitcher_gb_rate pitcher_ld_rate pitcher_popup_rate pitcher_iso_against
pitcher_slg_against pitcher_l3_hr9 pitcher_hr_luck""".split()

BATTED_BALL_SURFACE = """recent_ev recent_hard_hit_rate recent_sweet_spot_rate
recent_max_distance season_max_distance l20pa_barrel_rate l20pa_fb_rate
l20pa_hard_hit_rate l20pa_bbe l25pa_air_rate l25pa_avg_ev l25pa_sweet_spot_rate
xhr_bbe""".split()

MENU_FIELDS_WITH_NO_HISTORY = """season_slg season_babip hr_per_pa pitcher_whip
recent_hard_hit_rate l25pa_air_rate l20pa_pull_rate recent_max_distance
recent_avg_hr_distance l25pa_bbe l25pa_sweet_spot_rate""".split()


def test_shapes():
    print("shapes")
    pre, slot = _pregame(), _slot()
    check(len(pre) > 100, f"PREGAME_SNAPSHOT_FIELDS should be >100 fields, is {len(pre)}")
    check(len(slot) > 200, f"SLOT_FIELDS should be >200 fields, is {len(slot)}")
    check(all(isinstance(k, str) and k for k in pre), "every snapshot field is a non-empty string")
    check(all(isinstance(k, str) and k for k in slot), "every slot field is a non-empty string")
    dup_pre = sorted({k for k in pre if pre.count(k) > 1})
    dup_slot = sorted({k for k in slot if slot.count(k) > 1})
    check(not dup_pre, f"PREGAME_SNAPSHOT_FIELDS has duplicates: {dup_pre}")
    check(not dup_slot, f"SLOT_FIELDS has duplicates: {dup_slot}")


def test_no_drift():
    """A field snapshotted pre-game but absent from SLOT_FIELDS is captured and
    then thrown away before the graded file. That is a silent data loss and it
    had already happened to seven fields."""
    print("no drift between the two collections")
    pre, slot = set(_pregame()), set(_slot())
    gap = sorted(pre - slot)
    check(not gap, f"snapshotted pre-game but never reaching the graded archive: {gap}")


def test_pitcher_surface():
    print("pitcher surface is captured")
    pre, slot = set(_pregame()), set(_slot())
    missing_pre = [k for k in PITCHER_SURFACE if k not in pre]
    missing_slot = [k for k in PITCHER_SURFACE if k not in slot]
    check(not missing_pre, f"pitcher fields missing from the pre-game snapshot: {missing_pre}")
    check(not missing_slot, f"pitcher fields missing from the graded archive: {missing_slot}")


def test_batted_ball_surface():
    print("batted-ball surface is captured")
    pre, slot = set(_pregame()), set(_slot())
    missing_pre = [k for k in BATTED_BALL_SURFACE if k not in pre]
    missing_slot = [k for k in BATTED_BALL_SURFACE if k not in slot]
    check(not missing_pre, f"batted-ball fields missing from the pre-game snapshot: {missing_pre}")
    check(not missing_slot, f"batted-ball fields missing from the graded archive: {missing_slot}")


def test_every_menu_field_now_has_a_home():
    """The specific columns the site lets a user filter on that had ZERO
    archived rows on 2026-08-23. If one of these ever leaves the archive again,
    this goes red with the field named."""
    print("filter-menu fields that had no history")
    pre, slot = set(_pregame()), set(_slot())
    for k in MENU_FIELDS_WITH_NO_HISTORY:
        check(k in pre, f"{k} is filterable on the site but not snapshotted pre-game")
        check(k in slot, f"{k} is filterable on the site but never reaches the archive")


def test_snapshot_is_used_where_it_is_declared():
    """PREGAME_SNAPSHOT_FIELDS must actually be read into slot_snapshot; a list
    nobody consumes is the same as no list."""
    print("the snapshot list is actually consumed")
    text = (ROOT / "bots" / "mlb_dashboard.py").read_text()
    check('"slot_snapshot": {k: row.get(k) for k in PREGAME_SNAPSHOT_FIELDS if k in row}' in text,
          "slot_snapshot is no longer built from PREGAME_SNAPSHOT_FIELDS")


def main() -> int:
    for fn in (test_shapes, test_no_drift, test_pitcher_surface,
               test_batted_ball_surface, test_every_menu_field_now_has_a_home,
               test_snapshot_is_used_where_it_is_declared):
        fn()
    if FAILS:
        print(f"\n{len(FAILS)} RED")
        return 1
    print("\nall green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
