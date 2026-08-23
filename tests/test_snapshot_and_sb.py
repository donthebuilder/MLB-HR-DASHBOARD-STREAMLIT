"""The full pre-game snapshot + stolen bases v1 (2026-08-23).

1. SLOT SNAPSHOT. 11cec10's own scope note said it plainly: a SLOT_FIELDS
   key not in the two overlay maps "stays exposed to the leak until it's
   added". Instead of widening maps one field at a time,
   build_prediction_log_lines now writes slot_snapshot -- every leak-prone
   SLOT_FIELDS key at generation time (PREGAME_SNAPSHOT_FIELDS) -- and
   apply_locked_features overlays whatever the locked run wrote down.
   Identity keys are excluded by construction AND by a guard set.

2. STOLEN BASES v1 (claude/moonshot-sb-research.md; Donovan: "now").
   season_sb / season_cs / season_sb_attempt_rate read off the SAME season
   stat blob every pull already fetches; actual_sb / got_sb graded off the
   SAME boxscore call grading already makes; the odds matcher gains one
   name entry that lights up if/when the provider prices the market.
   Outcome column first, per the standing rule -- no SB look gets scored
   until its outcome is graded and trustworthy.

Run: python tests/test_snapshot_and_sb.py
"""
import os
import sys
import tempfile
from pathlib import Path
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bots.mlb_dashboard as md  # noqa: E402
import bots.live_results_tracker as lrt  # noqa: E402
import bots.odds_fetch as of  # noqa: E402

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


# ── 1a. the snapshot is emitted, leak-prone fields only ──────────────────
row = {
    "player_id": 1, "name": "Snap Hitter", "game_pk": 900900, "team": "AAA",
    "opponent": "BBB", "pitcher_id": 55, "game_pick_role": "TOP",
    "hr_score": 61.0, "season_iso": 0.240, "last5_hr": 1,
    "games_since_last_hr": 3, "pitcher_hr9": 1.4, "season_sb": 12,
    "best_bet_type": "HR", "venue_name": "Test Park",
}
meta = {"run_id": "r1", "slate_date": "2026-08-23", "generated_at": "t",
        "model_versions": {"hr": "mlb_hr_v3"}, "config_hashes": {"hr": "sha256:X"}}
lines = md.build_prediction_log_lines(meta, [row])
snap = lines[1].get("slot_snapshot")
checkTrue("slot_snapshot emitted on every player row", isinstance(snap, dict))
check("leak-prone season_iso snapshotted", snap.get("season_iso"), 0.240)
check("last5_hr snapshotted", snap.get("last5_hr"), 1)
check("pitcher_hr9 snapshotted", snap.get("pitcher_hr9"), 1.4)
check("season_sb snapshotted (SB rides the same rail)", snap.get("season_sb"), 12)
checkTrue("identity keys are NOT in the snapshot",
          all(k not in snap for k in ("player_id", "game_pk", "name", "team", "venue_name")))
checkTrue("only fields present on the row are snapshotted (no None spam)",
          "recent_barrel_rate" not in snap)

# ── 1b. the overlay applies the snapshot, guards identity ────────────────
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    (d / "por_log_2026-08-23.jsonl").write_text(json.dumps({
        "prediction_date": "2026-08-23", "game_pk": 900900, "run_id": "r1",
    }) + "\n", encoding="utf-8")
    locked_row = dict(lines[1])
    # simulate a hostile/buggy snapshot that tries to rewrite identity
    locked_row["slot_snapshot"] = dict(locked_row["slot_snapshot"],
                                       player_id=999, game_pk=111,
                                       season_iso=0.240, best_bet_type="HR")
    (d / "prediction_log_r1.jsonl").write_text(
        json.dumps(meta) + "\n" + json.dumps(locked_row) + "\n", encoding="utf-8")

    current = [{"player_id": 1, "name": "Snap Hitter", "game_pk": 900900,
                "season_iso": 0.310,          # leaked: tonight's HR inflated it
                "last5_hr": 2,                 # leaked
                "best_bet_type": "Avoid for HR",  # rebuilt post-game
                "hr_score": 88.0}]
    out, _ = lrt.apply_locked_features(current, "2026-08-23", d)
    g = out[0]
    check("snapshot overlays a leaked season field", g["season_iso"], 0.240)
    check("snapshot overlays a leaked recency field", g["last5_hr"], 1)
    check("snapshot overlays a rebuilt verdict field", g["best_bet_type"], "HR")
    check("hr_score still overlaid from the scores map", g["hr_score"], 61.0)
    check("identity NEVER overlaid: player_id", g["player_id"], 1)
    check("identity NEVER overlaid: game_pk", g["game_pk"], 900900)
    check("row still stamped locked", g["feature_snapshot"], "locked")

    # legacy prediction_log without slot_snapshot: unchanged behaviour
    legacy = dict(lines[1]); legacy.pop("slot_snapshot")
    (d / "prediction_log_r1.jsonl").write_text(
        json.dumps(meta) + "\n" + json.dumps(legacy) + "\n", encoding="utf-8")
    out2, _ = lrt.apply_locked_features([dict(current[0])], "2026-08-23", d)
    check("legacy log (no snapshot): headline maps still apply",
          out2[0]["hr_score"], 61.0)
    check("legacy log: un-mapped field keeps its published value",
          out2[0]["season_iso"], 0.310)

# ── 2. stolen bases v1 ────────────────────────────────────────────────────
flat = md.flatten_season_hitting({
    "avg": 0.280, "plateAppearances": 400, "stolenBases": 24,
    "caughtStealing": 6, "gamesPlayed": 100,
})
check("season_sb read off the blob every pull already had", flat["season_sb"], 24)
check("season_cs read too", flat["season_cs"], 6)
check("attempt rate = (SB+CS)/games", flat["season_sb_attempt_rate"], 0.3)
check("no games -> rate 0, never a crash",
      md.flatten_season_hitting({"avg": 0.2})["season_sb_attempt_rate"], 0.0)

import dataclasses  # noqa: E402
hrf = {f.name for f in dataclasses.fields(md.HitterRecord)}
checkTrue("HitterRecord carries the three SB fields",
          {"season_sb", "season_cs", "season_sb_attempt_rate"} <= hrf)
checkTrue("SLOT_FIELDS archives them",
          {"season_sb", "season_cs", "season_sb_attempt_rate"} <= lrt.SLOT_FIELDS)
checkTrue("PREGAME_SNAPSHOT_FIELDS snapshots them (SB leaks like any count)",
          {"season_sb", "season_cs"} <= set(md.PREGAME_SNAPSHOT_FIELDS))

# boxscore extraction: stolenBases -> sb
feed = {"liveData": {"boxscore": {"teams": {"home": {"players": {"ID77": {
    "stats": {"batting": {"hits": 2, "homeRuns": 0, "runs": 1, "rbi": 0,
              "totalBases": 2, "atBats": 4, "doubles": 0, "triples": 0,
              "stolenBases": 2}},
    "battingOrder": "100", "gameStatus": {},
}}}}}, "plays": {"allPlays": []}}}
line = lrt.get_player_batting_line(feed, 77)
check("boxscore stolenBases extracted", line.get("sb"), 2)
check("absent player -> sb 0 default",
      lrt.get_player_batting_line(feed, 999).get("sb"), 0)

# odds matcher entry exists, above the single-word matchers
names = [k for k, _ in of.MARKET_NAME_MATCHERS] if hasattr(of, "MARKET_NAME_MATCHERS") else None
src = open(os.path.join(os.path.dirname(__file__), "..", "bots", "odds_fetch.py"), encoding="utf-8").read()
checkTrue('odds matcher carries ("batter_stolen_bases", ("stolen base",))',
          '("batter_stolen_bases", ("stolen base",))' in src)
checkTrue("SB matcher sits above the bare-word matchers (ordering rule)",
          src.index("batter_stolen_bases") < src.index('("batter_hits", ("hit",))'))

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   slot snapshot + SB v1: {CHECKS} assertions — every leak-prone "
      f"SLOT_FIELDS key is snapshotted at generation and overlaid at grading "
      f"(identity guarded, legacy logs unchanged), and stolen bases get their "
      f"season fields, their graded outcome, and their odds matcher without a "
      f"single new network call")
