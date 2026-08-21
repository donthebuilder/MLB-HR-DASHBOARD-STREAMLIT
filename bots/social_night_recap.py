#!/usr/bin/env python3
"""🌙 Social — Night Recap (2026-08-21).

The first automated social trigger (spec section 22 / 15): once a slate is
fully graded, build ONE Moonshot night-recap draft, generate its captions
and graphics, and drop it into the approval queue as pending_review. Never
auto-publishes — SOCIAL_AUTO_PUBLISH stays false by default, and even when
true this script never calls a publisher; that only happens from the queue
UI or a future workflow that reads an *approved* post (see docs/SOCIAL.md).

Usage (in a workflow, after grading has settled for the day):
    python bots/social_night_recap.py --date 2026-08-21
    python bots/social_night_recap.py                 # defaults to today (UTC)
    python bots/social_night_recap.py --dry-run        # build + print, write nothing

Spam guard (spec section 16): at most one Night Recap per product per date.
The post id itself IS the guard — night_recap.build() is idempotent, and
this script exits quietly if a queue entry with that id already exists.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.social import assets, captions, night_recap, publishers, schema, store
from bots.social.brands import brand
from bots.social.fingerprint import fingerprint as make_fingerprint


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="Slate date, YYYY-MM-DD. Defaults to today (UTC).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    date_str = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    post_id = f"moonshot-mlb-{date_str}-night-recap"

    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        print(f"· {post_id} is already in the queue — one recap per product/date, nothing to do.")
        return 0

    data = night_recap.build(date_str=date_str)
    if not data:
        print(f"· no complete recap available for {date_str} yet.")
        return 0

    brand_cfg = brand("moonshot")
    fps = {
        "x": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                               content_type="night_recap", subject="RECAP", platform="x"),
        "instagram": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                                       content_type="night_recap", subject="RECAP", platform="instagram"),
    }
    known = store.load_fingerprints()
    if fps["x"] in known and fps["instagram"] in known:
        print(f"· {post_id} was already published to every platform — not re-queueing "
              f"(use the queue UI's Repost to override).")
        return 0

    post = schema.new_post(
        id=post_id, brand="dash", product="moonshot", sport="MLB",
        content_type="night_recap", data=data, fingerprints=fps,
    )

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

    print(f"· rendering assets for {post_id}")
    stats = [
        ("HIT BOARD", data.get("board_record", "—")),
        ("HR SCORERS FOUND", str(data.get("hr_scorers", "—"))),
        ("SLATE HR COVERAGE", data.get("slate_hr_coverage", "—")),
    ]
    if data.get("hr_pick_record"):
        stats.append(("HR PICK RECORD", data["hr_pick_record"]))
    title = post.get("headline") or "NIGHT RECAP"
    subtitle = f"{date_str} · {brand_cfg['name']} {brand_cfg.get('sport', '')}".strip()
    highlights = data.get("top_cashes") or []

    if args.dry_run:
        import json
        post["assets"] = {v: f"(would render: social/assets/{date_str}/{post_id}-{v}.png)"
                           for v in ("square", "story", "landscape")}
        print(json.dumps(post, indent=2))
        return 0

    for variant in ("square", "story", "landscape"):
        def _render(v=variant):
            return assets.render_recap_card(variant=v, brand_cfg=brand_cfg, title=title,
                                             subtitle=subtitle, stats=stats, highlights=highlights)
        rel = assets.cached_or_render(post_id=post_id, date_str=date_str, variant=variant, render_fn=_render)
        post["assets"][variant] = rel

    posts = store.upsert_post(existing_queue, post)
    store.save_queue(posts)
    for platform, fp in fps.items():
        store.append_history_event(post=post, platform=platform, event="pending_review",
                                     fingerprint=fp, caption=post["captions"].get(platform))

    publishers.notify_discord(
        f"🌙 New social draft pending review: **{title}**\n"
        f"{subtitle} — {data.get('board_record', '—')} board, "
        f"{data.get('slate_hr_coverage', '—')} slate HR coverage.\n"
        f"Approve or edit it in the Social Queue page."
    )
    print(f"· queued {post_id} as pending_review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
