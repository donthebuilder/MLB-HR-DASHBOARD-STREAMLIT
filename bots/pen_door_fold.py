"""PEN DOOR, FOLDED (2026-10-16, Donovan: fewer pings so nobody mutes on day one). Behind PEN_DOOR_FOLD_ON (default OFF).

With the switch on, the pen door stops posting scratch / delay / lineup / pitching-change as separate Discord
messages. Instead each game gets ONE "Game status" message that is EDITED in place (Discord webhook message edit:
POST ...?wait=true gives the message id, PATCH .../messages/<id> updates it). It exists only when something
affects a CALLED pick (a pick with a role) in that game. pick_at_plate and the night's finals go to a separate
webhook, PEN_DOOR_OWN_WEBHOOK (Donovan's own server); unset means they are not sent at all.

This file is pure helpers and the Discord edit calls (an `opener` can be injected, so tests send nothing).
bots/pen_door_watch.py decides WHEN; bots/copy_pendoor.py owns the words.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    import discord_post
except ImportError:
    from bots import discord_post

MAX_LINES = 8          # a game's status keeps its newest lines
KEEP_DAYS = 3          # game-status state older than this is dropped
EMBED_COLOR = 0xF97316


def fold_on() -> bool:
    return str(os.environ.get("PEN_DOOR_FOLD_ON", "")).strip().lower() in ("on", "1", "true", "yes")


def own_urls() -> list[str]:
    """Donovan's own server. Unset or empty = nothing is sent there (and nothing is fetched for it)."""
    return discord_post.webhook_urls(os.environ.get("PEN_DOOR_OWN_WEBHOOK", ""))


def hook_id(url: str) -> str:
    """A stable short id for a webhook, so the state file never stores the secret URL."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


# ── classification (pure) ───────────────────────────────────────────────────
def called_only(picks: list[dict]) -> list[dict]:
    """A CALLED pick is one with a role (the same test every pen-door alert has always used)."""
    return [p for p in picks if p.get("role")]


def classify_substitution(det: dict, ev: dict, called: list[dict]) -> dict | None:
    """An in-game lineup change that touches a CALLED pick, or None.

    det/ev = a playEvent's details and the event itself. Uses the player ids when the feed carries them
    (player = the man coming in, replacedPlayer = the man going out); otherwise the description
    ("Offensive Substitution: Pinch-hitter A replaces B.") is split at 'replaces'. A pick is only ever
    matched by his full name, never a guess. Returns {kind, label, incoming, outgoing, pick, pick_out}.
    """
    et = str(det.get("eventType") or "")
    if et not in ("offensive_substitution", "defensive_substitution"):
        return None
    desc = str(det.get("description") or "")
    low = desc.lower()
    by_id = {int(p["pid"]): p for p in called if p.get("pid")}
    inc = (ev.get("player") or {}).get("id")
    out = (ev.get("replacedPlayer") or {}).get("id")
    in_pick = by_id.get(int(inc)) if inc else None
    out_pick = by_id.get(int(out)) if out else None
    inc_name = str((ev.get("player") or {}).get("fullName") or "")
    out_name = str((ev.get("replacedPlayer") or {}).get("fullName") or "")
    if not (in_pick or out_pick) and " replaces " in low:
        cut = low.index(" replaces ")
        before, after = low[:cut], low[cut + len(" replaces "):]
        for p in called:
            nm = str(p.get("name") or "").lower()
            if not nm:
                continue
            if nm in after and not out_pick:
                out_pick = p
            elif nm in before and not in_pick:
                in_pick = p
    if not (in_pick or out_pick):
        return None
    if "pinch-hitter" in low or "pinch hitter" in low:
        label = "pinch-hits for"
    elif "pinch-runner" in low or "pinch runner" in low:
        label = "pinch-runs for"
    else:
        label = "replaces"
    if not inc_name or not out_name:
        body = desc.split(":", 1)[-1].strip().rstrip(".")
        m = body.lower().find(" replaces ")
        if m > 0:
            a, b = body[:m], body[m + len(" replaces "):]
            inc_name = inc_name or a.replace("Pinch-hitter ", "").replace("Pinch-runner ", "").strip()
            out_name = out_name or b.split(",")[0].strip()
    return {"kind": "pinch" if label != "replaces" else "defensive", "label": label,
            "incoming": (in_pick or {}).get("name") or inc_name or "?",
            "outgoing": (out_pick or {}).get("name") or out_name or "?",
            "pick": in_pick or out_pick, "pick_out": out_pick is not None}


def pitcher_stats_text(stat: dict | None) -> str:
    """'3.45 ERA · 60.1 IP · 70 K · 20 BB' from a statsapi season pitching stat dict; only fields present. '' when none."""
    s = stat or {}
    parts = []
    if s.get("era") not in (None, "", "-.--"):
        parts.append(f"{s['era']} ERA")
    if s.get("inningsPitched") not in (None, ""):
        parts.append(f"{s['inningsPitched']} IP")
    if s.get("strikeOuts") is not None:
        parts.append(f"{s['strikeOuts']} K")
    if s.get("baseOnBalls") is not None:
        parts.append(f"{s['baseOnBalls']} BB")
    return " · ".join(parts)


# ── state: one message per game, edited in place ────────────────────────────
def prune_state(st: dict, today: str) -> None:
    gs = st.get("gs") or {}
    for pk in list(gs):
        if str((gs[pk] or {}).get("day") or "") < _shift(today, -KEEP_DAYS):
            gs.pop(pk, None)


def _shift(day: str, n: int) -> str:
    import datetime as dt
    return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()


def add_lines(st: dict, pk, day: str, new_lines: list[str]) -> dict:
    rec = st.setdefault("gs", {}).setdefault(str(pk), {"day": day, "lines": [], "ids": {}})
    for ln in new_lines:
        if ln and ln not in rec["lines"]:
            rec["lines"].append(ln)
    rec["lines"] = rec["lines"][-MAX_LINES:]
    return rec


def game_status_payload(title: str, lines: list[str], footer: str) -> dict:
    embed = {"title": title, "description": "\n".join(f"· {ln}" for ln in lines)[:4000], "color": EMBED_COLOR}
    if footer:
        embed["footer"] = {"text": footer}
    return {"embeds": [embed]}


# ── Discord: post-with-id and edit ──────────────────────────────────────────
def _call(method: str, url: str, payload: dict, opener=None, sleep=None, max_retries: int = 2) -> dict:
    opener = opener or urllib.request.urlopen
    sleep = sleep or time.sleep
    body = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"Content-Type": "application/json", "User-Agent": discord_post.USER_AGENT})
        try:
            resp = opener(req, timeout=15)
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                attempt += 1
                sleep(discord_post._retry_after(e))
                continue
            raise


def _msg_url(url: str, msg_id: str) -> str:
    return f"{url.split('?')[0].rstrip('/')}/messages/{msg_id}"


def upsert_message(url: str, payload: dict, msg_id: str | None, opener=None, sleep=None) -> str:
    """Edit message `msg_id` on this webhook, or create one (?wait=true returns its id). A message that was deleted
    (404 on edit) is created again. Returns the live message id; raises when nothing could be sent."""
    if msg_id:
        try:
            _call("PATCH", _msg_url(url, msg_id), payload, opener, sleep)
            return msg_id
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    made = _call("POST", url.split("?")[0] + "?wait=true", payload, opener, sleep)
    return str(made.get("id") or "")


def publish_game_status(st: dict, pk, day: str, title: str, new_lines: list[str], footer: str, urls: list[str],
                        opener=None, sleep=None) -> tuple[int, int]:
    """Add the lines to the game's one status message and send it (edit if it exists). Returns (delivered, failed)."""
    rec = add_lines(st, pk, day, new_lines)
    payload = game_status_payload(title, rec["lines"], footer)
    ok = bad = 0
    for url in urls:
        hid = hook_id(url)
        try:
            rec["ids"][hid] = upsert_message(url, payload, rec["ids"].get(hid), opener, sleep)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"game status {pk} FAILED on a hook: {exc}", file=sys.stderr)
            bad += 1
    return ok, bad


def send_plain(urls: list[str], payload: dict, opener=None, sleep=None) -> tuple[int, int]:
    """One new message per hook (own webhook posts are never edited). Returns (delivered, failed)."""
    ok = bad = 0
    for url in urls:
        try:
            _call("POST", url.split("?")[0], payload, opener, sleep)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"own webhook post FAILED: {exc}", file=sys.stderr)
            bad += 1
    return ok, bad
