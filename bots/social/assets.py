"""Server-side card rendering — the headless equivalent of the site's
components/shareCard.js, redesigned (2026-08-22) around the same visual
language as the Discord "night receipts" card in live_results_tracker.py's
_render_night_card() — Donovan's own reference for "this one I like": a thin
accent top bar, a dim eyebrow label, a big bold headline, rounded progress
bars with per-category colour instead of a plain stat list, and a clean
divider before the footer. render_recap_card() (the original, flat
label/value list) is kept only as a thin compatibility wrapper — every
content-type builder should call render_card() directly.

Still deliberately generic: callers pass a brand config (brands.py) and
plain data (label/value/ok/n strings and numbers) built by a content-type
builder, never sport-specific fields — the same function draws a Moonshot
night recap, a Tuddy pick-cashed alert, or a daily board.

Do not regenerate an asset that already exists for a given post id + variant
— see cached_or_render() — matching spec section 9 ("do not generate the
same image repeatedly if it already exists").
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .store import ASSETS_DIR

# The bundled DejaVu/Liberation TTFs (see _FONT_CANDIDATES_* below) have no
# colour-emoji glyphs, so any text carrying one — and several real fields do,
# e.g. today_slim.json's top_pick_reason ("💎 HR Bet") — renders as a tofu
# box (2026-08-22 visual QA on the Storyline card). Strip pictographic
# characters before drawing; the underlying `data` dict (and captions, which
# read from `data` separately) keeps the emoji intact, this only cleans what
# actually gets drawn onto the PNG.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF"
    "️"
    "]+",
    flags=re.UNICODE,
)


def _clean(text: Any) -> str:
    return " ".join(_EMOJI_RE.sub("", str(text)).split())

# W, H per variant. Square/landscape match common X/IG crops; story is 9:16.
SIZES = {
    "square": (1080, 1080),
    "story": (1080, 1920),
    "landscape": (1600, 900),
}

_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_FONT_CANDIDATES_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

# Category accent colours, lifted straight from live_results_tracker.py's
# _render_night_card CAT_COL — the exact palette Donovan pointed at as "I
# like how those look." Builders may pass any of these keys as a bar's
# `color`, or a raw brand accent hex.
CAT_COLORS = {
    "TOP": (252, 211, 77), "TOP15": (252, 211, 77),
    "HR": (251, 146, 60), "HIT": (96, 165, 250),
    "HRR": (34, 211, 238), "CONTACT": (167, 139, 250),
}


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    for path in (_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REG):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    # No TTF on this runner — degrade to PIL's bitmap default rather than
    # fail the whole asset step over a missing font package.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _fit_font(draw, text: str, max_width: int, start_size: int, *, bold: bool = True, min_size: int = 14):
    """Shrink the font until `text` fits max_width, down to min_size. A long
    player name ("Pete Crow-Armstrong") at a fixed huge size ran off the
    right edge of the card (2026-08-22 visual QA) — every headline-sized
    string (card title, big_stat value) is fit through this now instead of
    a single unconditional size."""
    size = start_size
    while size > min_size:
        f = _font(size, bold=bold)
        try:
            w = draw.textlength(text, font=f)
        except Exception:
            bbox = draw.textbbox((0, 0), text, font=f)
            w = bbox[2] - bbox[0]
        if w <= max_width:
            return f
        size -= 2
    return _font(min_size, bold=bold)


def _color(c, fallback):
    """Accept a CAT_COLORS key, a #hex string, an (r,g,b) tuple, or None."""
    if c is None:
        return fallback
    if isinstance(c, tuple):
        return c
    if isinstance(c, str) and c.startswith("#"):
        return _hex(c)
    return CAT_COLORS.get(str(c).upper(), fallback)


def render_card(
    *,
    variant: str,
    brand_cfg: dict[str, Any],
    kind: str,
    title: str,
    subtitle: str = "",
    bars: list[dict[str, Any]] | None = None,
    big_stat: dict[str, Any] | None = None,
    list_items: list[str] | None = None,
    footer_note: str | None = None,
) -> bytes:
    """The one card renderer every content type draws through.

    bars:       [{"label": "TOP15", "ok": 6, "n": 15, "color": "TOP15"}, ...]
                rounded progress-bar rows, night-receipts style.
    big_stat:   {"label": "PETE ALONSO", "value": "443 FT", "sub": "longest
                HR of the night"} — one big centered/left number block, for
                a single-subject card (pick cashed, player spotlight).
    list_items: ["Pete Crow-Armstrong — TOP/HR/CONTACT · score 82", ...] —
                checkmark list, for boards / top-cashes / watchlists.
    Sections stack in the order bars -> big_stat -> list_items, each one
    guarded to stop drawing rather than run into the footer band.
    """
    from PIL import Image, ImageDraw

    W, H = SIZES[variant]
    S = min(W, H)
    accent = _hex(brand_cfg.get("accent") or "#f97316")
    accent2 = _hex(brand_cfg.get("accent_secondary") or "#FCD34D")
    bg = (9, 9, 11)
    ink = (235, 235, 238)
    ink2 = (140, 140, 148)
    track = (30, 30, 34)
    divider = (40, 40, 44)
    green = (74, 222, 128)

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    # thin accent bar top edge — the night-receipts card's signature detail.
    bar_h = max(4, int(S * 0.006))
    d.rectangle((0, 0, W, bar_h), fill=accent)

    pad = int(S * 0.055)
    footer_h = int(S * 0.09)
    content_right = W - pad
    content_width = W - 2 * pad
    y = pad + bar_h

    # header: brand chip (monogram — DejaVu has no pictographic glyphs, see
    # the prior version of this file) + name, kind eyebrow at far right.
    chip_size = int(S * 0.055)
    d.rounded_rectangle((pad, y, pad + chip_size, y + chip_size),
                         radius=chip_size // 4, fill=accent)
    monogram = "".join(w[0] for w in str(brand_cfg.get("name", "D")).split()[:2]).upper() or "D"
    d.text((pad + chip_size / 2, y + chip_size / 2), monogram,
           font=_font(int(chip_size * 0.5), bold=True), fill=(255, 255, 255), anchor="mm")
    name_font = _font(int(S * 0.032), bold=True)
    d.text((pad + chip_size * 1.25, y + chip_size * 0.5), brand_cfg.get("name", "DASH"),
           font=name_font, fill=ink, anchor="lm")
    kind_font = _font(int(S * 0.02), bold=True)
    d.text((W - pad, y + chip_size * 0.5), _clean(kind).upper(),
           font=kind_font, fill=ink2, anchor="rm")
    y += chip_size + int(S * 0.045)

    # title + subtitle — the big bold headline, always one colour: ink, not
    # the accent, so it reads as content rather than chrome (accent is
    # reserved for the top/bottom bars and category colour now).
    title_clean = _clean(title)
    title_font = _fit_font(d, title_clean, content_width, int(S * 0.068), bold=True, min_size=int(S * 0.03))
    d.text((pad, y), title_clean, font=title_font, fill=ink, anchor="la")
    y += int(S * 0.08)
    if subtitle:
        sub_font = _font(int(S * 0.026))
        d.text((pad, y), _clean(subtitle), font=sub_font, fill=ink2, anchor="la")
        y += int(S * 0.045)
    y += int(S * 0.02)

    # bars — rounded progress rows, per-category colour, night-receipts style
    if bars:
        label_font = _font(int(S * 0.028), bold=True)
        sub_font = _font(int(S * 0.017))
        count_font = _font(int(S * 0.032), bold=True)
        pct_font = _font(int(S * 0.02))
        row_h = int(S * 0.1)
        bar_x = pad + int(S * 0.32)
        bar_w = int(S * 0.34)
        label_max_w = bar_x - pad - int(S * 0.015)
        for row in bars:
            if y + row_h > H - footer_h:
                break
            col = _color(row.get("color"), accent)
            label = _clean(row.get("label", ""))
            ok, n = row.get("ok"), row.get("n")
            row_label_font = _fit_font(d, label, label_max_w, int(S * 0.028), bold=True, min_size=int(S * 0.016))
            d.text((pad, y + int(row_h * 0.08)), label, font=row_label_font, fill=col, anchor="la")
            if row.get("sub"):
                d.text((pad, y + int(row_h * 0.42)), _clean(row["sub"]), font=sub_font, fill=ink2, anchor="la")
            d.rounded_rectangle((bar_x, y + int(row_h * 0.12), bar_x + bar_w, y + int(row_h * 0.44)),
                                 radius=int(row_h * 0.14), fill=track)
            if isinstance(ok, (int, float)) and isinstance(n, (int, float)) and n:
                frac = max(0.0, min(1.0, ok / n))
                if frac > 0:
                    fill_end = max(bar_x + 6, int(bar_x + bar_w * frac))
                    d.rounded_rectangle((bar_x, y + int(row_h * 0.12), fill_end, y + int(row_h * 0.44)),
                                         radius=int(row_h * 0.14), fill=col)
                d.text((content_right, y), f"{int(ok)}/{int(n)}", font=count_font, fill=ink, anchor="ra")
                pct = 100.0 * frac
                d.text((content_right, y + int(row_h * 0.42)), f"{pct:.0f}%",
                       font=pct_font, fill=green if pct >= 50 else ink2, anchor="ra")
            elif row.get("value") is not None:
                d.text((content_right, y), _clean(row["value"]), font=count_font, fill=ink, anchor="ra")
            y += row_h

    # big_stat — one large centered-left number block for a single-subject card
    if big_stat:
        if y + int(S * 0.22) <= H - footer_h:
            y += int(S * 0.02)
            bs_label_font = _font(int(S * 0.024), bold=True)
            bs_value_clean = _clean(big_stat.get("value", ""))
            bs_value_font = _fit_font(d, bs_value_clean, content_width,
                                       int(S * 0.11), bold=True, min_size=int(S * 0.04))
            bs_sub_font = _font(int(S * 0.024))
            if big_stat.get("label"):
                d.text((pad, y), _clean(big_stat["label"]).upper(), font=bs_label_font, fill=accent2, anchor="la")
                y += int(S * 0.04)
            d.text((pad, y), bs_value_clean, font=bs_value_font, fill=ink, anchor="la")
            y += int(S * 0.14)
            if big_stat.get("sub"):
                d.text((pad, y), _clean(big_stat["sub"]), font=bs_sub_font, fill=ink2, anchor="la")
                y += int(S * 0.04)
        y += int(S * 0.02)

    # list_items — checkmark list (top cashes, board picks, watchlist names)
    if list_items:
        y += int(S * 0.01)
        d.line((pad, y, content_right, y), fill=divider, width=1)
        y += int(S * 0.03)
        item_font = _font(int(S * 0.026), bold=True)
        # No fixed item cap beyond what actually fits — the y-position guard
        # below stops before the footer band either way; a hardcoded slice
        # here (e.g. [:8]) would silently drop rows a taller card (or a
        # shorter title/no bars/no big_stat above it) had room for, which
        # under-delivered "top 10" content types like Top Plays.
        for i, line in enumerate(list_items[:12]):
            if y + int(S * 0.045) > H - footer_h:
                break
            rank = f"{i + 1}. " if len(list_items) > 1 else ""
            full = f"{rank}{_clean(line)}"
            text = full
            # Truncate with an ellipsis rather than let a long line run off
            # the card — daily_board / watchlist rows can be arbitrarily
            # long ("Name (TEAM vs OPP) — ROLE · score") and a fixed font
            # size means some just won't fit (2026-08-22 visual QA).
            while text and d.textlength(text, font=item_font) > content_width:
                text = text[:-1]
            if text != full and len(text) > 1:
                text = text[:-1] + "…"
            d.text((pad, y), text, font=item_font, fill=accent2, anchor="la")
            y += int(S * 0.045)

    # footer — divider + site url, then the accent bar again at the bottom
    foot_y = H - footer_h + int(footer_h * 0.35)
    d.line((pad, foot_y - int(S * 0.02), content_right, foot_y - int(S * 0.02)), fill=divider, width=1)
    foot_font = _font(int(S * 0.02), bold=True)
    if footer_note:
        d.text((pad, foot_y), _clean(footer_note), font=_font(int(S * 0.018)), fill=ink2, anchor="la")
    site = brand_cfg.get("site") or ""
    if site:
        d.text((content_right, foot_y), site, font=foot_font, fill=ink2, anchor="ra")
    d.rectangle((0, H - bar_h, W, H), fill=accent)

    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_recap_card(
    *,
    variant: str,
    brand_cfg: dict[str, Any],
    title: str,
    subtitle: str,
    stats: list[tuple[str, str]],
    highlights: list[str] | None = None,
) -> bytes:
    """Compatibility wrapper over render_card() for the original flat
    label/value shape — kept so nothing calling the pre-2026-08-22 signature
    breaks. New builders should call render_card() directly and pass real
    ok/n pairs as `bars` wherever the stat is a record (e.g. "6/15"), which
    render_card renders as a rounded progress bar instead of plain text."""
    bars = []
    plain_list = []
    for label, value in stats:
        ok = n = None
        sval = str(value)
        if "/" in sval:
            parts = sval.split("/", 1)
            if parts[0].strip().lstrip("-").isdigit() and parts[1].strip().split()[0].lstrip("-").isdigit():
                try:
                    ok = int(parts[0].strip())
                    n = int(parts[1].strip().split()[0])
                except Exception:
                    ok = n = None
        if ok is not None and n is not None:
            bars.append({"label": label, "ok": ok, "n": n})
        else:
            bars.append({"label": label, "value": value})
    return render_card(
        variant=variant, brand_cfg=brand_cfg, kind="RECAP", title=title, subtitle=subtitle,
        bars=bars, list_items=highlights,
    )


def cached_or_render(*, post_id: str, date_str: str, variant: str, render_fn) -> str:
    """Return the path (relative to public/data/current/, matching every
    other file this repo publishes) for this post+variant, writing the file
    only if it doesn't already exist. render_fn is a zero-arg callable
    returning PNG bytes, called lazily so an existing asset costs nothing
    but a stat() — spec section 9's "do not regenerate" rule. Use
    store.asset_url() to turn this into a fetchable https:// URL (needed by
    the Instagram adapter, which requires a public image URL)."""
    day_dir = ASSETS_DIR / date_str
    day_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{post_id}-{variant}.png"
    path = day_dir / fname
    rel = f"social/assets/{date_str}/{fname}"
    if path.exists() and path.stat().st_size > 0:
        return rel
    path.write_bytes(render_fn())
    return rel
