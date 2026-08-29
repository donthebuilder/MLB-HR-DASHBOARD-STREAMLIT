#!/usr/bin/env python3
"""📈 Social — Track Record (2026-08-22).

An on-demand share card of the site's own pooled proof numbers (the same
TOP/HIT/HRR figures the Boards tab's PROOF banners show) — not tied to any
single slate, so it's triggered manually rather than on a schedule. Same
dedupe/never-auto-post rules as every other social entrypoint; one per
calendar day (re-run tomorrow for fresh pooled numbers).

Usage:
    python bots/social_track_record.py
    python bots/social_track_record.py --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.social import assets, captions, schema, store, track_record
from bots.social.brands import brand
from bots.social.fingerprint import fingerprint as make_fingerprint


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="Label date, YYYY-MM-DD. Defaults to today (UTC).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    date_str = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    post_id = f"moonshot-mlb-{date_str}-track-record"

    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        print(f"· {post_id} is already in the queue — one per day, nothing to do.")
        return 0

    data = track_record.build(date_str=date_str)
    if not data:
        print("· no pooled track record available yet.")
        return 0

    brand_cfg = brand("moonshot")
    fps = {
        "x": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                               content_type="track_record", subject="TRACK RECORD", platform="x"),
        "instagram": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                                       content_type="track_record", subject="TRACK RECORD", platform="instagram"),
    }
    known = store.load_fingerprints()
    if fps["x"] in known and fps["instagram"] in known:
        print(f"· {post_id} was already published to every platform — not re-queueing.")
        return 0

    post = schema.new_post(id=post_id, brand="dash", product="moonshot", sport="MLB",
                            content_type="track_record", data=data, fingerprints=fps)

    print(f"· building captions for {post_id}")
    cap = captions.generate_captions(post, brand_cfg=brand_cfg)
    if cap:
        post["headline"] = cap.get("headline")
        post["captions"]["x"] = cap.get("x_caption")
        post["captions"]["instagram"] = cap.get("instagram_caption")
        post["captions"]["story"] = cap.get("story_text")
        post["recommended_platforms"] = cap.get("recommended_platforms") or []
    else:
        print("  · captions unavailable — post will queue with empty captions for manual entry")

    title = post.get("headline") or "TRACK RECORD"
    subtitle = f"{data.get('days', '?')}-day pooled record"
    # each row is a real ok/n pair -> render_card draws it as a rounded
    # progress bar with its own percentage, same as the Night Recap's bars.
    color_by_label = {"TOP15 BOARD": "TOP15", "TOP PICKS": "TOP", "HRR PICKS": "HRR", "HIT PICKS": "HIT"}
    bars = []
    for row in (data.get("rows") or []):
        bars.append({
            "label": row["label"], "sub": row.get("sub"),
            "ok": row["ok"], "n": row["n"],
            "color": color_by_label.get(row["label"]),
        })

    if args.dry_run:
        import json
        post["assets"] = {v: f"(would render: social/assets/{date_str}/{post_id}-{v}.png)"
                           for v in ("square", "story", "landscape")}
        print(json.dumps(post, indent=2))
        return 0

    print(f"· rendering assets for {post_id}")
    for variant in ("square", "story", "landscape"):
        def _render(v=variant):
            return assets.render_card(variant=v, brand_cfg=brand_cfg, kind="TRACK RECORD", title=title,
                                       subtitle=subtitle, bars=bars)
        rel = assets.cached_or_render(post_id=post_id, date_str=date_str, variant=variant, render_fn=_render)
        post["assets"][variant] = rel

    posts = store.upsert_post(existing_queue, post)
    store.save_queue(posts)
    for platform, fp in fps.items():
        store.append_history_event(post=post, platform=platform, event="pending_review",
                                     fingerprint=fp, caption=post["captions"].get(platform))
    print(f"· queued {post_id} as pending_review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
