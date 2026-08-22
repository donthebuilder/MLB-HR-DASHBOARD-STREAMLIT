#!/usr/bin/env python3
"""🧢 Social — Player Card (2026-08-22).

An on-demand profile card for any hitter currently on today's slate — pass
--name exactly as it appears on the site. Works any time before or during
their game, unlike Player Spotlight (which only exists after a graded HR
night). Same dedupe/never-auto-post rules as every other social
entrypoint; one card per player per day.

Usage:
    python bots/social_player_card.py --name "Pete Crow-Armstrong"
    python bots/social_player_card.py --name "Pete Crow-Armstrong" --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.social import assets, captions, player_card, schema, store
from bots.social.brands import brand
from bots.social.fingerprint import fingerprint as make_fingerprint


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in str(s)).strip("-").lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="Slate date, YYYY-MM-DD. Defaults to today (UTC).")
    ap.add_argument("--name", required=True, help="Exact player name as it appears on the site")
    ap.add_argument("--team", default=None,
                     help="Disambiguate a same-name collision (real MLB has them) by team abbreviation")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    date_str = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    data = player_card.build(date_str=date_str, name=args.name, team=args.team)
    if not data:
        print(f"· {args.name!r} is not on today's slate.")
        return 0

    subject = str(data.get("name", args.name))
    post_id = f"moonshot-mlb-{date_str}-player-card-{_slug(subject)}"

    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        print(f"· {post_id} is already in the queue — nothing to do.")
        return 0

    brand_cfg = brand("moonshot")
    fps = {
        "x": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                               content_type="player_card", subject=subject, platform="x"),
        "instagram": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                                       content_type="player_card", subject=subject, platform="instagram"),
    }
    known = store.load_fingerprints()
    if fps["x"] in known and fps["instagram"] in known:
        return 0

    post = schema.new_post(id=post_id, brand="dash", product="moonshot", sport="MLB",
                            content_type="player_card", data=data, fingerprints=fps)

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

    matchup = f"{data['team']} vs {data['opponent']}" if data.get("team") and data.get("opponent") else date_str
    title = subject
    subtitle = f"{matchup}" + (f" · {data['role']}" if data.get("role") else "")

    big_stat = None
    if data.get("top_board_score"):
        big_stat = {"label": "MODEL SCORE", "value": str(data["top_board_score"]),
                     "sub": f"HR score {data['hr_score']}" if data.get("hr_score") else None}

    list_items = []
    season_bits = []
    if data.get("season_avg") is not None:
        season_bits.append(f"{data['season_avg']:.3f} AVG")
    if data.get("season_hr") is not None:
        season_bits.append(f"{data['season_hr']} HR")
    if data.get("season_ops") is not None:
        season_bits.append(f"{data['season_ops']:.3f} OPS")
    if season_bits:
        list_items.append("SEASON — " + " · ".join(season_bits))
    if data.get("last5_avg") is not None or data.get("last5_hr") is not None:
        bits = []
        if data.get("last5_avg") is not None:
            bits.append(f"{data['last5_avg']:.3f} AVG")
        if data.get("last5_hr") is not None:
            bits.append(f"{data['last5_hr']} HR")
        list_items.append("LAST 5 — " + " · ".join(bits))
    if data.get("last7_avg") is not None or data.get("last7_hr") is not None:
        bits = []
        if data.get("last7_avg") is not None:
            bits.append(f"{data['last7_avg']:.3f} AVG")
        if data.get("last7_hr") is not None:
            bits.append(f"{data['last7_hr']} HR")
        list_items.append("LAST 7 — " + " · ".join(bits))

    if args.dry_run:
        import json
        post["assets"] = {v: f"(would render: social/assets/{date_str}/{post_id}-{v}.png)"
                           for v in ("square", "story", "landscape")}
        print(json.dumps(post, indent=2))
        return 0

    print(f"· rendering assets for {post_id}")
    for variant in ("square", "story", "landscape"):
        def _render(v=variant):
            return assets.render_card(variant=v, brand_cfg=brand_cfg, kind="PLAYER CARD", title=title,
                                       subtitle=subtitle, big_stat=big_stat, list_items=list_items)
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
