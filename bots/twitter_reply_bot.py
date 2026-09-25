"""@CalledItHR mention auto-reply bot.

Someone @-mentions @CalledItHR with a player's name -> reply once with that
player's real board line from tonight's published data. Nothing else.

    source    X mentions timeline (GET /2/users/:id/mentions, since_id)
    data      today_slim.json (MOONSHOT) / nfl_week.json (TUDDY) off the data branch
    state     bots/twitter_replies.json (kept on the `reply-state` branch by the workflow)
    output    one reply per mention (POST /2/tweets)

Rules (each one is a skip, never a guess):
  * exactly one player named in the mention, and he is on tonight's board
  * his game is inside the window (MLB: started <4h ago or starts within 16h;
    NFL: not final, kickoff within the next 72h or <4h ago)
  * every number in the reply is a field of the published file -- a template
    whose field is missing is not used
  * never reply to ourselves, never reply to a reply to one of our replies
  * max 5 replies an hour, 30 a day, 2 per author a day
  * no links (a reply with a link costs ~13x more on pay-per-use)
  * no retries: a refused post is logged and the run stops

Posts only when REPLY_BOT_LIVE=1. Otherwise it logs what it would have said.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
STATE_PATH = Path(os.environ.get("REPLY_STATE_PATH", HERE / "twitter_replies.json"))
NICK_PATH = HERE / "data" / "nicknames.json"
DATA_BASE = os.environ.get(
    "REPLY_DATA_BASE",
    "https://raw.githubusercontent.com/donthebuilder/MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current",
)
API = "https://api.x.com/2"

LIVE = os.environ.get("REPLY_BOT_LIVE") == "1"
MAX_PER_HOUR = 5
MAX_PER_DAY = 30
MAX_PER_AUTHOR_DAY = 2
MAX_MENTION_AGE = timedelta(hours=3)
KEEP_LOG = timedelta(days=14)
MLB_WINDOW = (timedelta(hours=-4), timedelta(hours=16))
NFL_WINDOW = (timedelta(hours=-4), timedelta(hours=72))

CALL_ROLES = {"TOP", "HR", "HIT", "HRR"}  # lib/callStatus.js: CALLED = TOP/HR/HIT/HRR
ROLE_WORD = {"TOP": "TOP pick", "HR": "HR call", "HIT": "hit call", "HRR": "H+R+RBI call"}
# Last names that are also everyday words -- never matched on their own.
STOP_LAST = {"will", "may", "bell", "hunter", "young", "king", "story", "wood",
             "brown", "white", "green", "black", "love", "hill", "hall", "cook", "price",
             "rice", "house", "sale", "pages", "gray", "grey", "hope", "moore", "ford",
             "walker", "lamb", "chase", "allen", "bowers", "rogers", "long", "rich"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if re.search(r"T\d\d:\d\d\+", s):  # nfl kickoff '2026-09-25T00:15Z' -> no seconds
        s = s.replace("+", ":00+", 1)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def log(msg: str) -> None:
    print(f"[reply-bot] {msg}", flush=True)


# --------------------------------------------------------------------------- X API (OAuth 1.0a user context)

def _oauth_header(method: str, url: str, params: dict, nonce: str | None = None, stamp: str | None = None) -> str:
    ck, cs = os.environ["TWITTER_API_KEY"], os.environ["TWITTER_API_SECRET"]
    tk, ts = os.environ["TWITTER_ACCESS_TOKEN"], os.environ["TWITTER_ACCESS_SECRET"]
    q = lambda v: urllib.parse.quote(str(v), safe="~")
    oauth = {
        "oauth_consumer_key": ck,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": stamp or str(int(time.time())),
        "oauth_token": tk,
        "oauth_version": "1.0",
    }
    allp = {**params, **oauth}
    base_params = "&".join(f"{k}={v}" for k, v in sorted((q(k), q(v)) for k, v in allp.items()))
    base = "&".join([method.upper(), q(url), q(base_params)])
    sig = base64.b64encode(hmac.new(f"{q(cs)}&{q(ts)}".encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{q(k)}="{q(v)}"' for k, v in sorted(oauth.items()))


def x_get(path: str, params: dict | None = None) -> dict:
    url = f"{API}{path}"
    params = params or {}
    r = requests.get(url, params=params, headers={"Authorization": _oauth_header("GET", url, params)}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"GET {path} -> {r.status_code} {r.text[:300]}")
    return r.json()


def x_reply(text: str, to_id: str) -> str:
    url = f"{API}/tweets"
    body = {"text": text, "reply": {"in_reply_to_tweet_id": to_id}}
    r = requests.post(url, json=body, headers={"Authorization": _oauth_header("POST", url, {})}, timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"POST /tweets -> {r.status_code} {r.text[:300]}")
    return r.json()["data"]["id"]


# --------------------------------------------------------------------------- state

def load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("mentions", [])
    return s


def save_state(s: dict) -> None:
    cutoff = now_utc() - KEEP_LOG
    s["mentions"] = [m for m in s["mentions"] if (parse_ts(m.get("timestamp")) or now_utc()) >= cutoff]
    STATE_PATH.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- names

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("-", " ").replace("’", "'")
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_suffix(n: str) -> str:
    return re.sub(r" (jr|sr|ii|iii|iv)$", "", n)


def clean_mention_text(t: str) -> str:
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[@#$]\w+", " ", t)
    return norm(t)


def build_index(names_by_sport: dict[str, set[str]], nicks: dict) -> dict[str, set[tuple[str, str]]]:
    """phrase -> {(sport, full name)}. A phrase that points at two men is dropped at match time."""
    idx: dict[str, set[tuple[str, str]]] = {}
    add = lambda k, v: idx.setdefault(k, set()).add(v)
    last_count: dict[str, int] = {}
    for sport, names in names_by_sport.items():
        for n in names:
            last = strip_suffix(norm(n)).split(" ")[-1]
            last_count[last] = last_count.get(last, 0) + 1
    for sport, names in names_by_sport.items():
        for n in names:
            full = norm(n)
            add(full, (sport, n))
            add(strip_suffix(full), (sport, n))
            last = strip_suffix(full).split(" ")[-1]
            if len(last) >= 4 and last not in STOP_LAST and last_count[last] == 1:
                add(last, (sport, n))
        for nick, target in (nicks.get(sport) or {}).items():
            if target in names:
                add(norm(nick), (sport, target))
    return idx


def find_player(text: str, idx: dict, names_by_sport: dict[str, set[str]]) -> tuple[str, str] | None:
    t = f" {clean_mention_text(text)} "
    hits: set[tuple[str, str]] = set()
    for phrase in sorted(idx, key=len, reverse=True):
        if f" {phrase} " in t:
            hits |= idx[phrase]
            t = t.replace(f" {phrase} ", " | ")
    if not hits:  # typos: two-word window vs full names only
        words = t.split()
        best, best_r = None, 0.0
        for i in range(len(words) - 1):
            w = f"{words[i]} {words[i + 1]}"
            for sport, names in names_by_sport.items():
                for n in names:
                    r = SequenceMatcher(None, w, strip_suffix(norm(n))).ratio()
                    if r > best_r:
                        best, best_r = (sport, n), r
        if best and best_r >= 0.88:
            hits = {best}
    return next(iter(hits)) if len(hits) == 1 else None


# --------------------------------------------------------------------------- data

def fetch(name: str):
    r = requests.get(f"{DATA_BASE}/{name}", timeout=60)
    r.raise_for_status()
    return r.json()


def mlb_row(rows: list, name: str, now: datetime) -> dict | None:
    lo, hi = MLB_WINDOW
    cands = [r for r in rows if r.get("name") == name and (gt := parse_ts(r.get("game_time")))
             and now + lo <= gt <= now + hi]
    return min(cands, key=lambda r: parse_ts(r["game_time"])) if cands else None


def nfl_row(week: dict, name: str, now: datetime) -> tuple[dict, int, int] | None:
    pool = [p for p in week.get("players", []) if (p.get("scores") or {}).get("TD") is not None and not p.get("on_bye")]
    pool.sort(key=lambda p: -p["scores"]["TD"])
    for rank, p in enumerate(pool, 1):
        if p.get("name") != name:
            continue
        g = next((g for g in week.get("games", []) if p.get("team") in (g.get("home"), g.get("away"))), None)
        ko = parse_ts(g.get("kickoff")) if g else None
        lo, hi = NFL_WINDOW
        if not g or g.get("completed") or not ko or not (now + lo <= ko <= now + hi):
            return None
        return p, rank, len(pool)
    return None


# --------------------------------------------------------------------------- replies

def fmt(x, nd=0):
    return f"{x:.{nd}f}" if isinstance(x, (int, float)) else None


def mlb_reply(r: dict, n_board: int, pick: int) -> str | None:
    roles = [x for x in (r.get("game_pick_role") or "").split("/") if x]
    calls = [ROLE_WORD[x] for x in roles if x in CALL_ROLES]
    head = "🤖 CALLED IT" if calls else "🤖 MOONSHOT"
    status = f"Tonight's call: {', '.join(calls)}" if calls else "On the board. Not a call tonight."
    name = r["name"].upper()
    rank, hr, season = r.get("board_rank"), fmt(r.get("hr_score")), r.get("season_hr")
    p, thr = r.get("pitcher_name"), r.get("pitcher_throws")
    proj = " (projected)" if r.get("pitcher_projected") else ""
    bats = r.get("bats")
    side = {"L": "L", "R": "R"}.get(bats) or ({"R": "L", "L": "R"}.get(thr) if bats == "S" else None)
    hr9 = r.get(f"pitcher_hr9_vs_{side.lower()}hb") if side else None
    pf = r.get("park_hr_factor")
    if rank is None or hr is None:
        return None
    t = []
    t.append([f"#{rank} of {n_board} on the Moonshot board", f"HR Score {hr}"]
             + ([f"{season} HR this season"] if season is not None else []))
    if p and thr:
        t.append([f"vs {p}, {thr}HP{proj}"] + ([f"Park HR factor {pf:.2f}"] if isinstance(pf, (int, float)) else [])
                 + [f"HR Score {hr} · #{rank} on the board"])
    if p and isinstance(hr9, (int, float)) and side:
        t.append([f"{p}{proj}: {hr9:.2f} HR/9 vs {side}HB", f"HR Score {hr} · #{rank} on the board"])
    if season is not None:
        t.append([f"#{rank} on the board · HR Score {hr}", f"{season} HR this season"]
                 + ([f"Tonight: {p}{proj}"] if p else []))
    lines = t[pick % len(t)]
    return "\n".join([head, "", name, status, *lines])


def nfl_reply(p: dict, rank: int, n_board: int, pick: int) -> str | None:
    td = fmt(p["scores"]["TD"])
    name, pos, team, opp = p["name"].upper(), p.get("position"), p.get("team"), p.get("opp")
    since, season = p.get("games_since_last_td"), p.get("season_td")
    extra = []
    if p.get("high_confidence_td_flag"):
        extra.append("A+ TD grade")
    if p.get("questionable"):
        extra.append("Listed questionable")
    t = [[f"#{rank} of {n_board} on the Tuddy TD board", f"TD Score {td}"]]
    if team and opp:
        t.append([f"{pos} · {team} vs {opp}" if pos else f"{team} vs {opp}", f"TD Score {td} · #{rank} on the board"])
    if isinstance(season, int) and isinstance(since, int):
        last = "scored last game" if since == 0 else f"last TD {since} game{'s' if since != 1 else ''} ago"
        t.append([f"{season} TD this season · {last}", f"TD Score {td} · #{rank} on the board"])
    lines = t[pick % len(t)]
    return "\n".join(["🤖 TUDDY", "", name, *lines, *extra])


# --------------------------------------------------------------------------- main

def main() -> int:
    for k in ("TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET"):
        if not os.environ.get(k):
            log(f"missing {k} -- nothing to do")
            return 0
    now = now_utc()
    state = load_state()
    log_rows = state["mentions"]
    seen = {m["mention_id"] for m in log_rows}
    our_replies = {m["reply_id"] for m in log_rows if m.get("reply_id")}

    if not state.get("self_id"):
        state["self_id"] = x_get("/users/me")["data"]["id"]
    me = state["self_id"]

    params = {"max_results": "100",
              "tweet.fields": "author_id,created_at,referenced_tweets,in_reply_to_user_id"}
    if state.get("since_id"):
        params["since_id"] = state["since_id"]
    else:  # first run: never answer a backlog
        params["start_time"] = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        mentions = x_get(f"/users/{me}/mentions", params).get("data", [])
    except RuntimeError as e:
        log(f"mentions read failed, stopping: {e}")
        save_state(state)
        return 1
    if not mentions:
        log("no new mentions")
        save_state(state)
        return 0
    mentions.sort(key=lambda m: int(m["id"]))  # oldest first; since_id only moves past what we handled
    log(f"{len(mentions)} new mention(s)  live={LIVE}")

    nicks = json.loads(NICK_PATH.read_text())
    slate, week = fetch("today_slim.json"), fetch("nfl_week.json")
    names = {"mlb": {r["name"] for r in slate if r.get("name")},
             "nfl": {p["name"] for p in week.get("players", []) if (p.get("scores") or {}).get("TD") is not None}}
    idx = build_index(names, nicks)
    n_mlb = max((r.get("board_rank") or 0 for r in slate), default=0)

    def record(m, status, reason="", sport=None, player=None, reply_id=None, text=None):
        log_rows.append({"mention_id": m["id"], "author_id": m.get("author_id"), "player_name": player,
                         "sport": sport, "status": status, "reason": reason, "reply_id": reply_id,
                         "text": text, "timestamp": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")})
        state["since_id"] = m["id"]
        log(f"{m['id']} {status} {player or ''} {reason}".rstrip())

    sent = lambda since: sum(1 for x in log_rows if x["status"] in ("sent", "dry_run")
                             and (parse_ts(x["timestamp"]) or now) >= since)
    for m in mentions:
        if m["id"] in seen:
            state["since_id"] = m["id"]
            continue
        created = parse_ts(m.get("created_at")) or now
        parent = next((r["id"] for r in m.get("referenced_tweets", []) if r["type"] == "replied_to"), None)
        if m.get("author_id") == me:
            record(m, "skipped", "own post"); continue
        if parent and parent in our_replies:
            record(m, "skipped", "reply to our reply"); continue
        if now - created > MAX_MENTION_AGE:
            record(m, "skipped", "too old"); continue
        if sent(now - timedelta(hours=1)) >= MAX_PER_HOUR or sent(now - timedelta(days=1)) >= MAX_PER_DAY:
            log("throttle reached -- the rest wait for the next run"); break
        by_author = sum(1 for x in log_rows if x.get("author_id") == m.get("author_id")
                        and x["status"] in ("sent", "dry_run") and (parse_ts(x["timestamp"]) or now) >= now - timedelta(days=1))
        if by_author >= MAX_PER_AUTHOR_DAY:
            record(m, "skipped", "author cap"); continue

        hit = find_player(m.get("text", ""), idx, names)
        if not hit:
            record(m, "skipped", "no single player on tonight's boards"); continue
        sport, player = hit
        pick = int(m["id"]) % 997
        if sport == "mlb":
            row = mlb_row(slate, player, now)
            text = mlb_reply(row, n_mlb, pick) if row else None
        else:
            got = nfl_row(week, player, now)
            text = nfl_reply(*got, pick) if got else None
        if not text:
            record(m, "skipped", "game outside the window or no score", sport.upper(), player); continue

        if not LIVE:
            record(m, "dry_run", "", sport.upper(), player, None, text)
            print(text + "\n")
            continue
        try:
            rid = x_reply(text, m["id"])
        except RuntimeError as e:
            record(m, "error", str(e)[:200], sport.upper(), player, None, text)
            break  # no retries, no hammering a refusing API
        our_replies.add(rid)
        record(m, "sent", "", sport.upper(), player, rid, text)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
