#!/usr/bin/env python3
"""
MLB HR Dashboard — Streamlit front end.

Replaces the Next.js/Vercel site. Reads the same JSON the bot already
publishes, so nothing about the scoring model changes.

How data reaches this app
-------------------------
GitHub Actions runs bots/mlb_dashboard.py on a schedule, writes the slate
into public/data/, then force-pushes a single-commit `data` branch. This app
fetches those files straight off that branch over HTTPS and caches them for
5 minutes -- so new slates appear without a redeploy, and the repo Streamlit
clones stays a few MB instead of 20 GB.

Local files (public/data/current/...) win when present, which makes local
development work with no network.

Run locally:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
import streamlit as st

# ── CONFIG ──────────────────────────────────────────────────────────────────
# Which repo's `data` branch to read. Override without editing this file by
# adding one line to Streamlit → Settings → Secrets:
#     GITHUB_REPO = "yourname/your-repo-name"
# That way naming the new repo something different can't break the app.
DEFAULT_REPO = "donthebuilder/MLB-HR-DASHBOARD-STREAMLIT"


def _cfg(key: str, default: str) -> str:
    try:
        return str(st.secrets.get(key, "") or default)
    except Exception:
        return default


GITHUB_REPO = _cfg("GITHUB_REPO", DEFAULT_REPO)
DATA_BRANCH = _cfg("DATA_BRANCH", "data")
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{DATA_BRANCH}"
CACHE_TTL = 300  # seconds

REPO_ROOT = Path(__file__).resolve().parent
LOCAL_DATA = REPO_ROOT / "public" / "data"

st.set_page_config(
    page_title="MLB HR Dashboard",
    page_icon="⚾",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .block-container {padding-top: 1.6rem; padding-bottom: 3rem;}
      [data-testid="stMetricValue"] {font-size: 1.5rem;}
      .pick-card {
        border: 1px solid rgba(250,250,250,.15);
        border-radius: 10px;
        padding: .75rem 1rem;
        margin-bottom: .6rem;
        background: rgba(255,255,255,.03);
      }
      .pill {
        display:inline-block; padding:2px 9px; margin:2px 4px 2px 0;
        border-radius:999px; font-size:.72rem; font-weight:600;
        background:rgba(255,255,255,.10); border:1px solid rgba(255,255,255,.15);
      }
      .muted {opacity:.72; font-size:.85rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ── LOADERS ─────────────────────────────────────────────────────────────────
def _github_headers() -> Dict[str, str]:
    """Private-repo support: put GITHUB_TOKEN in Streamlit secrets if needed."""
    token = ""
    try:
        token = st.secrets.get("GITHUB_TOKEN", "")
    except Exception:
        token = ""
    return {"Authorization": f"token {token}"} if token else {}


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_bytes(rel_path: str) -> Optional[bytes]:
    """rel_path is relative to repo root, e.g. 'public/data/current/today_slim.json'."""
    local = REPO_ROOT / rel_path
    if local.exists() and local.stat().st_size > 0:
        return local.read_bytes()
    try:
        resp = requests.get(f"{RAW_BASE}/{rel_path}", headers=_github_headers(), timeout=45)
        if resp.status_code == 200:
            return resp.content
    except Exception:
        return None
    return None


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_json(rel_path: str) -> Any:
    raw = load_bytes(rel_path)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_text(rel_path: str) -> Optional[str]:
    raw = load_bytes(rel_path)
    return raw.decode("utf-8", errors="replace") if raw else None


@st.cache_data(ttl=CACHE_TTL, show_spinner="Loading slate…")
def load_slate(label: str) -> pd.DataFrame:
    """Slim file first (~3 MB); fall back to the full file only if slim is absent."""
    for rel in (
        f"public/data/current/{label}_slim.json",
        f"public/data/current/{label}.json",
        f"public/data/{label}.json",
    ):
        payload = load_json(rel)
        if payload is None:
            continue
        rows = payload
        if isinstance(payload, dict):
            rows = payload.get("players") or payload.get("rows") or []
        if isinstance(rows, list) and rows:
            return pd.DataFrame(rows)
    return pd.DataFrame()


def num(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        return float(val)
    except Exception:
        return default


def col(df: pd.DataFrame, name: str, default: Any = None) -> pd.Series:
    """Safe column access — the bot's schema drifts between versions."""
    if name in df.columns:
        return df[name]
    return pd.Series([default] * len(df), index=df.index)


def pills(tags: Any, limit: int = 8) -> str:
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    if not isinstance(tags, list):
        return ""
    return "".join(f"<span class='pill'>{str(t)}</span>" for t in tags[:limit] if t)


def matchup_label(row: Dict[str, Any]) -> str:
    return f"{row.get('team', '?')} vs {row.get('opponent', '?')}"


# ── SIDEBAR ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚾ MLB HR Dashboard")
    slate = st.radio("Slate", ["today", "tomorrow"], horizontal=True, key="slate")
    if st.button("🔄 Refresh data", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    st.divider()

df = load_slate(slate)

if df.empty:
    st.error(
        f"No slate data found for **{slate}**.\n\n"
        f"The app looks for `public/data/current/{slate}_slim.json` locally, then on the "
        f"`{DATA_BRANCH}` branch of `{GITHUB_REPO}`. If the bot hasn't published yet today, "
        "check the **MLB HR Bot — Auto Run** workflow in GitHub Actions."
    )
    st.stop()

# Filters depend on the loaded slate, so they're built after the load.
with st.sidebar:
    teams = sorted({t for t in col(df, "team", "").tolist() if isinstance(t, str) and t})
    team_pick = st.multiselect("Team", teams, default=[])
    query = st.text_input("Search player", "")
    min_hr = st.slider("Min HR score", 0, 100, 0, step=5)
    confirmed_only = st.checkbox("Confirmed lineups only", value=False)
    st.divider()
    st.caption(f"{len(df)} players · cache refreshes every {CACHE_TTL // 60} min")

view = df.copy()
if team_pick:
    view = view[col(view, "team", "").isin(team_pick)]
if query:
    view = view[col(view, "name", "").astype(str).str.contains(query, case=False, na=False)]
if min_hr:
    view = view[pd.to_numeric(col(view, "hr_score", 0), errors="coerce").fillna(0) >= min_hr]
if confirmed_only and "lineup_confirmed" in view.columns:
    view = view[view["lineup_confirmed"] == True]  # noqa: E712

# ── HEADER ──────────────────────────────────────────────────────────────────
hdr_l, hdr_r = st.columns([3, 2])
with hdr_l:
    st.title(f"{slate.capitalize()}'s Slate")
    game_ct = col(df, "game_pk", None).nunique()
    st.caption(f"{game_ct} games · {len(df)} hitters scored")
with hdr_r:
    hr_series = pd.to_numeric(col(df, "hr_score", 0), errors="coerce").fillna(0)
    conf = col(df, "lineup_confirmed", False)
    m1, m2, m3 = st.columns(3)
    m1.metric("HR 80+", int((hr_series >= 80).sum()))
    m2.metric("HR 90+", int((hr_series >= 90).sum()))
    m3.metric("Confirmed", f"{int(pd.Series(conf).fillna(False).astype(bool).sum())}/{len(df)}")

tab_board, tab_games, tab_pairs, tab_pools, tab_results, tab_bot = st.tabs(
    ["🏆 Board", "🗓️ Games", "🎯 Pairs", "🧩 Pools", "✅ Results", "🤖 Bot Report"]
)

# ── BOARD ───────────────────────────────────────────────────────────────────
with tab_board:
    sort_options = {
        "HR score": "hr_score",
        "Board score (V2)": "top_board_score_v2",
        "Overall": "overall_score",
        "Hit score": "hit_score",
        "HRR score": "hrr_score",
        "Contact score": "contact_score",
        "HR due score": "hr_due_score",
    }
    c1, c2 = st.columns([2, 1])
    sort_by = c1.selectbox("Rank by", list(sort_options), index=0)
    top_n = c2.number_input("Show top", 5, 200, 25, step=5)
    key = sort_options[sort_by]

    ranked = view.copy()
    ranked["_sort"] = pd.to_numeric(col(ranked, key, 0), errors="coerce").fillna(0)
    ranked = ranked.sort_values("_sort", ascending=False).head(int(top_n))

    st.markdown("#### Top picks")
    for i, (_, r) in enumerate(ranked.head(12).iterrows(), start=1):
        row = r.to_dict()
        role = row.get("game_pick_role") or row.get("final_hr_role") or row.get("best_bet_type") or ""
        with st.container():
            st.markdown(
                f"<div class='pick-card'>"
                f"<b>{i}. {row.get('name', '?')}</b> "
                f"<span class='muted'>({row.get('team', '?')} vs {row.get('opponent', '?')} · "
                f"spot {row.get('lineup_spot', '-')}{' · ✅ confirmed' if row.get('lineup_confirmed') else ' · projected'})</span>"
                f"{('  <span class=pill>' + str(role) + '</span>') if role else ''}<br>"
                f"HR <b>{num(row.get('hr_score')):.0f}</b> · Board {num(row.get('top_board_score_v2')):.0f} · "
                f"HRW {num(row.get('hrw_score')):.0f} · DC {num(row.get('damage_conversion_score')):.0f} · "
                f"HR/PA {num(row.get('hr_per_pa')):.3f} · Season HR {int(num(row.get('season_hr')))}<br>"
                f"<span class='muted'>vs {row.get('pitcher_name', 'TBD')} "
                f"({row.get('pitcher_throws', '?')}) · HR/9 {num(row.get('pitcher_hr9')):.2f} · "
                f"{row.get('matchup_label') or row.get('matchup_tier') or ''}</span><br>"
                f"{pills(row.get('top_board_tags') or row.get('signal_pills'))}"
                f"<div class='muted'>{row.get('hr_reason') or row.get('top_pick_reason') or ''}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    st.markdown("#### Full board")
    table_cols = [
        "name", "team", "opponent", "lineup_spot", "lineup_confirmed", "bats",
        "hr_score", "top_board_score_v2", "hrw_score", "hit_score", "hrr_score",
        "contact_score", "overall_score", "season_hr", "hr_per_pa", "pa_per_hr",
        "last5_hr", "games_since_last_hr", "pitcher_name", "pitcher_throws",
        "pitcher_hr9", "matchup_tier", "park_hr_factor", "weather_label",
    ]
    present = [c for c in table_cols if c in ranked.columns]
    st.dataframe(ranked[present], width="stretch", hide_index=True, height=520)

    st.download_button(
        "⬇️ Download board as CSV",
        ranked[present].to_csv(index=False).encode("utf-8"),
        file_name=f"mlb_{slate}_board.csv",
        mime="text/csv",
    )

# ── GAMES ───────────────────────────────────────────────────────────────────
with tab_games:
    if "game_pk" not in view.columns:
        st.info("No game grouping available in this payload.")
    else:
        games = view.copy()
        games["_hr"] = pd.to_numeric(col(games, "hr_score", 0), errors="coerce").fillna(0)
        order = games.groupby("game_pk")["_hr"].max().sort_values(ascending=False).index.tolist()

        for gpk in order:
            g = games[games["game_pk"] == gpk].sort_values("_hr", ascending=False)
            head = g.iloc[0].to_dict()
            title = (
                f"{matchup_label(head)} · {head.get('venue_name', '')} · "
                f"top HR {num(head.get('hr_score')):.0f}"
            )
            with st.expander(title, expanded=False):
                e1, e2, e3, e4 = st.columns(4)
                e1.metric("Temp", f"{num(head.get('weather_temp_f')):.0f}°F" if head.get("weather_temp_f") else "—")
                e2.metric("Wind", f"{num(head.get('weather_wind_mph')):.0f} mph" if head.get("weather_wind_mph") else "—")
                e3.metric("Park HR", f"{num(head.get('park_hr_factor'), 1):.2f}")
                e4.metric("Roof", str(head.get("roof") or "—"))
                if head.get("weather_label"):
                    st.caption(str(head.get("weather_label")))

                gcols = [c for c in [
                    "name", "team", "lineup_spot", "game_pick_role", "hr_score",
                    "hit_score", "hrr_score", "contact_score", "season_hr",
                    "pitcher_name", "pitcher_throws", "hr_reason",
                ] if c in g.columns]
                st.dataframe(g[gcols].head(20), width="stretch", hide_index=True)

# ── PAIRS ───────────────────────────────────────────────────────────────────
pair_payload = load_json("public/data/current/pair_builder_latest.json") or {}

with tab_pairs:
    pairs = pair_payload.get("recommended_pairs") or []
    if not pairs:
        st.info("No pair builder output published yet for this slate.")
    else:
        st.caption(
            f"Pair Builder · {pair_payload.get('date', '')} · "
            f"{pair_payload.get('role', '')} · {len(pairs)} pairs"
        )
        for p in pairs:
            names = " + ".join(str(pl.get("name", "?")) for pl in (p.get("players") or []))
            with st.container():
                st.markdown(
                    f"<div class='pick-card'><b>{p.get('type', 'PAIR')}</b> "
                    f"<span class='muted'>· score {num(p.get('pair_score')):.1f} · risk {p.get('risk', '—')}</span><br>"
                    f"<b>{names}</b><br>{pills(p.get('tags'))}"
                    f"<div class='muted'>{p.get('reason', '')}</div></div>",
                    unsafe_allow_html=True,
                )
                pl = p.get("players") or []
                if pl:
                    pdf = pd.DataFrame(pl)
                    show = [c for c in [
                        "name", "team", "opponent", "lineup_spot", "hr_score",
                        "hrw_score", "season_hr", "hr_per_pa", "pitcher_name",
                        "pitcher_throws", "pitcher_hr9",
                    ] if c in pdf.columns]
                    st.dataframe(pdf[show], width="stretch", hide_index=True)

# ── POOLS ───────────────────────────────────────────────────────────────────
with tab_pools:
    p4 = pair_payload.get("pools_4man") or []
    p6 = pair_payload.get("pools_6man") or []
    if not p4 and not p6:
        st.info("No pools published yet for this slate.")
    for title, pools in (("4-man pools", p4), ("6-man pools", p6)):
        if not pools:
            continue
        st.markdown(f"#### {title}")
        for pool in pools:
            st.markdown(
                f"<div class='pick-card'><b>{pool.get('name', 'Pool')}</b> "
                f"<span class='muted'>· score {num(pool.get('pool_score')):.1f} · "
                f"risk {pool.get('risk', '—')} · {pool.get('size', len(pool.get('players') or []))} players</span><br>"
                f"{pills(pool.get('tags'))}"
                f"<div class='muted'>{pool.get('reason', '')}</div></div>",
                unsafe_allow_html=True,
            )
            pl = pool.get("players") or []
            if pl:
                pdf = pd.DataFrame(pl)
                show = [c for c in [
                    "name", "team", "opponent", "lineup_spot", "hr_score",
                    "hrw_score", "season_hr", "pitcher_name", "pitcher_hr9",
                ] if c in pdf.columns]
                st.dataframe(pdf[show], width="stretch", hide_index=True)

# ── RESULTS ─────────────────────────────────────────────────────────────────
with tab_results:
    which = st.radio("Results view", ["Live", "Final"], horizontal=True)
    rel = "public/data/current/results_live.json" if which == "Live" else "public/data/current/results_final.json"
    res = load_json(rel) or {}
    rows = res.get("results") or []

    if not rows:
        st.info(f"No {which.lower()} results published yet. The grading workflow runs after games finish.")
    else:
        rdf = pd.DataFrame(rows)
        st.caption(f"{res.get('label', '')} · {res.get('date', '')} · {len(rdf)} graded picks")

        hr_hits = int(pd.to_numeric(col(rdf, "got_hr", 0), errors="coerce").fillna(0).sum())
        hit_hits = int(pd.to_numeric(col(rdf, "got_base_hit", 0), errors="coerce").fillna(0).sum())
        xbh = int(pd.to_numeric(col(rdf, "got_xbh", 0), errors="coerce").fillna(0).sum())
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Picks graded", len(rdf))
        k2.metric("HRs hit", hr_hits)
        k3.metric("Base hits", hit_hits)
        k4.metric("XBH", xbh)

        if "grade" in rdf.columns:
            st.markdown("##### Grade breakdown")
            st.dataframe(
                rdf["grade"].value_counts().rename_axis("grade").reset_index(name="count"),
                width="stretch", hide_index=True,
            )

        show = [c for c in [
            "name", "team", "pick_type", "bet_type", "rank", "hr_score",
            "actual_hr", "actual_hits", "actual_tb", "actual_rbi", "actual_runs",
            "grade", "outcome_text", "game_status",
        ] if c in rdf.columns]
        st.dataframe(rdf[show], width="stretch", hide_index=True, height=520)

# ── BOT REPORT ──────────────────────────────────────────────────────────────
with tab_bot:
    txt = None
    for rel in (f"public/data/current/{slate}.txt", f"public/data/{slate}.txt"):
        txt = load_text(rel)
        if txt:
            break
    if not txt:
        st.info("No text report published for this slate yet.")
    else:
        st.download_button(
            "⬇️ Download report (.txt)",
            txt.encode("utf-8"),
            file_name=f"mlb_{slate}_report.txt",
            mime="text/plain",
        )
        find = st.text_input("Filter report lines (blank = full report)", "")
        if find:
            keep = [ln for ln in txt.splitlines() if find.lower() in ln.lower()]
            st.code("\n".join(keep) or "No matching lines.", language="text")
        else:
            st.code(txt, language="text")
