"""Server-side card rendering — the headless equivalent of the site's
components/shareCard.js.

shareCard.js draws to an in-browser <canvas> and triggers a download; there
is no browser in a GitHub Actions runner, so this module renders the same
"dark field, one accent glow, big numbers" visual language with Pillow
instead (already a bot dependency — see bots/requirements.txt). It is
deliberately generic: callers pass a brand config (brands.py) and a list of
(label, value) stat rows built by the content-type builder (night_recap.py
for the first one), never sport-specific fields — the same function draws a
Moonshot recap or a Tuddy recap.

Do not regenerate an asset that already exists for a given post id + variant
— see cached_or_render() — matching spec section 9 ("do not generate the
same image repeatedly if it already exists").
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .store import ASSETS_DIR

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


def render_recap_card(
    *,
    variant: str,
    brand_cfg: dict[str, Any],
    title: str,
    subtitle: str,
    stats: list[tuple[str, str]],
    highlights: list[str] | None = None,
) -> bytes:
    from PIL import Image, ImageDraw

    W, H = SIZES[variant]
    # S is the sizing unit for fonts/spacing — min(W, H), not W. Square and
    # story share a width (1080) so this changes nothing for them, but
    # landscape (1600x900) is WIDER than it is tall, and every font/spacing
    # value below was originally W-scaled — on landscape that overflowed
    # the shorter height and drew the last stat row through the footer.
    S = min(W, H)
    accent = _hex(brand_cfg.get("accent") or "#f97316")
    accent2 = _hex(brand_cfg.get("accent_secondary") or "#FCD34D")
    bg = (10, 10, 13)
    ink = (244, 244, 245)
    ink2 = (161, 161, 170)

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    # a soft accent glow in the top-left corner, same "ember field" idea as
    # shareCard.js's radial gradients — approximated with concentric circles
    # since Pillow has no native radial-gradient fill.
    glow_r = int(W * 0.55)
    steps = 24
    for i in range(steps, 0, -1):
        r = int(glow_r * i / steps)
        alpha = 0.16 * (1 - i / steps)
        col = tuple(int(bg[j] + (accent[j] - bg[j]) * alpha) for j in range(3))
        d.ellipse((-r // 3, -r // 3, r, r), fill=col)

    pad = int(S * 0.055)
    footer_h = int(S * 0.12)  # reserved band at the bottom; nothing else may draw into it

    # header: brand chip + wordmark. A letter monogram, not the brand's
    # emoji icon — DejaVu (the font this renders with on a bare Actions
    # runner) has no pictographic glyphs, so an emoji here draws as a tofu
    # box. Same call components/shareCard.js already made for its own chip
    # ("HR" text, not an emoji) — see that file's header comment.
    chip = pad
    chip_size = int(S * 0.06)
    d.rounded_rectangle((chip, pad, chip + chip_size, pad + chip_size),
                         radius=chip_size // 4, fill=accent)
    monogram = "".join(w[0] for w in str(brand_cfg.get("name", "D")).split()[:2]).upper() or "D"
    icon_font = _font(int(chip_size * 0.5), bold=True)
    d.text((chip + chip_size / 2, pad + chip_size / 2), monogram,
           font=icon_font, fill=(255, 255, 255), anchor="mm")

    name_font = _font(int(S * 0.045), bold=True)
    d.text((chip + chip_size * 1.25, pad + chip_size * 0.5), brand_cfg.get("name", "DASH"),
           font=name_font, fill=ink, anchor="lm")

    sub_font = _font(int(S * 0.024))
    d.text((chip, pad + chip_size * 1.35), subtitle, font=sub_font, fill=ink2, anchor="la")

    y = pad + chip_size * 2.2

    # title
    title_font = _font(int(S * 0.06), bold=True)
    d.text((pad, y), title, font=title_font, fill=accent, anchor="la")
    y += int(S * 0.11)

    # stat rows — the flexible part every content type feeds. Guarded the
    # same way the highlights loop below is: stop drawing rather than run
    # into the reserved footer band, so a landscape card with a long stat
    # list truncates cleanly instead of overlapping the site URL.
    label_font = _font(int(S * 0.03))
    value_font = _font(int(S * 0.045), bold=True)
    row_h = int(S * 0.09)
    for label, value in stats:
        if y + row_h > H - footer_h:
            break
        d.text((pad, y), str(label), font=label_font, fill=ink2, anchor="la")
        d.text((W - pad, y - int(S * 0.006)), str(value), font=value_font, fill=ink, anchor="ra")
        y += row_h
        d.line((pad, y - int(row_h * 0.28), W - pad, y - int(row_h * 0.28)),
               fill=(255, 255, 255, 20), width=1)

    # highlight lines (e.g. top cashes) near the bottom of the card
    if highlights:
        y += int(S * 0.02)
        hl_font = _font(int(S * 0.026), bold=True)
        for line in highlights[:6]:
            if y + int(S * 0.045) > H - footer_h:
                break
            d.text((pad, y), f"✓ {line}", font=hl_font, fill=accent2, anchor="la")
            y += int(S * 0.045)

    # footer
    foot_font = _font(int(S * 0.022), bold=True)
    site = brand_cfg.get("site") or ""
    if site:
        d.text((W - pad, H - pad), site, font=foot_font, fill=ink2, anchor="rs")
    d.rectangle((0, H - 6, W, H), fill=accent)

    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
