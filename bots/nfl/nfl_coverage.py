#!/usr/bin/env python3
"""nfl_coverage.py — man/zone and coverage-shell splits.

I told Donovan earlier this wasn't available and that was WRONG. It isn't in
play-by-play, which is where I looked; it's in `load_participation`, which
carries NGS charting: `defense_man_zone_type`, `defense_coverage_type`,
`route`, `was_pressure`, `defenders_in_box`, `offense_formation`. Verified fill
rates across 2023-2025:

    man_zone      ~100% of plays
    coverage type  ~49% (Cover 0/1/2/2-Man/3/4/6/9, COMBO, BLOWN)
    route         ~100%

The 49% on shell type is a real limitation and is surfaced rather than hidden:
a Cover-3 rate computed off half the snaps is still useful, but the reader
should know the denominator.

Two views, matching how the reference tool presents it:
  TEAM     what a defense plays — man%, zone%, and the shell distribution
  PLAYER   how he does against each — targets, catches, yards, TDs vs man
           and vs zone, so "he eats zone" stops being a vibe
"""
from __future__ import annotations
import functools

import nflreadpy as nfl
import polars as pl

SHELLS = ["COVER_0", "COVER_1", "COVER_2", "2_MAN", "COVER_3", "COVER_4", "COVER_6", "COVER_9"]
SHELL_LABEL = {
    "COVER_0": "C0", "COVER_1": "C1", "COVER_2": "C2", "2_MAN": "C2M",
    "COVER_3": "C3", "COVER_4": "C4", "COVER_6": "C6", "COVER_9": "C9",
}


@functools.lru_cache(maxsize=4)
def _joined(season: int) -> pl.DataFrame:
    part = nfl.load_participation(seasons=[season])
    pbp = nfl.load_pbp(seasons=[season]).select(
        ["game_id", "play_id", "season_type", "defteam", "posteam",
         "receiver_player_id", "pass_attempt", "complete_pass",
         "yards_gained", "pass_touchdown", "yardline_100"])
    j = part.join(pbp, left_on=["nflverse_game_id", "play_id"],
                  right_on=["game_id", "play_id"], how="inner")
    return j.filter(pl.col("season_type") == "REG", pl.col("pass_attempt") == 1)


def team_profile(season: int) -> dict:
    """Per defense: man/zone rate and the shell distribution."""
    j = _joined(season)
    out: dict = {}
    tot = (j.group_by("defteam").agg(
        pl.len().alias("att"),
        (pl.col("defense_man_zone_type") == "MAN_COVERAGE").sum().alias("man"),
        (pl.col("defense_man_zone_type") == "ZONE_COVERAGE").sum().alias("zone"),
        pl.col("defense_coverage_type").is_in(SHELLS).sum().alias("shell_n"),
        pl.col("complete_pass").fill_null(0).sum().alias("cmp"),
        pl.col("yards_gained").fill_null(0).sum().alias("yds"),
    ))
    for r in tot.iter_rows(named=True):
        att = max(1, int(r["att"]))
        out[r["defteam"]] = {
            "att": int(r["att"]), "cmp": int(r["cmp"]), "yds": int(r["yds"]),
            "ypa": round(float(r["yds"]) / att, 1),
            "man_pct": round(100 * float(r["man"]) / att, 1),
            "zone_pct": round(100 * float(r["zone"]) / att, 1),
            # The denominator for every shell below. Shown, not buried: these
            # are computed off the ~49% of snaps NGS charted a shell for.
            "shell_n": int(r["shell_n"]),
            "shells": {},
        }
    sh = (j.filter(pl.col("defense_coverage_type").is_in(SHELLS))
            .group_by(["defteam", "defense_coverage_type"]).agg(pl.len().alias("n")))
    for r in sh.iter_rows(named=True):
        t = out.get(r["defteam"])
        if not t:
            continue
        den = max(1, t["shell_n"])
        t["shells"][SHELL_LABEL[r["defense_coverage_type"]]] = round(100 * r["n"] / den, 1)
    return out


def player_vs_coverage(season: int, min_targets: int = 8) -> dict:
    """Per receiver: his line against man and against zone."""
    j = _joined(season).filter(pl.col("receiver_player_id").is_not_null())
    g = (j.group_by(["receiver_player_id", "defense_man_zone_type"]).agg(
            pl.len().alias("tgts"),
            pl.col("complete_pass").fill_null(0).sum().alias("rec"),
            pl.col("yards_gained").fill_null(0).sum().alias("yds"),
            pl.col("pass_touchdown").fill_null(0).sum().alias("td"),
            (pl.col("yardline_100") <= 20).sum().alias("rz_tgts")))
    out: dict = {}
    for r in g.iter_rows(named=True):
        mz = r["defense_man_zone_type"]
        key = "man" if mz == "MAN_COVERAGE" else "zone" if mz == "ZONE_COVERAGE" else None
        if not key:
            continue
        tg = int(r["tgts"])
        out.setdefault(r["receiver_player_id"], {})[key] = {
            "tgts": tg, "rec": int(r["rec"]), "yds": int(r["yds"]),
            "td": int(r["td"]), "rz_tgts": int(r["rz_tgts"]),
            "ypr": round(float(r["yds"]) / max(1, int(r["rec"])), 1),
            "ypt": round(float(r["yds"]) / max(1, tg), 1),
            "catch_pct": round(100 * float(r["rec"]) / max(1, tg), 1),
        }
    # Drop anyone whose combined sample can't support a split.
    return {k: v for k, v in out.items()
            if sum(x["tgts"] for x in v.values()) >= min_targets}


if __name__ == "__main__":
    t = team_profile(2025)
    print("teams:", len(t))
    for tm in ("PIT", "LA"):
        if tm in t:
            d = t[tm]
            print(f"\n{tm}: att={d['att']} ypa={d['ypa']} man={d['man_pct']}% zone={d['zone_pct']}% "
                  f"(shell sample {d['shell_n']})")
            print("   shells:", d["shells"])
    p = player_vs_coverage(2025)
    print(f"\n{len(p)} receivers with a coverage split")
    wk = nfl.load_player_stats(seasons=[2025], summary_level="week")
    for nm in ("Ja'Marr Chase", "Drake London"):
        ids = wk.filter(pl.col("player_display_name") == nm)["player_id"].to_list()
        if ids and ids[0] in p:
            print(f"  {nm}: {p[ids[0]]}")
