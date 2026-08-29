"""Builds the Track Record content object — the site's own pooled
proof-of-performance numbers (the same "TOP % · HIT % · HRR %" figures the
Boards tab's PROOF banners show), as one shareable card.

Reads backtest_summary.json's per_day tier breakdown and pools the raw
ok/n counts across every date it has — the exact same pooling backtest
report itself does, just recomputed here from the per-metric counts rather
than re-parsing the site's rendered percentages. Nothing here is invented:
every number is a straight sum of counts the nightly backtest job already
published.
"""

from __future__ import annotations

import json
import urllib.request
import datetime as dt
from typing import Any

RAW = ("https://raw.githubusercontent.com/donthebuilder/"
       "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")

# (tier, metric, display label) -- the four headline numbers the site's own
# PROOF banners lead with (see docs: "TOP 21.3% ... HIT 69.6% ... HRR 50.9%").
# Order matters: this is the order they render in on the card.
HEADLINE_TIERS = [
    ("TOP_15_BOARD", "HR", "TOP15 BOARD", "HR RATE"),
    ("TOP_PICKS", "HR", "TOP PICKS", "HR RATE"),
    ("HRR_PICKS", "2+ HRR", "HRR PICKS", "2+ RUNS RATE"),
    ("HIT_PICKS", "1+ Hit", "HIT PICKS", "1+ HIT RATE"),
]


def _fetch(url: str, timeout: int = 20) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  · fetch failed for {url}: {e}")
        return None


def build(*, date_str: str | None = None) -> dict[str, Any] | None:
    """Returns a Track Record `data` dict pooled across every date
    backtest_summary.json currently holds, or None if that file is
    unavailable or none of the headline tiers have any counts yet."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    summary = _fetch(f"{RAW}/backtest_summary.json")
    per_day = (summary or {}).get("per_day") if isinstance(summary, dict) else None
    if not isinstance(per_day, dict) or not per_day:
        print("  · backtest_summary.json unavailable or empty")
        return None

    pools: dict[tuple[str, str], list[int]] = {}
    for _day, day in per_day.items():
        for tier, t in (day.get("tiers") or {}).items():
            counts = t.get("metric_counts") or {}
            for metric, pair in counts.items():
                if not (isinstance(pair, list) and len(pair) == 2):
                    continue
                ok, n = pair
                key = (tier, metric)
                acc = pools.setdefault(key, [0, 0])
                acc[0] += int(ok or 0)
                acc[1] += int(n or 0)

    rows: list[dict[str, Any]] = []
    for tier, metric, label, sub in HEADLINE_TIERS:
        pair = pools.get((tier, metric))
        if not pair or not pair[1]:
            continue
        ok, n = pair
        rows.append({
            "label": label,
            "sub": sub,
            "ok": ok,
            "n": n,
            "pct": round(100.0 * ok / n, 1),
        })

    if not rows:
        print("  · none of the headline tiers have any pooled counts yet")
        return None

    data: dict[str, Any] = {
        "date": date_str,
        "days": len(per_day),
        "rows": rows,
    }
    return {k: v for k, v in data.items() if v not in (None, [], "")}
