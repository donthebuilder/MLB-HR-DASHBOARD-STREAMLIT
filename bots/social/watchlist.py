"""Builds the Watchlist / Trend content object — players in today's slate
who are genuinely running hot, by their own last-5/last-7-game numbers.

Reads today_slim.json's last5_hr / last7_hr / last5_hits fields — real
per-player recent-game tallies the model already computed, not a
freshly-invented "trending" score. A player qualifies only by a fixed,
documented threshold (2+ HR in their last 5 games), so this list is
reproducible from the published file, not a vibe.
"""

from __future__ import annotations

import json
import urllib.request
import datetime as dt
from typing import Any

RAW = ("https://raw.githubusercontent.com/donthebuilder/"
       "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current")

MIN_LAST5_HR = 2  # threshold for "hot" — 2+ HR in the player's last 5 games


def _fetch(url: str, timeout: int = 20) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  · fetch failed for {url}: {e}")
        return None


def build(*, date_str: str | None = None, top_n: int = 6) -> dict[str, Any] | None:
    """Returns a Watchlist `data` dict, or None if today_slim.json isn't
    available or nobody in today's slate clears MIN_LAST5_HR."""
    date_str = date_str or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    rows = _fetch(f"{RAW}/today_slim.json")
    if not isinstance(rows, list) or not rows:
        print("  · today_slim.json unavailable or empty")
        return None

    hot = [r for r in rows if int(r.get("last5_hr") or 0) >= MIN_LAST5_HR]
    if not hot:
        print(f"  · nobody in today's slate has {MIN_LAST5_HR}+ HR in their last 5 games")
        return None

    hot.sort(key=lambda r: (-(int(r.get("last5_hr") or 0)), -(int(r.get("last7_hr") or 0))))
    top = hot[:top_n]

    names: list[dict[str, Any]] = []
    for r in top:
        names.append({
            "name": r.get("name"),
            "team": r.get("team"),
            "opponent": r.get("opponent"),
            "last5_hr": int(r.get("last5_hr") or 0),
            "last7_hr": int(r.get("last7_hr") or 0) if r.get("last7_hr") else None,
        })

    data: dict[str, Any] = {
        "date": date_str,
        "threshold": f"{MIN_LAST5_HR}+ HR in last 5 games",
        "count": len(hot),
        "names": names or None,
    }
    return {k: v for k, v in data.items() if v not in (None, [], "")}
