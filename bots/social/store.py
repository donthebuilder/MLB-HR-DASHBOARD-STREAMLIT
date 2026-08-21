"""Where social posts live: the queue, the fingerprint index, and history.

Same pattern as bots/pick_lock.py's ledger, because it already solves the
problem this package also has — a GitHub Actions runner checks out `main`
fresh every run, and the only place last run's state survives is the `data`
branch, fetched back over HTTPS. Three files, two tiers:

  social/queue.json           Compact, evolving. Every post currently in
                               draft/pending_review/approved/scheduled, plus
                               the last N decided ones so the UI can show
                               recent history without a second fetch.
  social/fingerprints.json    Compact. The set of every (post, platform)
                               fingerprint ever published or rejected — the
                               O(1) duplicate-protection check. Never shrinks.
  social/history/social_history_<date>.jsonl
                               Durable, append-only, one line per lifecycle
                               event (queued/approved/published/failed/
                               rejected). This is the audit trail spec
                               section 11 asks for; it is never edited after
                               being written, only appended to, and old
                               dates are trimmed by publish_data.sh the same
                               way graded_results_*.json is (kept ~180 days,
                               not deleted on a whim).

Local files here are staged the same way every other bot writes: under
public/data/current/, picked up by .github/scripts/publish_data.sh, and
carried forward across runs that don't touch them.
"""

from __future__ import annotations

import json
import os
import urllib.request
import datetime as dt
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent  # bots/social -> bots -> repo root
PUBLIC = REPO_ROOT / "public" / "data"
CURRENT = PUBLIC / "current"
SOCIAL_DIR = CURRENT / "social"
HISTORY_DIR = SOCIAL_DIR / "history"
ASSETS_DIR = SOCIAL_DIR / "assets"

RAW = ("https://raw.githubusercontent.com/donthebuilder/"
       "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")

QUEUE_KEEP = 300           # posts kept in the compact queue file
HISTORY_FILES_KEEP = 180   # per-date jsonl files kept (~6 months)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _fetch_json(rel: str, default: Any) -> Any:
    """Local-first, then the data branch, matching pick_lock.fetch_lock()."""
    local = CURRENT / rel
    if local.exists():
        try:
            return json.loads(local.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  · local {rel} unreadable ({e}) — falling back to the branch")
    url = f"{RAW}/{rel}?t={int(_now_utc().timestamp())}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  · no previous {rel} fetched ({e}) — starting fresh")
        return default


def load_queue() -> list[dict[str, Any]]:
    payload = _fetch_json("social/queue.json", {"posts": []})
    posts = payload.get("posts") if isinstance(payload, dict) else None
    return posts if isinstance(posts, list) else []


def save_queue(posts: list[dict[str, Any]]) -> None:
    """Newest first, capped. Decided posts (published/rejected/failed) age
    out of the compact file over time — the durable record for those lives
    in history/, never in this file."""
    SOCIAL_DIR.mkdir(parents=True, exist_ok=True)
    posts = sorted(posts, key=lambda p: p.get("created_at") or "", reverse=True)[:QUEUE_KEEP]
    payload = {"updated_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "posts": posts}
    (SOCIAL_DIR / "queue.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_fingerprints() -> set[str]:
    payload = _fetch_json("social/fingerprints.json", {"fingerprints": []})
    fps = payload.get("fingerprints") if isinstance(payload, dict) else None
    return set(fps) if isinstance(fps, list) else set()


def save_fingerprints(fps: set[str]) -> None:
    SOCIAL_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
               "fingerprints": sorted(fps)}
    (SOCIAL_DIR / "fingerprints.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def append_history_event(*, post: dict[str, Any], platform: str, event: str,
                          fingerprint: str, caption: str | None = None,
                          asset: str | None = None,
                          platform_response_id: str | None = None,
                          error: str | None = None) -> None:
    """One durable audit-trail line. `event` is one of the queue statuses
    (pending_review/approved/published/rejected/failed) plus 'queued' for
    the very first write. Filed under TODAY's date (when the event happens),
    not the post's content date — a recap for last night approved this
    morning belongs in this morning's audit file."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    date_str = _now_utc().strftime("%Y-%m-%d")
    row = {
        "post_id": post.get("id"),
        "fingerprint": fingerprint,
        "platform": platform,
        "brand": post.get("brand"),
        "product": post.get("product"),
        "sport": post.get("sport"),
        "content_type": post.get("content_type"),
        "event": event,
        "caption": caption,
        "asset": asset,
        "platform_response_id": platform_response_id,
        "error": error,
        "at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = HISTORY_DIR / f"social_history_{date_str}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def asset_url(rel: str) -> str:
    """Turn a path relative to public/data/current/ (what assets.py returns)
    into the public https:// URL it will resolve to once published — the
    same raw.githubusercontent base every reader in this repo already uses.
    Needed by the Instagram adapter, which requires a publicly-fetchable
    image URL rather than a local file."""
    return f"{RAW}/{rel.lstrip('/')}"


def upsert_post(posts: list[dict[str, Any]], post: dict[str, Any]) -> list[dict[str, Any]]:
    """Replace by id if present, else prepend. Small lists (QUEUE_KEEP=300),
    so a linear scan is fine and keeps this dependency-free."""
    out = [p for p in posts if p.get("id") != post.get("id")]
    out.insert(0, post)
    return out
