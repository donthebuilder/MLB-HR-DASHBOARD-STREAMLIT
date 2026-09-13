#!/usr/bin/env python3
"""nfl_td_lab.py — does anything actually improve the touchdown model?

THE POINT. The TD model is six terms and, by SCORING.md's own admission, its
2025 edge was mostly overfit (+7.9 in sample, +1.7 out). Two things the MLB
side has and this does not are an opponent model with any resolution (the
pitcher gets six terms; the defence here gets one raw counting stat at 8%) and
any notion of availability (MLB weights a confirmed lineup 100 vs 65; snap
share carries zero weight here). This measures whether closing either helps.

THE PROTOCOL, and it is the whole value of the file. Weights are tuned on one
season and reported on the other, never both. A candidate ships only if it
improves the season it was NOT tuned on. Anything else is how the current
model got its +7.9.

    python nfl_td_lab.py --cache      # build + cache both seasons (slow)
    python nfl_td_lab.py --solo       # each candidate as a solo ranker
    python nfl_td_lab.py --sweep      # add each candidate to the live model
"""
from __future__ import annotations
import argparse
import os
import sys

import polars as pl
import nflreadpy as nfl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nfl_features import build, _roll, _pbp, FORM_W            # noqa: E402
from nfl_scoring import MODELS, OUTCOME, derive, _pctile       # noqa: E402

CACHE = os.path.expanduser("~/.nfl_td_lab")
SEASONS = (2024, 2025)


# ── CANDIDATE 1: a defence with resolution ───────────────────────────────────
# `opp_td_soft` is raw TDs allowed, which is mostly a volume statement: a
# defence on the field a lot concedes a lot. The pitcher analogue is a RATE —
# HR per nine, not HR. So: of the red-zone plays this defence actually faced,
# how many became touchdowns. Same for the goal line, where TDs are really won.
def defence_rz(season: int) -> pl.DataFrame:
    """Per defensive team-week: red-zone and goal-line TD conversion allowed."""
    p = _pbp(season)
    need = {"defteam", "week", "yardline_100", "touchdown"}
    if not need.issubset(set(p.columns)):
        raise KeyError(f"pbp missing {need - set(p.columns)}")
    scrim = p.filter(
        (pl.col("pass_attempt") == 1) | (pl.col("rush_attempt") == 1),
        pl.col("defteam").is_not_null(),
        pl.col("yardline_100").is_not_null(),
    )
    return scrim.group_by("defteam", "week").agg(
        (pl.col("yardline_100") <= 20).sum().alias("d_rz_plays"),
        ((pl.col("yardline_100") <= 20) & (pl.col("touchdown") == 1)).sum().alias("d_rz_td"),
        (pl.col("yardline_100") <= 10).sum().alias("d_gl_plays"),
        ((pl.col("yardline_100") <= 10) & (pl.col("touchdown") == 1)).sum().alias("d_gl_td"),
    ).with_columns(
        # A rate on a thin denominator is noise, so the denominator is floored
        # rather than the row dropped: a defence nobody reached the red zone
        # against reads as average, not as elite.
        (pl.col("d_rz_td") / pl.col("d_rz_plays").clip(4)).alias("d_rz_td_rate"),
        (pl.col("d_gl_td") / pl.col("d_gl_plays").clip(2)).alias("d_gl_td_rate"),
    ).rename({"defteam": "team"})


# ── CANDIDATE 2: availability ────────────────────────────────────────────────
# Snap share is the denominator under both opportunity terms and has zero
# weight in any model. A goal-line vulture at 30% of snaps and an every-down
# back are currently ranked as peers on the same red-zone touch count.
def snaps(season: int) -> pl.DataFrame:
    s = nfl.load_snap_counts(seasons=[season])
    if isinstance(s, pl.LazyFrame):
        s = s.collect()
    s = s.filter(pl.col("game_type") == "REG") if "game_type" in s.columns else s
    col = "offense_pct" if "offense_pct" in s.columns else "offense_snaps"
    key = "pfr_player_id" if "pfr_player_id" in s.columns else "player_id"
    return s.select(pl.col(key).alias("pfr_id"), "week", pl.col(col).alias("snap_pct"))


def id_map(season: int) -> pl.DataFrame:
    r = nfl.load_rosters(seasons=[season])
    if isinstance(r, pl.LazyFrame):
        r = r.collect()
    have = set(r.columns)
    gsis = "gsis_id" if "gsis_id" in have else "player_id"
    if "pfr_id" not in have:
        return pl.DataFrame({"pfr_id": [], "player_id": []})
    return r.select(pl.col("pfr_id"), pl.col(gsis).alias("player_id")).drop_nulls().unique()


def enrich(season: int) -> pl.DataFrame:
    """The live feature table plus the candidates, all trailing-only."""
    tbl = build(season)

    d = defence_rz(season)
    dform = _roll(d.select(["team", "week", "d_rz_td_rate", "d_gl_td_rate"]),
                  ["d_rz_td_rate", "d_gl_td_rate"], by="team",
                  prefix="f_oppz_", gp="f_oppz_gp")
    tbl = tbl.join(dform, left_on=["opponent_team", "week"], right_on=["team", "week"],
                   how="left")

    try:
        sn = snaps(season).join(id_map(season), on="pfr_id", how="inner")
        sform = _roll(sn.select(["player_id", "week", "snap_pct"]), ["snap_pct"],
                      prefix="f_", gp="f_snap_gp")
        tbl = tbl.join(sform, on=["player_id", "week"], how="left")
    except Exception as e:                                    # pragma: no cover
        print(f"  ! snaps unavailable: {type(e).__name__}: {e}")
        tbl = tbl.with_columns(pl.lit(None, dtype=pl.Float64).alias("f_snap_pct"))

    # position-specific defence: joined on opponent AND the player's own
    # position, so two players in the same game get different numbers. That is
    # the whole difference between this and the failed team-level candidate.
    try:
        dp = defence_by_pos(season)
        dpf = _roll(dp.select(["team", "week", "position", "dp_td", "dp_yds"]),
                    ["dp_td", "dp_yds"], by=["team", "position"],
                    prefix="f_", gp="f_dp_gp")
        tbl = tbl.join(dpf, left_on=["opponent_team", "week", "position"],
                       right_on=["team", "week", "position"], how="left")
    except Exception as e:                                    # pragma: no cover
        print(f"  ! defence_by_pos unavailable: {type(e).__name__}: {e}")

    # snap TREND, not level: nfl_snaps.py's own stated point is that the
    # actionable half is the direction, because a receiver who was at 45% and
    # is now at 80% has moved up the depth chart and his counting stats have
    # not caught up yet.
    if "f_snap_pct" in tbl.columns:
        try:
            sn2 = snaps(season).join(id_map(season), on="pfr_id", how="inner")
            # BUG, caught by the result being EXACTLY 0.0 at every weight in
            # both seasons: the first version built this side with the same
            # _roll and the same FORM_W as f_snap_pct, so it was the identical
            # column and the difference was always zero. A trend needs a
            # SHORTER window than the baseline it is compared against — here
            # the single most recent week he played, strictly before w.
            one = (sn2.select(["player_id", "week", "snap_pct"])
                      .sort(["player_id", "week"])
                      .with_columns(pl.col("snap_pct").shift(1).over("player_id")
                                      .alias("r1_snap_pct"))
                      .select(["player_id", "week", "r1_snap_pct"]))
            tbl = tbl.join(one, on=["player_id", "week"], how="left")
            tbl = tbl.with_columns(
                (pl.col("r1_snap_pct") - pl.col("f_snap_pct")).alias("f_snap_trend"))
        except Exception:
            pass

    for c in ("f_oppz_d_rz_td_rate", "f_oppz_d_gl_td_rate", "f_snap_pct",
              "f_dp_td", "f_snap_trend"):
        if c not in tbl.columns:
            tbl = tbl.with_columns(pl.lit(0.0).alias(c))
    return tbl


# ── CANDIDATE 3: a defence that knows WHO it is facing ───────────────────────
# The first defensive candidate failed, and SCORING.md's own rule says why:
# "context modulates, volume selects." A team-level red-zone rate is
# player-agnostic — it says something true about the game and nothing about
# which of the twenty eligible players in that game to pick, so adding it can
# only dilute the terms that do select.
#
# The pitcher is not player-agnostic. A left-handed power arm is a specific
# threat to a specific hitter. The real analogue is not "this defence concedes
# touchdowns", it is "this defence concedes touchdowns TO RUNNING BACKS" —
# which differs per player because the player's position picks the number.
def defence_by_pos(season: int) -> pl.DataFrame:
    """Per defensive team-week: TDs and red-zone touches allowed, BY position."""
    wk = nfl.load_player_stats(seasons=[season], summary_level="week")
    if isinstance(wk, pl.LazyFrame):
        wk = wk.collect()
    wk = wk.filter(pl.col("season_type") == "REG")
    pos = "position" if "position" in wk.columns else "position_group"
    return wk.filter(pl.col(pos).is_in(["RB", "WR", "TE"])).group_by(
        "opponent_team", "week", pos).agg(
        (pl.col("receiving_tds").fill_null(0) + pl.col("rushing_tds").fill_null(0))
            .sum().alias("dp_td"),
        (pl.col("receiving_yards").fill_null(0) + pl.col("rushing_yards").fill_null(0))
            .sum().alias("dp_yds"),
    ).rename({"opponent_team": "team", pos: "position"})


CANDIDATES = {
    "f_oppz_d_rz_td_rate": "defence: red-zone TD rate allowed",
    "f_oppz_d_gl_td_rate": "defence: goal-line TD rate allowed",
    "f_snap_pct":          "his snap share",
    "f_dp_td":             "defence: TDs allowed to HIS position",
    "f_snap_trend":        "his snap share, rising or falling",
    # ── FOUND BY THE RESIDUAL SCAN, not by anyone's hunch ────────────────────
    # Inside a score band, plain passing-game volume still separates TD scorers
    # by +7 to +8.5 points in BOTH seasons. The TD model contains no volume
    # term at all: its six components are red-zone/goal-line opportunity,
    # implied total, xTD and regression. Red-zone touches are rare and noisy
    # over a four-week window; targets are the much better-sampled statement
    # of "the offence looks for him", and offences look for their focal points
    # near the end zone too.
    "f_targets":           "his targets (residual scan)",
    "f_target_share":      "his share of team targets (residual scan)",
    "f_receptions":        "his catches (residual scan)",
}


# ── measurement ──────────────────────────────────────────────────────────────
def topk_hit(d: pl.DataFrame, col: str, topk: int) -> tuple[int, float]:
    picks = hits = 0
    for w in sorted(d["week"].unique().to_list()):
        live = d.filter(pl.col("week") == w)
        if live.height == 0:
            continue
        sel = live.sort(col, descending=True).head(topk)
        picks += sel.height
        hits += int(sel["hit"].sum())
    return picks, 100 * hits / max(1, picks)


def scored(tbl: pl.DataFrame, weights: dict[str, float]) -> pl.DataFrame:
    m = MODELS["TD"]
    d = derive(tbl).filter(pl.col("position").is_in(m["pos"]))
    total = None
    for raw, wgt in weights.items():
        invert = raw.startswith("-")
        col = raw.lstrip("-")
        part = _pctile(d, col, invert) * wgt
        total = part if total is None else total + part
    return d.with_columns((total * 100).alias("score"),
                          OUTCOME["TD"].alias("y")).with_columns(
        (pl.col("y") >= m["bar"]).cast(pl.Int8).alias("hit"))


def rescale(weights: dict[str, float]) -> dict[str, float]:
    s = sum(weights.values())
    return {k: v / s for k, v in weights.items()}


def load(season: int) -> pl.DataFrame:
    path = f"{CACHE}/td_{season}.parquet"
    if not os.path.exists(path):
        raise SystemExit(f"no cache for {season} — run with --cache first")
    return pl.read_parquet(path)


def cmd_cache(seasons) -> None:
    os.makedirs(CACHE, exist_ok=True)
    for s in seasons:
        print(f"building {s} …", flush=True)
        t = enrich(s)
        t.write_parquet(f"{CACHE}/td_{s}.parquet")
        cov = {c: round(100 * float(t[c].is_not_null().mean()), 1) for c in CANDIDATES}
        print(f"  {t.height} player-weeks · candidate coverage {cov}")


def cmd_solo(topk: int) -> None:
    """Is each candidate selective on its own, or player-agnostic context?

    SCORING.md's own rule — context modulates, volume selects — was established
    exactly this way, so a candidate gets held to the same test before anyone
    argues about its weight.
    """
    for s in SEASONS:
        d = scored(load(s), MODELS["TD"]["w"])
        base = 100 * float(d["hit"].mean())
        print(f"\n{s}  (base {base:.1f}%, n={d.height})")
        print(f"  {'ranker':<40}{'top-'+str(topk):>9}")
        for col, lab in {**CANDIDATES, "f_gl_opp": "goal-line opportunity (live, 30%)",
                         "score": "the live TD model"}.items():
            if col not in d.columns:
                continue
            _, hit = topk_hit(d, col, topk)
            print(f"  {lab:<40}{hit:>8.1f}%")


def cmd_sweep(topk: int) -> None:
    """Add each candidate at several weights, taken pro-rata off the others.

    Tuned on one season, reported on both. The number that decides anything is
    the one from the season NOT tuned on.
    """
    live = MODELS["TD"]["w"]
    data = {s: load(s) for s in SEASONS}
    basel = {s: topk_hit(scored(data[s], live), "score", topk)[1] for s in SEASONS}
    print(f"\nlive model, top-{topk}/wk:  " +
          "   ".join(f"{s} {basel[s]:.1f}%" for s in SEASONS))

    for col, lab in CANDIDATES.items():
        print(f"\n── {lab}  ({col})")
        print(f"  {'weight':>7}" + "".join(f"{s:>10}" for s in SEASONS))
        for w in (0.05, 0.08, 0.12, 0.18):
            trial = rescale({**{k: v * (1 - w) for k, v in live.items()}, col: w})
            row = f"  {w:>6.0%} "
            for s in SEASONS:
                d = data[s]
                if d[col].null_count() == d.height:
                    row += f"{'n/a':>10}"
                    continue
                hit = topk_hit(scored(d, trial), "score", topk)[1]
                row += f"{hit - basel[s]:>+9.1f}"
            print(row)
        print("   (change vs the live model, in points of top-15 hit rate)")



# ── THE RESIDUAL SCAN ────────────────────────────────────────────────────────
# A port of bots/missed_signals.py, which is the machine that found the HR
# model's last two real additions. Guessing candidates and sweeping weights —
# everything above this line — tests only what someone already thought of. This
# asks the data the open question instead:
#
#   AMONG PLAYERS THE MODEL SCORED THE SAME, WHAT STILL SEPARATES THE ONES WHO
#   SCORED A TOUCHDOWN?
#
# If a field still sorts inside a score band, the model is not using it. If it
# does not, the model has already priced it however impressive it looks raw.
#
# Three things carried over from missed_signals.py's own scar tissue, each of
# which it learned the hard way:
#   · ties break at RANDOM. Python's sort is stable, rows arrive in score
#     order, and a heavily-tied field would otherwise split by input order.
#     That bug once manufactured a +31.5 point lift out of nothing.
#   · the permutation shuffles the outcome WITHIN each band, so the null is
#     "this field carries nothing the model doesn't already have" rather than
#     the much weaker "this field carries nothing at all".
#   · the within-band correlation of score vs field is printed, so a band too
#     wide to be holding the model fixed cannot hide.
SAFE_CONTEXT = ("implied_total", "total_line", "spread", "wind_mph", "indoors",
                "is_carryover", "inj_q")


def _candidate_cols(d: pl.DataFrame) -> list[str]:
    """Trailing-only columns, minus anything the model already scores.

    ONLY f_*/b_* and a short pre-game context list. Every other numeric column
    in this table is a WEEK-w actual — receiving_tds, carries, the outcome
    itself — and testing those would be measuring the outcome against itself.
    """
    used = {c.lstrip("-") for c in MODELS["TD"]["w"]}
    used |= {"score", "hit", "y", "f_td_actual", "f_xtd", "td_regression"}
    out = []
    for c, t in zip(d.columns, d.dtypes):
        if not t.is_numeric():
            continue
        if c in used or c.startswith("c_"):
            continue
        if c.startswith(("f_", "b_")) or c in SAFE_CONTEXT:
            out.append(c)
    return out


def _bands(d: pl.DataFrame, width: float) -> dict:
    """Score bands, holding the model's own opinion fixed."""
    by = {}
    for row in d.iter_rows(named=True):
        by.setdefault(int(row["score"] // width), []).append(row)
    return by


def _lift(pairs_by_band, rng) -> tuple[float, int, int]:
    hi_ok = hi_n = lo_ok = lo_n = 0
    for pairs in pairs_by_band.values():
        if len(pairs) < 20:
            continue
        pairs = sorted(pairs, key=lambda x: (x[0], rng.random()))
        if pairs[0][0] == pairs[-1][0]:
            continue
        h = len(pairs) // 2
        lo, hi = pairs[:h], pairs[h:]
        lo_ok += sum(y for _v, y in lo); lo_n += len(lo)
        hi_ok += sum(y for _v, y in hi); hi_n += len(hi)
    if not lo_n or not hi_n:
        return 0.0, 0, 0
    return (100 * hi_ok / hi_n - 100 * lo_ok / lo_n), hi_n, lo_n


def _perm_p(pairs_by_band, observed: float, iters: int, rng) -> float:
    vals = {b: [v for v, _ in p] for b, p in pairs_by_band.items()}
    ys = {b: [y for _, y in p] for b, p in pairs_by_band.items()}
    hits = 0
    for _ in range(iters):
        fake = {}
        for b, vv in vals.items():
            yy = ys[b][:]
            rng.shuffle(yy)
            fake[b] = list(zip(vv, yy))
        if abs(_lift(fake, rng)[0]) >= abs(observed):
            hits += 1
    return (hits + 1) / (iters + 1)


def _within_band_r(banded, col: str) -> float | None:
    """Sample-weighted |r(score, field)| inside bands. Near zero = the band is
    genuinely holding the model fixed, which is the assumption everything else
    here rests on."""
    num = den = 0.0
    for rows in banded.values():
        pts = [(r["score"], r[col]) for r in rows if r[col] is not None]
        if len(pts) < 20:
            continue
        n = len(pts)
        mx = sum(a for a, _ in pts) / n
        my = sum(b for _, b in pts) / n
        cov = sum((a - mx) * (b - my) for a, b in pts) / n
        sx = (sum((a - mx) ** 2 for a, _ in pts) / n) ** 0.5
        sy = (sum((b - my) ** 2 for _, b in pts) / n) ** 0.5
        if sx and sy:
            num += abs(cov / (sx * sy)) * n
            den += n
    return num / den if den else None


def cmd_residual(width: float, iters: int, min_n: int, top: int, seed: int) -> None:
    import random
    rng = random.Random(seed)
    for season in SEASONS:
        d = scored(load(season), MODELS["TD"]["w"])
        banded = _bands(d, width)
        usable = {b: r for b, r in banded.items() if len(r) >= 20}
        print(f"\n{season} — {d.height} player-weeks, {len(usable)} usable score "
              f"bands of width {width:g}, base {100 * float(d['hit'].mean()):.1f}%")

        cols = _candidate_cols(d)
        results = []
        for col in cols:
            pbb = {}
            n_tot = 0
            for b, rows in usable.items():
                pairs = [(float(r[col]), int(r["hit"])) for r in rows if r[col] is not None]
                if len(pairs) >= 20:
                    pbb[b] = pairs
                    n_tot += len(pairs)
            if n_tot < min_n or not pbb:
                continue
            lift, hi_n, lo_n = _lift(pbb, rng)
            if hi_n == 0:
                continue
            p = _perm_p(pbb, lift, iters, rng)
            results.append((col, lift, p, n_tot, _within_band_r(usable, col)))

        # Benjamini-Hochberg: 60-odd fields tested at once will hand you three
        # "significant" results by chance alone if you read raw p-values.
        results.sort(key=lambda x: x[2])
        m = len(results)
        out = []
        for i, (col, lift, p, n, r) in enumerate(results, start=1):
            out.append((col, lift, p, min(1.0, p * m / i), n, r))
        out.sort(key=lambda x: -abs(x[1]))

        print(f"  {'field':<34}{'lift':>8}{'p':>8}{'q':>8}{'n':>7}{'r|band':>8}")
        for col, lift, p, q, n, r in out[:top]:
            flag = " *" if q <= 0.05 else ""
            print(f"  {col:<34}{lift:>+7.1f}{p:>8.3f}{q:>8.3f}{n:>7}"
                  f"{(f'{r:.2f}' if r is not None else '  —'):>8}{flag}")
        keep = [o for o in out if o[3] <= 0.05]
        print(f"  {m} fields tested · {len(keep)} survive Benjamini-Hochberg at q<=0.05"
              + (" — none" if not keep else ""))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", action="store_true")
    ap.add_argument("--solo", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--residual", action="store_true")
    ap.add_argument("--width", type=float, default=4.0)
    ap.add_argument("--iters", type=int, default=600)
    ap.add_argument("--min-n", type=int, default=400)
    ap.add_argument("--top", type=int, default=18)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--season", type=int, default=None, help="cache one season only")
    ap.add_argument("--topk", type=int, default=15)
    a = ap.parse_args()
    if a.cache:
        cmd_cache([a.season] if a.season else SEASONS)
    if a.solo:
        cmd_solo(a.topk)
    if a.sweep:
        cmd_sweep(a.topk)
    if a.residual:
        cmd_residual(a.width, a.iters, a.min_n, a.top, a.seed)
    if not (a.cache or a.solo or a.sweep or a.residual):
        ap.print_help()
