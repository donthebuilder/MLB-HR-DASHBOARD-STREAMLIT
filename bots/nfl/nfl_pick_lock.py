#!/usr/bin/env python3
"""nfl_pick_lock.py — TUDDY's sibling of bots/pick_lock.py.

B10(b)/(e), 2026-09-15. Donovan approved the plumbing for both blocked B10
items in one AskUserQuestion: "Build the plumbing now, flags later." The
trace that led here (see claude/STATUS.md's 2026-09-15 "latest" section in
the DASH Network project) found that (b) Storylines-graded and (e) Signals
were logged as blocked on "missing archived badge/flag data" -- true, but
one layer shallow. NFL has ZERO model-derived signal flags published
anywhere at all (nothing like MLB's weak_spot_flag/hrw_score/
pitch_type_match_flag), so even a working archive would have nothing to
lock yet. This file is the archive, built now, so that whenever real flags
get designed and added to nfl_scoring.py, they land on top of infrastructure
that has already been accumulating honest pregame-locked history -- rather
than every future flag needing its own bespoke freeze mechanism, or shipping
against a live recompute the way the card already silently does today.

TWO INDEPENDENT LOCKS, one file, exactly mirroring pick_lock.py's own split
(that file's block comment: "Everything above this decides WHO holds a
designation. This decides WHICH RUN'S NUMBERS are official... a different
question."):

1. CARD LOCK — nfl_picks.py's ranked ladder (5 rungs per market: TD,
   REC_YDS, REC, RUSH_YDS, RUSH_ATT, PASS_YDS, KICK_PTS), keyed on
   "market|rank" the same way pick_lock.py keys a pool/pair ticket on its
   stable slot rather than its occupant. nfl_picks.py's own header already
   names the bug this closes: "Rewritten in place on every pass of the same
   run of the same week, so the last one wins" -- a player who held rank 1
   Tuesday can be bumped by Sunday's recompute even after his own game has
   already kicked off and he has already gone (or not gone) for the score,
   exactly the "receipts card claims impossible" bug pick_lock.py exists to
   stop on the MLB side, one level up (a market ladder rung instead of a
   per-game category).

2. PREDICTION OF RECORD — for every game_id, which run's numbers are
   official, locked the instant `now` first crosses that game's OWN
   kickoff. Ported using the exact "pregame run, from disk" fix pick_lock.py
   shipped 2026-09-14 (see that file's own block comment): the run standing
   at kickoff is not "whatever this execution just rebuilt after noticing
   the game started," it's the LATEST nfl_prediction_log_*.jsonl generated
   BEFORE kickoff that actually covered that game. Falls back to rows
   standing now, honestly flagged locked_late, only when no pregame log
   exists for that game (a late first build of the week).

STAGGERED KICKOFFS, NOT ONE SLATE A NIGHT. MLB freezes on first pitch, one
event a game, ~15 games a night, all within a few hours of each other. NFL's
card spans a whole week -- Thursday night, four Sunday windows, Sunday
night, Monday night -- so "the slate started" is not one instant the way a
baseball night's first pitch effectively is. Locking has to be evaluated
PER GAME_ID, independently, exactly the granularity pick_lock.py already
uses for prediction_of_record (keyed on game_pk, not on "today"). A rung
occupied by a Thursday-night player locks on Thursday while the other four
rungs in that same market are still fully open to a legitimate pre-game
re-pick through the weekend.

WHY GAME_ID COMES FROM THE CURRENT WEEK.json, NOT FROM EACH HISTORICAL RUN.
nfl_prediction_log lines carry `team`/`opp` but no `game_id` (unlike the
card, which nfl_bot.py never had to key by game either -- see nfl_picks.py's
own header on why NFL uses a week-wide ladder, not per-game slots). A team
plays exactly one game in a given week (a bye aside), so team -> game_id is
a fixed fact of the CURRENT week's schedule, not something that needs to be
re-derived per historical run -- reading it fresh off this run's own
nfl_week.json is correct, not a leak: kickoff time and the matchup are
schedule facts, known from the moment the week opens, never a live score.

WHAT THIS DOES NOT DO. No signal flags of any kind -- none exist to lock.
No designation lock inside a game (NFL has no per-game category the way
MLB's TOP/HR/HIT/HRR/CONTACT are -- see nfl_picks.py's header for why a
week-wide ladder replaces that shape entirely). No odds freeze (see
nfl_odds_fetch.py's own module docstring, item 4 -- a separate, still-open
port). No preseason support -- preseason has no week number, and this
file's whole design keys on (season, week); it exits cleanly and does
nothing until the regular season starts, rather than inventing a
preseason-shaped identity nothing downstream needs yet.

STATE lives in {prefix}pick_lock.json on the data branch, fetched back over
HTTPS the same way pick_lock.py does (the runner checks out `main`; last
run's ledger only exists on `data`). Resets to a fresh ledger the moment
(season, week) changes, same as pick_lock.py resets on slate date. The
prediction-of-record half is ALSO durably appended, forever, to
{prefix}por_log_{season}_w{week:02d}.jsonl the instant each game_id locks --
por_log_*.jsonl's own reason applies here unchanged: the ledger's own
prediction_of_record key is wiped the moment the week rolls, so without a
durable copy a future eval pass could only ever see the current week.

Usage (in nfl.yml, AFTER "Build slate"/"Fetch NFL odds" and BEFORE
"Grade the card" -- so grading always grades the locked ladder, not the raw
recompute):
    python nfl_pick_lock.py --apply --dir "$GITHUB_WORKSPACE/public/data/current" --prefix nfl_
    python nfl_pick_lock.py --apply --dry-run --dir ... --prefix nfl_   # report, change nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob as globmod
import json
import urllib.request
from pathlib import Path
from typing import Any

RAW = ("https://raw.githubusercontent.com/donthebuilder/"
       "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")

# Must match nfl_picks.DEPTH. Not imported from nfl_scoring/nfl_picks on
# purpose -- this script only ever reads the already-published card JSON,
# never the model dependency tree, so it stays runnable standalone (same
# reasoning nfl_bot.py's own module docstring gives for reimplementing
# _current_git_sha()/_run_env_metadata() rather than importing mlb_dashboard).
DEPTH = 5

STUB_FIELDS = ("player_id", "name", "team", "opp", "position")
# The rung fields that are frozen at lock time, restored verbatim onto a
# locked rung rather than let a later recompute silently update them --
# they were computed FOR the occupant standing at that instant, the same
# reasoning as pick_lock.py's own TICKET_FIELDS comment.
RUNG_META_FIELDS = ("score", "grade", "low_sample", "questionable", "carryover")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(v: Any) -> dt.datetime | None:
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def fetch_lock(prefix: str, season: int, week: int, current_dir: Path) -> dict:
    """Last run's ledger for this exact (season, week). A miss, or a ledger
    for a different week, is a fresh start -- same rule as pick_lock.py's
    fetch_lock() discarding a ledger for a different slate date.

    LOCAL FIRST, for the same reason: a ledger for this (season, week)
    already sitting in the checkout was written by an earlier invocation in
    THIS SAME job and is by definition fresher than the branch.
    """
    local = current_dir / f"{prefix}pick_lock.json"
    j = load_json(local)
    if isinstance(j, dict) and j.get("season") == season and j.get("week") == week:
        return j

    url = f"{RAW}/{prefix}pick_lock.json?t={int(now_utc().timestamp())}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            j = json.loads(r.read().decode())
        if isinstance(j, dict) and j.get("season") == season and j.get("week") == week:
            return j
        return {}
    except Exception as e:
        print(f"  · no previous lock fetched ({e}) — starting fresh for {season} week {week}")
        return {}


def team_game_map(games: list[dict]) -> tuple[dict[str, str], dict[str, dt.datetime]]:
    """(team_abbr -> game_id), (game_id -> kickoff) off THIS run's own
    nfl_week.json games list. See the module docstring for why the current
    week's own schedule is the right source for every historical run's
    lines too -- kickoff and matchup are schedule facts, not live output."""
    team_game: dict[str, str] = {}
    kickoff: dict[str, dt.datetime] = {}
    for g in games:
        gid = str(g.get("game_id") or "")
        t = parse_ts(g.get("kickoff"))
        if not gid or t is None:
            continue
        kickoff[gid] = t
        for side in ("home", "away"):
            team = g.get(side)
            if team:
                team_game[str(team)] = gid
    return team_game, kickoff


# ── PREDICTION OF RECORD ──────────────────────────────────────────────────
_PL_HEADER_CACHE: dict[str, tuple[dict, set]] = {}


def _prediction_log_index(current_dir: Path, prefix: str, season: int, week: int) -> dict[str, tuple[dict, set]]:
    """path -> (header dict, {team,...}) for every {prefix}prediction_log
    file whose run_id embeds this exact (season, week) -- run_id's own key
    segment is "{season}-wk{week:02d}" in week mode (build_nfl_run_meta() in
    nfl_bot.py), so the glob itself is the season/week filter; no separate
    per-line season/week check needed. Cached per path for the life of the
    process, same as pick_lock.py's own _PL_HEADER_CACHE."""
    pattern = str(current_dir / f"{prefix}prediction_log_{season}-wk{week:02d}.*.jsonl")
    out: dict[str, tuple[dict, set]] = {}
    for name in sorted(globmod.glob(pattern)):
        p = Path(name)
        key = str(p)
        if key in _PL_HEADER_CACHE:
            out[key] = _PL_HEADER_CACHE[key]
            continue
        header: dict = {}
        teams: set = set()
        try:
            with p.open(encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if i == 0 and obj.get("player_id") is None and obj.get("run_id"):
                        header = obj
                        continue
                    t = obj.get("team")
                    if t:
                        teams.add(str(t))
        except Exception:
            continue
        if not header.get("run_id") or not header.get("generated_at"):
            continue
        _PL_HEADER_CACHE[key] = (header, teams)
        out[key] = _PL_HEADER_CACHE[key]
    return out


def pregame_run_for_game(team_a: str, team_b: str, kickoff_time: dt.datetime,
                          index: dict[str, tuple[dict, set]]) -> dict | None:
    """The latest run generated BEFORE kickoff_time whose lines covered
    EITHER team in this game_id, or None. Either team is enough -- a run
    that scored the away team's players scored the whole slate that
    execution touched, and a bye/inactive-heavy team can otherwise vanish
    from a run's team set entirely. Mirrors pick_lock.py's
    pregame_run_for_game() exactly, one level of indirection removed
    (game_pk -> team set, since NFL's prediction_log has no game_id field
    of its own -- see the module docstring)."""
    best: tuple[dt.datetime, dict] | None = None
    for _path, (header, teams) in index.items():
        if team_a not in teams and team_b not in teams:
            continue
        gen = parse_ts(header.get("generated_at"))
        if gen is None or gen >= kickoff_time:
            continue
        if best is None or gen > best[0]:
            mv = header.get("model_versions") if isinstance(header.get("model_versions"), dict) else {}
            ch = (header.get("config_hashes") or {}).get("nfl") if isinstance(header.get("config_hashes"), dict) else None
            best = (gen, {
                "run_id": header["run_id"],
                "model_versions": mv or {},
                "config_hash": ch,
                "generated_at": header["generated_at"],
            })
    return best[1] if best else None


def append_por_log(season: int, week: int, newly_locked: list[tuple[str, dict]],
                    current_dir: Path, prefix: str) -> int:
    """Durable, append-only history of prediction_of_record entries, the NFL
    sibling of pick_lock.py's append_por_log() -- ONE FILE PER WEEK (not per
    date; a week is NFL's slate unit) because {prefix}pick_lock.json's own
    prediction_of_record key resets to {} the instant (season, week)
    changes. Each game_id locked gets ONE line, forever, the moment it
    locks. Append-only and dedup-safe against a retry writing the same
    game_id twice."""
    if not newly_locked:
        return 0
    path = current_dir / f"{prefix}por_log_{season}_w{week:02d}.jsonl"
    seen: set[str] = set()
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    gid = json.loads(line).get("game_id")
                except Exception:
                    continue
                if gid:
                    seen.add(str(gid))
        except Exception:
            pass
    written = 0
    with path.open("a", encoding="utf-8") as f:
        for gid, rec in newly_locked:
            if str(gid) in seen:
                continue
            row = {"season": season, "week": week, "game_id": gid, **rec}
            f.write(json.dumps(row, default=str))
            f.write("\n")
            written += 1
    return written


# ── CARD LOCK ──────────────────────────────────────────────────────────────

def slot_key(market: str, rank: int) -> str:
    return f"{market}|{rank}"


def rung_game_id(rung: dict, team_game: dict[str, str]) -> str | None:
    return team_game.get(str(rung.get("team") or "")) or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--week", type=int, default=None,
                     help="override the week; normally read off {prefix}week.json")
    ap.add_argument("--dir", type=str, required=True,
                     help="the current/ directory nfl_bot.py published into")
    ap.add_argument("--prefix", type=str, default="")
    ap.add_argument("--apply", action="store_true", help="rewrite the card files")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    a = ap.parse_args()

    current = Path(a.dir)
    week_path = current / f"{a.prefix}week.json"
    payload = load_json(week_path)
    if not isinstance(payload, dict):
        print(f"no {week_path.name} found — nothing to lock")
        return 0

    mode = payload.get("mode")
    season = int(payload.get("season") or a.season)
    week = payload.get("week")
    if week is None:
        week = a.week
    if mode != "week" or not week:
        # PRESEASON, DELIBERATELY OUT OF SCOPE — see the module docstring.
        # Preseason carries no week number, and this whole file keys its
        # ledger identity on (season, week); inventing one for preseason is
        # not this pass's job.
        print(f"mode={mode!r}, week={week!r} — not in week mode, nothing to lock")
        return 0
    week = int(week)

    games = payload.get("games") or []
    team_game, kickoff_by_game = team_game_map(games)
    if not kickoff_by_game:
        print("no games with a known kickoff on this slate — nothing to lock")
        return 0

    lock = fetch_lock(a.prefix, season, week, current)
    card_locks: dict[str, dict] = lock.get("card_locks") or {}
    rejected: list[dict] = lock.get("rejected") or []
    por: dict[str, dict] = lock.get("prediction_of_record") or {}

    now = now_utc()
    stamp = now.isoformat(timespec="seconds")

    # ── prediction_of_record ────────────────────────────────────────────
    # Every game_id with a known kickoff, locked once and only once, the
    # instant `now` first crosses it. Independent of the card lock below --
    # the two read the same games/now and write disjoint ledger keys, same
    # relationship as pick_lock.py's own two sections.
    meta = load_json(current / f"{a.prefix}meta.json") or {}
    run_meta_now = meta.get("run_meta") if isinstance(meta, dict) else None
    index = _prediction_log_index(current, a.prefix, season, week)

    por_newly_locked: list[tuple[str, dict]] = []
    game_teams: dict[str, tuple[str, str]] = {}
    for g in games:
        gid = str(g.get("game_id") or "")
        if gid:
            game_teams[gid] = (str(g.get("home") or ""), str(g.get("away") or ""))

    for gid, kickoff_time in kickoff_by_game.items():
        if gid in por:
            continue  # already locked — immutable from here
        if now < kickoff_time:
            continue  # still pregame; no record yet, correctly
        home, away = game_teams.get(gid, ("", ""))
        pre = pregame_run_for_game(home, away, kickoff_time, index)
        if pre is not None:
            info = {"run_id": pre["run_id"], "model_versions": pre["model_versions"],
                     "config_hash": pre["config_hash"]}
            generated_at = pre["generated_at"]
            por_source = "pregame_log"
        else:
            info = {
                "run_id": (run_meta_now or {}).get("run_id"),
                "model_versions": (run_meta_now or {}).get("model_versions") or {},
                "config_hash": ((run_meta_now or {}).get("config_hashes") or {}).get("nfl"),
            }
            generated_at = (run_meta_now or {}).get("generated_at")
            por_source = "rows_standing"
        generated_at_dt = parse_ts(generated_at) if generated_at else None
        por[gid] = {
            "run_id": info["run_id"],
            "model_versions": info["model_versions"],
            "config_hash": info.get("config_hash"),
            "generated_at": generated_at,
            "kickoff": kickoff_time.isoformat(),
            "locked_at": stamp,
            "locked_late": (bool(generated_at_dt and generated_at_dt > kickoff_time)
                             if generated_at_dt is not None else None),
            "por_source": por_source,
            "home": home, "away": away,
        }
        por_newly_locked.append((gid, por[gid]))

    # ── card lock ─────────────────────────────────────────────────────────
    picks_path = current / f"{a.prefix}picks.json"
    picks_payload = load_json(picks_path)
    froze_now = changed_before = 0
    card_rejects: list[dict] = []
    live_index: dict[str, dict] = {}  # player_id -> live rung dict, for restore

    if isinstance(picks_payload, dict):
        card = picks_payload.get("card") or {}
        for market, blk in card.items():
            rungs = blk.get("rungs") or []
            for r in rungs:
                pid = str(r.get("player_id") or "")
                if pid:
                    live_index[pid] = r
            for rung in rungs:
                rank = rung.get("rank")
                if not rank:
                    continue
                key = slot_key(market, int(rank))
                gid = rung_game_id(rung, team_game)
                started = bool(gid and now >= kickoff_by_game.get(gid, now + dt.timedelta(days=1)))
                occ_pid = rung.get("player_id")

                slot = card_locks.get(key)
                if slot is None:
                    card_locks[key] = {
                        "market": market, "rank": int(rank),
                        "stub": {k: rung.get(k) for k in STUB_FIELDS},
                        "meta": {k: rung.get(k) for k in RUNG_META_FIELDS},
                        "game_id": gid, "at": stamp,
                        "locked": started, "locked_late": started,
                        "history": [{"player_id": occ_pid, "name": rung.get("name"), "at": stamp}],
                    }
                    if started:
                        froze_now += 1
                    continue

                if slot.get("locked"):
                    if str(slot["stub"].get("player_id")) != occ_pid:
                        card_rejects.append({
                            "slot": key, "at": stamp,
                            "locked": slot["stub"].get("name"),
                            "attempted": rung.get("name"),
                        })
                    continue

                if started:
                    # THE FREEZE INSTANT. Lock whatever this ledger already
                    # had standing (the last PRE-GAME pass), never this
                    # run's own post-kickoff recompute — same ordering rule
                    # as pick_lock.py's own ticket freeze, and the exact
                    # line that file's own comment says is easy to get
                    # backwards.
                    slot["locked"] = True
                    slot["locked_at"] = stamp
                    froze_now += 1
                    if str(slot["stub"].get("player_id")) != occ_pid:
                        card_rejects.append({
                            "slot": key, "at": stamp,
                            "locked": slot["stub"].get("name"),
                            "attempted": rung.get("name"),
                        })
                    continue

                # Pre-game: a re-pick is legitimate.
                if str(slot["stub"].get("player_id")) != occ_pid:
                    slot["stub"] = {k: rung.get(k) for k in STUB_FIELDS}
                    slot["meta"] = {k: rung.get(k) for k in RUNG_META_FIELDS}
                    slot["game_id"] = gid
                    slot["at"] = stamp
                    slot.setdefault("history", []).append(
                        {"player_id": occ_pid, "name": rung.get("name"), "at": stamp})
                    changed_before += 1

    rejected.extend(card_rejects)

    # ── restore locked rungs onto the published card ────────────────────
    restored = 0
    if a.apply and not a.dry_run and isinstance(picks_payload, dict):
        card = picks_payload.get("card") or {}
        for market, blk in card.items():
            rungs = blk.get("rungs") or []
            new_rungs = []
            for rung in rungs:
                rank = rung.get("rank")
                key = slot_key(market, int(rank)) if rank else None
                slot = card_locks.get(key) if key else None
                if slot and slot.get("locked"):
                    pid = slot["stub"].get("player_id")
                    live = live_index.get(str(pid)) if pid else None
                    frozen = dict(slot["stub"])
                    frozen.update(slot["meta"])
                    frozen["rank"] = rank
                    if live:
                        # Frozen name/identity, LIVE current stats where the
                        # player is still on the slate — same reasoning as
                        # pick_lock.py's index_players() restore: the ledger
                        # only has to remember WHO, not carry a stale copy of
                        # every field forever.
                        for k in ("name", "team", "opp", "position"):
                            if live.get(k) is not None:
                                frozen[k] = live[k]
                    new_rungs.append(frozen)
                    if str(pid) != str(rung.get("player_id")):
                        restored += 1
                else:
                    new_rungs.append(rung)
            blk["rungs"] = new_rungs
        picks_body = json.dumps(picks_payload, separators=(",", ":"))
        picks_path.write_text(picks_body)
        arch = current / f"{a.prefix}picks_{season}_w{week:02d}.json"
        arch.write_text(picks_body)

    # ── write the ledger ─────────────────────────────────────────────────
    ledger = {
        "season": season, "week": week,
        "updated": stamp,
        "runs": int(lock.get("runs") or 0) + 1,
        "card_locks": card_locks,
        "rejected": rejected,
        "prediction_of_record": por,
        "rule": ("Each market's 5-rung ladder is keyed by market|rank, exactly like a pool/pair "
                 "ticket in pick_lock.py: before the occupant's own game kicks off a re-pick is "
                 "legitimate and every change is kept in history[]; the moment that game starts "
                 "the rung freezes to whoever was standing there and any later change is recorded "
                 "in rejected[] instead of applied. prediction_of_record locks, per game_id, the "
                 "run standing at THAT game's own kickoff -- the pregame nfl_prediction_log, never "
                 "a post-kickoff rebuild; locked_late marks a game whose first-seen run was already "
                 "generated after kickoff (a late first build of the week)."),
    }

    n_por_logged = 0
    if a.apply and not a.dry_run:
        current.mkdir(parents=True, exist_ok=True)
        (current / f"{a.prefix}pick_lock.json").write_text(json.dumps(ledger, indent=1))
        n_por_logged = append_por_log(season, week, por_newly_locked, current, a.prefix)

    n_locked = sum(1 for s in card_locks.values() if s.get("locked"))
    n_late = sum(1 for s in card_locks.values() if s.get("locked_late"))
    print(f"nfl pick lock — {season} week {week}, run #{ledger['runs']}")
    print(f"  {len(card_locks)} rungs tracked · {n_locked} locked ({n_late} locked late)")
    print(f"  pre-lock re-picks recorded: {changed_before} · froze this run: {froze_now}")
    n_por_late = sum(1 for p in por.values() if p.get("locked_late") is not False)
    n_por_no_run = sum(1 for p in por.values() if not p.get("run_id"))
    print(f"  prediction_of_record: {len(por)} game(s) locked to a run "
          f"({len(por_newly_locked)} newly this run, {n_por_late} late, "
          f"{n_por_no_run} missing a run_id, {n_por_logged} appended to "
          f"{a.prefix}por_log_{season}_w{week:02d}.jsonl)")
    if a.apply and not a.dry_run and restored:
        print(f"  restored {restored} locked rung(s) onto the published card")
    if card_rejects:
        print(f"  !! {len(card_rejects)} POST-LOCK RUNG CHANGES REJECTED:")
        for r in card_rejects:
            print(f"     {r['slot']}: kept {r['locked']}, refused {r['attempted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
