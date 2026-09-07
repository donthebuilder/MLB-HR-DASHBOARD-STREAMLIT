"""Injury designations, from ESPN, because nflverse cannot supply them.

WHY THIS FILE EXISTS
--------------------
`nfl_features.injuries()` calls `nflreadpy.load_injuries()`. That function has
had no data since the 2024 season and, as of 2026-09-07, does not merely return
empty for the current year — it raises:

    ValueError: Season must be between 2009 and 2025

So `inj_q` has been 0 for every player all season, `nfl_bot` publishes
`questionable: False` on all of them, and `injury_status` was never a key on a
player row at all. The site has been rendering

    {player.injury_status && ' · ' + player.injury_status}

against a field that has never existed, on the boards, the draft board, the team
page and the player card. Week 1 of 2026 has 529 players on the card and not one
availability tag anywhere on the site.

ESPN publishes the whole league's injury report as one document, and the bot
already talks to ESPN for the scoreboard, box scores and scoring plays, so this
adds a source we already depend on rather than a new one.

THE JOIN
--------
ESPN keys players by its own athlete id; everything in this project is keyed by
gsis_id. `nflreadpy.load_players()` carries both, so the crosswalk is a lookup,
not a name match. Names are never used to join — "Michael Carter" is two people.

Measured against the live Week 1 card on 2026-09-07:
    800 ESPN injury rows -> 798 resolve to a gsis_id
    (the 2 that don't are defensive players who are on no prop board)
    401 of 529 slate players appear on the report at all
    of those: 65 Questionable, 1 Out, 335 listed but Active

WHAT COUNTS AS A TAG
--------------------
Only a designation that changes whether you'd bet him. ESPN lists a player as
"Active" when he carries a note but is playing — 335 of the 401 above. Tagging
those would put a badge on two thirds of the board and teach everyone to ignore
the badge, so an Active player publishes no tag, exactly like a player who was
never on the report. Absence means "nothing to report", which is the truth.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not feed the model. `QUESTIONABLE_DAMP` in nfl_features.py exists to
take 15% off a questionable player's opportunity features, and it has never
fired. Wiring this into `inj_q` would change 65 players' scores on the Week 1
card, hours before kickoff, with no backtest of a damp that has never run on
real data. That is a scoring change and it needs its own pass and its own
verification. This file only publishes what is true so the site can show it.
"""
from __future__ import annotations

import re
from typing import Any

import requests

# One document, whole league, ~9 MB.
INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
TIMEOUT = 20

# ESPN's wording -> what the site shows. Anything not here publishes no tag.
# "Active" is deliberately absent; see the module docstring.
STATUS_MAP = {
    "Questionable": "Q",
    "Doubtful": "D",
    "Out": "OUT",
    "Injured Reserve": "IR",
    "Suspension": "SUSP",
    "Physically Unable to Perform": "PUP",
    "Non Football Injury": "NFI",
}

# Designations that mean he is not playing this week. The site can colour these
# differently from a Q, and a future scoring pass can drop them outright.
OUT_CODES = frozenset({"OUT", "IR", "SUSP", "PUP", "NFI"})


def _espn_athlete_id(athlete: dict[str, Any]) -> str | None:
    """ESPN's athlete id, taken from the headshot or profile URL.

    The injury record's own `id` is the id of the REPORT, not the player, which
    is an easy and silent thing to get wrong — an early draft of this joined on
    it and matched nobody.
    """
    href = (athlete.get("headshot") or {}).get("href") or ""
    m = re.search(r"/(\d+)\.png", href)
    if m:
        return m.group(1)
    for link in athlete.get("links") or []:
        m = re.search(r"/id/(\d+)", link.get("href") or "")
        if m:
            return m.group(1)
    return None


def _crosswalk() -> dict[str, str]:
    """espn_id -> gsis_id, from the player registry nflverse still publishes."""
    import nflreadpy as nfl

    df = nfl.load_players().select(["gsis_id", "espn_id"]).drop_nulls("gsis_id")
    out: dict[str, str] = {}
    for gsis, espn in zip(df["gsis_id"].to_list(), df["espn_id"].to_list()):
        if espn is None:
            continue
        # espn_id arrives as a float on some builds ("4870808.0" is not an id).
        key = str(espn).strip()
        if key.endswith(".0"):
            key = key[:-2]
        if key and key != "None":
            out[key] = gsis
    return out


def espn_ids() -> dict[str, str]:
    """{gsis_id: espn_id} for the player rows, so the site can show a face.

    The site already has every piece of a headshot except this one. It carries a
    3,252-entry GSIS->ESPN map (lib/nfl/headshotIds.js) and a CDN URL builder
    (lib/nfl/nflAssets.js), and FRANCHISE renders faces from them today. TUDDY
    cannot: that map is ~72 KB and its own header says it is imported by server
    components only, while every TUDDY tab is a client component. Importing it
    there would ship 72 KB to the browser to save a field that costs eight bytes
    a row here.

    So the id rides on the payload instead. Same crosswalk the injury join
    already builds, read the other way round.

    Never raises, for the same reason fetch() doesn't: a face is decoration and
    must not cost a slate.
    """
    try:
        xw = _crosswalk()
    except Exception as exc:  # noqa: BLE001
        print(f"  espn ids: crosswalk failed ({type(exc).__name__}) — no faces this run")
        return {}
    return {gsis: espn for espn, gsis in xw.items()}


def attach_espn_ids(rows: list[dict[str, Any]], ids: dict[str, str]) -> int:
    """Write `espn_id` onto published player rows. Returns how many."""
    n = 0
    for row in rows:
        espn = ids.get(row.get("player_id"))
        if espn:
            row["espn_id"] = espn
            n += 1
    return n


def fetch(session: requests.Session | None = None) -> dict[str, str]:
    """{gsis_id: code} for every player carrying a real designation.

    Never raises. An injury feed that is down must not take the slate down with
    it — the board is still correct without tags, it is only less informed, and
    a bot run that dies here publishes nothing at all. The caller logs the count
    so a silent zero is visible in the run output rather than looking like a
    healthy league.
    """
    sess = session or requests
    try:
        r = sess.get(INJURIES_URL, timeout=TIMEOUT)
        r.raise_for_status()
        body = r.json()
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        print(f"  injuries: ESPN fetch failed ({type(exc).__name__}) — no tags this run")
        return {}

    try:
        xw = _crosswalk()
    except Exception as exc:  # noqa: BLE001
        print(f"  injuries: crosswalk failed ({type(exc).__name__}) — no tags this run")
        return {}

    out: dict[str, str] = {}
    seen = unmapped = 0
    for team in body.get("injuries") or []:
        for item in team.get("injuries") or []:
            seen += 1
            code = STATUS_MAP.get((item.get("status") or "").strip())
            if not code:
                continue
            espn_id = _espn_athlete_id(item.get("athlete") or {})
            gsis = xw.get(espn_id) if espn_id else None
            if not gsis:
                unmapped += 1
                continue
            # A player can appear twice (two body parts). Out beats Questionable.
            prior = out.get(gsis)
            if prior is None or (code in OUT_CODES and prior not in OUT_CODES):
                out[gsis] = code
    print(f"  injuries: {seen} ESPN rows -> {len(out)} tagged"
          + (f", {unmapped} unmatched" if unmapped else ""))
    return out


def attach(rows: list[dict[str, Any]], status: dict[str, str]) -> int:
    """Write `injury_status` onto published player rows. Returns how many.

    `questionable` is set from the same source so the existing Picks-tab "?"
    flag stops lying, but the feature-side `inj_q` is left alone — that one
    moves scores, and this pass does not.
    """
    n = 0
    for row in rows:
        code = status.get(row.get("player_id"))
        if not code:
            continue
        row["injury_status"] = code
        row["questionable"] = code == "Q"
        n += 1
    return n
