#!/usr/bin/env python3
"""
🚪📻 LIVE WATCH (2026-08-08). Started life as pen-door-only; grew into the
full in-game Discord layer the same day ("what other ones can we add to help
people" — all of them, plus bases-loaded spots):

  🚪 pitching change            (10-min disjoint window — no state needed)
  📋 lineup posted              our picks' batting spots, once per side
  ⚠️ SCRATCH                    a designated pick missing from a posted lineup
  ☔ delay / postponement       once per game per status
  🏁 final recap                picks' lines the moment a game goes final
  🚨 opportunity                a pick AT THE PLATE with 2+ runners on

Everything except the pitching-change window needs memory across runs — a
lineup that pinged at 4:10 must not ping at 4:20 — so state lives in
state/pen_door_state.json, persisted between runs by actions/cache in
pen-door.yml. Losing the state file is safe: worst case is one repeat ping.

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
import urllib.request
from pathlib import Path

API = "https://statsapi.mlb.com/api/v1"
FEED_FIELDS = ("gameData,teams,home,away,abbreviation,liveData,plays,allPlays,"
               "about,halfInning,inning,playEvents,details,eventType,description,startTime,"
               "currentPlay,matchup,batter,id,fullName,"
               "linescore,currentInning,isTopInning,offense,first,second,third")
SLATE_URL = ("https://raw.githubusercontent.com/donthebuilder/MLB-HR-DASHBOARD-STREAMLIT/"
             "data/public/data/current/today_slim.json")
WINDOW_MIN = 10
STATE_PATH = Path("state/pen_door_state.json")


def get_json(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "moonshot-live-watch/2.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        print(f"fetch failed {url[:80]}: {exc}", file=sys.stderr)
        return None


def discord_urls() -> list[str]:
    raw = os.environ.get("DISCORD_WEBHOOK", "")
    return [u.strip() for u in raw.replace(",", "\n").split() if u.strip().startswith("http")]


def post(msg: str) -> tuple[int, int]:
    """Send to every configured webhook. Returns (delivered, failed).

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
    run (see the tail of main).
    """
    urls = discord_urls()
    if not urls:
        print("DISCORD_WEBHOOK is unset or holds no http(s) URL — nothing can be delivered",
              file=sys.stderr)
        return 0, 0
    ok = bad = 0
    for i, url in enumerate(urls, 1):
        try:
            req = urllib.request.Request(
                url, data=json.dumps({"content": msg[:1900]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "moonshot-bot"})
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"discord hook {i}/{len(urls)}: HTTP {r.status}")
            ok += 1
        except Exception as exc:
            # 404 = webhook deleted in Discord; 401 = token rotated. Both mean
            # the Actions secret is stale and only a human can refresh it.
            code = getattr(exc, "code", None)
            hint = " (webhook deleted — refresh the DISCORD_WEBHOOK secret)" if code == 404 else ""
            print(f"discord hook {i}/{len(urls)} FAILED: {exc}{hint}", file=sys.stderr)
            bad += 1
    return ok, bad


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


def main() -> int:
    now = dt.datetime.now(dt.UTC)
    tick = now.replace(minute=(now.minute // WINDOW_MIN) * WINDOW_MIN, second=0, microsecond=0)
    w_start, w_end = tick - dt.timedelta(minutes=WINDOW_MIN), tick
    today, yday = now.date().isoformat(), (now.date() - dt.timedelta(days=1)).isoformat()

    st = load_state()
    picks = slate_picks()
    abbrs = team_abbrs()

    sched = get_json(f"{API}/schedule?sportId=1&startDate={yday}&endDate={today}"
                     "&fields=dates,games,gamePk,status,abstractGameState,detailedState,"
                     "teams,home,away,team,id,score")
    games = [g for d in (sched or {}).get("dates", []) for g in d.get("games", [])]
    lines: list[str] = []

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

        # ☔ delay / postponement — once per game per status
        if any(w in detailed for w in ("Delayed", "Postponed", "Suspended")):
            k = f"delay:{pk}:{detailed}"
            if not seen(st, k):
                mark(st, k)
                lines.append(f"☔ **{a_ab}@{h_ab} — {detailed.upper()}**"
                             + (f" · {len(designated)} pick{'s' if len(designated) != 1 else ''} affected"
                                f" ({', '.join(surname(p['name']) for p in designated[:4])})" if designated else ""))

        # 🏁 final recap — the receipts, the moment they're in
        if abstract == "Final" and designated and not seen(st, f"final:{pk}"):
            box = get_json(f"{API}/game/{pk}/boxscore")
            if box:
                mark(st, f"final:{pk}")
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
                lines.append(f"🏁 **FINAL {score}** — " + (" · ".join(pl_lines) if pl_lines else "no pick lines found"))

        # 📋 lineups + ⚠️ scratches — pregame only
        if abstract == "Preview" and gp:
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
                if not seen(st, k):
                    mark(st, k)
                    ours = [(p, order.index(p["pid"]) + 1) for p in team_picks if p["pid"] in order]
                    if ours:
                        spots = " · ".join(f"{surname(p['name'])} {i}{'st' if i == 1 else 'nd' if i == 2 else 'rd' if i == 3 else 'th'}"
                                           + (f" ({p['role']})" if p['role'] else "") for p, i in ours[:6])
                        lines.append(f"📋 **{ab2} lineup posted** — {spots}")
                # scratches: designated picks on this team NOT in the posted order
                for p in [x for x in team_picks if x["role"] and x["pid"] not in order]:
                    ks = f"scratch:{pk}:{p['pid']}"
                    if not seen(st, ks):
                        mark(st, ks)
                        lines.append(f"⚠️ **SCRATCH WATCH — {p['name']}** ({p['role']} pick) is NOT in the posted {ab2} lineup")

    # live-game work: pen doors + opportunity spots, one feed per live game
    live_pks = [g["gamePk"] for g in games if (g.get("status") or {}).get("abstractGameState") == "Live"]
    for pk in live_pks:
        feed = get_json(f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live?fields={FEED_FIELDS}")
        if not feed:
            continue
        gteams = (feed.get("gameData", {}) or {}).get("teams", {}) or {}
        home_ab = (gteams.get("home") or {}).get("abbreviation", "")
        away_ab = (gteams.get("away") or {}).get("abbreviation", "")
        gp = picks.get(pk, [])

        # 🚪 pitching changes in this run's disjoint window
        for play in ((feed.get("liveData", {}) or {}).get("plays", {}) or {}).get("allPlays", []) or []:
            about = play.get("about") or {}
            for ev in play.get("playEvents") or []:
                det = ev.get("details") or {}
                if det.get("eventType") != "pitching_substitution":
                    continue
                try:
                    t = dt.datetime.fromisoformat(str(ev.get("startTime") or "").replace("Z", "+00:00"))
                except ValueError:
                    continue
                if not (w_start <= t < w_end):
                    continue
                half = str(about.get("halfInning") or "")
                pitching_ab = home_ab if half == "top" else away_ab
                batting_ab = away_ab if half == "top" else home_ab
                our = [surname(p["name"]) for p in gp if p["team"] == batting_ab][:3]
                line = f"🚪 **{pitching_ab}** pen, {half} {about.get('inning')}: {str(det.get('description') or '').rstrip('.')}"
                if our:
                    line += f" — our bats attacking: {', '.join(our)}"
                lines.append(line)

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
                    mark(st, k)
                    spot = "bases loaded" if runners == 3 else f"{runners} on"
                    lines.append(f"🚨 **{p['name']}** ({p['role']} pick) at the plate RIGHT NOW with the {spot}"
                                 f" — inning {inn}, {away_ab}@{home_ab}")

    if not lines:
        print("quiet window")
        save_state(st)
        return 0

    ok, bad = post("\n".join(lines[:12]))
    if ok:
        print(f"delivered {len(lines)} alert(s) to {ok} webhook(s)" + (f", {bad} failed" if bad else ""))
        save_state(st)
        return 0

    # NOTHING GOT THROUGH. Two things have to happen, and the old code did
    # neither.
    #
    # 1. The run must go RED. A silent green run is why this went unnoticed
    #    for however long it has been broken -- the Actions tab is the only
    #    place the failure is visible and it was showing a checkmark.
    # 2. The state must NOT be saved. seen()/mark() are stamped while the
    #    lines are being BUILT, before any delivery is attempted, so saving
    #    here would record "already alerted" for alerts that never left the
    #    building -- and every one of them is once-per-game-per-player. A
    #    single failed run would burn tonight's scratch alert permanently.
    #    Dropping the state means the next run re-detects and re-sends.
    print(f"DELIVERED NOTHING — {len(lines)} alert(s) had nowhere to go. "
          f"State not saved, so the next run will retry them.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
