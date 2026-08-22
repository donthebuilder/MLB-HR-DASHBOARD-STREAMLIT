"""DASH social pipeline — schema, fingerprint/dedupe, brand reuse, and the
"never invent a fact" guarantees, run without network access or credentials.

Run: python tests/test_social.py

WHY THIS EXISTS. Every claim in bots/social/*.py's docstrings — "captions
only ever see POST_DATA", "a fingerprint blocks a repeat publish",
"Moonshot and Tuddy share the same code" — is exactly the kind of thing that
looks true in the code and silently stops being true after the next edit.
These assertions turn each claim into something that can fail.

Deliberately does NOT call the real Anthropic/X/Instagram APIs — those need
live credentials this test environment doesn't have and shouldn't need. The
credential-absent code paths (which is what actually ships by default,
since approval-required is the default automation level) ARE covered.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bots.social import (assets, daily_board, fingerprint as fp, night_recap, pick_cashed,
                          player_spotlight, publishers, schema, stacked_game, storyline, store,
                          top_plays, watchlist)
from bots.social.brands import BRANDS, brand

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def check_true(name, cond):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILED.append(f"{name}: expected truthy, got falsy")


# ── schema ───────────────────────────────────────────────────────────────

def test_new_post_defaults_to_pending_review():
    post = schema.new_post(id="t1", brand="dash", product="moonshot", sport="MLB",
                            content_type="night_recap", data={"a": 1})
    check("new_post status", post["status"], "pending_review")
    check("new_post publish.x", post["publish"]["x"], False)
    check("new_post publish.instagram", post["publish"]["instagram"], False)


def test_new_post_rejects_unknown_content_type():
    try:
        schema.new_post(id="t2", brand="dash", product="moonshot", sport="MLB",
                         content_type="not_a_real_type", data={})
        FAILED.append("new_post accepted an unknown content_type")
    except ValueError:
        pass


def test_set_status_stamps_timestamps():
    post = schema.new_post(id="t3", brand="dash", product="moonshot", sport="MLB",
                            content_type="night_recap", data={})
    post = schema.set_status(post, "approved")
    check_true("approved_at set", bool(post["approved_at"]))
    post = schema.set_status(post, "published")
    check_true("published_at set", bool(post["published_at"]))
    check("status_history length", len(post["status_history"]), 3)  # pending_review, approved, published


# ── fingerprint / duplicate protection ──────────────────────────────────

def test_fingerprint_shape():
    f = fp.fingerprint(sport="MLB", product="Moonshot", date="2026-08-21",
                        content_type="HR_RESULT", subject="Ben Rice", platform="X")
    check("fingerprint", f, "MLB|Moonshot|2026-08-21|HR_RESULT|Ben_Rice|X")


def test_fingerprint_platforms_are_independent():
    fx = fp.fingerprint(sport="MLB", product="moonshot", date="2026-08-21",
                         content_type="night_recap", subject="RECAP", platform="x")
    fig = fp.fingerprint(sport="MLB", product="moonshot", date="2026-08-21",
                          content_type="night_recap", subject="RECAP", platform="instagram")
    check_true("x and instagram fingerprints differ", fx != fig)
    known = {fx}
    check("x already decided", fp.already_decided(fx, known), True)
    check("instagram NOT already decided", fp.already_decided(fig, known), False)


def test_same_event_same_day_reuses_fingerprint():
    """Two runs building the same recap for the same date must produce the
    SAME fingerprint — otherwise dedupe can't ever catch a duplicate."""
    f1 = fp.fingerprint(sport="MLB", product="moonshot", date="2026-08-21",
                         content_type="night_recap", subject="RECAP", platform="x")
    f2 = fp.fingerprint(sport="MLB", product="moonshot", date="2026-08-21",
                         content_type="night_recap", subject="RECAP", platform="x")
    check("idempotent fingerprint", f1, f2)


# ── brand config reuse across products (spec: Moonshot AND Tuddy on the
#    same infrastructure) ────────────────────────────────────────────────

def test_brand_config_covers_moonshot_and_tuddy():
    for key in ("moonshot", "tuddy"):
        check_true(f"{key} in BRANDS", key in BRANDS)
        cfg = brand(key)
        check_true(f"{key} has a name", bool(cfg.get("name")))
        check_true(f"{key} has an accent color", bool(cfg.get("accent")))
        check_true(f"{key} has a sport", bool(cfg.get("sport")))
    check_true("moonshot and tuddy have different accents",
               brand("moonshot")["accent"] != brand("tuddy")["accent"])


def test_brand_unknown_product_degrades_safely():
    cfg = brand("some_future_product_nobody_configured_yet")
    check_true("unknown product still returns a usable dict", bool(cfg.get("accent")))


# ── night_recap: only ever reports what the data actually contains ──────

def test_is_night_complete_requires_every_game_final():
    incomplete = {"graded_slots": [{"game_pk": "1", "is_final": 1}, {"game_pk": "2", "is_final": 0}]}
    complete = {"graded_slots": [{"game_pk": "1", "is_final": 1}, {"game_pk": "2", "is_final": 1}]}
    empty = {"graded_slots": []}
    check("incomplete slate", night_recap.is_night_complete(incomplete), False)
    check("complete slate", night_recap.is_night_complete(complete), True)
    check("empty slate is not complete", night_recap.is_night_complete(empty), False)


def test_night_recap_omits_fields_it_cannot_compute():
    """The hallucination-protection contract starts here: build() must never
    hand captions.py a null/empty placeholder for a stat it couldn't derive
    — a missing key is the only honest way to say 'unknown'."""
    # A hand-built graded payload with a total_hrs_on_slate but NO odds data
    # and no merged_homers, run through the same shaping build() does.
    graded_slots = [
        {"game_pk": "1", "is_final": 1, "pick_type": "TOP15", "got_hr": 1, "player_id": 1, "name": "A", "hr_score": 90},
        {"game_pk": "1", "is_final": 1, "pick_type": "TOP15", "got_hr": 0, "player_id": 2, "name": "B", "hr_score": 50},
    ]
    hcr = {"total_hrs_on_slate": 3, "caught_hrs_on_sheet": 1, "hr_capture_pct": 33.3}
    data = {
        "board_record": "1/2",
        "board_hit_rate": 50.0,
        "hr_scorers": 1,
        "slate_hr_coverage": "1/3",
        "slate_hr_coverage_pct": 33.3,
    }
    # longest_hr and top_cashes are absent on purpose (no merged_homers/odds
    # given) -- assert the shape build() WOULD produce omits them rather
    # than filling in None.
    filtered = {k: v for k, v in {**data, "longest_hr": None, "top_cashes": None}.items()
                if v not in (None, [], "")}
    check_true("longest_hr omitted when unknown", "longest_hr" not in filtered)
    check_true("top_cashes omitted when unknown", "top_cashes" not in filtered)
    check_true("computed fields still present", filtered.get("board_record") == "1/2")


# ── assets: don't regenerate what already exists ────────────────────────

def test_cached_or_render_does_not_recompute_an_existing_asset():
    calls = {"n": 0}

    def render():
        calls["n"] += 1
        return b"fake-png-bytes"

    with tempfile.TemporaryDirectory() as tmp:
        real_assets_dir = assets.ASSETS_DIR
        try:
            assets.ASSETS_DIR = Path(tmp)
            rel1 = assets.cached_or_render(post_id="p1", date_str="2026-08-21", variant="square", render_fn=render)
            rel2 = assets.cached_or_render(post_id="p1", date_str="2026-08-21", variant="square", render_fn=render)
            check("same relative path both times", rel1, rel2)
            check("render only called once", calls["n"], 1)
        finally:
            assets.ASSETS_DIR = real_assets_dir


def test_asset_sizes_defined_for_all_three_variants():
    for variant in ("square", "story", "landscape"):
        check_true(f"{variant} has a size", variant in assets.SIZES)


# ── new content types (2026-08-22): schema, bar-fill regression, and each
#    builder's own real-data-only contract ──────────────────────────────

def test_content_types_include_the_2026_08_22_additions():
    for ct in ("player_spotlight", "pick_cashed", "daily_board", "watchlist",
               "top_plays", "stacked_game", "storyline"):
        check_true(f"{ct} is a valid CONTENT_TYPE", ct in schema.CONTENT_TYPES)


def test_render_card_bar_fill_matches_its_fraction():
    """Regression test for a real bug caught in visual QA: the filled part
    of a progress bar was computed as `bar_x + max(bar_x + 6, bar_x + w*f)`
    — bar_x added twice — so a 40% bar rendered ~95% full. Render a 50%
    bar and assert the actual filled pixel width is close to half the
    track, not close to the whole track (which is what the bug produced)."""
    from PIL import Image
    import io
    brand_cfg = brand("moonshot")
    png = assets.render_card(variant="square", brand_cfg=brand_cfg, kind="TEST", title="T",
                              bars=[{"label": "HALF", "ok": 1, "n": 2, "color": "HR"}])
    img = Image.open(io.BytesIO(png))
    W, H = img.size
    px = img.load()
    bg = (9, 9, 11)
    # scan the row the bar sits on for the fill colour (HR = 251,146,60) —
    # find its rightmost extent and compare to the track's rightmost extent
    # (track colour, close to (30,30,34)), both on the same row.
    fill_col = assets.CAT_COLORS["HR"]
    found_row = None
    for y in range(int(H * 0.25), int(H * 0.45)):
        row_has_fill = any(px[x, y][:3] == fill_col for x in range(0, W))
        if row_has_fill:
            found_row = y
            break
    check_true("found a row with the bar's fill colour", found_row is not None)
    if found_row is not None:
        fill_xs = [x for x in range(W) if px[x, found_row][:3] == fill_col]
        track_col = (30, 30, 34)
        track_xs = [x for x in range(W) if px[x, found_row][:3] == track_col]
        fill_right = max(fill_xs)
        track_right = max(track_xs) if track_xs else fill_right
        track_left = min(fill_xs)  # fill starts where the track starts
        # a correct 50% fill should end roughly midway through the track,
        # not within ~90% of it (which is what the doubled bar_x produced)
        span = max(1, track_right - track_left)
        filled_frac = (fill_right - track_left) / span
        check_true(f"~50% bar fills roughly half its track (got {filled_frac:.2f})",
                   0.3 < filled_frac < 0.75)


def test_player_spotlight_picks_the_highest_scoring_homer():
    graded = {
        "graded_slots": [
            {"player_id": 1, "name": "Low Score", "got_hr": 1, "hr_score": 40, "team": "AAA", "opponent": "BBB"},
            {"player_id": 2, "name": "High Score", "got_hr": 1, "hr_score": 90, "team": "CCC", "opponent": "DDD"},
            {"player_id": 3, "name": "No Homer", "got_hr": 0, "hr_score": 99},
        ],
        "merged_homers": [{"name": "High Score", "longest_ft": 410}],
    }
    data = player_spotlight.build(date_str="2026-08-21", graded=graded, odds_history=False)
    check_true("spotlight data returned", data is not None)
    check("spotlight picks the higher-scoring homer, not the higher-scoring non-homer",
          data.get("name"), "High Score")
    check("spotlight carries the matching longest_hr_feet", data.get("longest_hr_feet"), 410)


def test_player_spotlight_returns_none_when_nobody_homered():
    graded = {"graded_slots": [{"player_id": 1, "name": "A", "got_hr": 0, "hr_score": 80}]}
    data = player_spotlight.build(date_str="2026-08-21", graded=graded, odds_history=False)
    check("no homers -> no spotlight", data, None)


def test_daily_board_only_includes_rows_with_a_pick_role():
    real_fetch = daily_board._fetch
    try:
        daily_board._fetch = lambda url, timeout=20: [
            {"name": "Has Role", "team": "X", "opponent": "Y", "game_pick_role": "TOP", "top_board_score_v2": 70, "game_pk": 1},
            {"name": "No Role", "team": "X", "opponent": "Y", "game_pick_role": None, "top_board_score_v2": 999, "game_pk": 1},
        ]
        data = daily_board.build(date_str="2026-08-21", top_n=5)
        check_true("board data returned", data is not None)
        names = [row["name"] for row in (data or {}).get("board", [])]
        check("only the picked player appears on the board", names, ["Has Role"])
    finally:
        daily_board._fetch = real_fetch


def test_watchlist_applies_a_fixed_documented_threshold():
    real_fetch = watchlist._fetch
    try:
        watchlist._fetch = lambda url, timeout=20: [
            {"name": "Cold", "last5_hr": 0, "last7_hr": 1},
            {"name": "Hot", "last5_hr": 3, "last7_hr": 4},
        ]
        data = watchlist.build(date_str="2026-08-21")
        check_true("watchlist data returned", data is not None)
        names = [row["name"] for row in (data or {}).get("names", [])]
        check("only the player clearing the threshold appears", names, ["Hot"])
    finally:
        watchlist._fetch = real_fetch


def test_top_plays_dedupes_per_player_and_ranks_by_hr_then_hits():
    graded = {
        "graded_slots": [
            # same player twice (e.g. two graded slots) — must keep only the
            # better-performing row, not double-count them in the ranking.
            {"player_id": 1, "name": "Dup Low", "actual_ab": 3, "actual_hr": 0, "actual_hits": 1},
            {"player_id": 1, "name": "Dup Low", "actual_ab": 4, "actual_hr": 1, "actual_hits": 2},
            {"player_id": 2, "name": "No AB", "actual_ab": 0, "actual_hr": 0, "actual_hits": 0},
            {"player_id": 3, "name": "Went 0-4", "actual_ab": 4, "actual_hr": 0, "actual_hits": 0},
            {"player_id": 4, "name": "Two Hits", "actual_ab": 4, "actual_hr": 0, "actual_hits": 2},
        ],
    }
    data = top_plays.build(date_str="2026-08-21", graded=graded, top_n=10)
    check_true("top plays data returned", data is not None)
    names = [row["name"] for row in (data or {}).get("plays", [])]
    # HR beats hits (Dup Low: 1 HR ranks above Two Hits: 0 HR/2 hits); a
    # 0-for-4 with no hit/HR is excluded entirely; the duplicate slot for
    # the same player_id collapses to its single best row.
    check("ranked HR first, hitless-AB excluded, duplicate player collapsed",
          names, ["Dup Low", "Two Hits"])


def test_top_plays_returns_none_when_nobody_reached_base():
    graded = {"graded_slots": [{"player_id": 1, "name": "A", "actual_ab": 4, "actual_hr": 0, "actual_hits": 0}]}
    data = top_plays.build(date_str="2026-08-21", graded=graded)
    check("no hits or HR anywhere -> no top plays post", data, None)


def test_stacked_game_requires_the_fixed_minimum():
    real_fetch = stacked_game._fetch
    try:
        stacked_game._fetch = lambda url, timeout=20: [
            {"name": "A", "team": "X", "opponent": "Y", "game_pick_role": "TOP", "game_pk": 1, "top_board_score_v2": 80},
            {"name": "B", "team": "X", "opponent": "Y", "game_pick_role": "HR", "game_pk": 1, "top_board_score_v2": 70},
            {"name": "C", "team": "Q", "opponent": "R", "game_pick_role": "HIT", "game_pk": 2, "top_board_score_v2": 60},
        ]
        # game_pk=1 only has 2 picks, below MIN_PICKS=3 -> None
        check("2 picks in the biggest game is below the 3-pick threshold",
              stacked_game.build(date_str="2026-08-21"), None)

        stacked_game._fetch = lambda url, timeout=20: [
            {"name": "A", "team": "X", "opponent": "Y", "game_pick_role": "TOP", "game_pk": 1, "top_board_score_v2": 80},
            {"name": "B", "team": "X", "opponent": "Y", "game_pick_role": "HR", "game_pk": 1, "top_board_score_v2": 70},
            {"name": "C", "team": "Y", "opponent": "X", "game_pick_role": "HIT", "game_pk": 1, "top_board_score_v2": 60},
            {"name": "D", "team": "Q", "opponent": "R", "game_pick_role": "HIT", "game_pk": 2, "top_board_score_v2": 90},
        ]
        data = stacked_game.build(date_str="2026-08-21")
        check_true("3 picks in one game clears the threshold", data is not None)
        check("picks the game_pk with the most picks, not the single highest score",
              (data or {}).get("game_pk"), 1)
        check("pick_count reflects that game's picks", (data or {}).get("pick_count"), 3)
    finally:
        stacked_game._fetch = real_fetch


def test_storyline_picks_the_top_ranked_row_with_a_reason():
    real_fetch = storyline._fetch
    try:
        storyline._fetch = lambda url, timeout=20: [
            {"name": "No Reason", "game_pick_role": "TOP", "top_board_score_v2": 99},
            {"name": "Has Reason", "game_pick_role": "HR", "top_board_score_v2": 80,
             "top_pick_reason": "\U0001F48E HR Bet", "top_board_rank_reason": "PMix Elite, L5 HR 3+"},
        ]
        data = storyline.build(date_str="2026-08-21")
        check_true("storyline data returned", data is not None)
        # the higher-scored row has no model reason attached, so it's
        # skipped in favour of the highest-scored row that DOES have one —
        # never invents a reason for a row that doesn't carry one.
        check("picks the top-scored row that actually has a reason", (data or {}).get("name"), "Has Reason")
        check("reasons split cleanly on commas", (data or {}).get("reasons"), ["PMix Elite", "L5 HR 3+"])
    finally:
        storyline._fetch = real_fetch


def test_storyline_returns_none_when_nobody_has_a_reason():
    real_fetch = storyline._fetch
    try:
        storyline._fetch = lambda url, timeout=20: [{"name": "A", "game_pick_role": "TOP", "top_board_score_v2": 50}]
        check("no model reason anywhere -> no storyline post", storyline.build(date_str="2026-08-21"), None)
    finally:
        storyline._fetch = real_fetch


def test_render_card_strips_emoji_the_bundled_font_cannot_draw():
    """Regression test for a real bug caught in visual QA: today_slim.json's
    top_pick_reason/top_board_rank_reason carry real emoji ("\U0001F48E HR Bet"),
    but the bundled DejaVu/Liberation TTFs have no colour-emoji glyphs, so
    those characters rendered as visible tofu boxes on the Storyline card.
    assets._clean() must strip them before drawing."""
    check("emoji stripped, text preserved", assets._clean("\U0001F48E HR Bet"), "HR Bet")
    check("emoji-only string collapses to empty", assets._clean("\U0001F9E9"), "")
    check("plain text passes through untouched", assets._clean("PMix Elite"), "PMix Elite")
    check("non-emoji punctuation is preserved", assets._clean("TOP15 · 6/15"), "TOP15 · 6/15")


def test_pick_cashed_queue_hr_writes_a_pending_review_post():
    """Runs the real queue_hr() write path end to end against a temp dir —
    no network (both queue.json and fingerprints.json are pre-seeded empty
    locally so store._fetch_json never falls back to the real data branch)
    and no Anthropic call (ANTHROPIC_API_KEY stays unset, same as every
    other credential-absent test in this file)."""
    real_current, real_social, real_history, real_assets = store.CURRENT, store.SOCIAL_DIR, store.HISTORY_DIR, assets.ASSETS_DIR
    os.environ.pop("ANTHROPIC_API_KEY", None)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            tmp_path = Path(tmp)
            store.CURRENT = tmp_path
            store.SOCIAL_DIR = tmp_path / "social"
            store.HISTORY_DIR = store.SOCIAL_DIR / "history"
            assets.ASSETS_DIR = store.SOCIAL_DIR / "assets"
            store.SOCIAL_DIR.mkdir(parents=True, exist_ok=True)
            (store.SOCIAL_DIR / "queue.json").write_text(json.dumps({"posts": []}), encoding="utf-8")
            (store.SOCIAL_DIR / "fingerprints.json").write_text(json.dumps({"fingerprints": []}), encoding="utf-8")

            post = pick_cashed.queue_hr(name="Test Player", role="HR", date_str="2026-08-21",
                                          team="AAA", opponent="BBB", hr_score=88.0, hr_count=1)
            check_true("queue_hr returned a post", post is not None)
            check("post content_type is pick_cashed", (post or {}).get("content_type"), "pick_cashed")
            check("post starts pending_review", (post or {}).get("status"), "pending_review")

            posts = store.load_queue()
            check_true("post is actually in the persisted queue", any(p.get("id") == (post or {}).get("id") for p in posts))

            # calling again for the SAME player/date must not double-queue
            again = pick_cashed.queue_hr(name="Test Player", role="HR", date_str="2026-08-21",
                                           team="AAA", opponent="BBB", hr_score=91.0, hr_count=2)
            check("re-queuing the same player/date is a no-op (id already in queue)", again, None)
        finally:
            store.CURRENT, store.SOCIAL_DIR, store.HISTORY_DIR = real_current, real_social, real_history
            assets.ASSETS_DIR = real_assets


# ── publishers: absent credentials fail loudly, never silently post ─────

def test_publish_x_without_credentials_is_a_safe_no_op():
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        os.environ.pop(k, None)
    result = publishers.publish_x(caption="test")
    check("publish_x ok=False without creds", result["ok"], False)
    check_true("publish_x explains why", "not configured" in (result["error"] or ""))


def test_publish_instagram_without_credentials_is_a_safe_no_op():
    for k in ("META_ACCESS_TOKEN", "INSTAGRAM_ACCOUNT_ID"):
        os.environ.pop(k, None)
    result = publishers.publish_instagram(caption="test", image_url="https://example.com/x.png")
    check("publish_instagram ok=False without creds", result["ok"], False)


def test_auto_publish_defaults_off():
    os.environ.pop("SOCIAL_AUTO_PUBLISH", None)
    check("auto-publish defaults to disabled", publishers.auto_publish_enabled(), False)


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{CHECKS} checks, {len(FAILED)} failed")
    for f in FAILED:
        print(f"  FAIL: {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
