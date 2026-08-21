"""Claude as DASH's content-intelligence layer — never its data layer.

generate_captions() sends Claude exactly the post's `data` dict (already
sourced from a published DASH results/graded file — see night_recap.py) and
nothing else. Claude is told, in the system prompt AND by what it's simply
never given, that every fact it uses must come from that dict; there is no
odds feed, no box score, no player database in its context for it to reach
for instead. Structured output is enforced by forcing a single tool call
(tool_choice), not by asking nicely and parsing prose — a malformed or
missing field fails the call rather than shipping silently wrong.

Requires ANTHROPIC_API_KEY. Missing key or any API failure returns None;
the caller queues the post with status=pending_review and empty captions
rather than crash a grading/publishing run over a caption service being
down (spec section 18: publishing/content generation is downstream only).
"""

from __future__ import annotations

import json
import os
from typing import Any

CAPTION_TOOL = {
    "name": "submit_captions",
    "description": "Submit the generated social captions for this DASH post.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "Short internal headline for the queue UI, not posted anywhere."},
            "x_caption": {"type": "string", "description": "Caption for X/Twitter. Concise, no hashtag stuffing, natural sports-analytics language."},
            "instagram_caption": {"type": "string", "description": "Caption for Instagram. Slightly more descriptive, a strong first line, at most a few hashtags."},
            "story_text": {"type": "string", "description": "Very short (<= 12 words) text overlay for a 9:16 story graphic. Omit if nothing punchy fits."},
            "recommended_platforms": {
                "type": "array",
                "items": {"type": "string", "enum": ["x", "instagram"]},
                "description": "Which platforms this content is actually good for. Omit a platform if the content is too thin for it.",
            },
        },
        "required": ["headline", "x_caption", "instagram_caption", "recommended_platforms"],
    },
}

SYSTEM_PROMPT = """You write social captions for DASH Network, a sports-analytics/model brand \
(products include Moonshot for MLB and Tuddy for NFL).

HARD RULE — you will be given a JSON object called POST_DATA. Every specific \
number, name, odds price, score or result in your captions MUST come from \
POST_DATA. If a fact isn't in POST_DATA, you do not know it — do not invent \
it, estimate it, or round a number you weren't given. If POST_DATA is too \
thin for a good caption on some platform, say so by leaving that platform \
out of recommended_platforms rather than padding the caption with a made-up \
detail.

STYLE
- X: optimize naturally for discoverability around MLB/NFL, sports analytics, \
player props, betting models — using searchable terms in real sentences, not \
a hashtag list. Keep it concise. Do not keyword-stuff.
- Instagram: lead with a strong first sentence, can be slightly more \
descriptive, use at most a handful of hashtags if any, and only ones that fit \
naturally.
- Never sound robotic. Write like someone who actually built and watched the \
model, not a template filling in blanks.
- Do not use more emoji than the source content itself uses to describe \
results (a home run can get one 💣, a slate does not need five)."""


def _client():
    try:
        import anthropic  # local import: bots/requirements.txt only, not streamlit's light reqs
    except ImportError:
        print("  · anthropic package not installed — run `pip install -r bots/requirements.txt`")
        return None
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("  · ANTHROPIC_API_KEY is unset — captions will not be generated")
        return None
    return anthropic.Anthropic(api_key=key)


def generate_captions(post: dict[str, Any], *, brand_cfg: dict[str, Any]) -> dict[str, Any] | None:
    client = _client()
    if client is None:
        return None

    model = os.environ.get("SOCIAL_CAPTION_MODEL", "claude-sonnet-4-5")
    payload = {
        "brand": brand_cfg.get("name"),
        "sport": post.get("sport"),
        "content_type": post.get("content_type"),
        "instagram_hashtags_allowed": brand_cfg.get("hashtags_instagram") or [],
        "POST_DATA": post.get("data") or {},
    }

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[CAPTION_TOOL],
            tool_choice={"type": "tool", "name": "submit_captions"},
            messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        )
    except Exception as e:
        print(f"  · caption generation failed: {e}")
        return None

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_captions":
            out = dict(block.input or {})
            # Belt-and-suspenders: strip any field the model filled with an
            # empty/placeholder string rather than trust it produced nothing.
            out = {k: v for k, v in out.items() if v not in (None, "", [])}
            return out
    print("  · model responded without the expected tool call")
    return None
