"""Publisher adapters — direct platform APIs, no third-party subscription.

You told the pipeline to default to manual (Copy Caption) but wire the APIs
if it isn't much extra work, since you already have both accounts made.
Buffer would add a recurring monthly cost past its free-tier caps for no
real benefit here — X's write API and Instagram's Graph API are both free
for a single account you own, so both adapters below talk to the platforms
directly. Nothing calls them unless SOCIAL_AUTO_PUBLISH=true AND the
platform's credentials are present; both are safe no-ops otherwise, and the
approval-required queue is the default path regardless (see night_recap.py
and docs/SOCIAL.md).

Interface every future adapter (Discord, TikTok, whatever comes next)
should match: takes (caption, image_bytes, image_url) and a config dict,
returns {"ok": bool, "platform_response_id": str|None, "error": str|None}.
Never raises — a publish failure is data, not an exception, so it can be
recorded in history and retried without taking down the caller (spec
section 18: publishing is downstream only, never crash a workflow).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any


def _result(ok: bool, response_id: str | None = None, error: str | None = None) -> dict[str, Any]:
    return {"ok": ok, "platform_response_id": response_id, "error": error}


def auto_publish_enabled() -> bool:
    return str(os.environ.get("SOCIAL_AUTO_PUBLISH", "false")).strip().lower() in ("1", "true", "yes")


# ── X / Twitter — direct API v2 (write) + v1.1 media upload ────────────────
#
# Posting as your own account uses OAuth 1.0a user-context signing (API
# key/secret + access token/secret from a free X developer app). No paid
# tier is required for text+image posts at DASH's volume. Hand-rolled HMAC
# signing here rather than adding requests-oauthlib as a new dependency —
# this repo already treats new pip installs as a cost worth avoiding
# (bots/requirements.txt vs the light streamlit requirements.txt split).

def _x_creds() -> dict[str, str] | None:
    keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    vals = {k: os.environ.get(k, "") for k in keys}
    if not all(vals.values()):
        return None
    return vals


def _oauth1_header(method: str, url: str, creds: dict[str, str],
                    extra_params: dict[str, str] | None = None) -> str:
    params = {
        "oauth_consumer_key": creds["X_API_KEY"],
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": creds["X_ACCESS_TOKEN"],
        "oauth_version": "1.0",
    }
    all_params = dict(params)
    all_params.update(extra_params or {})
    base = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
                     for k, v in sorted(all_params.items()))
    sig_base = "&".join([method.upper(), urllib.parse.quote(url, safe=""), urllib.parse.quote(base, safe="")])
    signing_key = f"{urllib.parse.quote(creds['X_API_SECRET'], safe='')}&{urllib.parse.quote(creds['X_ACCESS_TOKEN_SECRET'], safe='')}"
    sig = hmac.new(signing_key.encode(), sig_base.encode(), hashlib.sha1).digest()
    params["oauth_signature"] = base64.b64encode(sig).decode()
    return "OAuth " + ", ".join(f'{k}="{urllib.parse.quote(v, safe="")}"' for k, v in sorted(params.items()))


def publish_x(*, caption: str, image_bytes: bytes | None = None) -> dict[str, Any]:
    creds = _x_creds()
    if creds is None:
        return _result(False, error="X credentials not configured (X_API_KEY/X_API_SECRET/"
                                     "X_ACCESS_TOKEN/X_ACCESS_TOKEN_SECRET)")
    try:
        media_id = None
        if image_bytes:
            upload_url = "https://upload.twitter.com/1.1/media/upload.json"
            b64 = base64.b64encode(image_bytes).decode()
            body = urllib.parse.urlencode({"media_data": b64}).encode()
            header = _oauth1_header("POST", upload_url, creds)
            req = urllib.request.Request(upload_url, data=body,
                                          headers={"Authorization": header,
                                                   "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=30) as r:
                media_id = json.loads(r.read().decode()).get("media_id_string")

        tweet_url = "https://api.twitter.com/2/tweets"
        # oauth_body is signed as an empty param set — v2 takes a JSON body,
        # which OAuth 1.0a signing does not cover (only form-encoded params
        # are part of the signature base string).
        header = _oauth1_header("POST", tweet_url, creds)
        payload: dict[str, Any] = {"text": caption}
        if media_id:
            payload["media"] = {"media_ids": [media_id]}
        req = urllib.request.Request(tweet_url, data=json.dumps(payload).encode(),
                                      headers={"Authorization": header, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        tweet_id = (data.get("data") or {}).get("id")
        if tweet_id:
            return _result(True, response_id=tweet_id)
        return _result(False, error=f"unexpected response: {data}")
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return _result(False, error=f"X API {e.code}: {e.read().decode(errors='replace')[:400]}")
    except Exception as e:
        return _result(False, error=f"X publish failed: {e}")


# ── Instagram — direct Graph API (Business account + linked Facebook Page) ─
#
# Two calls: create a media container from a PUBLIC image URL, then publish
# the container. Needs an Instagram professional account linked to a
# Facebook Page and a Meta developer app's Page access token — see
# docs/SOCIAL.md for the one-time setup. No App Review is required for
# publishing to your OWN account under your own app in Development Mode.

def _ig_creds() -> dict[str, str] | None:
    token = os.environ.get("META_ACCESS_TOKEN", "")
    ig_id = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")
    if not token or not ig_id:
        return None
    return {"token": token, "ig_id": ig_id}


def _graph_post(path: str, params: dict[str, str]) -> dict[str, Any]:
    url = f"https://graph.facebook.com/v21.0/{path}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def publish_instagram(*, caption: str, image_url: str) -> dict[str, Any]:
    creds = _ig_creds()
    if creds is None:
        return _result(False, error="Instagram credentials not configured "
                                     "(META_ACCESS_TOKEN/INSTAGRAM_ACCOUNT_ID)")
    if not image_url:
        return _result(False, error="Instagram publishing requires a public image_url — "
                                     "generate the asset before publishing")
    try:
        created = _graph_post(f"{creds['ig_id']}/media", {
            "image_url": image_url, "caption": caption, "access_token": creds["token"],
        })
        creation_id = created.get("id")
        if not creation_id:
            return _result(False, error=f"container creation failed: {created}")
        published = _graph_post(f"{creds['ig_id']}/media_publish", {
            "creation_id": creation_id, "access_token": creds["token"],
        })
        media_id = published.get("id")
        if media_id:
            return _result(True, response_id=media_id)
        return _result(False, error=f"publish failed: {published}")
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return _result(False, error=f"Instagram API {e.code}: {e.read().decode(errors='replace')[:400]}")
    except Exception as e:
        return _result(False, error=f"Instagram publish failed: {e}")


# ── Discord — reuses the existing DISCORD_WEBHOOK convention (comma or
# newline separated, fire-and-forget) already used across bots/*.py. Not a
# "publish destination" in the spec's sense; used only to ping ops that a
# new post is waiting in the queue. ──────────────────────────────────────

def _discord_urls() -> list[str]:
    raw = os.environ.get("DISCORD_WEBHOOK", "")
    return [u.strip() for u in raw.replace(",", "\n").split() if u.strip().startswith("http")]


def notify_discord(msg: str) -> None:
    for url in _discord_urls():
        try:
            req = urllib.request.Request(
                url, data=json.dumps({"content": msg[:1900]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "moonshot-bot"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            print(f"  · discord notify failed: {exc}")
