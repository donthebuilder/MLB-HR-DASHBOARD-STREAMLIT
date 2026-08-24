#!/usr/bin/env python3
"""nfl_results.py — grade the pick card.

    python nfl_results.py --mode preseason --season 2026
    python nfl_results.py --mode week --season 2026 --week 3

TWO SOURCES, ONE GRADING RULE.

  REGULAR SEASON   nflreadpy.load_player_stats(summary_level="week"). Clean,
                   canonical, keyed by gsis id.
  PRESEASON        ESPN box scores. nflverse carries NO preseason at all, so
                   this is the only free source — the same reason nfl_espn.py
                   exists for the schedule.

Whatever the source, the numbers land in nflverse's OWN column names and are
graded by the SAME `OUTCOME` expressions nfl_scoring.py uses and the backtest
grades on. That is the whole discipline here: if the card's hit rate were
computed by a second implementation of "did he clear 40 receiving yards", the
report card and the live record would drift apart and both would look right.

ESPN athlete ids are not gsis ids, so preseason lines are joined through
load_players()'s espn_id -> gsis_id mapping. Where that join fails the player
is dropped rather than name-matched: a wrong join silently credits one man's
touchdown to another, which is worse than an ungraded rung.

WHAT GETS PUBLISHED. Every player who recorded a line, not just the card. The
site lets you swap your own name into any rung, and he can be anyone on the
slate — publishing only the card's ten men would make most overrides ungradeable.
A full week is a few hundred rows of seven small numbers; it is not worth being
clever about.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
from pathlib import Path

import polars as pl

import nfl_espn
from nfl_scoring import MODELS, OUTCOME

# The columns every source must produce. OUTCOME is written against these.
STATS = ["passing_yards", "carries", "rushing_yards", "rushing_tds",
         "receptions", "receiving_yards", "receiving_tds", "fg_made", "pat_made"]


# ── regular season ────────────────────────────────────────────────────────────

def _reg_lines(season: int, week: int) -> pl.DataFrame:
    import nflreadpy as nfl
    d = (nfl.load_player_stats(seasons=[season], summary_level="week")
           .filter(pl.col("season_type") == "REG", pl.col("week") == week))
    have = set(d.columns)
    # A stat a source doesn't carry becomes 0, not null — OUTCOME sums columns
    # and a single null would poison a whole market's grade into null.
    d = d.with_columns([
        (pl.col(c) if c in have else pl.lit(0)).fill_null(0).cast(pl.Float64).alias(c)
        for c in STATS
    ])
    name_col = "player_display_name" if "player_display_name" in have else "player_name"
    return d.select([
        pl.col("player_id").cast(pl.Utf8),
        pl.col(name_col).alias("name"),
        pl.col("team").cast(pl.Utf8) if "team" in have else pl.lit("").alias("team"),
        pl.col("position").cast(pl.Utf8) if "position" in have else pl.lit("").alias("position"),
        *[pl.col(c) for c in STATS],
    ])


# ── preseason ─────────────────────────────────────────────────────────────────

def _espn_to_gsis() -> tuple[dict[str, str], dict[str, str]]:
    """(espn_id -> gsis_id, gsis_id -> position). Empty dicts if the players
    table is unavailable. Position rides the same load as the espn/gsis xref
    so preseason lines can carry a position column exactly like _reg_lines()
    does, with no second API call."""
    try:
        import nflreadpy as nfl
        p = nfl.load_players()
    except Exception as exc:
        print(f"  players table unavailable ({type(exc).__name__}) — preseason cannot be joined")
        return {}, {}
    if "espn_id" not in p.columns or "gsis_id" not in p.columns:
        print("  players table has no espn_id/gsis_id pair")
        return {}, {}
    has_pos = "position" in p.columns
    cols = ["espn_id", "gsis_id"] + (["position"] if has_pos else [])
    espn_gsis: dict[str, str] = {}
    gsis_pos: dict[str, str] = {}
    for r in p.select(cols).iter_rows():
        e, g = r[0], r[1]
        pos = r[2] if has_pos else None
        if e is None or g is None:
            continue
        # espn_id arrives as a float on some builds — 12345.0 must key as "12345".
        e = str(e).strip()
        if e.endswith(".0"):
            e = e[:-2]
        if e:
            espn_gsis[e] = str(g)
        if pos:
            gsis_pos[str(g)] = str(pos)
    return espn_gsis, gsis_pos


def _pre_lines(season: int, week: int | None) -> pl.DataFrame:
    games = [g for g in nfl_espn.fetch(seasontype=1, week=week, year=season)
             if g.get("completed")]
    print(f"  {len(games)} completed preseason game(s)")
    if not games:
        return pl.DataFrame(schema={"player_id": pl.Utf8, "name": pl.Utf8, "team": pl.Utf8,
                                    "position": pl.Utf8, **{c: pl.Float64 for c in STATS}})
    xref, pos_by_gsis = _espn_to_gsis()
    rows, unjoined = [], 0
    for g in games:
        for r in nfl_espn.box_score(g["game_id"]):
            gsis = xref.get(str(r.get("espn_id") or ""))
            if not gsis:
                unjoined += 1
                continue
            rows.append({"player_id": gsis, "name": r.get("name") or "",
                         "team": r.get("team") or "",
                         "position": pos_by_gsis.get(gsis, ""),
                         **{c: float(r.get(c) or 0.0) for c in STATS}})
    if unjoined:
        print(f"  {unjoined} ESPN line(s) had no gsis match — dropped, not name-matched")
    if not rows:
        return pl.DataFrame(schema={"player_id": pl.Utf8, "name": pl.Utf8, "team": pl.Utf8,
                                    "position": pl.Utf8, **{c: pl.Float64 for c in STATS}})
    # A man can appear in two categories of the same box score; sum, don't
    # overwrite, or a rusher who also caught a pass loses one of the two.
    return (pl.DataFrame(rows)
              .group_by("player_id")
              .agg([pl.col("name").first(), pl.col("team").first(), pl.col("position").first(),
                    *[pl.col(c).sum() for c in STATS]]))


# ── grading ───────────────────────────────────────────────────────────────────

def outcomes(lines: pl.DataFrame) -> dict[str, dict[str, float]]:
    """{player_id: {market_key: actual}} using the backtest's own expressions."""
    if not lines.height:
        return {}
    d = lines.with_columns([OUTCOME[k].alias(f"_o_{k}") for k in MODELS])
    out: dict[str, dict[str, float]] = {}
    for r in d.iter_rows(named=True):
        out[str(r["player_id"])] = {k: float(r[f"_o_{k}"] or 0.0) for k in MODELS}
    return out


def eligible_lines(actual: dict[str, dict[str, float]],
                    positions: dict[str, str]) -> dict[str, dict[str, float]]:
    """Filter outcomes()'s {player_id: {market: value}} down to the markets
    each player is actually eligible for, keyed by MODELS[market]["pos"] --
    the same position list nfl_scoring.score() filters its own pool on.

    outcomes() defaults every one of the 7 markets to 0.0 for every player
    (float(... or 0.0)), so truthiness (`if v`) can't distinguish "he
    genuinely went scoreless" (an RB with 0 TDs -- a real miss) from "this
    market doesn't apply to him" (a kicker's REC_YDS) -- both are 0.0.
    Position eligibility is the only thing that actually tells them apart.

    A player with unknown position (positions.get(pid) is None, e.g. a
    preseason ESPN row whose gsis join found no position) is ineligible for
    every market -- the same drop-rather-than-guess call _espn_to_gsis()
    already makes for an unjoined line."""
    return {
        pid: {k: v for k, v in vals.items() if positions.get(pid) in MODELS[k]["pos"]}
        for pid, vals in actual.items()
    }


def grade(card: dict, actual: dict) -> tuple[dict, dict]:
    """Score every rung. Returns (graded card, per-market totals)."""
    graded, totals = {}, {}
    for key, blk in (card or {}).items():
        bar = float(blk.get("bar", 1))
        rungs, hit, n = [], 0, 0
        for r in blk.get("rungs", []):
            line = actual.get(str(r.get("player_id")))
            # No line at all = did not play (inactive, cut, never dressed).
            # VOID, not a miss — the same rule the MLB tracker and the watch
            # ledger use, and the same reason: an unasked question has no answer.
            val = None if line is None else line.get(key)
            ok = None if val is None else bool(val >= bar)
            if ok is not None:
                n += 1
                hit += 1 if ok else 0
            rungs.append({**r, "actual": val, "hit": ok})
        graded[key] = {**blk, "rungs": rungs}
        totals[key] = {"n": n, "hit": hit,
                       "pct": round(100 * hit / n, 1) if n else None,
                       "void": len(blk.get("rungs", [])) - n}
    return graded, totals


# ── MODEL FOUNDATION: outcome log (2026-08-24) ───────────────────────────────
#
# results.json is OVERWRITTEN every single grading run (nfl.yml's "Grade the
# card" step runs on every one of its ~12 scheduled firings/week, continue-
# on-error, unconditional) -- so without this, every earlier grading pass's
# numbers are gone the instant a newer one lands, the same loss the MLB side
# closed with bots/live_results_tracker.py's append_outcome_log()/
# write_outcome_log(). This is that idea's NFL sibling, not a port of its
# mechanics: MLB's version keys one file per SLATE NIGHT and appends one
# revision per player-GAME (player_game_id = "{game_pk}|{player_id}"), with
# a supersedes chain, because MLB grades one night's games at a time. NFL
# grades a whole WEEK at once (see grade() above) and a week's games span
# three-plus calendar dates (Thu/Sun/Mon), so there is no single "the slate
# night" to key a file by, and no single game_pk this payload belongs to.
# The natural unit here is one line per grading RUN (this function's whole
# `payload` -- card, totals, lines, names -- as it stood when this pass
# finished), appended to a file named for the UTC calendar date the run
# executed on. That date describes "when this grading pass ran," not "the
# night of the game" the way MLB's does -- a real difference from MLB's
# shape, documented here rather than silently assumed away.

def append_nfl_outcome_log(payload: dict, now: dt.datetime, out_dir: Path, prefix: str = "") -> "Path | None":
    """Append this grading run's full payload as one line to
    {out_dir}/{prefix}outcome_log_{date}.jsonl, `date` = `now`'s UTC
    calendar date. Grading is idempotent and re-run often (every firing, per
    nfl.yml's own comment on the "Grade the card" step) -- appending every
    call rather than de-duplicating means a day with several grading passes
    (a live Sunday, waves 3 hours apart) accumulates several lines, each a
    true record of what the card looked like at that point in the week; the
    caller can always take the last line for "latest," and no earlier
    revision is ever overwritten or lost. Best-effort: an outcome-log
    failure must never block the results.json the rest of main() already
    wrote."""
    try:
        date_str = now.date().isoformat()
        path = out_dir / f"{prefix}outcome_log_{date_str}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str))
            f.write("\n")
        return path
    except Exception as exc:
        print(f"nfl outcome log append failed: {exc}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["preseason", "week"], default="preseason")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--card", type=str, default="../public/data/nfl/picks.json")
    ap.add_argument("--out", type=str, default="../public/data/nfl")
    ap.add_argument("--prefix", type=str, default="")
    a = ap.parse_args()

    print(f"grading {a.mode} · season {a.season}" + (f" · week {a.week}" if a.week else ""))
    if a.mode == "week":
        if not a.week:
            print("--week is required in week mode")
            return 2
        lines = _reg_lines(a.season, a.week)
    else:
        lines = _pre_lines(a.season, a.week)
    print(f"  {lines.height} player line(s)")

    actual = outcomes(lines)
    # See eligible_lines()'s docstring for why this join has to happen by
    # position, not by truthiness.
    positions = {str(r["player_id"]): r.get("position") or "" for r in lines.iter_rows(named=True)}

    card = {}
    cp = Path(a.card)
    if cp.exists():
        try:
            card = (json.loads(cp.read_text()) or {}).get("card", {})
        except Exception as exc:
            print(f"  card unreadable ({type(exc).__name__}) — publishing lines only")
    else:
        print(f"  no card at {cp} — publishing lines only")

    graded, totals = grade(card, actual)

    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "season": a.season,
        "week": a.week,
        "mode": a.mode,
        # PRESEASON IS COUNTED, AND LABELLED. Donovan asked for every game to
        # count. Starters play two series, so these lines are thin by nature —
        # the flag rides on the payload so the site can say so next to a record
        # built partly out of exhibition football.
        "exhibition": a.mode == "preseason",
        "graded_at": now.isoformat(),
        "graded_at_human": now.strftime("%b %-d, %-I:%M %p UTC"),
        "bars": {k: m["bar"] for k, m in MODELS.items()},
        # Every player who recorded a line — see the module docstring.
        "lines": eligible_lines(actual, positions),
        "names": {str(r["player_id"]): r["name"] for r in lines.iter_rows(named=True)},
        "card": graded,
        "totals": totals,
    }

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{a.prefix}results.json"
    dest.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {dest} ({dest.stat().st_size / 1024:.0f} KB)")
    for k, t in totals.items():
        if t["n"]:
            print(f"  {k:<9} {t['hit']}/{t['n']}  {t['pct']:.0f}%"
                  + (f"  ({t['void']} void)" if t["void"] else ""))

    log_path = append_nfl_outcome_log(payload, now, out, a.prefix)
    if log_path is not None:
        print(f"  outcome log: {log_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
