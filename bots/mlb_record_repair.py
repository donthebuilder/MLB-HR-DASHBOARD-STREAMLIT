#!/usr/bin/env python3
"""mlb_record_repair.py -- grade a published night against its LOCK (2026-10-04).

Donovan, 10-04: fix the MOONSHOT record. The record audit found, 09-09 -> 10-03:
  · 137 designated slots (115 players) the prediction of record locked that
    are missing from graded_results_<date>.json -- games skipped while live
    and never regraded, or a lock-holder the rebuilt slate had dropped;
  · 159 graded TOP/HR/HIT/HRR/CONTACT slots that belong to a stand-in, not
    the player who held that role at lock (fixed going forward: bot 9214495a).

For each night this reads the locked roles (por_rows_<date>.jsonl, one row per
rated hitter, written at each game's lock) and the night's graded file, then:
  · a graded designated slot whose player did not hold that role at lock is
    relabelled pick_type STANDIN (kept as context, original role in
    standin_for) -- it leaves every role count, like PINCHED;
  · a locked holder with no graded slot in his role is graded from MLB's own
    final game feed with the tracker's grade_slot(), and added.
Then designed_hit / top_beat_game, the `results` list, merged_homers and the
.txt summary are rebuilt from the corrected slots, the game states are
refreshed, and the file records what changed in `repairs`. A game with no
lock rows is left exactly as it was (nothing to grade it against). Nothing
pregame is re-scored: every role and score is the lock's.

Every change is also written to repairs_<date>.jsonl beside the file (old ->
new, reason, source) -- the rows for record_corrections.

  python3 bots/mlb_record_repair.py --dir public/data/current --dates 2026-09-09,2026-09-11 [--apply]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import live_results_tracker as t  # noqa: E402

ROLES = ("TOP", "HR", "HIT", "HRR", "CONTACT")


# Final-mode copies of the three row helpers nested in live_results_tracker.main()
# (they read nothing but the slot), so a repaired `results` list is built the
# same way the tracker builds a final one.
def _grade_for_row(r):
    # One definition of the WIN bars (the digest's), shared with the tracker.
    if r.get("void"):
        return "DNP"
    if t.win_bar_cleared(r):
        return "WIN"
    if int(r.get("actual_ab", 0)) > 0:
        return "LOSS"
    return "DNP"


def _bet_for_row(r):
    pt = (r.get("pick_type") or "").upper()
    return {"HR": "HR", "TOP": "TOP", "TOP15": "TOP15", "HIT": "HIT", "HRR": "HRR", "CONTACT": "TB"}.get(pt, pt or "PICK")


def _outcome_text(r):
    ab, hits, hr = int(r.get("actual_ab", 0)), int(r.get("actual_hits", 0)), int(r.get("actual_hr", 0))
    tb, rbi, runs = int(r.get("actual_tb", 0)), int(r.get("actual_rbi", 0)), int(r.get("actual_runs", 0))
    if ab == 0 and hits == 0 and hr == 0:
        return "Game not started"
    line = f"{hits}/{ab}"
    extras = [x for x in (f"{hr} HR" if hr else "", f"{tb} TB" if tb else "", f"{rbi} RBI" if rbi else "", f"{runs} R" if runs else "") if x]
    return line + (" · " + ", ".join(extras) if extras else "")


def _roles(raw) -> list[str]:
    return [r.strip().upper() for r in str(raw or "").split("/") if r.strip().upper() in ROLES]


def _por(current: Path, date: str) -> list[dict]:
    p = current / f"por_rows_{date}.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _all_tb(feed: dict) -> list[tuple[int, int]]:
    """(player_id, total bases) for every batter in the game's box."""
    out = []
    teams = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    for side in ("home", "away"):
        for key, pdata in (teams.get(side, {}).get("players", {}) or {}).items():
            b = (pdata.get("stats", {}) or {}).get("batting", {}) or {}
            if b.get("atBats") is None:
                continue
            pid = int(str(key).replace("ID", "") or 0)
            tb = int(b.get("totalBases") or 0)
            out.append((pid, tb))
    return out


def repair(current: Path, date: str, apply: bool) -> dict:
    gpath = current / f"graded_results_{date}.json"
    if not gpath.exists():
        return {"date": date, "skipped": "no graded file"}
    g = json.loads(gpath.read_text(encoding="utf-8"))
    por = _por(current, date)
    if not por:
        return {"date": date, "skipped": "no por_rows (nothing locked to grade against)"}

    locked_games = {int(r["game_pk"]) for r in por}
    holder = {}   # (pk, role) -> por row
    for r in por:
        for role in _roles(r.get("game_pick_role")):
            holder.setdefault((int(r["game_pk"]), role), r)

    slots = list(g.get("graded_slots") or [])
    have = {(int(s.get("game_pk") or 0), int(s.get("player_id") or 0), str(s.get("pick_type") or "").upper()) for s in slots}
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    changes = []

    # 1. stand-ins out of the role counts
    for s in slots:
        role = str(s.get("pick_type") or "").upper()
        pk = int(s.get("game_pk") or 0)
        if role not in ROLES or pk not in locked_games:
            continue
        h = holder.get((pk, role))
        if h is not None and int(h["player_id"]) == int(s.get("player_id") or 0):
            continue
        s["pick_type"] = "STANDIN"
        s["standin_for"] = role
        changes.append({"kind": "standin", "game_pk": pk, "player_id": int(s.get("player_id") or 0), "name": s.get("name"),
                        "old": {"pick_type": role}, "new": {"pick_type": "STANDIN", "standin_for": role},
                        "locked_holder": (int(h["player_id"]) if h is not None else None)})

    # 2. the locked holders, graded from the final game feed
    feeds: dict[int, dict] = {}
    added = []
    for (pk, role), r in sorted(holder.items()):
        pid = int(r["player_id"])
        if (pk, pid, role) in have:
            continue
        if pk not in feeds:
            feeds[pk] = t.fetch_game_feed(pk)
        feed = feeds[pk]
        if not t.game_is_final(feed):
            changes.append({"kind": "not_final", "game_pk": pk, "player_id": pid, "role": role})
            continue
        sc = r.get("scores") or {}
        base = {
            "game_pk": pk, "team": r.get("team"), "opponent": r.get("opp"), "player_id": pid, "name": r.get("player"),
            "game_pick_role": r.get("game_pick_role"), "pick_type": role,
            "hr_score": sc.get("hr"), "hit_score": sc.get("hit"), "hrr_score": sc.get("hrr"),
            "contact_score": sc.get("contact"), "overall_score": sc.get("overall"),
            "locked_run_id": r.get("run_id"), "feature_snapshot": "locked",
            "repaired": {"at": stamp, "why": "locked designation missing from the graded file", "source": f"por_rows_{date}.jsonl + statsapi game feed {pk}"},
        }
        graded = t.grade_slot(base, t.get_player_batting_line(feed, pid))
        graded["game_status"] = t.get_game_status(feed)
        graded["is_final"] = 1
        slots.append(graded)
        added.append(graded)
        changes.append({"kind": "added", "game_pk": pk, "player_id": pid, "name": r.get("player"), "old": None,
                        "new": {"pick_type": role, "got_hr": graded.get("got_hr"), "actual_hits": graded.get("actual_hits"), "actual_ab": graded.get("actual_ab")}})

    if not any(c["kind"] in ("standin", "added") for c in changes):
        return {"date": date, "changes": 0, "not_final": sum(1 for c in changes if c["kind"] == "not_final")}

    # 3. everything derived from the slots
    slots = t.annotate_designed(slots)
    for pk in {int(s.get("game_pk") or 0) for s in added if str(s.get("pick_type")) == "TOP"}:
        feed = feeds.get(pk) or t.fetch_game_feed(pk)
        mates = _all_tb(feed)
        for s in slots:
            if int(s.get("game_pk") or 0) != pk or str(s.get("pick_type")) != "TOP":
                continue
            own = int(s.get("actual_tb") or 0)
            best_other = max((tb for p2, tb in mates if p2 != int(s.get("player_id") or 0)), default=0)
            s["top_beat_game"] = 1 if own >= best_other and own > 0 else 0
            s["top_game_best_tb"] = best_other
    for pk, feed in feeds.items():
        (g.setdefault("game_status_by_pk", {}))[str(pk)] = t.get_game_status(feed)
    g["skipped_live_games"] = [pk for pk in (g.get("skipped_live_games") or []) if not (int(pk) in feeds and t.game_is_final(feeds[int(pk)]))]
    g["graded_slots"] = slots
    g["results"] = [{**s, "grade": _grade_for_row(s), "bet_type": _bet_for_row(s), "outcome_text": _outcome_text(s)} for s in slots]
    g["merged_homers"] = t.merge_homer_entries(slots)
    if not g["skipped_live_games"] and all(str((v or {}).get("abstract_state", "")).lower() == "final" for v in (g.get("game_status_by_pk") or {}).values()):
        g["live_mode"] = False
        g["slate_status"] = "final"
        g["label"] = f"Final · {date}"
    g.setdefault("repairs", []).append({
        "at": stamp, "by": "bots/mlb_record_repair.py",
        "added": sum(1 for c in changes if c["kind"] == "added"),
        "standins": sum(1 for c in changes if c["kind"] == "standin"),
        "why": "graded against the prediction of record: locked holders graded in their roles, stand-ins relabelled STANDIN",
    })
    summary = t.build_summary_text(date, slots, g["merged_homers"], g.get("pair_pool_results") or [],
                                   g.get("hr_capture_report") or {}, t.build_unique_player_hr_report(slots), live_mode=False)
    if apply:
        gpath.write_text(json.dumps(g, indent=2), encoding="utf-8")
        (current / f"graded_results_{date}.txt").write_text(summary + "\n", encoding="utf-8")
        with (current / f"repairs_{date}.jsonl").open("w", encoding="utf-8") as fh:
            for c in changes:
                if c["kind"] in ("standin", "added"):
                    fh.write(json.dumps({"date": date, **c, "at": stamp}) + "\n")
    return {"date": date, "added": sum(1 for c in changes if c["kind"] == "added"),
            "standins": sum(1 for c in changes if c["kind"] == "standin"),
            "added_hr": sum(1 for s in added if int(s.get("got_hr") or 0) == 1),
            "not_final": sum(1 for c in changes if c["kind"] == "not_final")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--dates", required=True, help="comma list of YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="write the files (default: report only)")
    a = ap.parse_args()
    for d in [x.strip() for x in a.dates.split(",") if x.strip()]:
        print(json.dumps(repair(Path(a.dir), d, a.apply)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
