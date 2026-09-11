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
    # nflverse publishes a season's parquet only once the season exists -- in
    # August `seasons=[2026]` is a 404, not an empty frame. Grading a week that
    # has not been played is a normal thing to ask for now that the bot prices
    # the upcoming week, so it comes back with nothing rather than an exception.
    try:
        stats = nfl.load_player_stats(seasons=[season], summary_level="week")
    except Exception as exc:
        print(f"  {season} stats unavailable ({type(exc).__name__}: {exc})")
        return pl.DataFrame()
    # Week numbers do not collide across types -- 1-18 REG, 19-22 POST -- so
    # the week alone identifies the games. Filtering to REG here meant a playoff
    # week always graded empty, which the "nothing to grade yet" guard then read
    # as "not played", so no postseason call was ever settled.
    d = stats.filter(pl.col("week") == week)
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



# Where the bot publishes. Same repo and branch publish_data.sh force-pushes to;
# see nfl_bot's dataSource note on the site side for the same URL.
DATA_BASE = ("https://raw.githubusercontent.com/donthebuilder/"
             "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")


def _fetch_archived_card(season: int, week: int, prefix: str):
    """Last week's card, off the data branch. None when it isn't there."""
    import tempfile, urllib.request
    url = f"{DATA_BASE}/{prefix}picks_{season}_w{week:02d}.json"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            if r.status != 200:
                return None
            body = r.read()
        json.loads(body)          # refuse to hand the grader a half-file
        tmp = Path(tempfile.mkdtemp()) / f"{prefix}picks_{season}_w{week:02d}.json"
        tmp.write_bytes(body)
        return tmp
    except Exception as exc:
        print(f"  no archived week {week} card ({type(exc).__name__})")
        return None


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

    # THE WEEK BEING GRADED IS THE CARD'S WEEK. The scheduled workflow passes
    # no --week, and until 2026-09-05 this returned 2 here -- so from the first
    # regular-season run onward nothing would ever have been graded. The card
    # this run just wrote carries its week; that is the week its rungs were
    # promised for, so that is the week to grade. ESPN's current week is the
    # fallback for a run with no card, then the calendar.
    # THE WEEK TO GRADE, which is not the week to price -- see
    # nfl_features.schedule_weeks. This used to be the live card's week, and the
    # live card rolls forward, so week N stopped being gradeable the moment the
    # board moved on: two runs, eight and a half hours after Monday night. Miss
    # that and every Monday-night rung stayed void for good. The schedule keeps
    # week N gradeable until week N+1 kicks off on Thursday.
    sched_grade = None
    season_over = False
    try:
        from nfl_features import schedule_weeks, season_has_ended
        sched_grade = schedule_weeks(a.season)[1]
        season_over = season_has_ended(a.season)
    except Exception as exc:
        print(f"  schedule weeks unavailable ({type(exc).__name__}: {exc})")

    # NOTHING LEFT TO GRADE (item 4, 2026-09-11). nfl_bot.py's build_payload()
    # already stops on its own once schedule_weeks's `price` goes None past
    # the season's last game -- see its own comment on this exact failure
    # mode. Grading had no equivalent: `sched_grade` above freezes on the
    # final week forever rather than expiring (see season_has_ended()'s own
    # docstring), so without this check, nfl.yml's ~12 firings/week would
    # re-grade and re-append an identical final-week payload to results.json
    # AND the outcome log for the entire off-season, every week, until next
    # August -- several hundred KB of duplicate writes a week, forever,
    # rather than the handful of legitimate re-grades an active week
    # produces. Only fires once season_has_ended()'s own grace period has
    # passed, so in-season re-grading (Monday-night corrections, a run
    # picking up a stat fix days later) is untouched, and an explicit
    # --week (a deliberate re-grade on demand) always overrides this, same
    # as it overrides everything below.
    if a.mode == "week" and not a.week and season_over:
        print(f"  the {a.season} season is over -- nothing left to grade, "
              f"leaving the published results and outcome log as they stand")
        return 0

    card_week = None
    try:
        cp0 = Path(a.card)
        if cp0.exists():
            card_week = (json.loads(cp0.read_text()) or {}).get("week")
    except Exception:
        card_week = None
    if a.mode == "week" and not a.week:
        a.week = int(sched_grade) if sched_grade else (
            int(card_week) if card_week else nfl_espn.resolve_week(a.season, None))
    # PRESEASON NEVER GOT A WEEK NUMBER AT ALL (item 6, 2026-09-11) -- the
    # block above only ever resolves a.week for mode == "week", so every
    # scheduled preseason grading run left a.week at argparse's None default
    # and the archive write below (`if a.week: ...`) never fired. The site's
    # own lib/nfl/resultsArchive.js unconditionally asks for
    # nfl_results_<season>_p01..p04.json on every load regardless -- those
    # four fetches have 404ed for the entire preseason, every visit, since
    # the per-week archive shipped 2026-09-05. See
    # nfl_espn.preseason_week_from_date()'s own docstring for why an
    # approximate calendar-based week number is good enough here.
    if a.mode == "preseason" and not a.week:
        a.week = nfl_espn.preseason_week_from_date(a.season)
    print(f"grading {a.mode} · season {a.season}" + (f" · week {a.week}" if a.week else ""))
    if a.mode == "week":
        lines = _reg_lines(a.season, a.week)
    else:
        lines = _pre_lines(a.season, a.week)
    print(f"  {lines.height} player line(s)")
    # NOTHING PLAYED YET IS NOT A GRADE OF ZERO. Writing an empty results file
    # would blow the season-to-date record and the week archive away every time
    # the bot builds a card for a week that has not kicked off -- which, now
    # that the bot prices the upcoming week, is most of its runs.
    if lines.is_empty():
        print("  nothing to grade yet -- leaving the existing results untouched")
        return 0

    actual = outcomes(lines)
    # See eligible_lines()'s docstring for why this join has to happen by
    # position, not by truthiness.
    positions = {str(r["player_id"]): r.get("position") or "" for r in lines.iter_rows(named=True)}

    # THE CARD FOR THE WEEK BEING GRADED, not whatever the live file happens to
    # hold. Once pricing and grading came apart, `nfl_picks.json` on a Tuesday
    # is already week N+1 -- grading week N's outcomes against it would score
    # the wrong men. nfl_bot writes a per-week copy next to it; prefer that.
    #
    # It will not be in the workspace on a later run: the runner checks out
    # main, and last week's output only exists on the data branch. So fetch it
    # from there, the same way pick_lock.py fetches its ledger back. Fails soft
    # to the live card, which is correct whenever the two weeks agree.
    card = {}
    cp = Path(a.card)
    if a.week:
        arch = cp.parent / f"{a.prefix}picks_{a.season}_w{int(a.week):02d}.json"
        if arch.exists():
            cp = arch
            print(f"  grading against {arch.name}")
        else:
            fetched = _fetch_archived_card(a.season, int(a.week), a.prefix)
            if fetched is not None:
                cp = fetched
                print(f"  grading against the data branch's week {a.week} card")
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
    body = json.dumps(payload, separators=(",", ":"))
    dest = out / f"{a.prefix}results.json"
    dest.write_text(body)
    print(f"wrote {dest} ({dest.stat().st_size / 1024:.0f} KB)")
    # THE ARCHIVE THE SITE CAN GUESS (2026-09-05). The outcome log below keeps
    # every pass, but its filename carries the run date, which a static fetch
    # list can't know. This is one file per graded WEEK under a fixed name --
    # nfl_results_2026_w03.json, p02 for preseason -- rewritten in place on
    # every pass of the same week so the last grade wins. publish_data.sh
    # carries them forward; the site's lib/nfl/resultsArchive.js harvests
    # them for the season-to-date record and the week picker.
    if a.week:
        tag = f"{'p' if a.mode == 'preseason' else 'w'}{int(a.week):02d}"
        arch = out / f"{a.prefix}results_{a.season}_{tag}.json"
        arch.write_text(body)
        print(f"wrote {arch.name}")
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
