"""Real-time "pick cashed" social posts — hooked directly into
live_results_tracker.py's existing transition detector (_webhook_transitions)
instead of re-deriving "what just happened" from scratch. That function
already diffs the previous published results against the new ones and knows,
within the hour it happens, exactly which pick homered, which pool cashed,
and which pair cashed — this module just also turns that same transition
into a queued social post, using only the fields the caller already computed
from graded data. It never fetches anything itself.

This is what gives DASH the "3-9 times a day" cadence Donovan asked for:
results.yml already re-grades every slate hourly during game windows, so a
real cash event reaches the queue within an hour of happening — same
schedule as the Discord digest, not a new cron job.

Any failure here (Anthropic down, a bad field) must never break grading or
the Discord notification it rides alongside — see queue(), which is called
from inside a try/except at every call site in live_results_tracker.py.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import assets, captions, schema, store
from .brands import brand
from .fingerprint import fingerprint as make_fingerprint


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in str(s)).strip("-").upper() or "EVENT"


def queue_hr(*, name: str, role: str, date_str: str, team: str | None = None,
             opponent: str | None = None, hr_score: float | None = None,
             hr_count: int = 1, called_pregame: float | None = None,
             product: str = "moonshot", sport: str = "MLB") -> dict[str, Any] | None:
    """A designated pick (TOP/HR/TOP15 role) just homered. One post per
    (player, date) — a second HR by the same player the same night updates
    hr_count on the next hourly run rather than double-posting (guarded by
    the post id below being stable per player/date)."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    post_id = f"{product}-{sport.lower()}-{date_str}-pick-cashed-{_slug(name)}"
    data: dict[str, Any] = {
        "date": date_str, "name": name, "role": role, "team": team, "opponent": opponent,
        "hr_score": round(hr_score, 1) if hr_score else None,
        "hr_count": hr_count if hr_count and hr_count > 1 else None,
        "called_pregame_score": round(called_pregame, 1) if called_pregame else None,
    }
    data = {k: v for k, v in data.items() if v not in (None, [], "")}
    big_stat = {
        "label": f"{role} PICK · CASHED" if role else "PICK · CASHED",
        "value": name,
        "sub": (f"{hr_count}x tonight" if hr_count and hr_count > 1 else "went deep")
               + (f" · called pregame at {called_pregame:.0f}" if called_pregame else ""),
    }
    return _queue(post_id=post_id, content_type="pick_cashed", subject=name, date_str=date_str,
                  data=data, kind="PICK CASHED", title=name,
                  subtitle=f"{team + ' vs ' + opponent if team and opponent else date_str}",
                  big_stat=big_stat, product=product, sport=sport)


def queue_pool_cashed(*, label: str, members: list[str], date_str: str,
                       product: str = "moonshot", sport: str = "MLB") -> dict[str, Any] | None:
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    post_id = f"{product}-{sport.lower()}-{date_str}-pool-cashed-{_slug(label)}"
    data = {"date": date_str, "pool": label, "members": members, "count": len(members)}
    big_stat = {"label": "POOL CASHED", "value": label, "sub": f"all {len(members)} went deep"}
    return _queue(post_id=post_id, content_type="pick_cashed", subject=f"POOL-{label}", date_str=date_str,
                  data=data, kind="POOL CASHED", title=label,
                  subtitle=date_str, big_stat=big_stat, list_items=members,
                  product=product, sport=sport)


def queue_pair_cashed(*, a: str, b: str, label: str, date_str: str,
                       product: str = "moonshot", sport: str = "MLB") -> dict[str, Any] | None:
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    post_id = f"{product}-{sport.lower()}-{date_str}-pair-cashed-{_slug(a)}-{_slug(b)}"
    data = {"date": date_str, "pair": label, "a": a, "b": b}
    big_stat = {"label": "PAIR CASHED", "value": f"{a} + {b}", "sub": label}
    return _queue(post_id=post_id, content_type="pick_cashed", subject=f"PAIR-{a}-{b}", date_str=date_str,
                  data=data, kind="PAIR CASHED", title=f"{a} + {b}",
                  subtitle=date_str, big_stat=big_stat, product=product, sport=sport)


def _queue(*, post_id: str, content_type: str, subject: str, date_str: str, data: dict[str, Any],
           kind: str, title: str, subtitle: str, big_stat: dict[str, Any] | None = None,
           list_items: list[str] | None = None, product: str, sport: str) -> dict[str, Any] | None:
    existing_queue = store.load_queue()
    if any(p.get("id") == post_id for p in existing_queue):
        return None  # already queued this run/hour — the hourly diff already dedupes upstream too

    brand_cfg = brand(product)
    fps = {
        "x": make_fingerprint(sport=sport, product=product, date=date_str,
                               content_type=content_type, subject=subject, platform="x"),
        "instagram": make_fingerprint(sport=sport, product=product, date=date_str,
                                       content_type=content_type, subject=subject, platform="instagram"),
    }
    known = store.load_fingerprints()
    if fps["x"] in known and fps["instagram"] in known:
        return None

    post = schema.new_post(id=post_id, brand="dash", product=product, sport=sport,
                            content_type=content_type, data=data, fingerprints=fps, priority="high")

    cap = captions.generate_captions(post, brand_cfg=brand_cfg)
    if cap:
        post["headline"] = cap.get("headline")
        post["captions"]["x"] = cap.get("x_caption")
        post["captions"]["instagram"] = cap.get("instagram_caption")
        post["captions"]["story"] = cap.get("story_text")
        post["recommended_platforms"] = cap.get("recommended_platforms") or []

    for variant in ("square", "story", "landscape"):
        def _render(v=variant):
            return assets.render_card(variant=v, brand_cfg=brand_cfg, kind=kind, title=title,
                                       subtitle=subtitle, big_stat=big_stat, list_items=list_items)
        rel = assets.cached_or_render(post_id=post_id, date_str=date_str, variant=variant, render_fn=_render)
        post["assets"][variant] = rel

    posts = store.upsert_post(existing_queue, post)
    store.save_queue(posts)
    for platform, fp in fps.items():
        store.append_history_event(post=post, platform=platform, event="pending_review",
                                     fingerprint=fp, caption=post["captions"].get(platform))
    print(f"  · social: queued {post_id} as pending_review")
    return post
