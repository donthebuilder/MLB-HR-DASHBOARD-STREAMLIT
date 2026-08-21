"""The one social-content object every DASH product shares.

Every post that reaches the queue — a Moonshot night recap today, a Tuddy
week recap tomorrow — is this same shape. Nothing here is MLB-specific;
sport-specific numbers live inside `data`, a plain dict of whatever that
content type actually needs. `data` must be built ONLY from fields that
already exist on a published DASH data file — see night_recap.py for the
pattern. Claude never sees more than this object and is never allowed to
add a key to `data` that wasn't already there (see captions.py).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

STATUSES = (
    "draft",
    "pending_review",
    "approved",
    "scheduled",
    "published",
    "rejected",
    "failed",
)

CONTENT_TYPES = (
    "daily_board",
    "player_spotlight",
    "watchlist",
    "live_result",
    "pick_cashed",
    "night_recap",
    "trend",
)

PLATFORMS = ("x", "instagram", "discord")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_post(
    *,
    id: str,
    brand: str,
    product: str,
    sport: str,
    content_type: str,
    data: dict[str, Any],
    priority: str = "normal",
    fingerprints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build one social-content object, status defaulted to pending_review —
    the spec is explicit that generated content must never default to
    auto-posting (see bots/social/README section in docs/SOCIAL.md)."""
    if content_type not in CONTENT_TYPES:
        raise ValueError(f"unknown content_type {content_type!r}")
    return {
        "id": id,
        "created_at": now_iso(),
        "brand": brand,
        "product": product,
        "sport": sport,
        "content_type": content_type,
        "status": "pending_review",
        "priority": priority,
        "data": data,
        "assets": {"square": None, "story": None, "landscape": None},
        "captions": {"x": None, "instagram": None, "story": None},
        "headline": None,
        "recommended_platforms": [],
        "publish": {"x": False, "instagram": False},
        # one fingerprint per destination platform — see fingerprint.py.
        # {"x": "MLB|Moonshot|2026-08-21|NIGHT_RECAP|RECAP|X", "instagram": "..."}
        "fingerprints": fingerprints or {},
        "approved_at": None,
        "published_at": None,
        "error": None,
        "status_history": [{"status": "pending_review", "at": now_iso()}],
    }


def set_status(post: dict[str, Any], status: str, *, note: str | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    post = dict(post)
    post["status"] = status
    at = now_iso()
    if status == "approved":
        post["approved_at"] = at
    if status == "published":
        post["published_at"] = at
    hist = list(post.get("status_history") or [])
    entry: dict[str, Any] = {"status": status, "at": at}
    if note:
        entry["note"] = note
    hist.append(entry)
    post["status_history"] = hist
    return post
