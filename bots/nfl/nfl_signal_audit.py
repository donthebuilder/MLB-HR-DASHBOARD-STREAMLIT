#!/usr/bin/env python3
"""nfl_signal_audit.py -- grade TUDDY's signal flags against what happened.

    python nfl_signal_audit.py --season 2026 --out ../../public/data/current

The NFL sibling of MOONSHOT's SignalAudit (components/SignalAudit.js): every
flag the site wears, graded against real touchdowns, with the baseline taken
from the same pool the flag could have fired on. Writes one small file,
nfl_signal_audit.json, that the Accountability tab reads as-is.

WHY THIS RUNS BOT-SIDE. The inputs are run-stamped archives on the data
branch (nfl_prediction_log_<season>-wkNN.<time>.gha-<id>.jsonl and
nfl_signal_log_... beside it). Their names carry a GHA run id, so a browser
cannot guess them, and listing the branch from Vercel means an
unauthenticated GitHub API call against a shared IP. Here the Actions
token lists the tree in one call, and the site gets a few KB.

THE FREEZE -- the whole point, so it is spelled out. A flag is graded as it
stood before HIS game kicked off, never after. For every graded player:

  1. find his team's game that week in nflverse's schedule (kickoff in ET,
     converted to UTC);
  2. take the LAST archived run of that week generated before that kickoff;
  3. read his TD score (prediction log) and his flags (signal log, same
     run_id) out of that one run.

Per game, not per week: a Sunday 1pm player is graded on the Sunday-morning
build, not on Wednesday's, so injury news that moved his flag counts the way
it did on the site. Runs are ordered by the generated_at in each file's
header line, read with an HTTP Range request so no full file is downloaded
just to learn its time.

THE POOL. A player counts only if (a) he carried a TD score in his freeze run
and (b) nfl_results_<season>_wNN.json has a TD entry for him -- i.e.
nfl_results.py's eligible_lines() recorded him as having played at an
eligible position. That second rule is what keeps inactives and
not-yet-played games out of the denominator; a Thursday-only grade simply
has a smaller pool, and it grows as the week is graded.

THE BASELINE follows SignalAudit.js's rule: the TD rate of pool players
WHERE THAT FIELD EXISTS. The signal-log flags only exist from 2026-09-22 on
(bot 21787254); comparing them against weeks that never carried them would
be a thumb on the scale. So each flag gets its own pool.

VERDICTS are sample-gated exactly like MLB's: nothing is called earning or
failing under MIN_N flagged player-games.

NOTHING INVENTED. A flag with no graded history is published with n=0 and
`banking_since`, and the site says so. No backfill is possible or attempted:
the number has to be the one that stood before the game.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = "donthebuilder/MLB-HR-DASHBOARD-STREAMLIT"
BRANCH = "data"
DIR = "public/data/current"
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{DIR}"
MIN_N = 40
LIFT_BAR = 3.0  # percentage points, same bar SignalAudit.js uses
ET = ZoneInfo("America/New_York")

# key, icon, label, source, has(row), hit(row), invert, note
SIGNALS = [
    dict(key="hiconf", icon="\U0001F512", label="High confidence (TD score 78+)",
         source="prediction_log", invert=False,
         note="Exactly TD score >= 78 -- the A+ grade, so this grades the grade."),
    dict(key="scored_last", icon="\U0001F501", label="Scored last time out",
         source="signal_log", invert=False,
         note="games_since_last_td == 0 as it stood before kickoff."),
    dict(key="cov_target", icon="\U0001F3AF", label="Coverage mismatch: TARGET",
         source="signal_log", invert=False,
         note="coverage_mismatch_tag == TARGET."),
    dict(key="cov_avoid", icon="⚠️", label="Coverage mismatch: AVOID (expects LESS)",
         source="signal_log", invert=True,
         note="coverage_mismatch_tag == AVOID. Lift is flipped: scoring LESS is the flag working."),
]


def _has_hit(key: str, pred: dict | None, sig: dict | None):
    """(has, hit) for one player-game. has=False keeps him out of that pool."""
    if key == "hiconf":
        if pred is None or pred.get("score") is None:
            return False, False
        return True, float(pred["score"]) >= 78
    if sig is None:
        return False, False
    if key == "scored_last":
        g = sig.get("games_since_last_td")
        return (g is not None), (g == 0)
    if key in ("cov_target", "cov_avoid"):
        if sig.get("td_score") is None:
            return False, False
        want = "TARGET" if key == "cov_target" else "AVOID"
        return True, sig.get("coverage_mismatch_tag") == want
    return False, False


# ---------------------------------------------------------------- fetching
def _get(url: str, headers: dict | None = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_branch() -> list[str]:
    """Every filename in DIR on the data branch, one API call."""
    hdr = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"
    url = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}:{DIR}"
    tree = json.loads(_get(url, hdr))
    if tree.get("truncated"):
        print("  warning: tree listing truncated")
    return [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]


def read_header(name: str) -> dict | None:
    """First line of a run file, via a Range request."""
    try:
        body = _get(f"{RAW}/{name}", {"Range": "bytes=0-4095"})
        line = body.split(b"\n", 1)[0]
        return json.loads(line)
    except Exception as exc:
        print(f"  header unreadable: {name} ({type(exc).__name__})")
        return None


def read_jsonl(name: str) -> list[dict]:
    body = _get(f"{RAW}/{name}", timeout=60)
    out = []
    for i, line in enumerate(body.splitlines()):
        if i == 0 or not line.strip():
            continue  # header
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def read_json(name: str):
    try:
        return json.loads(_get(f"{RAW}/{name}"))
    except Exception:
        return None


# ---------------------------------------------------------------- schedule
def kickoffs(season: int) -> dict[tuple[int, str], dt.datetime]:
    """{(week, team): kickoff UTC} for the regular season."""
    import nflreadpy as nfl
    import polars as pl
    s = nfl.load_schedules().filter(
        (pl.col("season") == season) & (pl.col("game_type") == "REG"))
    out: dict[tuple[int, str], dt.datetime] = {}
    for r in s.select(["week", "gameday", "gametime", "home_team", "away_team"]).iter_rows(named=True):
        if not r["gameday"] or not r["gametime"]:
            continue
        local = dt.datetime.strptime(f"{r['gameday']} {r['gametime']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        ko = local.astimezone(dt.timezone.utc)
        out[(int(r["week"]), r["home_team"])] = ko
        out[(int(r["week"]), r["away_team"])] = ko
    return out


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- build
def build(season: int, files: list[str], ko: dict, now: dt.datetime) -> dict:
    wk_re = re.compile(rf"^nfl_(prediction|signal)_log_{season}-wk(\d\d)\.(.+)\.jsonl$")
    runs: dict[int, dict[str, dict]] = {}   # week -> run_id -> {pred, sig}
    for f in files:
        m = wk_re.match(f)
        if not m:
            continue
        kind, wk, rest = m.group(1), int(m.group(2)), m.group(3)
        run_id = f"{season}-wk{wk:02d}.{rest}"
        runs.setdefault(wk, {}).setdefault(run_id, {})[
            "pred" if kind == "prediction" else "sig"] = f

    graded_weeks = sorted(
        int(m.group(1)) for f in files
        if (m := re.match(rf"^nfl_results_{season}_w(\d\d)\.json$", f)))

    # Headers only for weeks that have been graded -- nothing else is needed.
    need = [(wk, rid, v["pred"]) for wk in graded_weeks
            for rid, v in runs.get(wk, {}).items() if "pred" in v]
    with cf.ThreadPoolExecutor(8) as ex:
        heads = dict(zip([rid for _, rid, _ in need],
                         ex.map(lambda t: read_header(t[2]), need)))
    run_time = {rid: _ts(h["generated_at"]) for rid, h in heads.items()
                if h and h.get("generated_at") and h.get("mode") == "week"}

    # Earliest signal log anywhere this season: when the banking started.
    sig_times = []
    sig_heads_needed = sorted(v["sig"] for wk in runs.values() for v in wk.values() if "sig" in v)
    if sig_heads_needed:
        h = read_header(sig_heads_needed[0])
        if h and h.get("generated_at"):
            sig_times.append(h["generated_at"])
    banking_since = sig_times[0] if sig_times else None

    cache: dict[str, list[dict]] = {}

    def rows_of(name: str) -> list[dict]:
        if name not in cache:
            cache[name] = read_jsonl(name)
        return cache[name]

    acc = {s["key"]: {"pool_n": 0, "pool_td": 0, "n": 0, "td": 0, "weeks": []} for s in SIGNALS}
    weeks_out = []
    for wk in graded_weeks:
        res = read_json(f"nfl_results_{season}_w{wk:02d}.json")
        if not res:
            continue
        lines = res.get("lines") or {}
        played = {pid: v["TD"] for pid, v in lines.items() if isinstance(v, dict) and "TD" in v}
        wk_runs = sorted(((t, rid) for rid, t in run_time.items() if rid.startswith(f"{season}-wk{wk:02d}.")))
        if not wk_runs:
            weeks_out.append({"week": wk, "pool_n": 0, "note": "no archived runs for this week"})
            continue

        # Which freeze run applies to each team this week.
        teams = {t for (w, t) in ko if w == wk}
        freeze_for_team: dict[str, str] = {}
        for team in teams:
            k = ko[(wk, team)]
            before = [rid for t, rid in wk_runs if t < k]
            if before:
                freeze_for_team[team] = before[-1]

        # Index the freeze runs' rows.
        pred_idx: dict[str, dict[str, dict]] = {}
        sig_idx: dict[str, dict[str, dict]] = {}
        for rid in set(freeze_for_team.values()):
            v = runs[wk][rid]
            pred_idx[rid] = {r["player_id"]: r for r in rows_of(v["pred"]) if r.get("market") == "TD"}
            sig_idx[rid] = ({r["player_id"]: r for r in rows_of(v["sig"])} if "sig" in v else {})

        per = {s["key"]: {"pool_n": 0, "pool_td": 0, "n": 0, "td": 0} for s in SIGNALS}
        wk_pool = 0
        wk_td = 0
        unmatched_team = 0
        # Walk players via the prediction logs (a player's team picks his run).
        seen = set()
        for rid in set(freeze_for_team.values()):
            for pid, pred in pred_idx[rid].items():
                if pid in seen or pid not in played:
                    continue
                team = pred.get("team")
                if freeze_for_team.get(team) != rid:
                    if team not in freeze_for_team:
                        unmatched_team += 1
                    continue
                seen.add(pid)
                scored = float(played[pid] or 0) >= 1
                wk_pool += 1
                wk_td += scored
                sig = sig_idx[rid].get(pid)
                for s in SIGNALS:
                    has, hit = _has_hit(s["key"], pred, sig)
                    if not has:
                        continue
                    p = per[s["key"]]
                    p["pool_n"] += 1
                    p["pool_td"] += scored
                    if hit:
                        p["n"] += 1
                        p["td"] += scored
        for s in SIGNALS:
            a, p = acc[s["key"]], per[s["key"]]
            for k in ("pool_n", "pool_td", "n", "td"):
                a[k] += p[k]
            if p["pool_n"]:
                a["weeks"].append({"week": wk, **p})
        weeks_out.append({
            "week": wk,
            "graded_at": res.get("graded_at"),
            "pool_n": wk_pool,
            "pool_td": wk_td,
            "freeze_runs": len(set(freeze_for_team.values())),
            "unmatched_team_rows": unmatched_team,
        })

    signals_out = []
    for s in SIGNALS:
        a = acc[s["key"]]
        base = a["pool_td"] / a["pool_n"] if a["pool_n"] else None
        rate = a["td"] / a["n"] if a["n"] else None
        lift = None
        if base is not None and rate is not None:
            lift = round((rate - base) * 100 * (-1 if s["invert"] else 1), 1)
        if a["n"] == 0:
            verdict = "banking"
        elif a["n"] < MIN_N:
            verdict = "young"
        elif lift >= LIFT_BAR:
            verdict = "earning"
        elif lift <= -LIFT_BAR:
            verdict = "failing"
        else:
            verdict = "flat"
        signals_out.append({
            "key": s["key"], "icon": s["icon"], "label": s["label"], "note": s["note"],
            "source": s["source"], "invert": s["invert"],
            "n": a["n"], "td": a["td"],
            "rate": round(rate * 100, 1) if rate is not None else None,
            "pool_n": a["pool_n"], "pool_td": a["pool_td"],
            "base": round(base * 100, 1) if base is not None else None,
            "lift": lift, "verdict": verdict,
            "weeks": a["weeks"],
            "banking_since": banking_since if s["source"] == "signal_log" else None,
        })

    return {
        "schema_version": 1,
        "season": season,
        "generated_at": now.isoformat(),
        "min_n": MIN_N,
        "lift_bar": LIFT_BAR,
        "graded_weeks": [w["week"] for w in weeks_out if w.get("pool_n")],
        "weeks": weeks_out,
        "signals": signals_out,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--out", type=str, default="../../public/data/current")
    a = ap.parse_args()
    files = list_branch()
    ko = kickoffs(a.season)
    doc = build(a.season, files, ko, dt.datetime.now(dt.timezone.utc))
    out = Path(a.out) / "nfl_signal_audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, separators=(",", ":")))
    for s in doc["signals"]:
        print(f"  {s['key']:<12} n={s['n']:<4} rate={s['rate']} base={s['base']} lift={s['lift']} {s['verdict']}")
    print(f"wrote {out} ({out.stat().st_size} bytes), weeks {doc['graded_weeks']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
