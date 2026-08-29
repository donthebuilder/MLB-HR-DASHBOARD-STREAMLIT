#!/usr/bin/env python3
"""⚾ Social — Game Card (2026-08-22).

An on-demand share card for one specific matchup — pass either --game-pk
or --team (either side's abbreviation). Shows the picks in that game, and
once it's graded, overlays each pick's real result. Same dedupe/never-
auto-post rules as every other social entrypoint; one card per game per
day (re-run later the same day for an updated result overlay only if you
delete the earlier draft first — this is a deliberate cap, not a bug).

Usage:
    python bots/social_game_card.py --team NYY
    python bots/social_game_card.py --game-pk 823509
    python bots/social_game_card.py --team NYY --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.social import assets, captions, game_card, schema, store
from bots.social.brands import brand
from bots.social.fingerprint import fingerprint as make_fingerprint


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in str(s)).strip("-").lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="Slate date, YYYY-MM-DD. Defaults to today (UTC).")
    ap.add_argument("--game-pk", default=None)
    ap.add_argument("--team", default=None, help="Either side's team abbreviation, e.g. NYY")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.game_pk and not args.team:
        print("· pass --game-pk or --team to identify which game")
        return 1

    date_str = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    data = game_card.build(date_str=date_str, game_pk=args.game_pk, team=args.team)
    if not data:
        print("· no matching game found (or it has no assigned picks yet).")
        return 0

    subject = f"{data.get('team', '?')}-{data.get('opponent', '?')}"
    post_id = f"moonshot-mlb-{date_str}-game-card-{_slug(subject)}"

    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        print(f"· {post_id} is already in the queue — nothing to do.")
        return 0

    brand_cfg = brand("moonshot")
    fps = {
        "x": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                               content_type="game_card", subject=subject, platform="x"),
        "instagram": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                                       content_type="game_card", subject=subject, platform="instagram"),
    }
    known = store.load_fingerprints()
    if fps["x"] in known and fps["instagram"] in known:
        return 0

    post = schema.new_post(id=post_id, brand="dash", product="moonshot", sport="MLB",
                            content_type="game_card", data=data, fingerprints=fps)

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

    matchup = f"{data.get('team', '?')} vs {data.get('opponent', '?')}"
    title = post.get("headline") or matchup
    status_label = {"final": "FINAL", "live": "LIVE", "graded": "GRADED", "upcoming": "UPCOMING"}.get(
        data.get("status", "upcoming"), "UPCOMING")
    subtitle = f"{date_str} · {status_label} · {data.get('pick_count', '?')} picks"
    list_items = []
    for row in (data.get("picks") or []):
        line = str(row.get("name", "?"))
        if row.get("role"):
            line += f" — {row['role']}"
        if row.get("score"):
            line += f" · {row['score']}"
        if "actual_hr" in row or "actual_hits" in row:
            line += f"  ({row.get('actual_hr', 0)} HR, {row.get('actual_hits', 0)} H)"
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
            return assets.render_card(variant=v, brand_cfg=brand_cfg, kind="GAME CARD", title=title,
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
