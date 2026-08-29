#!/usr/bin/env python3
"""📋 Social — Board Snapshot (2026-08-22).

An on-demand, wider capture of the site's current Board (top 10, vs the
automated Daily Board's top 5) — trigger any time you want a shareable
"here's the board right now" card, separate from the one scheduled post
per day. Same dedupe/never-auto-post rules as every other social
entrypoint; one snapshot per calendar day.

Usage:
    python bots/social_board_snapshot.py
    python bots/social_board_snapshot.py --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.social import assets, board_snapshot, captions, schema, store
from bots.social.brands import brand
from bots.social.fingerprint import fingerprint as make_fingerprint


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="Slate date, YYYY-MM-DD. Defaults to today (UTC).")
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    date_str = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    post_id = f"moonshot-mlb-{date_str}-board-snapshot"

    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        print(f"· {post_id} is already in the queue — one snapshot per day, nothing to do.")
        return 0

    data = board_snapshot.build(date_str=date_str, top_n=args.top_n)
    if not data:
        print(f"· no board available for {date_str} yet.")
        return 0

    brand_cfg = brand("moonshot")
    fps = {
        "x": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                               content_type="board_snapshot", subject="BOARD SNAPSHOT", platform="x"),
        "instagram": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                                       content_type="board_snapshot", subject="BOARD SNAPSHOT", platform="instagram"),
    }
    known = store.load_fingerprints()
    if fps["x"] in known and fps["instagram"] in known:
        print(f"· {post_id} was already published to every platform — not re-queueing.")
        return 0

    post = schema.new_post(id=post_id, brand="dash", product="moonshot", sport="MLB",
                            content_type="board_snapshot", data=data, fingerprints=fps)

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

    title = post.get("headline") or "THE BOARD RIGHT NOW"
    games = data.get("games"); hitters = data.get("hitters")
    subtitle = f"{date_str} · {games or '?'} games · {hitters or '?'} hitters".strip()
    list_items = []
    for row in (data.get("board") or []):
        line = str(row.get("name", "?"))
        if row.get("role"):
            line += f" — {row['role']}"
        if row.get("score"):
            line += f" · {row['score']}"
        list_items.append(line)

    if args.dry_run:
        import json
        post["assets"] = {v: f"(would render: social/assets/{date_str}/{post_id}-{v}.png)"
                           for v in ("square", "story", "landscape")}
        print(json.dumps(post, indent=2))
        return 0

    print(f"· rendering assets for {post_id}")
    for variant in ("square", "story", "landscape"):
        def _render(v=variant):
            return assets.render_card(variant=v, brand_cfg=brand_cfg, kind="BOARD SNAPSHOT", title=title,
                                       subtitle=subtitle, list_items=list_items)
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
