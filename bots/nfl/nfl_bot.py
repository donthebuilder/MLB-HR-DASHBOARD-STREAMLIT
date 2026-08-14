#!/usr/bin/env python3
"""nfl_bot.py — builds the JSON the NFL side of moonshot reads.

    python nfl_bot.py --mode preseason --out ../public/data/nfl
    python nfl_bot.py --mode week --season 2026 --week 1 --out ../public/data/nfl

Two modes, because August and October are not the same problem.

PRESEASON  Starters play two series. Weekly form does not exist and pretending
           otherwise would be inventing numbers. So the boards are built from
           last season's per-game baselines — the futures read — and every row
           is stamped `carryover: true` so the site can say so out loud.

WEEK       The real thing: trailing form, injuries, game context, the seven
           models scored as documented in SCORING.md.

Writes small files on purpose. moonshot-mlb is a read-only site and the MLB
side already learned the cost of committing 100MB payloads to a repo a
browser has to fetch.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
from pathlib import Path

import polars as pl

import nfl_espn
from nfl_features import build, season_baseline, PLAYER_FORM, USAGE_FORM
from nfl_scoring import MODELS, OUTCOME, score, derive, _pctile

PHX = dt.timezone(dt.timedelta(hours=-7))

# What the Research tab shows. Order is the column order on the site.
# (column, short label, description, decimal places, render as percent?)
#
# `dp` is NOT cosmetic. DenseTable defaults to toFixed(0), so every rate on
# this table -- target share, xTD, TDs per game, TDoE -- rendered as 0 or 1
# and the whole board read as noise. Precision is a property of the stat, so
# it's declared here with the stat and shipped in the payload.
RESEARCH = [
    ("f_target_share", "TGT%", "Share of his team's targets", 1, True),
    ("f_wopr", "WOPR", "Weighted opportunity — target share + air yards share", 3, False),
    ("f_targets", "TGT", "Targets per game", 1, False),
    ("f_receptions", "REC", "Receptions per game", 1, False),
    ("f_receiving_yards", "RECYD", "Receiving yards per game", 1, False),
    ("f_receiving_air_yards", "AIRYD", "Air yards per game — depth of target", 1, False),
    ("f_receiving_20", "20+", "Receptions of 20+ yards per game", 2, False),
    ("f_carries", "CAR", "Carries per game", 1, False),
    ("f_rushing_yards", "RUYD", "Rushing yards per game", 1, False),
    ("f_rz_opp", "RZ", "Red-zone touches per game", 2, False),
    ("f_gl_opp", "GL", "Goal-line touches — inside-10 targets, inside-5 carries", 2, False),
    ("f_xtd", "xTD", "Expected TDs per game from field position", 2, False),
    ("f_td_actual", "TD", "Actual TDs per game", 2, False),
    ("td_regression", "TDoE", "Expected minus actual — positive means he's due", 2, False),
    ("f_ngs_avg_separation", "SEP", "Average separation at the catch point (NGS)", 2, False),
    ("f_ngs_avg_yac_above_expectation", "YACOE", "YAC above expected (NGS)", 2, False),
    ("f_ngs_rush_yards_over_expected_per_att", "RYOE", "Rush yards over expected per attempt (NGS)", 2, False),
    ("f_passing_yards", "PAYD", "Passing yards per game", 1, False),
    ("f_attempts", "ATT", "Pass attempts per game", 1, False),
    ("f_passing_cpoe", "CPOE", "Completion % over expected", 1, False),
    ("f_fg_made", "FGM", "Field goals made per game", 2, False),
    ("f_pat_made", "PAT", "Extra points made per game", 2, False),
]


def _num(v):
    """JSON-safe number. NaN/inf become null rather than invalid JSON."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 3) if math.isfinite(f) else None


# ── preseason: last season's baselines, honestly labelled ─────────────────────

def preseason_rows(prior_season: int, teams: set[str]) -> pl.DataFrame:
    """Per-player baselines from the completed season, for the teams playing."""
    import nflreadpy as nfl
    base = season_baseline(prior_season)
    who = (nfl.load_player_stats(seasons=[prior_season], summary_level="week")
             .filter(pl.col("season_type") == "REG")
             .group_by("player_id").agg(
                 pl.col("player_display_name").last().alias("name"),
                 pl.col("position").last().alias("position"),
                 pl.col("team").last().alias("team")))
    # 2026 rosters place the player on the team he's actually on now.
    try:
        cur = (nfl.load_rosters_weekly(seasons=[prior_season + 1])
                 .group_by("gsis_id").agg(pl.col("team").last().alias("team_now")))
        who = who.join(cur, left_on="player_id", right_on="gsis_id", how="left") \
                 .with_columns(pl.coalesce(["team_now", "team"]).alias("team"))
    except Exception:
        pass
    d = base.join(who, on="player_id", how="inner").filter(pl.col("team").is_in(list(teams)))
    # rename b_* -> f_* so the same scoring code runs unchanged
    ren = {c: "f_" + c[2:] for c in d.columns if c.startswith("b_") and c != "b_gp"}
    return d.rename(ren).with_columns(
        pl.lit(1).cast(pl.Int8).alias("is_carryover"),
        pl.lit(0).cast(pl.Int8).alias("inj_q"),
    )


def _fill_missing(d: pl.DataFrame) -> pl.DataFrame:
    """Preseason has no game context and no NGS window — neutralise, don't fake."""
    needed = {
        "implied_total": None, "total_line": None, "spread": 0.0, "indoors": 0,
        "wind_mph": 0.0, "f_opp_d_rec_td": None, "f_opp_d_rush_td": None,
        "f_opp_d_pass_yds": None, "f_opp_d_rush_yds": None,
        "f_tm_fg_drive_rate": None, "f_tm_rz_td_rate": None, "f_tm_drives": None,
    }
    add = []
    # _pctile ranks .over("week"); preseason is one slate, so give it one.
    if "week" not in d.columns:
        add.append(pl.lit(0).cast(pl.Int32).alias("week"))
    for c, dflt in needed.items():
        if c not in d.columns:
            add.append(pl.lit(dflt).cast(pl.Float64).alias(c))
    for c, _, _, _dp, _pct in RESEARCH:
        if c not in d.columns and c != "td_regression":
            add.append(pl.lit(None).cast(pl.Float64).alias(c))
    return d.with_columns(add) if add else d


def score_all(tbl: pl.DataFrame, context_ok: bool) -> dict:
    """Score every market. When context is missing, its weight is redistributed
    across the components that ARE present rather than scored as zero."""
    out = {}
    for key, m in MODELS.items():
        avail, missing = {}, []
        for raw, w in m["w"].items():
            col = raw.lstrip("-")
            has = col in tbl.columns and tbl[col].null_count() < tbl.height
            if has and (context_ok or not col in CONTEXT_COLS):
                avail[raw] = w
            else:
                missing.append(raw)
        if not avail:
            continue
        total = sum(avail.values())
        avail = {k: v / total for k, v in avail.items()}   # renormalise to 1.0
        d = derive(tbl).filter(pl.col("position").is_in(m["pos"]))
        if d.height == 0:
            continue
        parts = []
        for raw, w in avail.items():
            inv = raw.startswith("-")
            col = raw.lstrip("-")
            nm = f"c_{col}"
            d = d.with_columns(_pctile(d, col, inv).alias(nm))
            parts.append(pl.col(nm) * w)
        tot = parts[0]
        for p in parts[1:]:
            tot = tot + p
        # ORDER MATTERS HERE. `tot` is a LAZY expression over the c_ columns,
        # so it has to be materialised BEFORE those columns are rescaled --
        # otherwise it resolves against the already-x100 values and the score
        # comes out x10000 (98.88 shipped as 9888, which is what the Research
        # table was showing).
        d = d.with_columns((tot * 100).alias("score"))
        # Components go out as 0-100 too, not the raw 0-1 percentile. The modal
        # renders them as "90 = top 10% of this slate on that input"; shipping
        # the fraction made every one of them round to 1.
        d = d.with_columns([(pl.col(f"c_{r.lstrip('-')}") * 100).round(0)
                            .alias(f"c_{r.lstrip('-')}") for r in avail])
        out[key] = {"df": d, "dropped": missing,
                    "weights": {k.lstrip("-"): round(v, 3) for k, v in avail.items()}}
    return out


CONTEXT_COLS = {"implied_total", "total_line", "opp_td_soft", "opp_pass_soft",
                "opp_rush_soft", "pass_script", "run_script", "kick_env",
                "f_tm_fg_drive_rate", "f_tm_rz_td_rate", "f_tm_drives"}


def build_payload(mode: str, season: int, week: int | None, out_dir: Path) -> dict:
    now = dt.datetime.now(PHX)

    if mode == "preseason":
        games = nfl_espn.fetch(seasontype=1, year=season)
        today = now.date().isoformat()
        upcoming = [g for g in games if str(g.get("kickoff", ""))[:10] >= today] or games
        seed = out_dir / "slate_seed.json"
        if not upcoming and seed.exists():
            upcoming = json.loads(seed.read_text()).get("games", [])
        teams = {t for g in upcoming for t in (g["home"], g["away"])}
        tbl = _fill_missing(preseason_rows(season - 1, teams))
        context_ok = False
        label = f"Preseason · {now:%b %-d}"
    else:
        tbl = build(season)
        if week:
            tbl = tbl.filter(pl.col("week") == week)
        games = nfl_espn.fetch(seasontype=2, year=season, week=week)
        upcoming = games
        context_ok = True
        label = f"Week {week}"
        tbl = tbl.with_columns(pl.col("player_display_name").alias("name"))

    scored = score_all(tbl, context_ok)

    # merge every market's score onto one player row
    players: dict[str, dict] = {}
    for key, blob in scored.items():
        d = blob["df"]
        comp_cols = [c for c in d.columns if c.startswith("c_")]
        for r in d.iter_rows(named=True):
            pid = r["player_id"]
            p = players.setdefault(pid, {
                "player_id": pid,
                "name": r.get("name") or r.get("player_display_name") or "—",
                "team": r.get("team"),
                "opp": r.get("opponent_team"),
                "position": r.get("position"),
                "carryover": bool(r.get("is_carryover") or 0),
                "questionable": bool(r.get("inj_q") or 0),
                # SAMPLE GATE. A goal-line vulture with 0.3 targets a game can
                # percentile-rank above a every-down back, because a rate built
                # on four touches has no business sitting at the same visual
                # weight as one built on two hundred. The site dims these rows
                # rather than hiding them — same call the MLB DenseTable makes.
                "low_sample": (
                    (r.get("f_targets") or 0) + (r.get("f_carries") or 0) < 3.0
                    and (r.get("position") in ("RB", "WR", "TE"))
                ),
                "scores": {}, "components": {}, "stats": {},
            })
            p["scores"][key] = _num(r.get("score"))
            p["components"][key] = {c[2:]: _num(r.get(c)) for c in comp_cols
                                    if _num(r.get(c)) is not None}
            for col, short, _, _dp, _pct in RESEARCH:
                v = _num(r.get(col))
                # Skip exact zeros: a running back carries PAYD/ATT/CPOE/FGM/PAT
                # as 0.000 and a wall of zeroes in the modal reads as data when
                # it's really "this stat doesn't apply to him".
                if col in r and v is not None and v != 0:
                    p["stats"][short] = v

    # preseason: attach opponent from the game list
    if mode == "preseason":
        opp = {}
        for g in upcoming:
            opp[g["home"]] = g["away"]
            opp[g["away"]] = g["home"]
        for p in players.values():
            p["opp"] = opp.get(p["team"])

    rows = sorted(players.values(),
                  key=lambda p: -(p["scores"].get("TD") or 0))

    return {
        "mode": mode,
        "season": season,
        "week": week,
        "label": label,
        "built_at": now.isoformat(),
        "built_at_human": now.strftime("%b %-d, %-I:%M %p") + " PHX",
        "context_available": context_ok,
        "games": upcoming,
        "players": rows,
        "markets": [
            {"key": k, "label": m["label"], "bar": m["bar"], "positions": m["pos"],
             "weights": scored.get(k, {}).get("weights", m["w"]),
             "dropped": scored.get(k, {}).get("dropped", [])}
            for k, m in MODELS.items() if k in scored
        ],
        "research_columns": [{"key": s, "label": s, "desc": d, "dp": dp, "pct": pct}
                             for _, s, d, dp, pct in RESEARCH],
        "counts": {"players": len(rows), "games": len(upcoming)},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["preseason", "week"], default="preseason")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--out", type=str, default="../public/data/nfl")
    # publish_data.sh only ever copies files out of public/data/current/, by
    # name. Rather than fork that script -- it handles concurrent publishers
    # and orphan-branch force-push correctly and is not worth reimplementing --
    # the workflow writes straight into current/ with an nfl_ prefix and the
    # three filenames get added to its PUBLISH_FILES list.
    ap.add_argument("--prefix", type=str, default="")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = build_payload(a.mode, a.season, a.week, out)
    (out / f"{a.prefix}week.json").write_text(json.dumps(payload, separators=(",", ":")))
    (out / f"{a.prefix}meta.json").write_text(json.dumps({
        "built_at": payload["built_at"],
        "built_at_human": payload["built_at_human"],
        "mode": payload["mode"], "label": payload["label"],
        "counts": payload["counts"],
    }, indent=2))
    kb = (out / f"{a.prefix}week.json").stat().st_size / 1024
    print(f"wrote {out}/{a.prefix}week.json  ({kb:.0f} KB, "
          f"{payload['counts']['players']} players, {payload['counts']['games']} games)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
