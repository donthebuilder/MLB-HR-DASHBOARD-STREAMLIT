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
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bots.make_slim as M  # noqa: E402
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

    # ── A REAL SLATE ROW CARRIES NO DATE KEY (2026-08-29, second pass) ────
    #
    # The first version of the stamp read game_date/slate_date/date off the
    # rows and returned "" when it found none. Measured against the live
    # branch after it shipped: 0 of 303 rows on today_slim.json carry any of
    # those three keys -- not empty, ABSENT. Every manifest went out stamped
    # "" , both shell guards read that as a mismatch, and detail/today and
    # detail/tomorrow were deleted at publish. A stale-detail bug became a
    # no-detail bug, and the site went from showing the wrong night's spray
    # charts to showing none at all.
    #
    # These rows are shaped like the real thing: game_pk + game_time, no
    # date key anywhere. If this ever stamps "" again, detail stops shipping.
    def dateless(game_time, player_base=970000, pitcher_base=570000):
        return [{
            "player_id": player_base + i,
            "name": f"H{i}",
            "game_pk": 960000 + (i // 9),
            "game_time": game_time,
            "pitcher_id": pitcher_base + (i // 9),
            "pitcher_name": f"P{i // 9}",
            "spray_chart": [{"ev": 101.0, "la": 27}],
            "pitcher_pitch_arsenal_detail": [{"pitch": "FF"}],
        } for i in range(27)]

    out3 = Path(tmp) / "detail" / "dateless"
    write_detail_files(dateless("2026-08-29T17:05:00Z"), out3)
    check("a dateless row set still stamps a real date",
          json.loads((out3 / "_manifest.json").read_text())["slate_date"], "2026-08-29")

    # A 10:07pm ET first pitch is ALREADY TOMORROW in UTC. Taking the UTC
    # date straight off it would stamp the 30th on a slate the run meta,
    # the site and the shell guards all call the 29th -- and a one-day
    # disagreement drops the directory exactly like an empty one does.
    out4 = Path(tmp) / "detail" / "latenight"
    write_detail_files(dateless("2026-08-30T02:07:00Z", 980000, 580000), out4)
    check("a late game does not roll the stamp into the next day",
          json.loads((out4 / "_manifest.json").read_text())["slate_date"], "2026-08-29")

    # The stamp must survive the two shell greps that read it. Both use a
    # ten-character [0-9-] class, so a short or malformed value reads as no
    # value at all and the directory is dropped.
    for label, value in (("dateless", "2026-08-29"), ("latenight", "2026-08-29")):
        raw = (Path(tmp) / "detail" / label / "_manifest.json").read_text()
        found = re.findall(r'"slate_date"\s*:\s*"([0-9-]{10})"', raw)
        check(f"the {label} stamp is greppable by publish_data.sh", found[:1], [value])

    # An unresolvable slate stamps empty ON PURPOSE and says so loudly --
    # publish_data.sh drops it, and "no detail published" is a true sentence.
    # What must never happen is a confident wrong date.
    out5 = Path(tmp) / "detail" / "unknown"
    write_detail_files([{
        "player_id": 990000 + i, "name": f"X{i}", "game_pk": 991000,
        "pitcher_id": 590000, "pitcher_name": "PX",
        "spray_chart": [{"ev": 99.0}],
    } for i in range(3)], out5)
    check("no date anywhere stamps empty rather than guessing",
          json.loads((out5 / "_manifest.json").read_text())["slate_date"], "")

    # ── THE COMMITTED run_meta LANDMINE (found in this fix's own dry run) ──
    #
    # public/data IS COMMITTED ON MAIN. A fresh CI checkout starts life with
    # public/data/current/today_run_meta.json stamped 2026-08-21, sitting
    # next to a detail/today from that same August night -- both several
    # days old, both perfectly readable.
    #
    # The first draft of this fix read run_meta BEFORE first pitch. Run it
    # in a checkout and it stamps 2026-08-21 with total confidence. Then
    # publish_data.sh compares that manifest against the SAME stale file,
    # the two agree, and a five-day-old detail directory publishes as
    # tonight's -- the original bug, with a checkmark next to it. Strictly
    # worse than an empty stamp, because an empty stamp gets dropped.
    #
    # So: anything derived from the rows beats anything read off disk, and
    # in CI run_meta is only believed when its run_id carries this job's
    # GITHUB_RUN_ID.
    stale_meta = M.CURRENT_DIR
    real_current = M.CURRENT_DIR
    fake_current = Path(tmp) / "asif_checkout" / "current"
    fake_current.mkdir(parents=True, exist_ok=True)
    (fake_current / "today_run_meta.json").write_text(json.dumps({
        "run_id": "2026-08-21.070937Z.gha-11111111",
        "slate_date": "2026-08-21",
    }))
    M.CURRENT_DIR = fake_current
    try:
        rows_tonight = dateless("2026-08-29T17:05:00Z")
        check("first pitch beats a stale run_meta sitting in the checkout",
              M._slate_date_of(rows_tonight, "today"), "2026-08-29")

        # No first pitch either, and the meta is from another run: refuse it.
        no_time = [{k: v for k, v in r.items() if k != "game_time"} for r in rows_tonight]
        os.environ["GITHUB_RUN_ID"] = "33248294815"
        check("a run_meta from a different run is refused, not believed",
              M._slate_date_of(no_time, "today"), "")
        os.environ["GITHUB_RUN_ID"] = "11111111"
        check("a run_meta from THIS run is believed",
              M._slate_date_of(no_time, "today"), "2026-08-21")
        os.environ.pop("GITHUB_RUN_ID", None)
        check("off CI there is no job id to check against, so it is read as written",
              M._slate_date_of(no_time, "today"), "2026-08-21")
    finally:
        M.CURRENT_DIR = real_current
        os.environ.pop("GITHUB_RUN_ID", None)

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   detail manifest + prune: {CHECKS} assertions, a detail directory now "
      f"describes exactly one slate and says which one, last night's files cannot "
      f"survive into tonight's directory, and a run that computes no detail leaves "
      f"the last good night alone instead of blanking it")
