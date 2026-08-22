#!/usr/bin/env python3
"""🌙 Social — Night Recap + Player Spotlight + Top Plays (2026-08-21,
spotlight added 2026-08-22, top plays added 2026-08-22).

The first automated social trigger (spec section 22 / 15): once a slate is
fully graded, build ONE Moonshot night-recap draft, ONE player-spotlight
draft for the night's top scorer, and ONE top-plays leaderboard draft —
all three reuse the same already-fetched graded payload (one fetch, three
posts) — generate their captions and graphics, and drop all three into the
approval queue as pending_review. Never auto-publishes — SOCIAL_AUTO_PUBLISH
stays false by default, and even when true this script never calls a
publisher; that only happens from the queue UI or a future workflow that
reads an *approved* post (see docs/SOCIAL.md).

Usage (in a workflow, after grading has settled for the day):
    python bots/social_night_recap.py --date 2026-08-21
    python bots/social_night_recap.py                 # defaults to today (UTC)
    python bots/social_night_recap.py --dry-run        # build + print, write nothing

Spam guard (spec section 16): at most one Night Recap, one Player Spotlight,
and one Top Plays post per product per date. The post id itself IS the
guard for each — every builder is idempotent, and this script exits quietly
per-post if a queue entry with that id already exists.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bots.social import assets, captions, night_recap, player_spotlight, publishers, schema, store, top_plays
from bots.social.brands import brand
from bots.social.fingerprint import fingerprint as make_fingerprint


def _queue_recap(*, date_str: str, graded: dict, brand_cfg: dict, dry_run: bool) -> dict | None:
    post_id = f"moonshot-mlb-{date_str}-night-recap"
    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        print(f"· {post_id} is already in the queue — one recap per product/date, nothing to do.")
        return None

    data = night_recap.build(date_str=date_str, graded=graded)
    if not data:
        print(f"· no complete recap available for {date_str} yet.")
        return None

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
        return None

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
    # bars: real ok/n records render as rounded progress bars (night-receipts
    # style); anything without an ok/n shape falls back to a plain value row.
    bars = []
    if data.get("board_record"):
        ok, n = _split_record(data["board_record"])
        bars.append({"label": "TOP15 BOARD", "ok": ok, "n": n, "color": "TOP15"})
    if data.get("hr_pick_record"):
        ok, n = _split_record(data["hr_pick_record"])
        bars.append({"label": "HR PICKS", "ok": ok, "n": n, "color": "HR"})
    if data.get("slate_hr_coverage"):
        ok, n = _split_record(data["slate_hr_coverage"])
        bars.append({"label": "SLATE HR COVERAGE", "ok": ok, "n": n, "sub": f"{data.get('hr_scorers', '?')} unique scorers"})

    title = post.get("headline") or "NIGHT RECAP"
    subtitle = f"{date_str} · {brand_cfg['name']} {brand_cfg.get('sport', '')}".strip()
    list_items = data.get("top_cashes") or []
    longest = data.get("longest_hr")
    footer_note = f"Longest HR: {longest['name']} — {longest['feet']} ft" if isinstance(longest, dict) else None

    if dry_run:
        import json
        post["assets"] = {v: f"(would render: social/assets/{date_str}/{post_id}-{v}.png)"
                           for v in ("square", "story", "landscape")}
        print(json.dumps(post, indent=2))
        return None

    for variant in ("square", "story", "landscape"):
        def _render(v=variant):
            return assets.render_card(variant=v, brand_cfg=brand_cfg, kind="NIGHT RECAP", title=title,
                                       subtitle=subtitle, bars=bars, list_items=list_items,
                                       footer_note=footer_note)
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
    return post


def _queue_spotlight(*, date_str: str, graded: dict, brand_cfg: dict, dry_run: bool) -> dict | None:
    data = player_spotlight.build(date_str=date_str, graded=graded)
    if not data:
        print(f"· no player spotlight available for {date_str} (nobody on the sheet homered).")
        return None

    subject = str(data.get("name", "spotlight"))
    post_id = f"moonshot-mlb-{date_str}-player-spotlight-{''.join(c if c.isalnum() else '-' for c in subject).strip('-').lower()}"
    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        print(f"· {post_id} is already in the queue — nothing to do.")
        return None

    fps = {
        "x": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                               content_type="player_spotlight", subject=subject, platform="x"),
        "instagram": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                                       content_type="player_spotlight", subject=subject, platform="instagram"),
    }
    known = store.load_fingerprints()
    if fps["x"] in known and fps["instagram"] in known:
        return None

    post = schema.new_post(id=post_id, brand="dash", product="moonshot", sport="MLB",
                            content_type="player_spotlight", data=data, fingerprints=fps)

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

    if dry_run:
        import json
        post["assets"] = {v: f"(would render: social/assets/{date_str}/{post_id}-{v}.png)"
                           for v in ("square", "story", "landscape")}
        print(json.dumps(post, indent=2))
        return None

    matchup = f"{data['team']} vs {data['opponent']}" if data.get("team") and data.get("opponent") else date_str
    # The player's NAME is already the card's title (drawn big, once) — the
    # big_stat below leads with whichever standout number is actually
    # available instead of repeating the name a second time (2026-08-22
    # visual QA: showing the name twice, at two font sizes, wasted the
    # whole card and the giant repeat clipped off-canvas anyway).
    if data.get("longest_hr_feet"):
        bs_label, bs_value = "LONGEST HR", f"{data['longest_hr_feet']} FT"
    elif data.get("multi_hr"):
        bs_label, bs_value = "MULTI-HR NIGHT", f"{data['multi_hr']}x HR"
    elif data.get("hr_score"):
        bs_label, bs_value = "MODEL SCORE", str(data["hr_score"])
    else:
        bs_label, bs_value = f"{data.get('pick_type', 'PLAYER')} PICK", "WENT DEEP"
    sub_bits = []
    if data.get("hr_score") and bs_label != "MODEL SCORE":
        sub_bits.append(f"score {data['hr_score']}")
    if data.get("odds_price"):
        sub_bits.append(f"{'+' if data['odds_price'] > 0 else ''}{data['odds_price']}")
    big_stat = {
        "label": bs_label,
        "value": bs_value,
        "sub": " · ".join(sub_bits) if sub_bits else matchup,
    }

    print(f"· rendering assets for {post_id}")
    for variant in ("square", "story", "landscape"):
        def _render(v=variant):
            return assets.render_card(variant=v, brand_cfg=brand_cfg, kind="PLAYER SPOTLIGHT",
                                       title=subject, subtitle=matchup, big_stat=big_stat)
        rel = assets.cached_or_render(post_id=post_id, date_str=date_str, variant=variant, render_fn=_render)
        post["assets"][variant] = rel

    posts = store.upsert_post(existing_queue, post)
    store.save_queue(posts)
    for platform, fp in fps.items():
        store.append_history_event(post=post, platform=platform, event="pending_review",
                                     fingerprint=fp, caption=post["captions"].get(platform))
    print(f"· queued {post_id} as pending_review")
    return post


def _queue_top_plays(*, date_str: str, graded: dict, brand_cfg: dict, dry_run: bool) -> dict | None:
    post_id = f"moonshot-mlb-{date_str}-top-plays"
    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        print(f"· {post_id} is already in the queue — nothing to do.")
        return None

    data = top_plays.build(date_str=date_str, graded=graded)
    if not data:
        print(f"· no top plays available for {date_str} yet.")
        return None

    fps = {
        "x": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                               content_type="top_plays", subject="TOP PLAYS", platform="x"),
        "instagram": make_fingerprint(sport="MLB", product="moonshot", date=date_str,
                                       content_type="top_plays", subject="TOP PLAYS", platform="instagram"),
    }
    known = store.load_fingerprints()
    if fps["x"] in known and fps["instagram"] in known:
        return None

    post = schema.new_post(id=post_id, brand="dash", product="moonshot", sport="MLB",
                            content_type="top_plays", data=data, fingerprints=fps)

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

    title = post.get("headline") or "TOP PLAYS OF THE NIGHT"
    subtitle = f"{date_str} · top {data.get('count', '?')} lines"
    list_items = []
    for row in (data.get("plays") or []):
        bits = []
        if row.get("hr"):
            bits.append(f"{row['hr']} HR")
        if row.get("hits"):
            bits.append(f"{row['hits']} H")
        line = f"{row.get('name', '?')} — {'/'.join(bits) if bits else '—'}"
        if row.get("ab"):
            line += f" ({row['ab']} AB)"
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
            return assets.render_card(variant=v, brand_cfg=brand_cfg, kind="TOP PLAYS", title=title,
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


def _split_record(record: str) -> tuple[int, int]:
    try:
        a, b = str(record).split("/", 1)
        return int(a.strip()), int(b.strip())
    except Exception:
        return 0, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="Slate date, YYYY-MM-DD. Defaults to today (UTC).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-spotlight", action="store_true", help="Only build the Night Recap.")
    ap.add_argument("--skip-top-plays", action="store_true", help="Skip the Top Plays leaderboard.")
    args = ap.parse_args()

    date_str = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    brand_cfg = brand("moonshot")

    graded = night_recap.fetch_graded(date_str)
    if not graded:
        print(f"· no graded results available for {date_str}")
        return 0
    if not night_recap.is_night_complete(graded):
        print(f"· {date_str} is not fully graded yet — skipping recap + spotlight")
        return 0

    _queue_recap(date_str=date_str, graded=graded, brand_cfg=brand_cfg, dry_run=args.dry_run)
    if not args.skip_spotlight:
        _queue_spotlight(date_str=date_str, graded=graded, brand_cfg=brand_cfg, dry_run=args.dry_run)
    if not args.skip_top_plays:
        _queue_top_plays(date_str=date_str, graded=graded, brand_cfg=brand_cfg, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
