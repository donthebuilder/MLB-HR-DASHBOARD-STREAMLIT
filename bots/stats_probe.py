#!/usr/bin/env python3
"""What does StatsAPI actually give us for the fields we do not carry yet?

Donovan, 2026-08-23: "find all of these stats and odds — they have all of
those. make a model based on pitcher meatballs, wild pitches, abs challenge
record for catcher and pitcher and batter, defense stats and caught stealing
percentage and pitcher caught stealing and pitcher pick off rate."

WHY A PROBE AND NOT JUST THE FETCHER
====================================
Of the eight things named, exactly two are already on the slate:

    pitcher_meatball_pct     ✓ published on every row, and used by NOTHING
    season_sb / season_cs    ✓ SB v1, 2026-08-23

The other six — wild pitches, pickoffs, catcher caught-stealing rate, team
defense, and the ABS challenge record for all three roles — are not carried
anywhere in this repo, and Claude's sandbox cannot reach statsapi.mlb.com at
all (the CONNECT tunnel is blocked; verified again today). So the field names
would have to be GUESSED, and this codebase already has a written record of
what guessing an API's shape costs: the odds pipeline took "eight round trips
of failure" to learn one provider's response, and its own comment says so.

ABS is the sharpest case. The automated ball-strike challenge system is new
for 2026; whether StatsAPI exposes a challenge record at all — and under what
name, on which endpoint, keyed to which role — is not something anyone should
assume. A model built on a field that turns out not to exist is worse than no
model, because it looks like it works: every hitter gets 0.0 and the term
quietly contributes nothing while appearing in the config hash.

So this asks, on a GitHub runner where the network works, and writes the
answer down. It fetches nothing it does not need, hits each endpoint once, and
prints EVERY key it finds so the answer survives a rename.

    python bots/stats_probe.py                  # print
    python bots/stats_probe.py --out probe.json # and write it

WHAT IT ANSWERS, one section each:
  1. pitching season stats  — wildPitches, pickoffs, balks, and the full key list
  2. catching/fielding      — caughtStealing, stolenBases, passedBall, and the
                              catcher's own throwing line
  3. team fielding          — what a team-level defensive line contains
  4. the live feed          — whether a game's playEvents carry an ABS/challenge
                              record, and what it is called
  5. statcast-ish extras    — anything on the person endpoint we are not using
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import ssl
import sys
import urllib.error
import urllib.request

BASE = "https://statsapi.mlb.com/api/v1"
FEED = "https://statsapi.mlb.com/api/v1.1"
SEASON = dt.date.today().year


def get(url: str, insecure: bool = False):
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "moonshot-stats-probe"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def head(title: str) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


def keys_of(d, prefix="") -> list:
    """Every leaf key path in a dict, so a rename cannot hide a field."""
    out = []
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                out += keys_of(v, p)
            else:
                out.append(p)
    elif isinstance(d, list) and d:
        out += keys_of(d[0], f"{prefix}[]")
    return out


def look_for(keys: list, *needles) -> list:
    low = [k for k in keys if any(nd.lower() in k.lower() for nd in needles)]
    return sorted(set(low))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="also write the findings as JSON")
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (for a machine that intercepts TLS)")
    ap.add_argument("--game", type=int, default=0, help="a specific gamePk for the feed probe")
    a = ap.parse_args()
    ins = a.insecure
    found: dict = {"season": SEASON, "probed_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    # ── a real game, so every id below is a real id ──────────────────────────
    head("0. a game to work from")
    game_pk = a.game
    try:
        if not game_pk:
            for back in range(0, 8):
                d = (dt.date.today() - dt.timedelta(days=back)).isoformat()
                sched = get(f"{BASE}/schedule?sportId=1&date={d}", ins)
                games = [g for day in sched.get("dates", []) for g in day.get("games", [])]
                done = [g for g in games if str(g.get("status", {}).get("abstractGameState")) == "Final"]
                if done:
                    game_pk = done[0]["gamePk"]
                    print(f"  using {d} · gamePk {game_pk} · {len(games)} games that day")
                    break
        if not game_pk:
            print("  no finished game found in the last week — cannot probe the feed")
    except Exception as e:                                        # noqa: BLE001
        print(f"  schedule failed: {type(e).__name__}: {e}")
    found["game_pk"] = game_pk

    box = {}
    if game_pk:
        try:
            box = get(f"{BASE}/game/{game_pk}/boxscore", ins)
        except Exception as e:                                    # noqa: BLE001
            print(f"  boxscore failed: {type(e).__name__}: {e}")

    # ── 1. PITCHING: wild pitches, pickoffs, balks ──────────────────────────
    head("1. pitching — wild pitches, pickoffs, balks")
    pit_id = None
    try:
        for side in ("home", "away"):
            for pid, p in (box.get("teams", {}).get(side, {}).get("players", {}) or {}).items():
                st = (p.get("stats", {}) or {}).get("pitching", {}) or {}
                if st.get("inningsPitched"):
                    pit_id = int(str(pid).replace("ID", ""))
                    break
            if pit_id:
                break
        if pit_id:
            js = get(f"{BASE}/people/{pit_id}/stats?stats=season&group=pitching&season={SEASON}", ins)
            stat = ((js.get("stats") or [{}])[0].get("splits") or [{}])[0].get("stat", {})
            ks = keys_of(stat)
            print(f"  pitcher {pit_id} · {len(ks)} season pitching fields")
            for label, needles in (("wild pitches", ("wildpitch",)),
                                   ("pickoffs", ("pickoff",)),
                                   ("balks", ("balk",)),
                                   ("holds/blown", ("hold", "blown")),
                                   ("stolen bases against", ("stolenbase", "caughtstealing")),
                                   ("meatball / heart", ("meatball", "heart", "zone"))):
                hit = look_for(ks, *needles)
                print(f"    {label:24} {', '.join(f'{k}={stat.get(k.split(chr(46))[-1])}' for k in hit) if hit else '— absent —'}")
            found["pitching_season_keys"] = ks
            found["pitching_sample"] = {k: stat.get(k) for k in ks[:80]}
        else:
            print("  no starter found in that box score")
    except Exception as e:                                        # noqa: BLE001
        print(f"  pitching stats failed: {type(e).__name__}: {e}")

    # ── 2. CATCHING: caught stealing, the catcher's throwing line ───────────
    head("2. fielding — the catcher's caught-stealing line")
    try:
        cat_id = None
        for side in ("home", "away"):
            for pid, p in (box.get("teams", {}).get(side, {}).get("players", {}) or {}).items():
                pos = (p.get("position", {}) or {}).get("abbreviation")
                if pos == "C":
                    cat_id = int(str(pid).replace("ID", ""))
                    break
            if cat_id:
                break
        if cat_id:
            js = get(f"{BASE}/people/{cat_id}/stats?stats=season&group=fielding&season={SEASON}", ins)
            splits = (js.get("stats") or [{}])[0].get("splits") or []
            print(f"  catcher {cat_id} · {len(splits)} fielding split(s) (one per position)")
            for sp in splits:
                stat = sp.get("stat", {})
                pos = (sp.get("position", {}) or {}).get("abbreviation", "?")
                ks = keys_of(stat)
                cs = look_for(ks, "caughtstealing", "stolenbase", "passedball", "throwing")
                print(f"    pos {pos:3} · {len(ks)} fields · {', '.join(f'{k}={stat.get(k)}' for k in cs) or 'no CS/SB fields'}")
                if pos == "C":
                    found["catcher_fielding_keys"] = ks
                    found["catcher_fielding_sample"] = stat
        else:
            print("  no catcher found in that box score")
    except Exception as e:                                        # noqa: BLE001
        print(f"  fielding stats failed: {type(e).__name__}: {e}")

    # ── 3. TEAM DEFENSE ─────────────────────────────────────────────────────
    head("3. team defense — what a team fielding line carries")
    try:
        js = get(f"{BASE}/teams/stats?season={SEASON}&sportIds=1&group=fielding&stats=season", ins)
        splits = (js.get("stats") or [{}])[0].get("splits") or []
        print(f"  {len(splits)} teams")
        if splits:
            stat = splits[0].get("stat", {})
            ks = keys_of(stat)
            print(f"  {len(ks)} fields: {', '.join(ks)}")
            found["team_fielding_keys"] = ks
            found["team_fielding_sample"] = {
                (splits[0].get("team", {}) or {}).get("abbreviation", "?"): stat}
    except Exception as e:                                        # noqa: BLE001
        print(f"  team fielding failed: {type(e).__name__}: {e}")

    # ── 4. ABS CHALLENGES — the one nobody should assume ────────────────────
    head("4. ABS challenges — does the live feed carry a challenge record?")
    try:
        if game_pk:
            feed = get(f"{FEED}/game/{game_pk}/feed/live", ins)
            plays = ((feed.get("liveData", {}) or {}).get("plays", {}) or {}).get("allPlays", []) or []
            print(f"  {len(plays)} plays in the feed")
            hits, sample = set(), None
            for pl in plays:
                for ev in pl.get("playEvents", []) or []:
                    for k in keys_of(ev):
                        if any(w in k.lower() for w in ("challenge", "abs", "review", "overturn")):
                            hits.add(k)
                            if sample is None:
                                sample = ev
            if hits:
                print(f"  CHALLENGE-ISH KEYS FOUND: {', '.join(sorted(hits))}")
                found["abs_keys"] = sorted(hits)
                found["abs_sample_event"] = sample
            else:
                print("  no challenge/ABS/review key anywhere in playEvents.")
                print("  → the ABS challenge record is NOT in the live feed under any of")
                print("    'challenge', 'abs', 'review', 'overturn'. If it exists it is")
                print("    somewhere else, and the model term must wait for it.")
                found["abs_keys"] = []
            # the whole event key surface, so a differently-named field is still visible
            allk = set()
            for pl in plays[:40]:
                for ev in pl.get("playEvents", []) or []:
                    allk.update(keys_of(ev))
            found["play_event_keys"] = sorted(allk)
            print(f"  (recorded {len(allk)} distinct playEvent key paths for reference)")
    except Exception as e:                                        # noqa: BLE001
        print(f"  live feed failed: {type(e).__name__}: {e}")

    # ── 5. WHAT ELSE IS ON A PITCHER WE ARE NOT USING ───────────────────────
    head("5. every pitching field, so nothing is missed by name")
    ks = found.get("pitching_season_keys") or []
    if ks:
        for i in range(0, len(ks), 6):
            print("   " + "  ".join(k.ljust(24) for k in ks[i:i + 6]))

    head("VERDICT")
    def say(label, ok, detail=""):
        print(f"  {'✓' if ok else '✗'} {label:34} {detail}")
    pk = found.get("pitching_season_keys") or []
    ck = found.get("catcher_fielding_keys") or []
    tk = found.get("team_fielding_keys") or []
    say("wild pitches", bool(look_for(pk, "wildpitch")), ", ".join(look_for(pk, "wildpitch")))
    say("pickoffs", bool(look_for(pk, "pickoff")), ", ".join(look_for(pk, "pickoff")))
    say("pitcher SB/CS against", bool(look_for(pk, "stolenbase", "caughtstealing")),
        ", ".join(look_for(pk, "stolenbase", "caughtstealing")))
    say("catcher caught stealing", bool(look_for(ck, "caughtstealing")),
        ", ".join(look_for(ck, "caughtstealing", "stolenbase")))
    say("team defense", bool(tk), f"{len(tk)} fields")
    say("ABS challenge record", bool(found.get("abs_keys")),
        ", ".join(found.get("abs_keys") or []) or "not in the live feed")
    print("\n  Already on the slate and unused by any model: pitcher_meatball_pct,")
    print("  season_sb, season_cs, season_sb_attempt_rate.")

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(found, f, indent=1, default=str)
        print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
