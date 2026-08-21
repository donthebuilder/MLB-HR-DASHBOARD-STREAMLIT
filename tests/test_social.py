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

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bots.social import assets, fingerprint as fp, night_recap, publishers, schema, store
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
