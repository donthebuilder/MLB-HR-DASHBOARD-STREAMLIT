#!/usr/bin/env python3
"""🌙 Social Queue — approve, edit, reject, or publish a DASH social draft.

Lives as a Streamlit multipage file (the `pages/` directory convention) so
it ships as a second page in the sidebar of the SAME app you already have
deployed, with zero changes to streamlit_app.py.

Reads (public, no auth needed) go straight to raw.githubusercontent.com,
same as the rest of this app. Writes (Approve/Reject/Edit Caption) go
through the GitHub Contents API, because the `data` branch has no other
write path from a Streamlit Cloud instance — see docs/SOCIAL.md for the
one-time secret you need to add: a GitHub token with `contents: write` on
this repo, saved in Streamlit → Settings → Secrets as GITHUB_TOKEN.

"Publish Now" only works if the platform credentials (X_API_KEY etc. /
META_ACCESS_TOKEN etc.) are ALSO present in Streamlit's own secrets — they
are separate from the GitHub Actions secrets of the same name, because
Streamlit Cloud and GitHub Actions are two different runtimes. Until then,
Copy Caption is the real publish path, which is the default anyway.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bots.social import schema, store  # noqa: E402  (stdlib-only imports, safe on Streamlit Cloud)
from bots.social.brands import brand  # noqa: E402
from bots.social.publishers import publish_instagram, publish_x  # noqa: E402

st.set_page_config(page_title="DASH Social Queue", page_icon="🌙", layout="wide")

GITHUB_REPO = "donthebuilder/MLB-HR-DASHBOARD-STREAMLIT"
DATA_BRANCH = "data"
API_BASE = f"https://api.github.com/repos/{GITHUB_REPO}/contents"


def _token() -> str:
    try:
        return str(st.secrets.get("GITHUB_TOKEN", "") or "")
    except Exception:
        return ""


def _api_get(path: str) -> tuple[dict | None, str | None]:
    """Returns (decoded_json_or_None, sha_or_None). A 404 is a normal empty
    state (no queue yet) — not an error."""
    token = _token()
    if not token:
        return None, None
    r = requests.get(f"{API_BASE}/{path}", params={"ref": DATA_BRANCH},
                      headers={"Authorization": f"token {token}"}, timeout=20)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    body = r.json()
    content = base64.b64decode(body["content"]).decode("utf-8")
    return json.loads(content), body["sha"]


def _api_put(path: str, payload: dict, sha: str | None, message: str) -> None:
    token = _token()
    if not token:
        raise RuntimeError("GITHUB_TOKEN secret is not set — writes are disabled. "
                            "See docs/SOCIAL.md.")
    body = {
        "message": message,
        "content": base64.b64encode(json.dumps(payload, indent=2).encode()).decode(),
        "branch": DATA_BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(f"{API_BASE}/{path}", json=body,
                      headers={"Authorization": f"token {token}"}, timeout=20)
    r.raise_for_status()


def load_queue_with_sha() -> tuple[list[dict], str | None]:
    payload, sha = _api_get("public/data/current/social/queue.json")
    if payload is None:
        # Fall back to the public read path — works even without a token,
        # just can't be used as a base for a write (no sha).
        return store.load_queue(), None
    posts = payload.get("posts") if isinstance(payload, dict) else None
    return (posts if isinstance(posts, list) else []), sha


def save_queue(posts: list[dict], sha: str | None, message: str) -> None:
    payload = {"updated_at": schema.now_iso(), "posts": posts}
    _api_put("public/data/current/social/queue.json", payload, sha, message)


def mark_fingerprints_decided(post: dict) -> None:
    """Add every one of this post's platform fingerprints to the durable
    decided-set once it reaches a terminal state (published or rejected) —
    the check build() runs before ever re-queueing the same (subject,
    platform, date) again. Best-effort: a failure here should not undo the
    status change the user just made."""
    try:
        payload, sha = _api_get("public/data/current/social/fingerprints.json")
        fps = set((payload or {}).get("fingerprints") or [])
        fps.update((post.get("fingerprints") or {}).values())
        _api_put("public/data/current/social/fingerprints.json",
                  {"updated_at": schema.now_iso(), "fingerprints": sorted(fps)}, sha,
                  f"social: mark fingerprints decided for {post.get('id')}")
    except Exception as e:
        st.warning(f"fingerprint index not updated ({e}) — duplicate protection for this post "
                    f"may not carry forward to the next run.")


def append_history(post: dict, platform: str, event: str, **extra) -> None:
    """Best-effort — a queue action should not fail just because the audit
    log couldn't be written this second."""
    try:
        date_str = schema.now_iso()[:10]
        path = f"public/data/current/social/history/social_history_{date_str}.jsonl"
        existing, sha = _api_get(path)
        # Contents API returns JSON-decoded content; a .jsonl file will fail
        # json.loads unless it happens to be a single line, so read it raw.
        token = _token()
        text = ""
        if token:
            r = requests.get(f"{API_BASE}/{path}", params={"ref": DATA_BRANCH},
                              headers={"Authorization": f"token {token}"}, timeout=20)
            if r.status_code == 200:
                body = r.json()
                text = base64.b64decode(body["content"]).decode("utf-8")
                sha = body["sha"]
            else:
                sha = None
        row = {"post_id": post.get("id"), "fingerprint": post.get("fingerprints", {}).get(platform),
               "platform": platform, "brand": post.get("brand"), "product": post.get("product"),
               "sport": post.get("sport"), "content_type": post.get("content_type"),
               "event": event, "at": schema.now_iso(), **extra}
        text += json.dumps(row) + "\n"
        body = {"message": f"social history: {event} {post.get('id')} [{platform}]",
                "content": base64.b64encode(text.encode()).decode(), "branch": DATA_BRANCH}
        if sha:
            body["sha"] = sha
        if token:
            requests.put(f"{API_BASE}/{path}", json=body,
                          headers={"Authorization": f"token {token}"}, timeout=20).raise_for_status()
    except Exception as e:
        st.warning(f"history log not written ({e}) — the status change itself was still saved.")


st.title("🌙 DASH Social Queue")
st.caption("Every DASH product's social drafts land here. Nothing posts anywhere until you approve it.")

if not _token():
    st.error("GITHUB_TOKEN is not set in this app's Secrets, so Approve/Reject/Edit can't write back "
             "to the data branch yet. You can still view drafts below. See docs/SOCIAL.md.")

posts, sha = load_queue_with_sha()

status_filter = st.multiselect(
    "Status", options=list(schema.STATUSES),
    default=["pending_review", "approved", "failed"],
)
visible = [p for p in posts if p.get("status") in status_filter] if status_filter else posts

if not visible:
    st.info("Nothing here yet. The Night Recap workflow queues one draft per product per night once "
            "grading is complete.")

for post in visible:
    b = brand(post.get("product", ""))
    with st.container(border=True):
        cols = st.columns([1, 2])
        with cols[0]:
            img_rel = post.get("assets", {}).get("square") or post.get("assets", {}).get("landscape")
            if img_rel:
                st.image(store.asset_url(img_rel), use_container_width=True)
            else:
                st.caption("no asset yet")
        with cols[1]:
            st.markdown(f"**{b.get('icon', '')} {b.get('name', post.get('product'))}** · "
                         f"{post.get('sport', '')} · `{post.get('content_type')}` · "
                         f"status: **{post.get('status')}** · created {post.get('created_at', '')[:16].replace('T', ' ')}")
            if post.get("headline"):
                st.markdown(f"##### {post['headline']}")

            x_cap = st.text_area("X caption", value=post.get("captions", {}).get("x") or "",
                                  key=f"x_{post['id']}", height=100)
            ig_cap = st.text_area("Instagram caption", value=post.get("captions", {}).get("instagram") or "",
                                   key=f"ig_{post['id']}", height=100)

            btns = st.columns(6)
            if btns[0].button("Approve", key=f"appr_{post['id']}"):
                post["captions"]["x"], post["captions"]["instagram"] = x_cap, ig_cap
                post = schema.set_status(post, "approved")
                save_queue(store.upsert_post(posts, post), sha, f"social: approve {post['id']}")
                append_history(post, "x", "approved", caption=x_cap)
                st.rerun()
            if btns[1].button("Save edits", key=f"save_{post['id']}"):
                post["captions"]["x"], post["captions"]["instagram"] = x_cap, ig_cap
                save_queue(store.upsert_post(posts, post), sha, f"social: edit {post['id']}")
                st.rerun()
            if btns[2].button("Reject", key=f"rej_{post['id']}"):
                post = schema.set_status(post, "rejected")
                save_queue(store.upsert_post(posts, post), sha, f"social: reject {post['id']}")
                for platform in (post.get("fingerprints") or {}):
                    append_history(post, platform, "rejected")
                mark_fingerprints_decided(post)
                st.rerun()
            if btns[3].button("Copy X", key=f"cpx_{post['id']}", help="Shows the caption in a copyable code block below."):
                st.session_state[f"show_copy_{post['id']}"] = "x"
            if btns[4].button("Copy IG", key=f"cpig_{post['id']}"):
                st.session_state[f"show_copy_{post['id']}"] = "instagram"
            if btns[5].button("Publish Now", key=f"pub_{post['id']}",
                               help="Only works if X/Instagram credentials are also set in this app's Secrets."):
                results = {}
                if "x" in (post.get("recommended_platforms") or ["x"]):
                    results["x"] = publish_x(caption=x_cap)
                if "instagram" in (post.get("recommended_platforms") or []):
                    img_url = store.asset_url(post.get("assets", {}).get("square", ""))
                    results["instagram"] = publish_instagram(caption=ig_cap, image_url=img_url)
                any_ok = any(r.get("ok") for r in results.values())
                post = schema.set_status(post, "published" if any_ok else "failed")
                post["error"] = "; ".join(r["error"] for r in results.values() if r.get("error")) or None
                save_queue(store.upsert_post(posts, post), sha, f"social: publish {post['id']}")
                for platform, r in results.items():
                    append_history(post, platform, "published" if r.get("ok") else "failed",
                                    platform_response_id=r.get("platform_response_id"), error=r.get("error"))
                if any(r.get("ok") for r in results.values()):
                    mark_fingerprints_decided(post)
                if any_ok:
                    st.success(f"Published: {results}")
                else:
                    st.error(f"Publish failed — credentials probably aren't configured here yet: {results}")
                st.rerun()

            shown = st.session_state.get(f"show_copy_{post['id']}")
            if shown:
                st.code(x_cap if shown == "x" else ig_cap, language=None)
