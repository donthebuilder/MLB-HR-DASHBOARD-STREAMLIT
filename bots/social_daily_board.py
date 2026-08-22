#!/usr/bin/env python3
"""📋 Social — Daily Board + Stacked Game + Storyline (2026-08-22, stacked
game and storyline added 2026-08-22).

The pre-game counterpart to the Night Recap: once today's lineups have
mostly locked (see .github/workflows/social-daily-board.yml's cron, timed
to sit after today.yml's lineup-confirmation runs), build ONE Moonshot
daily-board draft, ONE stacked-game draft (the single game carrying the
most board picks, once it clears 3), and ONE storyline draft (the model's
own top_pick_reason / top_board_rank_reason for its top pick) — all three
read the same today_slim.json ranking the site itself shows — and drop
each into the approval queue as pending_review. Same dedupe/never-auto-post
rules as social_night_recap.py.

Usage:
    python bots/social_daily_board.py --date 2026-08-22
    python bots/social_daily_board.py --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.social import assets, captions, daily_board, publishers, schema, stacked_game, storyline, store
from bots.social.brands import brand
from bots.social.fingerprint import fingerprint as make_fingerprint


def _queue_board(*, date_str: str, brand_cfg: dict, dry_run: bool) -> dict | None:
    post_id = f"moonshot-mlb-{date_str}-daily-board"

    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        print(f"· {post_id} is already in the queue — one board per product/date, nothing to do.")
        return None

    data = daily_board.build(date_str=date_str)
    if not data:
        print(f"· no board available for {date_str} yet.")
        return None

    fps = {
        "x": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                               content_type="daily_board", subject="BOARD", platform="x"),
        "instagram": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                                       content_type="daily_board", subject="BOARD", platform="instagram"),
    }
    known = store.load_fingerprints()
    if fps["x"] in known and fps["instagram"] in known:
        print(f"· {post_id} was already published to every platform — not re-queueing.")
        return None

    post = schema.new_post(id=post_id, brand="dash", product="moonshot", sport="MLB",
                            content_type="daily_board", data=data, fingerprints=fps)

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

    title = post.get("headline") or "TONIGHT'S BOARD"
    games = data.get("games"); hitters = data.get("hitters")
    subtitle = f"{date_str} · {games or '?'} games · {hitters or '?'} hitters".strip()
    list_items = []
    for row in (data.get("board") or []):
        # Kept short on purpose (name — role · score) so it survives the
        # card's list-row width without needing to truncate; team/opponent
        # still lives in `data` for the caption writer to use in prose.
        line = str(row.get("name", "?"))
        if row.get("role"):
            line += f" — {row['role']}"
        if row.get("score"):
            line += f" · {row['score']}"
        list_items.append(line)

    if dry_run:
        import json
        post["assets"] = {v: f"(would render: social/assets/{date_str}/{post_id}-{v}.png)"
                           for v in ("square", "story", "landscape")}
        print(json.dumps(post, indent=2))
        return None

    print(f"· rendering assets for {post_id}")
    for variant in ("square", "story", "landscape"):
        def _render(v=variant):
            return assets.render_card(variant=v, brand_cfg=brand_cfg, kind="DAILY BOARD", title=title,
                                       subtitle=subtitle, list_items=list_items)
        rel = assets.cached_or_render(post_id=post_id, date_str=date_str, variant=variant, render_fn=_render)
        post["assets"][variant] = rel

    posts = store.upsert_post(existing_queue, post)
    store.save_queue(posts)
    for platform, fp in fps.items():
        store.append_history_event(post=post, platform=platform, event="pending_review",
                                     fingerprint=fp, caption=post["captions"].get(platform))

    publishers.notify_discord(
        f"📋 New social draft pending review: **{title}**\n{subtitle}\n"
        f"Approve or edit it in the Social Queue page."
    )
    print(f"· queued {post_id} as pending_review")
    return post


def _queue_stacked_game(*, date_str: str, brand_cfg: dict, dry_run: bool) -> dict | None:
    post_id = f"moonshot-mlb-{date_str}-stacked-game"
    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        print(f"· {post_id} is already in the queue — nothing to do.")
        return None

    data = stacked_game.build(date_str=date_str)
    if not data:
        print(f"· no stacked game available for {date_str} yet.")
        return None

    fps = {
        "x": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                               content_type="stacked_game", subject="STACKED GAME", platform="x"),
        "instagram": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                                       content_type="stacked_game", subject="STACKED GAME", platform="instagram"),
    }
    known = store.load_fingerprints()
    if fps["x"] in known and fps["instagram"] in known:
        return None

    post = schema.new_post(id=post_id, brand="dash", product="moonshot", sport="MLB",
                            content_type="stacked_game", data=data, fingerprints=fps)

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

    team = data.get("team"); opponent = data.get("opponent")
    title = post.get("headline") or (f"{team} vs {opponent}" if team and opponent else "STACKED GAME")
    subtitle = f"{date_str} · {data.get('pick_count', '?')} picks in this one".strip()
    list_items = []
    for row in (data.get("picks") or []):
        line = str(row.get("name", "?"))
        if row.get("role"):
            line += f" — {row['role']}"
        if row.get("score"):
            line += f" · {row['score']}"
        list_items.append(line)

    if dry_run:
        import json
        post["assets"] = {v: f"(would render: social/assets/{date_str}/{post_id}-{v}.png)"
                           for v in ("square", "story", "landscape")}
        print(json.dumps(post, indent=2))
        return None

    print(f"· rendering assets for {post_id}")
    for variant in ("square", "story", "landscape"):
        def _render(v=variant):
            return assets.render_card(variant=v, brand_cfg=brand_cfg, kind="STACKED GAME", title=title,
                                       subtitle=subtitle, list_items=list_items)
        rel = assets.cached_or_render(post_id=post_id, date_str=date_str, variant=variant, render_fn=_render)
        post["assets"][variant] = rel

    posts = store.upsert_post(existing_queue, post)
    store.save_queue(posts)
    for platform, fp in fps.items():
        store.append_history_event(post=post, platform=platform, event="pending_review",
                                     fingerprint=fp, caption=post["captions"].get(platform))
    print(f"· queued {post_id} as pending_review")
    return post


def _queue_storyline(*, date_str: str, brand_cfg: dict, dry_run: bool) -> dict | None:
    data = storyline.build(date_str=date_str)
    if not data:
        print(f"· no storyline available for {date_str} yet.")
        return None

    subject = str(data.get("name", "storyline"))
    post_id = f"moonshot-mlb-{date_str}-storyline-{''.join(c if c.isalnum() else '-' for c in subject).strip('-').lower()}"
    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        print(f"· {post_id} is already in the queue — nothing to do.")
        return None

    fps = {
        "x": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                               content_type="storyline", subject=subject, platform="x"),
        "instagram": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                                       content_type="storyline", subject=subject, platform="instagram"),
    }
    known = store.load_fingerprints()
    if fps["x"] in known and fps["instagram"] in known:
        return None

    post = schema.new_post(id=post_id, brand="dash", product="moonshot", sport="MLB",
                            content_type="storyline", data=data, fingerprints=fps)

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
    big_stat = None
    if data.get("tag"):
        big_stat = {"label": data.get("role", "WHY THE MODEL LIKES IT"), "value": str(data["tag"]), "sub": matchup}
    list_items = data.get("reasons") or None

    if dry_run:
        import json
        post["assets"] = {v: f"(would render: social/assets/{date_str}/{post_id}-{v}.png)"
                           for v in ("square", "story", "landscape")}
        print(json.dumps(post, indent=2))
        return None

    print(f"· rendering assets for {post_id}")
    for variant in ("square", "story", "landscape"):
        def _render(v=variant):
            return assets.render_card(variant=v, brand_cfg=brand_cfg, kind="STORYLINE", title=subject,
                                       subtitle=matchup, big_stat=big_stat, list_items=list_items)
        rel = assets.cached_or_render(post_id=post_id, date_str=date_str, variant=variant, render_fn=_render)
        post["assets"][variant] = rel

    posts = store.upsert_post(existing_queue, post)
    store.save_queue(posts)
    for platform, fp in fps.items():
        store.append_history_event(post=post, platform=platform, event="pending_review",
                                     fingerprint=fp, caption=post["captions"].get(platform))
    print(f"· queued {post_id} as pending_review")
    return post


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="Slate date, YYYY-MM-DD. Defaults to today (UTC).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-stacked-game", action="store_true")
    ap.add_argument("--skip-storyline", action="store_true")
    args = ap.parse_args()

    date_str = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    brand_cfg = brand("moonshot")

    _queue_board(date_str=date_str, brand_cfg=brand_cfg, dry_run=args.dry_run)
    if not args.skip_stacked_game:
        _queue_stacked_game(date_str=date_str, brand_cfg=brand_cfg, dry_run=args.dry_run)
    if not args.skip_storyline:
        _queue_storyline(date_str=date_str, brand_cfg=brand_cfg, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
