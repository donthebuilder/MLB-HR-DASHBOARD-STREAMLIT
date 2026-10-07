"""Shared Discord webhook helpers for the standalone bots.

Every post goes through send(): it sets a User-Agent (Discord rejects urllib's
default one with HTTP 403), waits out a 429 (Retry-After) a bounded number of
times, and reports failure instead of swallowing it. The webhook secret may
hold several URLs separated by commas, whitespace or newlines. Dependency-free
on purpose (staleness_watch/autopsy must not import the tracker's reportlab
stack).

Ops alerts (staleness, a blocked slate publish, a held pick lock, a failed
check) are for the people running the site, not the fan room: ops_secret()
returns DISCORD_OPS_WEBHOOK when it is set. It falls back to DISCORD_WEBHOOK
only so an alert is never lost while no ops channel exists.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

USER_AGENT = "moonshot-bot"
MAX_RETRIES = 3          # extra tries after the first, only ever for a 429
MAX_WAIT_SECONDS = 30.0  # never sleep longer than this for one Retry-After
OPS_ENV = "DISCORD_OPS_WEBHOOK"
FAN_ENV = "DISCORD_WEBHOOK"


def webhook_urls(raw: str) -> list[str]:
    return [u.strip() for u in (raw or "").replace(",", "\n").split() if u.strip().startswith("http")]


def ops_secret() -> str:
    """The webhook secret for operational alerts: the ops channel when one is
    configured, else the shared room (so the alert still lands somewhere)."""
    ops = os.environ.get(OPS_ENV, "")
    if webhook_urls(ops):
        return ops
    return os.environ.get(FAN_ENV, "")


def ops_channel_is_set() -> bool:
    return bool(webhook_urls(os.environ.get(OPS_ENV, "")))


def _retry_after(err: urllib.error.HTTPError) -> float:
    """Seconds Discord asks us to wait: the Retry-After header, else the
    retry_after field of the JSON body, else 1s. Clamped to MAX_WAIT_SECONDS."""
    wait = None
    try:
        hdr = err.headers.get("Retry-After") if err.headers else None
        if hdr is not None:
            wait = float(hdr)
    except Exception:  # noqa: BLE001
        wait = None
    if wait is None:
        try:
            wait = float(json.loads(err.read().decode("utf-8")).get("retry_after"))
        except Exception:  # noqa: BLE001
            wait = None
    if wait is None or wait < 0:
        wait = 1.0
    return min(wait, MAX_WAIT_SECONDS)


def send(url: str, body: bytes, content_type: str = "application/json",
         timeout: int = 15, max_retries: int = MAX_RETRIES,
         opener=None, sleep=None) -> int:
    """POST one body to one webhook. Returns the HTTP status on success.
    Raises on failure. A 429 is retried after the wait Discord asked for, at
    most max_retries times, then raised. Any other error is raised at once."""
    opener = opener or urllib.request.urlopen
    sleep = sleep or time.sleep
    attempt = 0
    while True:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": content_type, "User-Agent": USER_AGENT})
        try:
            resp = opener(req, timeout=timeout)
            try:
                resp.read()
            except Exception:  # noqa: BLE001
                pass
            return int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                wait = _retry_after(e)
                attempt += 1
                print(f"  ! discord 429, waiting {wait:g}s (retry {attempt}/{max_retries})", file=sys.stderr)
                sleep(wait)
                continue
            raise


def post(raw_secret: str, payload: dict, timeout: int = 15,
         opener=None, sleep=None) -> tuple[int, int]:
    """POST payload to every URL in the secret. Returns (delivered, failed).
    A failure on one URL never blocks the rest, and is printed to stderr."""
    body = json.dumps(payload).encode("utf-8")
    ok = bad = 0
    for url in webhook_urls(raw_secret):
        try:
            send(url, body, timeout=timeout, opener=opener, sleep=sleep)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! discord post failed: {e}", file=sys.stderr)
            bad += 1
    return ok, bad


def post_ops(payload: dict, timeout: int = 15, opener=None, sleep=None) -> tuple[int, int, bool]:
    """Post an ops alert. Returns (delivered, failed, used_ops_channel)."""
    used_ops = ops_channel_is_set()
    ok, bad = post(ops_secret(), payload, timeout=timeout, opener=opener, sleep=sleep)
    return ok, bad, used_ops
