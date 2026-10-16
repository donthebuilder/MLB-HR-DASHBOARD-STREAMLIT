#!/usr/bin/env python3
"""
🚪📻 LIVE WATCH (2026-08-08; grouped + swept 2026-09-06). Started life as
pen-door-only; grew into the full in-game Discord layer the same day ("what
other ones can we add to help people" — all of them, plus bases-loaded spots):

  🚪 pitching change            live sweep inside the run — see below
  📋 lineup posted        ⎫
  ⚠️ SCRATCH               ⎬ one grouped embed, pregame/status, once per run
  ☔ delay / postponement  ⎪
  🏁 final recap           ⎭
  🚨 opportunity                a pick AT THE PLATE with 2+ runners on

TWO SPEEDS, NOT ONE (2026-09-06, Donovan: "pen door needs to be brought to
speed... and less notis"). Lineups/scratches/delays/finals don't change
inside a ten-minute window, so they're read once per run and sent as ONE
sectioned Discord embed — a grouped "tonight's status" update instead of a
wall of interleaved emoji lines competing for the same 12-line cap (the old
shape could silently drop an urgent live line behind a stack of routine
lineup posts; grouping by category removes that failure mode entirely,
since lineups no longer share a slot budget with anything urgent).

Pitching changes and "at the plate right now" are the opposite: a single
check per ten-minute cron tick means either of them can be up to ten minutes
stale by the time anyone sees it — for "he's up right now with two on," ten
minutes late is the same lie the site's own push catalog already refuses to
tell (see lib/dash/pushRules.js: "at the plate" is live-only there, never
pushed, for exactly this reason). Rather than drop the alert, this run now
SWEEPS live games up to three times, ~90 seconds apart, inside its own
process — cheap because it only re-fetches games already known to be live —
and posts each fresh pitching change or opportunity the moment it's found,
as its own small ping, not held for the digest. Worst-case lag drops from
"up to ten minutes" to "up to ~90 seconds" for anything that lands while
this run is alive; the remaining gap is only for something that happens in
the minute right after a run exits, which the next cron tick still catches.

DEDUPE. Lineup/scratch/delay/final/opportunity keys live in
state/pen_door_state.json — persisted between runs by actions/cache in
pen-door.yml — and now only get marked once their post actually delivers,
so a failed webhook never burns a once-per-game alert (recovered from the
same real incident, 2026-08-11, that added delivery reporting below).
Pitching changes used to rely solely on a disjoint 10-minute wall-clock
window, which breaks the moment a run sweeps itself more than once — the
same substitution would qualify for the window on every sweep. They're now
keyed by their own event timestamp instead (same state file), which is
exact regardless of how many times or how often this file checks, with a
generous 70-minute lookback so a missed cron tick or a state-cache miss
costs at most a short backlog, never a multi-hour one.

Field verification (all live, 2026-08-08): pitching_substitution events,
boxscore battingOrder arrays, schedule detailedState/scores. linescore
offense + currentPlay are standard feed fields; the opportunity alert
degrades to silence if they're absent — it never guesses.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

try:
    import discord_post
    import copy_pendoor as words
    import pen_door_fold as fold
except ImportError:
    from bots import discord_post
    from bots import copy_pendoor as words
    from bots import pen_door_fold as fold

API = "https://statsapi.mlb.com/api/v1"
FEED_FIELDS = ("gameData,teams,home,away,abbreviation,liveData,plays,allPlays,"
               "about,halfInning,inning,playEvents,details,eventType,description,startTime,"
               "currentPlay,matchup,batter,id,fullName,"
               "linescore,currentInning,isTopInning,offense,first,second,third")
SLATE_URL = ("https://raw.githubusercontent.com/donthebuilder/MLB-HR-DASHBOARD-STREAMLIT/"
             "data/public/data/current/today_slim.json")
PITCH_LOOKBACK_MIN = 70  # generous vs. the 10-min cron grain; startTime keys do the real dedup
STATE_PATH = Path("state/pen_door_state.json")
# PEN_DOOR_FOLD_ON also reads who came in / went out of a substitution (field filters only add, they never change the old fields)
FEED_FIELDS_FOLD = FEED_FIELDS + ",player,replacedPlayer"

# Live sweep: catch a pitching change or an at-bat opportunity closer to when
# it happens, without turning this into a long-running process. Budgeted well
# under pen-door.yml's 8-minute job timeout, which also has to cover checkout
# and cache restore/save around this script.
LIVE_SWEEP_GAP_S = 90
MAX_LIVE_SWEEPS = 3
LIVE_BUDGET_S = 300

EMBED_COLOR = 0xF97316
STATUS_SECTIONS_ON = words.STATUS_SECTIONS_ON   # which status sections post; edit in bots/copy_pendoor.py


def get_json(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "moonshot-live-watch/2.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        print(f"fetch failed {url[:80]}: {exc}", file=sys.stderr)
        return None


def discord_urls() -> list[str]:
    # OWN CHANNEL (2026-09-06, Donovan: "pen door needs its own [channel],
    # what other notis do we even have"). DISCORD_PENDOOR_WEBHOOK, if set,
    # takes this bot OUT of the shared DISCORD_WEBHOOK room entirely -- so
    # its live sweep pings stop competing with everything else (Live
    # Results Digest, the accountability/today/tomorrow bots, Called It) for
    # the same feed -- the social drafting bots that used to share this room
    # too are gone entirely as of the same day (never linked to Called It or
    # anything auto-publishing; approval-queue-only, nothing downstream). Unset,
    # this is a no-op and nothing about tonight changes.
    raw = os.environ.get("DISCORD_PENDOOR_WEBHOOK") or os.environ.get("DISCORD_WEBHOOK", "")
    return [u.strip() for u in raw.replace(",", "\n").split() if u.strip().startswith("http")]


def _deliver(payload: dict) -> tuple[int, int]:
    """Send one payload to every configured webhook. Returns (delivered, failed).

    WHY THIS REPORTS (2026-08-11, Donovan: "I wanted notis for when I'm not on
    the site for my phone — didn't work").

    This used to return None and swallow everything, which gave the run three
    separate ways to be silent and still look fine:

      · DISCORD_WEBHOOK unset, empty, or rotated to a value that no longer
        starts with http -> discord_urls() is [], the loop body never runs,
        nothing is sent and nothing is logged. The run goes green.
      · a DELETED webhook answers 404. urlopen raises, the except printed one
        stderr line, and the run still exited 0. Green again.
      · main then printed "posted N alert(s)" whether or not a single byte
        left the machine -- the log actively said it had posted.

    A notification channel that can fail green is a channel you cannot trust,
    and the only symptom is the thing Donovan actually reported: nothing
    arrives and nothing anywhere says why. So delivery is now counted, the
    HTTP status is logged per hook, and main turns a total failure into a RED
    run (see the tail of main). post() and post_embed() below both route
    through this so the guarantee is the same regardless of shape.
    """
    urls = discord_urls()
    if not urls:
        print("DISCORD_WEBHOOK is unset or holds no http(s) URL — nothing can be delivered",
              file=sys.stderr)
        return 0, 0
    ok = bad = 0
    for i, url in enumerate(urls, 1):
        try:
            status = discord_post.send(url, json.dumps(payload).encode("utf-8"), timeout=10)
            print(f"discord hook {i}/{len(urls)}: HTTP {status}")
            ok += 1
        except Exception as exc:
            # 404 = webhook deleted in Discord; 401 = token rotated. Both mean
            # the Actions secret is stale and only a human can refresh it.
            code = getattr(exc, "code", None)
            hint = " (webhook deleted — refresh the DISCORD_WEBHOOK secret)" if code == 404 else ""
            print(f"discord hook {i}/{len(urls)} FAILED: {exc}{hint}", file=sys.stderr)
            bad += 1
    return ok, bad


def post(msg: str) -> tuple[int, int]:
    """One plain-text ping — used for the live, one-at-a-time alerts
    (pitching change, opportunity) where immediacy matters more than
    grouping."""
    return _deliver({"content": msg[:1900]})


def post_embed(title: str, sections: list[tuple[str, list[str]]], footer: str = "") -> tuple[int, int]:
    """One grouped update — used for the pregame/status alerts, so a slate's
    worth of lineup posts reads as one organized card (same shape as the
    site's Tonight's Edge / live-results digest) instead of a flat list where
    an urgent line could get lost behind routine ones."""
    desc = "\n\n".join(f"**{head}**\n" + "\n".join(f"· {ln}" for ln in lines[:10])
                        for head, lines in sections if lines)
    embed = {"title": title, "description": desc[:4000], "color": EMBED_COLOR}
    if footer:
        embed["footer"] = {"text": footer}
    return _deliver({"embeds": [embed]})


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(st), encoding="utf-8")
    except Exception as exc:
        print(f"state save failed: {exc}", file=sys.stderr)


def seen(st: dict, key: str) -> bool:
    return key in st.setdefault("seen", {})


def mark(st: dict, key: str) -> None:
    st.setdefault("seen", {})[key] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def surname(nm: str) -> str:
    return str(nm or "").split()[-1] if nm else "?"


def batting_order(pk: int) -> dict[str, list[int]]:
    """The posted batting order of a game, {'home': [ids], 'away': [ids]}; a side with no card is [].
    A pick is only ever named as PLAYING when his id is in here (2026-10-09)."""
    box = get_json(f"{API}/game/{pk}/boxscore?fields=teams,home,away,team,id,battingOrder")
    out: dict[str, list[int]] = {"home": [], "away": []}
    for side in ("home", "away"):
        out[side] = [int(x) for x in (((box or {}).get("teams") or {}).get(side) or {}).get("battingOrder") or []]
    return out


def team_abbrs() -> dict[int, str]:
    j = get_json(f"{API}/teams?sportId=1&fields=teams,id,abbreviation")
    return {t["id"]: t.get("abbreviation", "") for t in (j or {}).get("teams", []) if t.get("id")}


def slate_picks() -> dict[int, list[dict]]:
    """game_pk → picks (designated get role; everyone else rides for names)."""
    data = get_json(SLATE_URL)
    out: dict[int, list[dict]] = {}
    if not data:
        return out
    rows = data if isinstance(data, list) else next(
        (data[k] for k in ("players", "all_players", "rows", "picks") if isinstance(data.get(k), list)), [])
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        try:
            pk = int(r.get("game_pk") or 0)
            pid = int(r.get("player_id") or 0)
        except (TypeError, ValueError):
            continue
        if pk and pid:
            out.setdefault(pk, []).append({
                "pid": pid, "name": str(r.get("name") or ""),
                "team": str(r.get("team") or "").upper(),
                "role": str(r.get("game_pick_role") or "").split("/")[0].strip().upper(),
            })
    return out


def gather_status_sections(st: dict, games: list[dict], picks: dict[int, list[dict]],
                            abbrs: dict[int, str]) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """One pass over today's slate: lineups, scratches, delays, finals.

    Returns (sections, pending_keys). Keys are NOT marked seen yet — that
    happens in main() only once the embed built from them actually delivers,
    so a dead webhook costs a retry next run instead of a permanently lost
    alert (same guarantee the old flat-list version had, kept through the
    reshape)."""
    delay_lines: list[str] = []
    final_lines: list[str] = []
    lineup_lines: list[str] = []
    scratch_lines: list[str] = []
    pending: list[str] = []

    for g in games:
        pk = g.get("gamePk")
        if not pk:
            continue
        status = g.get("status") or {}
        abstract = status.get("abstractGameState")
        detailed = str(status.get("detailedState") or "")
        gp = picks.get(pk, [])
        designated = [p for p in gp if p["role"]]
        home = (g.get("teams") or {}).get("home") or {}
        away = (g.get("teams") or {}).get("away") or {}
        h_ab = abbrs.get((home.get("team") or {}).get("id"), "?")
        a_ab = abbrs.get((away.get("team") or {}).get("id"), "?")

        # ☔ delay / postponement -- once per game per status. Names no one and counts only
        # picks in the POSTED batting order; before a card exists it says so (2026-10-09).
        if any(w in detailed for w in ("Delayed", "Postponed", "Suspended")) and "delay" in STATUS_SECTIONS_ON:
            k = f"delay:{pk}:{detailed}"
            if not seen(st, k):
                order = batting_order(pk)
                posted = bool(order["home"]) and bool(order["away"])
                played = [p for p in designated if p["pid"] in order["home"] + order["away"]]
                if "postpon" in detailed.lower():
                    count: int | None = len(designated)
                else:
                    count = len(played) if posted else None
                if posted and not played and "postpon" not in detailed.lower():
                    pending.append(k)            # every pick is out of it: nothing to say, and nothing to retry
                else:
                    pending.append(k)
                    delay_lines.append(words.delay_line(a_ab, h_ab, detailed, count))

        # 🏁 final recap — the receipts, the moment they're in
        if abstract == "Final" and designated and "final" in STATUS_SECTIONS_ON and not seen(st, f"final:{pk}"):
            box = get_json(f"{API}/game/{pk}/boxscore")
            if box:
                pending.append(f"final:{pk}")
                pl_lines = []
                allp = {}
                for side in ("home", "away"):
                    allp.update(((box.get("teams") or {}).get(side) or {}).get("players") or {})
                for p in designated:
                    bat = ((allp.get(f"ID{p['pid']}") or {}).get("stats") or {}).get("batting") or {}
                    if not bat:
                        continue
                    hr = int(bat.get("homeRuns") or 0)
                    pl_lines.append(f"{'💥 ' if hr else ''}{surname(p['name'])} "
                                    f"{int(bat.get('hits') or 0)}-{int(bat.get('atBats') or 0)}"
                                    + (f" {hr}HR" if hr else ""))
                score = f"{a_ab} {away.get('score', '?')}–{home.get('score', '?')} {h_ab}"
                final_lines.append(words.final_line(score, pl_lines))

        # 📋 lineups + ⚠️ scratches — pregame only
        if abstract == "Preview" and gp and ("lineup" in STATUS_SECTIONS_ON or "scratch" in STATUS_SECTIONS_ON):
            box = get_json(f"{API}/game/{pk}/boxscore?fields=teams,home,away,team,id,battingOrder,players,person")
            for side in ("home", "away"):
                t = ((box or {}).get("teams") or {}).get(side) or {}
                order = t.get("battingOrder") or []
                tid = (t.get("team") or {}).get("id")
                ab2 = abbrs.get(tid, "?")
                if not order:
                    continue
                team_picks = [p for p in gp if p["team"] == ab2]
                if not team_picks:
                    continue
                k = f"lineup:{pk}:{side}"
                if "lineup" in STATUS_SECTIONS_ON and not seen(st, k):
                    pending.append(k)
                    ours = [(p, order.index(p["pid"]) + 1) for p in team_picks if p["pid"] in order]
                    if ours:
                        spots = " · ".join(f"{surname(p['name'])} {i}{'st' if i == 1 else 'nd' if i == 2 else 'rd' if i == 3 else 'th'}"
                                           + (f" ({p['role']})" if p['role'] else "") for p, i in ours[:6])
                        lineup_lines.append(words.lineup_line(ab2, spots))
                # scratches: designated picks on this team NOT in the posted order
                for p in [x for x in team_picks if x["role"] and x["pid"] not in order]:
                    ks = f"scratch:{pk}:{p['pid']}"
                    if "scratch" in STATUS_SECTIONS_ON and not seen(st, ks):
                        pending.append(ks)
                        scratch_lines.append(words.scratch_line(p['name'], p['role'], ab2))

    sections = [
        (words.SECTION_HEADS["lineup"], lineup_lines),
        (words.SECTION_HEADS["scratch"], scratch_lines),
        (words.SECTION_HEADS["final"], final_lines),
        (words.SECTION_HEADS["delay"], delay_lines),
    ]
    return sections, pending


# ── PEN_DOOR_FOLD_ON: one edited "Game status" message per game, only when a CALLED pick is touched ──────────
_PITCHER_MEMO: dict = {}


def pitcher_stats(pid) -> str:
    """The new pitcher's season line from the MLB stats API ('3.45 ERA · 60.1 IP · 70 K · 20 BB'), or '' when it is not there.
    Read at the moment of the change and remembered for this process; never filled in from anything else."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid not in _PITCHER_MEMO:
        j = get_json(f"{API}/people/{pid}?hydrate=stats(group=[pitching],type=[season])"
                     "&fields=people,id,stats,splits,stat,era,inningsPitched,strikeOuts,baseOnBalls")
        stat = {}
        for person in (j or {}).get("people") or []:
            for grp in person.get("stats") or []:
                for sp in grp.get("splits") or []:
                    stat = sp.get("stat") or stat
        _PITCHER_MEMO[pid] = fold.pitcher_stats_text(stat)
    return _PITCHER_MEMO[pid]


def fold_substitution(st: dict, pk, ev: dict, det: dict, about: dict, gp: list[dict], cutoff, upd: dict) -> None:
    """A pinch-hit / pinch-run / defensive change DURING the game that involves a CALLED pick: adds one line to `upd`.
    Not the same thing as a pregame lineup change (that is gather_fold's scratch line, before first pitch)."""
    ts = str(ev.get("startTime") or "")
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return
    if t < cutoff:
        return
    k = f"sub:{pk}:{ts}"
    if seen(st, k) or k in upd["keys"]:
        return
    c = fold.classify_substitution(det, ev, fold.called_only(gp))
    if not c:
        mark(st, k)          # nobody called is involved: closed, never reconsidered
        return
    upd["lines"].append(words.ingame_change_line(c["label"], c["incoming"], c["outgoing"], c["pick"].get("role", ""),
                                                 c["pick_out"], str(about.get("halfInning") or ""), about.get("inning")))
    upd["keys"].append(k)


def gather_fold(st: dict, games: list[dict], picks: dict[int, list[dict]], abbrs: dict[int, str]) -> dict:
    """Pregame and status changes that touch a CALLED pick, per game: {pk: {away, home, lines, keys, day}}.
    A pregame lineup change (a called pick is not in the posted order before the game) and a delay / postponement
    of a game that has a called pick. Nothing is posted for a game with no called pick."""
    out: dict = {}
    for g in games:
        pk = g.get("gamePk")
        if not pk:
            continue
        status = g.get("status") or {}
        abstract = status.get("abstractGameState")
        detailed = str(status.get("detailedState") or "")
        gp = picks.get(pk, [])
        designated = fold.called_only(gp)
        if not designated:
            continue
        home = (g.get("teams") or {}).get("home") or {}
        away = (g.get("teams") or {}).get("away") or {}
        h_ab = abbrs.get((home.get("team") or {}).get("id"), "?")
        a_ab = abbrs.get((away.get("team") or {}).get("id"), "?")
        rec = {"away": a_ab, "home": h_ab, "lines": [], "keys": [], "day": str(g.get("_date") or "")}

        if any(w in detailed for w in ("Delayed", "Postponed", "Suspended")) and "delay" in STATUS_SECTIONS_ON:
            k = f"delay:{pk}:{detailed}"
            if not seen(st, k):
                order = batting_order(pk)
                posted = bool(order["home"]) and bool(order["away"])
                played = [p for p in designated if p["pid"] in order["home"] + order["away"]]
                postponed = "postpon" in detailed.lower()
                rec["keys"].append(k)
                if postponed or not posted or played:
                    count = len(designated) if postponed else (len(played) if posted else None)
                    rec["lines"].append(words.delay_line(a_ab, h_ab, detailed, count))

        if abstract == "Preview" and "scratch" in STATUS_SECTIONS_ON:
            box = get_json(f"{API}/game/{pk}/boxscore?fields=teams,home,away,team,id,battingOrder,players,person")
            for side in ("home", "away"):
                t = ((box or {}).get("teams") or {}).get(side) or {}
                order = t.get("battingOrder") or []
                ab2 = abbrs.get((t.get("team") or {}).get("id"), "?")
                if not order:
                    continue
                for p in [x for x in designated if x["team"] == ab2 and x["pid"] not in order]:
                    ks = f"scratch:{pk}:{p['pid']}"
                    if not seen(st, ks):
                        rec["keys"].append(ks)
                        rec["lines"].append(words.pregame_change_line(p["name"], p["role"], ab2))
        if rec["keys"]:
            out[pk] = rec
    return out


def gather_finals(st: dict, games: list[dict], picks: dict[int, list[dict]], abbrs: dict[int, str]) -> dict:
    """ONE finals post per night: {date: {lines, keys}} for each game date whose games with a called pick are ALL over
    (Final, or postponed / cancelled) and not posted yet. Goes to PEN_DOOR_OWN_WEBHOOK only (the caller checks it is set)."""
    by_date: dict[str, list[dict]] = {}
    for g in games:
        if g.get("gamePk") and fold.called_only(picks.get(g["gamePk"], [])):
            by_date.setdefault(str(g.get("_date") or ""), []).append(g)
    out: dict = {}
    for day, gs in by_date.items():
        key = f"finalnight:{day}"
        if not day or seen(st, key):
            continue
        done = lambda g: (g.get("status") or {}).get("abstractGameState") == "Final" or any(  # noqa: E731
            w in str((g.get("status") or {}).get("detailedState") or "") for w in ("Postponed", "Cancelled"))
        if not all(done(g) for g in gs):
            continue
        lines = []
        for g in gs:
            if (g.get("status") or {}).get("abstractGameState") != "Final":
                continue
            pk = g["gamePk"]
            box = get_json(f"{API}/game/{pk}/boxscore")
            if not box:
                lines = []
                break
            allp = {}
            for side in ("home", "away"):
                allp.update(((box.get("teams") or {}).get(side) or {}).get("players") or {})
            pl = []
            for p in fold.called_only(picks.get(pk, [])):
                bat = ((allp.get(f"ID{p['pid']}") or {}).get("stats") or {}).get("batting") or {}
                if not bat:
                    continue
                hr = int(bat.get("homeRuns") or 0)
                pl.append(f"{'💥 ' if hr else ''}{surname(p['name'])} {int(bat.get('hits') or 0)}-{int(bat.get('atBats') or 0)}"
                          + (f" {hr}HR" if hr else ""))
            home = (g.get("teams") or {}).get("home") or {}
            away = (g.get("teams") or {}).get("away") or {}
            h_ab = abbrs.get((home.get("team") or {}).get("id"), "?")
            a_ab = abbrs.get((away.get("team") or {}).get("id"), "?")
            lines.append(words.final_line(f"{a_ab} {away.get('score', '?')}–{home.get('score', '?')} {h_ab}", pl))
        if lines:
            out[day] = {"lines": lines, "keys": [key]}
    return out


def main_fold() -> int:
    """PEN_DOOR_FOLD_ON: one edited Game status message per game (only for a CALLED pick), finals once a night, pick-at-plate and
    finals to PEN_DOOR_OWN_WEBHOOK. The old digest, its separate pings and its state keys are not touched."""
    now = dt.datetime.now(dt.UTC)
    today, yday = now.date().isoformat(), (now.date() - dt.timedelta(days=1)).isoformat()
    st = load_state()
    fold.prune_state(st, today)
    picks = slate_picks()
    abbrs = team_abbrs()
    sched = get_json(f"{API}/schedule?sportId=1&startDate={yday}&endDate={today}"
                     "&fields=dates,date,games,gamePk,status,abstractGameState,detailedState,"
                     "teams,home,away,team,id,score")
    games = []
    for d in (sched or {}).get("dates", []):
        for g in d.get("games", []):
            g["_date"] = d.get("date")
            games.append(g)

    sent = failed = 0
    attempted = False
    ups = gather_fold(st, games, picks, abbrs)
    for pk, rec in ups.items():
        if not rec["lines"]:
            for k in rec["keys"]:
                mark(st, k)          # nothing to say (every pick is out of that delayed game): closed
            continue
        attempted = True
        ok, bad = fold.publish_game_status(st, pk, rec["day"] or today, words.game_status_title(rec["away"], rec["home"]),
                                           rec["lines"], words.GAME_STATUS_FOOTER, discord_urls())
        sent += ok
        failed += bad
        if ok:
            for k in rec["keys"]:
                mark(st, k)

    own = fold.own_urls()
    if own:                           # unset = finals are not read, not built and not sent
        for day, rec in gather_finals(st, games, picks, abbrs).items():
            attempted = True
            body = {"embeds": [{"title": words.finals_title(day), "description": "\n".join(f"· {ln}" for ln in rec["lines"])[:4000],
                                "color": fold.EMBED_COLOR, "footer": {"text": words.FINALS_FOOTER}}]}
            ok, bad = fold.send_plain(own, body)
            sent += ok
            failed += bad
            if ok:
                for k in rec["keys"]:
                    mark(st, k)

    live_ok, live_bad = live_sweep(st, fold_mode=True)
    save_state(st)
    sent += live_ok
    failed += live_bad
    if not attempted and (live_ok + live_bad) == 0:
        print("quiet window")
        return 0
    print(f"folded: {sent} delivered, {failed} failed")
    if sent == 0:
        print("DELIVERED NOTHING — every webhook refused or none configured. Unmarked items will retry next run.", file=sys.stderr)
        return 1
    return 0


def live_sweep(st: dict, fold_mode: bool = False) -> tuple[int, int]:
    """Up to MAX_LIVE_SWEEPS passes over whatever is live right now, ~90s
    apart, each pitching change or opportunity posted the instant it's
    found. Returns (alerts_sent, alerts_failed) across every sweep."""
    sent = failed = 0
    started = time.monotonic()
    last_sweep_s = 0.0

    for sweep_n in range(MAX_LIVE_SWEEPS):
        if sweep_n > 0:
            if time.monotonic() - started + LIVE_SWEEP_GAP_S + last_sweep_s > LIVE_BUDGET_S:
                break
            time.sleep(LIVE_SWEEP_GAP_S)
        t0 = time.monotonic()

        picks = slate_picks()
        sched = get_json(f"{API}/schedule?sportId=1&date={dt.datetime.now(dt.UTC).date().isoformat()}"
                         "&fields=dates,games,gamePk,status,abstractGameState")
        live_pks = [g["gamePk"] for d in (sched or {}).get("dates", []) for g in d.get("games", [])
                    if (g.get("status") or {}).get("abstractGameState") == "Live"]

        orders: dict[int, dict[str, list[int]]] = {}   # posted batting orders, fetched once per game per sweep
        for pk in live_pks:
            feed = get_json(f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live?fields={FEED_FIELDS_FOLD if fold_mode else FEED_FIELDS}")
            if not feed:
                continue
            gteams = (feed.get("gameData", {}) or {}).get("teams", {}) or {}
            home_ab = (gteams.get("home") or {}).get("abbreviation", "")
            away_ab = (gteams.get("away") or {}).get("abbreviation", "")
            gp = picks.get(pk, [])
            cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=PITCH_LOOKBACK_MIN)
            upd = {"away": away_ab, "home": home_ab, "lines": [], "keys": []}   # PEN_DOOR_FOLD_ON: this game's new status lines

            # 🚪 pitching changes — keyed by their own timestamp, not the run's
            # window, so a change seen on sweep 1 is never re-announced on
            # sweep 2 or 3 of the same run.
            for play in ((feed.get("liveData", {}) or {}).get("plays", {}) or {}).get("allPlays", []) or []:
                about = play.get("about") or {}
                for ev in play.get("playEvents") or []:
                    det = ev.get("details") or {}
                    if det.get("eventType") != "pitching_substitution":
                        if fold_mode:
                            fold_substitution(st, pk, ev, det, about, gp, cutoff, upd)   # a pinch-hit / in-game change touching a CALLED pick
                        continue
                    ts = str(ev.get("startTime") or "")
                    try:
                        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if t < cutoff:
                        continue
                    k = f"pen:{pk}:{ts}"
                    if seen(st, k):
                        continue
                    half = str(about.get("halfInning") or "")
                    pitching_ab = home_ab if half == "top" else away_ab
                    batting_ab = away_ab if half == "top" else home_ab
                    # RELEVANCE GATE (2026-09-06, Donovan: "pendoors a little
                    # much... less"). slate_picks()'s own docstring says it —
                    # "designated get role; everyone else rides for names" —
                    # so `gp` is every tracked hitter in the game, roughly 90
                    # a night, not just picks. Filtering only by team (the old
                    # test) meant a bullpen change in ANY live game posted,
                    # whether or not it touched a single designated pick: on
                    # a 15-game slate that is 30-60+ pings a night on its own,
                    # almost certainly the bulk of the noise being reported.
                    # A pitching change only means something to a follower of
                    # THIS account's picks when a DESIGNATED pick (role set —
                    # same test the ⚠️ scratch and 🚨 opportunity alerts
                    # already use) is on the batting side about to face the
                    # new arm. No pick facing him, no ping — skip, but still
                    # mark it seen so it is never reconsidered on the next
                    # sweep or a later run.
                    # LINEUP + GAME CHECK (2026-10-09). `gp` is already this game's picks (slate_picks is
                    # keyed by game_pk); a pick is named only when he is on the batting team AND his id is in
                    # this game's posted batting order for that side -- a pick the same run lists as out of
                    # the lineup (Scratch watch) is never named as playing.
                    order = orders.setdefault(pk, batting_order(pk))
                    side_ids = order["away" if half == "top" else "home"]
                    our = [p["name"] for p in gp if p["role"] and p["team"] == batting_ab and p["pid"] in side_ids][:3]
                    if not our:
                        mark(st, k)
                        continue
                    if fold_mode:
                        # the NEW pitcher by name plus his season line, read now; never the feed's stale sentence
                        newp, oldp = ev.get("player") or {}, ev.get("replacedPlayer") or {}
                        desc = words.clean_change(str(det.get("description") or ""))
                        new_name = str(newp.get("fullName") or "") or desc.split(" replaces ")[0].strip()
                        old_name = str(oldp.get("fullName") or "") or (desc.split(" replaces ", 1)[1].strip() if " replaces " in desc else "")
                        line = words.pitching_change_status_line(pitching_ab, half, about.get("inning"), new_name, old_name,
                                                                 pitcher_stats(newp.get("id")), our)
                        upd["lines"].append(line)
                        upd["keys"].append(k)
                        continue
                    line = words.pitching_change(pitching_ab, half, about.get("inning"), str(det.get("description") or ""), our)
                    ok, bad = post(line)
                    sent += ok
                    failed += bad
                    if ok:
                        mark(st, k)

            # 🚨 opportunity — a designated pick at the plate with 2+ on
            ld = feed.get("liveData", {}) or {}
            cur = (ld.get("plays", {}) or {}).get("currentPlay") or {}
            batter = ((cur.get("matchup") or {}).get("batter") or {})
            ls = ld.get("linescore") or {}
            off = ls.get("offense") or {}
            runners = sum(1 for b in ("first", "second", "third") if (off.get(b) or {}).get("id"))
            if batter.get("id") and runners >= 2:
                p = next((x for x in gp if x["role"] and x["pid"] == batter["id"]), None)
                if p:
                    inn = ls.get("currentInning")
                    k = f"opp:{pk}:{p['pid']}:{inn}"
                    if not seen(st, k):
                        line = words.pick_at_plate(p["name"], p["role"], runners,
                                                   "top" if ls.get("isTopInning") else "bottom", inn, away_ab, home_ab)
                        if fold_mode:
                            # PEN_DOOR_FOLD_ON: pick-at-the-plate goes to Donovan's own server only; unset = not sent
                            own = fold.own_urls()
                            ok, bad = fold.send_plain(own, {"content": line[:1900]}) if own else (0, 0)
                        else:
                            ok, bad = post(line)
                        sent += ok
                        failed += bad
                        if ok:
                            mark(st, k)

            if fold_mode and upd["lines"]:
                ok, bad = fold.publish_game_status(st, pk, dt.datetime.now(dt.UTC).date().isoformat(),
                                                   words.game_status_title(away_ab, home_ab), upd["lines"],
                                                   words.GAME_STATUS_FOOTER, discord_urls())
                sent += ok
                failed += bad
                if ok:
                    for k in upd["keys"]:
                        mark(st, k)

        last_sweep_s = time.monotonic() - t0

    return sent, failed


def main() -> int:
    if fold.fold_on():
        return main_fold()
    now = dt.datetime.now(dt.UTC)
    today, yday = now.date().isoformat(), (now.date() - dt.timedelta(days=1)).isoformat()

    st = load_state()
    picks = slate_picks()
    abbrs = team_abbrs()

    sched = get_json(f"{API}/schedule?sportId=1&startDate={yday}&endDate={today}"
                     "&fields=dates,games,gamePk,status,abstractGameState,detailedState,"
                     "teams,home,away,team,id,score")
    games = [g for d in (sched or {}).get("dates", []) for g in d.get("games", [])]

    # ── grouped digest: lineups, scratches, delays, finals — once, up front,
    # not held for the live sweep below ──
    sections, pending_keys = gather_status_sections(st, games, picks, abbrs)
    digest_ok = digest_bad = 0
    has_digest = any(lines for _, lines in sections)
    if has_digest:
        digest_ok, digest_bad = post_embed(words.STATUS_TITLE, sections, footer=words.STATUS_FOOTER)
        if digest_ok:
            for k in pending_keys:
                mark(st, k)

    # ── live sweep: pitching changes + opportunities, posted as they happen ──
    live_ok, live_bad = live_sweep(st)

    save_state(st)

    total_sent = digest_ok + live_ok
    had_content = has_digest or (live_ok + live_bad) > 0

    if not had_content:
        print("quiet window")
        return 0

    print(f"digest: {'sent' if has_digest else 'nothing to say'}"
          + (f" ({digest_ok} hook(s), {digest_bad} failed)" if has_digest else "")
          + f" · live: {live_ok} alert(s) delivered, {live_bad} failed")

    if total_sent == 0:
        # NOTHING GOT THROUGH ANYWHERE. The run must go RED — a silent green
        # run is why this went unnoticed for however long it was broken
        # (2026-08-11). State was only marked for items that actually
        # delivered (see above), so nothing here was lost; it just retries.
        print("DELIVERED NOTHING — every webhook refused or none configured. "
              "Unmarked items will retry next run.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
