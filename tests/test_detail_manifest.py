#!/usr/bin/env python3
"""make_slim's detail directories describe exactly one slate, and say which.

THE BUG THIS LOCKS DOWN (2026-08-29). Measured against the live data branch:
current/detail/today held 269 batter files across 15 game_pks, and ZERO of
those games were on the today_slim.json published beside it. Same for
tomorrow. 30 pitcher files matched 0 of the slate's 32 starters, so 27 of 30
starters on the board had no arsenal, no lineup-spot damage and no pitch-mix
chart -- while every hitter who plays most nights kept his player_id, so his
stale batter_<id>.json WAS found and his spray chart rendered another game's
batted balls as tonight's.

Nothing went red, because a detail directory carried no statement about which
night it belonged to. These assertions are that statement.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bots.make_slim import write_detail_files  # noqa: E402

CHECKS = 0
FAILED = []


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def slate(date, game_base, player_base, pitcher_base, games=3, per_game=9):
    rows = []
    for g in range(games):
        for b in range(per_game):
            rows.append({
                "player_id": player_base + g * per_game + b,
                "name": f"B{g}{b}",
                "game_pk": game_base + g,
                "game_date": date,
                "pitcher_id": pitcher_base + g,
                "pitcher_name": f"P{g}",
                "spray_chart": [{"x": 1, "y": 2}],
                "pitcher_pitch_arsenal_detail": [{"pitch_code": "FF", "usage_pct": 55.0}],
            })
    return rows


with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp) / "detail" / "today"

    # ── Night one ────────────────────────────────────────────────────────
    n1 = write_detail_files(slate("2026-09-01", 900000, 700000, 500000), out)
    man = json.loads((out / "_manifest.json").read_text())
    check("night one wrote a file per batter plus one per pitcher", n1, 27 + 3)
    check("manifest names the slate", man["slate_date"], "2026-09-01")
    check("manifest counts the batters", man["batter_count"], 27)
    check("manifest counts the pitchers", man["pitcher_count"], 3)
    check("manifest lists the games", man["game_pks"], ["900000", "900001", "900002"])
    check("manifest names its own directory", man["label"], "today")

    # ── Night two, same directory: the old night must not survive ─────────
    write_detail_files(slate("2026-09-02", 910000, 800000, 510000), out)
    names = sorted(p.name for p in out.glob("*.json"))
    check("no batter file from night one survives",
          [n for n in names if n.startswith("batter_7")], [])
    check("no pitcher file from night one survives",
          [n for n in names if n.startswith("pitcher_50")], [])
    check("night two's own files are all there",
          len([n for n in names if n.startswith("batter_8")]), 27)
    man2 = json.loads((out / "_manifest.json").read_text())
    check("the manifest moved to night two", man2["slate_date"], "2026-09-02")
    check("and the manifest itself was never pruned",
          (out / "_manifest.json").exists(), True)

    # ── A run that computes nothing must not blank the last good night ────
    before = sorted(p.name for p in out.glob("*.json"))
    n3 = write_detail_files([{"player_id": 1, "name": "no detail keys at all"}], out)
    after = sorted(p.name for p in out.glob("*.json"))
    check("a run with no detail payload writes nothing", n3, 0)
    check("and prunes nothing -- the last good night is still there", after, before)

    # ── The slate date comes from the rows, not the clock ─────────────────
    out2 = Path(tmp) / "detail" / "tomorrow"
    write_detail_files(slate("2026-12-25", 920000, 900000, 520000), out2)
    check("the stamp is the slate's own date",
          json.loads((out2 / "_manifest.json").read_text())["slate_date"], "2026-12-25")
    check("each label stamps its own directory",
          json.loads((out2 / "_manifest.json").read_text())["label"], "tomorrow")

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   detail manifest + prune: {CHECKS} assertions, a detail directory now "
      f"describes exactly one slate and says which one, last night's files cannot "
      f"survive into tonight's directory, and a run that computes no detail leaves "
      f"the last good night alone instead of blanking it")
