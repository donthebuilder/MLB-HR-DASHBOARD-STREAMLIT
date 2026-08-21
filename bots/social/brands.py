"""Brand configuration for the DASH Network social pipeline.

One dict per DASH product. Nothing downstream (captions, assets, the queue)
hard-codes "orange" or "Moonshot" — every consumer looks the product up by
key here and reads its name/accent/icon/hashtags off the config. Adding a
new product (NBA, UFC, tennis) means adding one row here, not touching
caption or asset code.
"""

from __future__ import annotations

BRANDS: dict[str, dict] = {
    "dash": {
        "name": "DASH Network",
        "sport": "",
        "accent": "#f97316",
        "accent_secondary": "#FCD34D",
        "icon": "📊",
        "site": "",
        "hashtags_x": [],
        "hashtags_instagram": [],
    },
    "moonshot": {
        "name": "Moonshot",
        "sport": "MLB",
        # same ember/orange language as components/shareCard.js on the site
        "accent": "#f97316",
        "accent_secondary": "#FCD34D",
        "icon": "🌙",
        "site": "moonshot-mlb.vercel.app",
        # X: no hashtag stuffing — see captions.py's platform instructions.
        "hashtags_x": [],
        "hashtags_instagram": ["#MLB", "#BaseballAnalytics", "#SportsBetting"],
    },
    "tuddy": {
        "name": "Tuddy",
        "sport": "NFL",
        "accent": "#16a34a",
        "accent_secondary": "#4ADE80",
        "icon": "🏈",
        "site": "",   # no dedicated Tuddy domain yet -- omit rather than show Moonshot's
        "hashtags_x": [],
        "hashtags_instagram": ["#NFL", "#FootballAnalytics", "#SportsBetting"],
    },
}


def brand(product: str) -> dict:
    """Look up a product's brand config. An unknown product gets a neutral
    fallback instead of a KeyError — a typo in a new content type should
    degrade to plain styling, not crash the workflow that calls it."""
    key = str(product or "").strip().lower()
    if key in BRANDS:
        return dict(BRANDS[key])
    return {
        "name": product or "DASH Network",
        "sport": "",
        "accent": "#6b7280",
        "accent_secondary": "#9ca3af",
        "icon": "📊",
        "site": "",
        "hashtags_x": [],
        "hashtags_instagram": [],
    }
