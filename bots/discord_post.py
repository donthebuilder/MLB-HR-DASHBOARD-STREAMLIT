"""Shared Discord webhook helpers for the small standalone bots.

Discord rejects urllib's default User-Agent (HTTP 403), so every post sets one
(live_results_tracker.py does the same); the webhook secret may hold several
URLs separated by commas, whitespace or newlines. Dependency-free on purpose
(staleness_watch/autopsy must not import the tracker's reportlab stack).
"""
from __future__ import annotations

import json
import sys
import urllib.request

USER_AGENT = "moonshot-bot"


def webhook_urls(raw: str) -> list[str]:
    return [u.strip() for u in (raw or "").replace(",", "\n").split() if u.strip().startswith("http")]


def post(raw_secret: str, payload: dict, timeout: int = 15) -> tuple[int, int]:
    """POST payload to every URL in the secret. Returns (delivered, failed)."""
    body = json.dumps(payload).encode("utf-8")
    ok = bad = 0
    for url in webhook_urls(raw_secret):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
            urllib.request.urlopen(req, timeout=timeout).read()
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! discord post failed: {e}", file=sys.stderr)
            bad += 1
    return ok, bad
