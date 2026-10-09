"""THE WORDS OF THE PEN-DOOR POSTS, IN ONE FILE (2026-10-09).

Edit the words here; bots/pen_door_watch.py only decides WHEN to post. The site's
phone-push words live in the site repo (lib/copy/notifications.js); the preview page
is generated from both: `python3 bots/copy_pendoor.py --samples` prints this file's
samples as JSON for the site's scripts/build-notifications-preview.mjs.

KEYS (trigger -> producer in bots/pen_door_watch.py)
  status.title / status.footer    the grouped "tonight's status" embed          main()
  status.scratch_line             a designated pick is not in the posted order  gather_status_sections
  status.delay_line               a delay, suspension or postponement           gather_status_sections
  status.lineup_line              a posted lineup (OFF by default: phone push owns it)
  status.final_line               a final recap (OFF by default: receipts own it)
  pitching_change                 a pitching change facing a pick who is playing live_sweep
  pick_at_plate                   a designated pick at the plate, 2+ on         live_sweep

RULES (same as the site): the player's full name once; plain words; no "bot", no raw
feed words ("Delayed: RAIN" -> "Rain delay", "Pitching Change: X replaces Y." ->
"X replaces Y", "bottom 7" -> "Bottom 7th"); no links; no probability words; a pick is
only ever named as PLAYING when the posted batting order lists him. Discord has room, so
the long form of an inning is used here ("Bottom 7th"); the phone uses the shorthand.
"""
from __future__ import annotations

import json
import re
import sys

# Which sections of the status embed are on. The phone push is the primary channel for
# scratch / lineup / delay (site: lib/dash/pushRules.js); Discord keeps the two that name
# designated picks, lineup-checked. Lineups and finals are duplicated by the phone push and
# the night receipts, so they are off (flip here to bring them back).
STATUS_SECTIONS_ON = ("scratch", "delay")

STATUS_TITLE = "📋 Tonight's status"
STATUS_FOOTER = "scratches · delays, once per check"
SECTION_HEADS = {
    "lineup": "📋 Lineups posted",
    "scratch": "⚠️ Scratch watch",
    "final": "🏁 Final recaps",
    "delay": "☔ Delays and postponements",
}


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def ordinal(n: int) -> str:
    n = int(n)
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def inning_text(half: str, inning) -> str:
    """'top' / 'bottom' + 7 -> 'Top 7th' / 'Bottom 7th'. Never 'bot 7'."""
    try:
        n = int(inning)
    except (TypeError, ValueError):
        return ""
    return f"{'Top' if str(half).lower().startswith(('top', 'mid')) else 'Bottom'} {ordinal(n)}"


def delay_headline(detailed: str) -> str:
    """'Delayed: RAIN' -> 'Rain delay'; 'Postponed' -> 'Postponed'; 'Suspended: Rain' -> 'Suspended (rain)'."""
    d = str(detailed or "").strip()
    m = re.search(r"(?:[:(]\s*)([A-Za-z][A-Za-z ]*?)\)?\s*$", d)
    reason = (m.group(1).strip() if m else "")
    if re.fullmatch(r"(?i)start|game", reason):
        reason = ""
    low = d.lower()
    if "postpon" in low:
        return f"Postponed ({reason.lower()})" if reason else "Postponed"
    if "suspend" in low:
        return f"Suspended ({reason.lower()})" if reason else "Suspended"
    return f"{reason.capitalize()} delay" if reason else "Delay"


# ── status embed lines ──────────────────────────────────────────────────────
def scratch_line(name: str, role: str, team: str) -> str:
    return f"**{name}** ({role} pick) is out of tonight's {team} lineup."


def delay_line(away: str, home: str, detailed: str, picks_in_lineup: int | None) -> str:
    """picks_in_lineup=None means the lineup is not posted yet: say that, count no one."""
    head = delay_headline(detailed)
    if picks_in_lineup is None:
        tail = " Lineups are not posted yet."
    elif picks_in_lineup > 0:
        tail = f" {picks_in_lineup} of your picks {'plays' if picks_in_lineup == 1 else 'play'} here."
    else:
        tail = ""
    return f"**{head}: {away} at {home}.**{tail}"


def lineup_line(team: str, spots: str) -> str:
    return f"**{team}** — {spots}"


def final_line(score: str, pick_lines: list[str]) -> str:
    return f"**Final {score}** — " + (" · ".join(pick_lines) if pick_lines else "no lines for your picks")


# ── live pings ──────────────────────────────────────────────────────────────
def clean_change(description: str) -> str:
    """'Pitching Change: A replaces B.' -> 'A replaces B'."""
    d = str(description or "").strip().rstrip(".")
    return re.sub(r"(?i)^pitching change:\s*", "", d)


def pitching_change(pitching_team: str, half: str, inning, description: str, attackers: list[str]) -> str:
    where = inning_text(half, inning)
    who = ", ".join(attackers)
    return (f"🚪 **{pitching_team}** bullpen · {where}: {clean_change(description)}. "
            f"Your {'pick' if len(attackers) == 1 else 'picks'} on that side: {who}")


def pick_at_plate(name: str, role: str, runners: int, half: str, inning, away: str, home: str) -> str:
    spot = "the bases loaded" if runners == 3 else f"{runners} runners on"
    return f"🚨 **{name}** ({role} pick) is up with {spot} · {away} at {home}, {inning_text(half, inning)}"


# ── samples for the preview page (TEST data) ────────────────────────────────
def samples() -> list[dict]:
    return [
        {"key": "status.scratch_line", "trigger": "A designated pick is not in the posted batting order.", "producer": "bots/pen_door_watch.py gather_status_sections",
         "title": STATUS_TITLE, "body": scratch_line("Sample Delta", "HRR", "EXA")},
        {"key": "status.delay_line", "variant": "lineup posted", "trigger": "A game is delayed; counts only picks in the posted order.", "producer": "bots/pen_door_watch.py gather_status_sections",
         "title": STATUS_TITLE, "body": delay_line("MPL", "SAM", "Delayed: RAIN", 2)},
        {"key": "status.delay_line", "variant": "no lineup yet", "trigger": "Same, lineups not posted.", "producer": "bots/pen_door_watch.py gather_status_sections",
         "title": STATUS_TITLE, "body": delay_line("MPL", "SAM", "Delayed Start: Rain", None)},
        {"key": "status.delay_line", "variant": "postponed", "trigger": "Postponed.", "producer": "bots/pen_door_watch.py gather_status_sections",
         "title": STATUS_TITLE, "body": delay_line("NIL", "TST", "Postponed", 0)},
        {"key": "pitching_change", "trigger": "A pitching change; names only picks who are in the posted batting order.", "producer": "bots/pen_door_watch.py live_sweep",
         "title": "Pen door", "body": pitching_change("TST", "bottom", 7, "Pitching Change: Sample Reliever replaces Sample Starter.", ["Sample Alpha", "Sample Bravo"])},
        {"key": "pick_at_plate", "variant": "bases loaded", "trigger": "A designated pick is at the plate with 2+ on.", "producer": "bots/pen_door_watch.py live_sweep",
         "title": "Pick at the plate", "body": pick_at_plate("Sample Delta", "HRR", 3, "top", 8, "TST", "EXA")},
        {"key": "pick_at_plate", "variant": "two on", "trigger": "Same, two on.", "producer": "bots/pen_door_watch.py live_sweep",
         "title": "Pick at the plate", "body": pick_at_plate("Sample Delta", "HRR", 2, "top", 8, "TST", "EXA")},
        {"key": "status.lineup_line", "variant": "OFF", "trigger": "A lineup is posted (off by default).", "producer": "bots/pen_door_watch.py gather_status_sections",
         "title": STATUS_TITLE, "body": lineup_line("EXA", "Sample Echo 1st")},
        {"key": "status.final_line", "variant": "OFF", "trigger": "A final recap (off by default).", "producer": "bots/pen_door_watch.py gather_status_sections",
         "title": STATUS_TITLE, "body": final_line("SAM 2–5 MPL", ["💥 Sample Hotel 2-4 1HR"])},
    ]


if __name__ == "__main__":
    if "--samples" in sys.argv:
        print(json.dumps(samples(), ensure_ascii=False))
    else:
        print(__doc__)
