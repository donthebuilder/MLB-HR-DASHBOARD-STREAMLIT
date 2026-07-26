#!/usr/bin/env python3
"""
MLB HR Dashboard — Streamlit front end.

Replaces the Next.js/Vercel site. Reads the same JSON the bot already
publishes, so nothing about the scoring model changes.

Feature parity note
-------------------
The scoring/role helpers below are a direct port of the old site's
lib/player.js and lib/scoring.js -- same field aliases, same thresholds, same
lane rules. That matters because the bot writes the same value under several
different key names depending on which pass produced it (hit_score vs
hit_shape_score vs base_hit_score, recent_375_num vs l20pa_375_num, ...).
Reading only the "obvious" key silently produces zeros on half the slate.

How data reaches this app
-------------------------
GitHub Actions runs bots/mlb_dashboard.py on a schedule, writes the slate
into public/data/, then force-pushes a single-commit `data` branch. This app
fetches those files over HTTPS and caches them for 5 minutes.

Heavy per-player logs (spray chart, pitch-type profile, pitcher arsenal) are
NOT in the main payload -- make_slim.py splits them into per-player files
under current/detail/, which this app fetches one at a time, on demand.

Run locally:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# ── CONFIG ──────────────────────────────────────────────────────────────────
# Override without editing this file via Streamlit → Settings → Secrets:
#     GITHUB_REPO = "yourname/your-repo-name"
DEFAULT_REPO = "donthebuilder/MLB-HR-DASHBOARD-STREAMLIT"

st.set_page_config(
    page_title="MLB HR Dashboard",
    page_icon="⚾",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _cfg(key: str, default: str) -> str:
    try:
        return str(st.secrets.get(key, "") or default)
    except Exception:
        return default


GITHUB_REPO = _cfg("GITHUB_REPO", DEFAULT_REPO)
DATA_BRANCH = _cfg("DATA_BRANCH", "data")
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{DATA_BRANCH}"
CACHE_TTL = 300
REPO_ROOT = Path(__file__).resolve().parent

# Palette matched to the trading terminal look: near-black chrome, a single
# green ramp for magnitude, red reserved for genuinely bad, orange and purple
# as the two accent lines (same roles they play on a chart's moving averages).
C = {
    "bg": "#0b0e11", "bg2": "#131722", "bg3": "#1b2130",
    "glass": "rgba(255,255,255,0.045)",
    "border": "rgba(255,255,255,0.09)", "border2": "rgba(255,255,255,0.16)",
    "text": "#d1d4dc", "text2": "#a3a6af", "text3": "#787b86",
    "orange": "#f5a623", "yellow": "#f5a623", "cyan": "#22d3ee",
    "green": "#26a65b", "red": "#ef5350", "purple": "#7b68ee", "blue": "#2962ff",
}

# Heat ramp: DARK green = low/bad, LIGHT green = high/good. One hue means the
# eye reads brightness as magnitude instead of trying to decode a rainbow.
GREEN_SCALE = [
    [0.00, "#06251a"],
    [0.25, "#0b4b30"],
    [0.50, "#12783f"],
    [0.75, "#4cb96a"],
    [1.00, "#b7f7c9"],
]
# Same ramp inverted, for metrics where a high number is bad for the hitter.
GREEN_SCALE_R = [[1 - stop, colr] for stop, colr in reversed(GREEN_SCALE)]

NUM_FONT = "'Roboto Mono','SF Mono','Cascadia Mono',Menlo,Consolas,monospace"

st.markdown(
    f"""
    <style>
      .block-container {{padding-top: 1.2rem; padding-bottom: 3rem; max-width: 1400px;}}

      /* Numbers in a mono face, as on the old site — keeps score columns
         aligned and stops digits from wobbling between rows. */
      [data-testid="stMetricValue"], .num {{
        font-family: {NUM_FONT}; font-size: 1.35rem; letter-spacing: -.02em;
      }}
      [data-testid="stMetricLabel"] {{
        text-transform: uppercase; letter-spacing: .06em;
        font-size: .68rem; color: {C['text3']};
      }}
      [data-testid="stMetric"] {{
        background: {C['bg2']}; border: 1px solid {C['border']};
        border-radius: 12px; padding: .6rem .8rem;
      }}

      h1 {{letter-spacing: -.02em; font-weight: 800;}}
      h4 {{color: {C['text2']}; font-size: .95rem; letter-spacing: .01em;}}

      .pick-card {{
        border: 1px solid {C['border']}; border-radius: 12px;
        padding: .7rem .95rem; margin-bottom: .55rem; background: {C['bg2']};
      }}
      .pill {{
        display:inline-block; padding:2px 9px; margin:2px 4px 2px 0;
        border-radius:999px; font-size:.7rem; font-weight:700;
        border:1px solid currentColor; font-family: {NUM_FONT};
      }}
      .muted {{color: {C['text3']}; font-size: .82rem;}}
      .grade {{font-weight:800; font-size:1.05rem; font-family: {NUM_FONT};}}

      /* Tabs: understated until active, then an orange underline. */
      .stTabs [data-baseweb="tab-list"] {{gap: 2px; border-bottom: 1px solid {C['border']};}}
      .stTabs [data-baseweb="tab"] {{
        height: 40px; padding: 0 14px; background: transparent;
        color: {C['text3']}; font-size: .86rem; font-weight: 600;
      }}
      .stTabs [aria-selected="true"] {{color: {C['text']}; border-bottom: 2px solid {C['orange']};}}

      .stDataFrame {{border: 1px solid {C['border']}; border-radius: 12px;}}
      section[data-testid="stSidebar"] {{background: {C['bg2']}; border-right: 1px solid {C['border']};}}
      .stExpander {{border: 1px solid {C['border']} !important; border-radius: 12px !important;}}
    </style>
    """,
    unsafe_allow_html=True,
)


# ── LOADERS ─────────────────────────────────────────────────────────────────
def _headers() -> Dict[str, str]:
    token = ""
    try:
        token = st.secrets.get("GITHUB_TOKEN", "")
    except Exception:
        token = ""
    return {"Authorization": f"token {token}"} if token else {}


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_json(rel_path: str) -> Any:
    local = REPO_ROOT / rel_path
    if local.exists() and local.stat().st_size > 0:
        try:
            return json.loads(local.read_text(encoding="utf-8"))
        except Exception:
            return None
    try:
        r = requests.get(f"{RAW_BASE}/{rel_path}", headers=_headers(), timeout=45)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_text(rel_path: str) -> Optional[str]:
    local = REPO_ROOT / rel_path
    if local.exists() and local.stat().st_size > 0:
        return local.read_text(encoding="utf-8", errors="replace")
    try:
        r = requests.get(f"{RAW_BASE}/{rel_path}", headers=_headers(), timeout=45)
        if r.status_code == 200:
            return r.text
    except Exception:
        return None
    return None


@st.cache_data(ttl=CACHE_TTL, show_spinner="Loading slate…")
def load_slate(label: str) -> List[Dict[str, Any]]:
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
            return rows
    return []


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_detail(kind: str, ident: Any) -> Dict[str, Any]:
    """One player's or pitcher's heavy logs. ~82 KB, fetched only on demand."""
    if ident in (None, ""):
        return {}
    return load_json(f"public/data/current/detail/{kind}_{ident}.json") or {}


# ── FIELD ACCESSORS (port of lib/player.js) ─────────────────────────────────
def n(v: Any, d: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else d
    except Exception:
        return d


def nn(p: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    """First key that holds a usable number. The bot's schema drifts by pass."""
    for k in keys:
        if k in p and p[k] not in (None, ""):
            try:
                x = float(p[k])
                if math.isfinite(x):
                    return x
            except Exception:
                continue
    return default


def txt(p: Dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default


def pct(v: Any) -> str:
    x = n(v, float("nan"))
    if not math.isfinite(x):
        return "—"
    return f"{round(x * 100 if x <= 1 else x)}%"


name_of = lambda p: txt(p, "name", "player", "player_name", default="Unknown")
team_of = lambda p: txt(p, "team", "team_abbr", "batting_team")
opp_of = lambda p: txt(p, "opponent", "opp", "pitcher_team")

hr_score = lambda p: nn(p, "hr_score")
hit_score = lambda p: nn(p, "hit_shape_score", "hit_score", "contact_hit_score", "base_hit_score")
prod_score = lambda p: nn(p, "production_shape_score", "hrr_score", "hrr_model_score", "run_rbi_score")
tb_score = lambda p: nn(p, "contact_shape_score", "contact_score", "tb_score", "total_base_score")
pmix_score = lambda p: nn(p, "pitch_mix_score", "pmix_score", "pitch_matchup_score", "pitch_fit_score")

ihr_val = lambda p: nn(p, "recent_ideal_hr_contact", "l20pa_ideal_hr_contact", "ideal_hr_contact")
avg_ev = lambda p: nn(p, "recent_ev", "avg_ev") or n((p.get("bbe_profile") or {}).get("avg_ev"))
max_ev = lambda p: nn(p, "max_ev") or n((p.get("bbe_profile") or {}).get("max_ev"))
barrel_rate = lambda p: nn(p, "recent_barrel_rate", "barrel_rate") or n((p.get("bbe_profile") or {}).get("barrel_rate"))
hard_hit = lambda p: nn(p, "recent_hard_hit_rate", "hard_hit_rate") or n((p.get("bbe_profile") or {}).get("hard_hit_rate"))
launch_angle = lambda p: nn(p, "recent_la", "avg_la", "l25pa_avg_la") or n((p.get("bbe_profile") or {}).get("avg_la"))
pull_rate = lambda p: nn(p, "recent_pull_rate", "pull_rate")

recent350 = lambda p: nn(p, "recent_350_num", "l20pa_350_num", "distance_350_num")
recent375 = lambda p: nn(p, "recent_375_num", "l20pa_375_num", "distance_375_num")
recent400 = lambda p: nn(p, "recent_400_num", "l20pa_400_num", "distance_400_num", "l25pa_400_plus")


def d350_rate(p: Dict[str, Any]) -> float:
    den = max(1.0, nn(p, "recent_350_den", "l20pa_bbe", "bbe_count", default=1.0))
    return recent350(p) / den


def low_sample(p: Dict[str, Any]) -> bool:
    pa = nn(p, "season_pa", "pa", "plate_appearances")
    den = max(1.0, nn(p, "recent_350_den", "l20pa_bbe", "bbe_count", default=1.0))
    return pa < 40 or den < 10


# ── SCORING / ROLES (port of lib/scoring.js) ────────────────────────────────
def role_raw(p: Dict[str, Any]) -> str:
    return txt(p, "pick_role", "beginner_label", "best_role", "role")


def explicit_trap(p: Dict[str, Any]) -> bool:
    r = role_raw(p).lower()
    return p.get("trap_flag") is True or any(w in r for w in ("avoid", "careful", "trap"))


def avoid_hr_candidate(p: Dict[str, Any]) -> bool:
    if explicit_trap(p):
        return True
    hr, hrr, hit, tb = hr_score(p), prod_score(p), hit_score(p), tb_score(p)
    ihr = ihr_val(p)
    pmix = nn(p, "pitch_mix_score", "pmix_score", "pitch_matchup_score", "pitch_fit_score", default=50.0)
    low_lift = 0 < ihr < 0.08 and recent375(p) == 0 and d350_rate(p) < 0.08
    better_other = max(hrr, hit, tb) >= hr + 14 and hr < 55
    k_risk = nn(p, "season_k_rate") >= 0.29 and hr < 60
    bad_pitch = 0 < pmix < 45 and hr < 55
    return low_lift or better_other or k_risk or bad_pitch


def compact_role(p: Dict[str, Any]) -> str:
    r = role_raw(p).lower()
    if avoid_hr_candidate(p):
        return "Avoid HR"
    if p.get("hidden_hr_value") or p.get("hidden_value_flag") or "hidden" in r:
        return "Value HR"
    if "strong" in r or "hr look" in r:
        return "HR"
    if "hrr" in r or "production" in r:
        return "HRR"
    if "hit" in r:
        return "Hit"
    if "contact" in r or "total" in r:
        return "TB"
    if hr_score(p) >= 55:
        return "HR"
    if prod_score(p) >= 60:
        return "HRR"
    if hit_score(p) >= 60:
        return "Hit"
    if tb_score(p) >= 60:
        return "TB"
    return "HR"


def tier_role(p: Dict[str, Any]) -> str:
    """Bot's conviction tier (🏆 HR Bet / 🔥 HR Lean / ...), not the type bucket."""
    return txt(p, "final_hr_role") or compact_role(p)


def tier_color(role: str) -> str:
    s = str(role or "")
    for token, key in (("🏆", "orange"), ("🔥", "orange"), ("🏁", "cyan"),
                       ("🔭", "purple"), ("💠", "blue"), ("⛔", "red")):
        if token in s:
            return C[key]
    return {"Value HR": C["purple"], "HRR": C["cyan"], "Hit": C["purple"],
            "TB": C["green"], "Avoid HR": C["red"]}.get(s, C["orange"])


def score_for(p: Dict[str, Any], kind: str = "hr") -> float:
    return {"hrr": prod_score, "hit": hit_score, "tb": tb_score}.get(kind, hr_score)(p)


def grade_for(p: Dict[str, Any], kind: str = "hr") -> str:
    s = score_for(p, kind)
    for cut, g in ((78, "A+"), (70, "A"), (62, "A-"), (54, "B+"), (46, "B")):
        if s >= cut:
            return g
    return "C+"


def is_aligned(p: Dict[str, Any]) -> bool:
    tags = p.get("top_board_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return any("🧩" in str(t) for t in tags)


def signal_pills(p: Dict[str, Any], kind: str = "hr") -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    def add(label: str, color: str = C["green"]) -> None:
        if label and not any(x["label"] == label for x in out):
            out.append({"label": label, "color": color})

    if p.get("trap_flag") and p.get("trap_reason"):
        r = str(p["trap_reason"]).lower()
        short = ("Low Arsenal" if "arsenal" in r else
                 "GB Pitcher" if ("gb" in r or "ground" in r) else
                 "High K" if "k rate" in r else
                 "Low Sample" if "sample" in r else "Trap")
        add(short, C["red"])
    elif avoid_hr_candidate(p) and kind == "hr":
        reasons = p.get("avoid_hr_reasons") or []
        r = str(reasons[0]).lower() if reasons else ""
        short = ("High K" if "k rate" in r else
                 "GB Pitcher" if ("gb" in r or "ground" in r) else
                 "Bad PMix" if "pitch" in r else
                 "Low Lift" if "lift" in r else None)
        if short:
            add(short, C["red"])

    l5hr = nn(p, "last5_hr")
    if l5hr >= 2:
        add(f"L5 {int(l5hr)}HR", C["orange"])
    elif p.get("hr_due_tag") == "Hot HR Form":
        add("Hot Form", C["orange"])

    if p.get("matchup_label") == "HR Attack":
        add("HR Attack", C["cyan"])
    elif p.get("pitcher_low_k_flag"):
        add("Low-K P", C["cyan"])
    elif p.get("weak_pitcher_flag"):
        add("Weak P", C["cyan"])

    if p.get("pitch_type_match_flag") and nn(p, "pitch_type_match_score") >= 80:
        note = str(p.get("pitch_type_match_note") or "")
        pitch = note.split("vs ")[1].split(":")[0].strip() if "vs " in note else ""
        add(f"PMix: {pitch}" if pitch else "PMix", C["cyan"])

    l5hh, l5pull = nn(p, "l5_hard_hit_rate"), nn(p, "l5_pull_rate")
    if l5hh >= 0.5:
        add(f"HH {round(l5hh * 100)}%", C["green"])
    elif l5pull >= 0.65:
        add(f"Pull {round(l5pull * 100)}%", C["green"])
    elif recent375(p) >= 1:
        add("375+", C["green"])
    elif hr_score(p) >= 55:
        add("Power", C["green"])
    elif pmix_score(p) >= 60:
        add("Pitch Fit", C["green"])

    if not out:
        add("Playable", C["text2"])
    return out[:3]


def risk_pill(p: Dict[str, Any], kind: str = "hr") -> Optional[Dict[str, str]]:
    if (avoid_hr_candidate(p) and kind == "hr") or p.get("trap_flag"):
        return None
    if low_sample(p):
        return {"label": "Low Sample", "color": C["yellow"]}
    if nn(p, "season_k_rate") >= 0.27:
        return None
    if nn(p, "lineup_spot") >= 7:
        return {"label": "Lower Order", "color": C["yellow"]}
    if kind == "hr" and prod_score(p) > hr_score(p) + 15:
        return {"label": "Better HRR", "color": C["cyan"]}
    return None


LANES = [
    ("all", "All"), ("strong", "Strong HR"), ("value", "Value"), ("due", "Due"),
    ("hot", "Hot"), ("target", "Weak Pitcher"), ("weather", "Weather/Park"),
    ("matchup", "Pitch Matchup"), ("aligned", "🧩 Aligned"), ("avoid", "Avoid HR"),
]


def lane_pass(p: Dict[str, Any], lane: str) -> bool:
    if lane == "all":
        return True
    role, hr = compact_role(p), hr_score(p)
    hrw, ihr = nn(p, "hrw_score"), ihr_val(p)
    hidden_fallback = (
        not avoid_hr_candidate(p) and hr < 55
        and (hrw >= 50 or ihr >= 0.1 or d350_rate(p) >= 0.1
             or recent375(p) >= 1 or pmix_score(p) >= 60 or nn(p, "last5_xbh") >= 2)
    )
    if lane == "strong":
        return role == "HR" and not avoid_hr_candidate(p)
    if lane == "value":
        return role == "Value HR" or hidden_fallback
    if lane == "due":
        return (not avoid_hr_candidate(p) and nn(p, "last5_hr") == 0 and hr >= 28
                and (ihr >= 0.08 or hrw >= 45 or d350_rate(p) >= 0.1))
    if lane == "hot":
        return nn(p, "last5_hr") >= 1 or nn(p, "last7_hr") >= 1 or nn(p, "last5_xbh") >= 2
    if lane == "target":
        return (p.get("weak_spot_flag") is True or nn(p, "pitcher_hr9") >= 1.2
                or nn(p, "pitcher_whip") >= 1.3 or nn(p, "pitcher_attack_score") >= 40)
    if lane == "weather":
        return nn(p, "park_factor", default=100.0) >= 105 or bool(txt(p, "weather_label"))
    if lane == "matchup":
        return pmix_score(p) >= 60 or bool(txt(p, "pitch_fit_summary"))
    if lane == "aligned":
        return is_aligned(p)
    if lane == "avoid":
        return p.get("true_avoid_hr") is True or avoid_hr_candidate(p)
    return True


# ── RENDER HELPERS ──────────────────────────────────────────────────────────
# Ported from GameTopPick.js / PlayerCard.js so the card UI matches the old
# site: role bubble + HRW timing bubble + mini stat line + tags + reason, with
# a coloured left border and the score set large on the right.
ROLE_MAP = {
    "🏆": ("HR Bet", "#f87171"),
    "🔥": ("HR Lean", "#f97316"),
    "🏁": ("HRR / XBH", "#22d3ee"),
    "💠": ("Contact", "#a78bfa"),
    "🔭": ("Power Watch", "#71717a"),
    "⛔": ("True Avoid", "#ef4444"),
}

# Each HRW band gets its own symbol. 80+ (volatile_hot) and 70-80
# (strong_capped) are deliberately different: the bot dampens 80+ as the less
# reliable of the two, so collapsing them into one icon would hide that.
HRW_MAP = {
    "volatile_hot": ("🌋", "#dc2626"),
    "strong_capped": ("🚀", "#f97316"),
    "sweet_spot": ("⚡", "#f59e0b"),
    "watch": ("🌤️", "#71717a"),
    "cold": ("🧊", "#60a5fa"),
}

GAME_ROLE_LABEL = {
    "TOP": ("🔥", "Top Pick", "#f97316"),
    "HR": ("🧨", "HR Pick", "#f87171"),
    "HIT": ("💠", "Hit Pick", "#a78bfa"),
    "HRR": ("🏁", "HRR Pick", "#22d3ee"),
    "CONTACT": ("⚾", "Contact Anchor", "#34d399"),
}


def role_config(p: Dict[str, Any]):
    raw = txt(p, "final_hr_role")
    if not raw:
        return None
    return ROLE_MAP.get(raw[0])


def bubble(emoji: str, label: str, color: str) -> str:
    return (
        f"<span style='display:inline-flex;align-items:center;gap:3px;font-size:10px;"
        f"font-weight:700;letter-spacing:.03em;background:{color}22;color:{color};"
        f"border:1px solid {color}55;border-radius:20px;padding:2px 8px;margin-right:4px;"
        f"white-space:nowrap;line-height:1.4'>{emoji} {label}</span>"
    )


def mini_stats(p: Dict[str, Any]) -> str:
    parts = []
    if nn(p, "last5_hr") > 0:
        parts.append(f"L5 {int(nn(p, 'last5_hr'))}HR")
    if nn(p, "hrw_score") > 0:
        parts.append(f"HRW {nn(p, 'hrw_score'):.0f}")
    if ihr_val(p) > 0:
        parts.append(f"IHR {ihr_val(p) * 100:.0f}%")
    if recent375(p) > 0:
        den = max(1, int(nn(p, "recent_350_den", default=1.0)))
        parts.append(f"375+ {int(recent375(p))}/{den}")
    return " · ".join(parts[:4])


def bar(label: str, value: float, maximum: float, color: str) -> str:
    w = max(0.0, min(100.0, (value / maximum) * 100 if maximum else 0.0))
    return (
        f"<div style='display:flex;align-items:center;gap:6px;margin-bottom:3px'>"
        f"<span style='width:44px;font-size:9px;color:{C['text3']};text-transform:uppercase'>{label}</span>"
        f"<div style='flex:1;height:5px;background:rgba(255,255,255,.07);border-radius:3px'>"
        f"<div style='width:{w}%;height:100%;background:{color};border-radius:3px'></div></div>"
        f"<span style='width:34px;font-size:10px;color:rgba(255,255,255,.72);text-align:right'>{value:.0f}</span>"
        f"</div>"
    )


def pills_html(items: List[Dict[str, str]]) -> str:
    return "".join(
        f"<span class='pill' style='color:{i['color']}'>{i['label']}</span>" for i in items
    )


def stat_table(pairs: List[tuple]) -> None:
    st.dataframe(
        pd.DataFrame([{"": k, " ": v} for k, v in pairs]),
        width="stretch", hide_index=True,
        height=min(400, 36 * len(pairs) + 38),
    )


# ── PLOTLY HELPERS ──────────────────────────────────────────────────────────
# One shared dark layout so every chart matches the theme instead of plotly's
# default white. Transparent backgrounds let the app's own panels show through.
def _layout(fig: "go.Figure", height: int = 320, title: str = "") -> "go.Figure":
    fig.update_layout(
        height=height,
        title=dict(text=title, font=dict(size=13, color=C["text2"])) if title else None,
        margin=dict(l=40, r=20, t=40 if title else 20, b=36),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=C["text2"], size=11),
        showlegend=False,
        xaxis=dict(gridcolor=C["border"], zerolinecolor=C["border"]),
        yaxis=dict(gridcolor=C["border"], zerolinecolor=C["border"]),
    )
    return fig


def radar(labels: List[str], values: List[float], color: str = "#f97316",
          title: str = "", height: int = 320, rng: float = 100.0,
          second: Optional[tuple] = None) -> None:
    """Closed-loop radar. `second` is an optional (label, values, colour) overlay
    so two profiles can be compared on the same axes."""
    fig = go.Figure()

    def rgba(hex_colr: str, alpha: float) -> str:
        h = hex_colr.lstrip("#")
        if len(h) != 6:
            return f"rgba(148,163,184,{alpha})"
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return f"rgba({r},{g},{b},{alpha})"

    def trace(vals, colr, nm):
        return go.Scatterpolar(
            r=list(vals) + [vals[0]], theta=labels + [labels[0]],
            fill="toself", name=nm,
            line=dict(color=colr, width=2),
            fillcolor=rgba(colr, 0.22),
        )

    fig.add_trace(trace(values, color, "This player"))
    if second:
        fig.add_trace(trace(second[1], second[2], second[0]))
        fig.update_layout(showlegend=True)
    fig.update_layout(
        polar=dict(
            bgcolor="rgba(0,0,0,0)",
            radialaxis=dict(visible=True, range=[0, rng], gridcolor=C["border"],
                            tickfont=dict(size=9, color=C["text3"])),
            angularaxis=dict(gridcolor=C["border"], tickfont=dict(size=10, color=C["text2"])),
        ),
    )
    _layout(fig, height, title)
    fig.update_layout(margin=dict(l=50, r=50, t=48 if title else 24, b=30))
    st.plotly_chart(fig, width="stretch")


def heatmap(df: pd.DataFrame, title: str = "", height: int = 340,
            fmt: str = "{:.0f}", reverse: bool = False) -> None:
    """Rows x columns matrix. Orange = hot (high) by default; set reverse when
    a high number is bad for the hitter (e.g. pitcher strikeout metrics)."""
    if df.empty:
        st.caption("Not enough data for this heatmap.")
        return
    scale = GREEN_SCALE_R if reverse else GREEN_SCALE
    fig = go.Figure(go.Heatmap(
        z=df.values, x=list(df.columns), y=list(df.index),
        colorscale=scale, showscale=True,
        colorbar=dict(thickness=10, tickfont=dict(size=9, color=C["text3"])),
        text=[[fmt.format(v) if pd.notna(v) else "" for v in row] for row in df.values],
        texttemplate="%{text}", textfont=dict(size=10, color="#dfe6e9"),
        hovertemplate="%{y} · %{x}: %{z:.2f}<extra></extra>",
    ))
    _layout(fig, height, title)
    st.plotly_chart(fig, width="stretch")


def candles(df: pd.DataFrame, date_col: str, val_col: str, title: str = "",
            height: int = 340, unit: str = "") -> None:
    """Candlesticks over a per-event value, grouped by date.

    Maps naturally onto batted-ball data: each day's candle opens at the
    first batted ball of that day, closes at the last, and the wick spans the
    day's weakest to hardest contact. A tall green candle is a day the hitter
    got hotter as it went; long upper wicks mean he had it in him.
    """
    d = df.dropna(subset=[val_col]).copy()
    if d.empty or date_col not in d.columns:
        st.caption("Not enough batted-ball data for a candlestick view.")
        return
    d = d.sort_values(date_col)
    g = d.groupby(date_col)[val_col]
    agg = pd.DataFrame({
        "open": g.first(), "close": g.last(), "high": g.max(), "low": g.min(),
        "n": g.count(),
    }).reset_index()
    if agg.empty:
        st.caption("Not enough batted-ball data for a candlestick view.")
        return
    fig = go.Figure(go.Candlestick(
        x=agg[date_col], open=agg["open"], high=agg["high"],
        low=agg["low"], close=agg["close"],
        increasing=dict(line=dict(color=C["green"]), fillcolor=C["green"]),
        decreasing=dict(line=dict(color=C["red"]), fillcolor=C["red"]),
        hovertext=[f"{n} batted ball{'s' if n != 1 else ''}" for n in agg["n"]],
    ))
    fig.update_layout(xaxis_rangeslider_visible=False)
    _layout(fig, height, title)
    fig.update_yaxes(title_text=unit)
    st.plotly_chart(fig, width="stretch")


def tags_html(tags: Any, limit: int = 6) -> str:
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    if not isinstance(tags, list):
        return ""
    return "".join(
        f"<span class='pill' style='color:{C['text2']}'>{t}</span>" for t in tags[:limit] if t
    )


def player_card(
    p: Dict[str, Any],
    rank: Optional[int] = None,
    kind: str = "hr",
    left_label: str = "",
    left_color: str = "",
) -> None:
    """Card matching the old site: coloured left rail, bubbles, bars, big score."""
    rc = role_config(p)
    role_label, role_color = rc if rc else (tier_role(p), tier_color(tier_role(p)))
    accent = left_color or role_color

    hrw_zone = txt(p, "hrw_zone")
    hrw = HRW_MAP.get(hrw_zone)
    weak = p.get("weak_spot_flag") is True

    bubbles = bubble(txt(p, "final_hr_role")[:1] or "•", role_label, role_color)
    if hrw:
        bubbles += bubble(hrw[0], f"HRW {nn(p, 'hrw_score'):.0f}", hrw[1])
    if weak:
        bubbles += bubble("⭐", "Weak Spot", "#f59e0b")
    if is_aligned(p):
        bubbles += bubble("🧩", "Aligned", "#a78bfa")
    if nn(p, "pitch_type_match_score") >= 80:
        bubbles += bubble("🎯", "Pitch Match", "#22d3ee")

    tags = p.get("top_board_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tag_html = "".join(
        f"<span style='font-size:9px;color:{C['text3']};background:{C['text3']}18;"
        f"border-radius:3px;padding:1px 5px;margin-right:4px'>{t}</span>"
        for t in tags[:4]
    )

    reason = txt(p, "simple_reason_1", "hr_reason", "top_pick_reason")
    if len(reason) > 110:
        reason = reason[:110] + "…"

    score = nn(p, "top_board_score_v2") or score_for(p, kind)
    conf = "✅ confirmed" if p.get("lineup_confirmed") else "◻︎ projected"
    head = f"{str(rank) + '. ' if rank else ''}{name_of(p)}"
    left = (
        f"<div style='font-size:9px;font-weight:700;letter-spacing:.06em;"
        f"color:{accent};white-space:nowrap;width:74px;flex-shrink:0'>{left_label}</div>"
        if left_label else ""
    )

    st.markdown(
        f"<div style='display:flex;align-items:center;gap:10px;padding:10px 14px;"
        f"margin-bottom:8px;background:rgba(255,255,255,.03);"
        f"border:1px solid rgba(250,250,250,.14);border-left:3px solid {accent};"
        f"border-radius:10px'>"
        f"{left}"
        f"<div style='flex:1;min-width:0'>"
        f"<div style='margin-bottom:5px'>"
        f"<span style='font-size:14px;font-weight:700'>{head}</span> "
        f"<span style='font-size:10px;color:{C['text3']}'>{team_of(p)} vs {opp_of(p)} · "
        f"spot {p.get('lineup_spot', '—')} · {conf}</span></div>"
        f"<div style='margin-bottom:6px'>{bubbles}</div>"
        f"{bar('HR', hr_score(p), 100, '#f97316')}"
        f"{bar('HRR', prod_score(p), 100, '#22d3ee')}"
        f"{bar('HIT', hit_score(p), 100, '#a78bfa')}"
        f"{bar('PMIX', pmix_score(p), 100, '#34d399')}"
        f"<div style='font-size:10px;color:{C['text3']};margin:4px 0 3px'>{mini_stats(p)}</div>"
        f"<div style='font-size:10px;color:{C['text3']};margin-bottom:3px'>"
        f"vs {txt(p, 'pitcher_name', default='TBD')} ({txt(p, 'pitcher_throws', default='?')}) · "
        f"HR/9 {nn(p, 'pitcher_hr9'):.2f} · WHIP {nn(p, 'pitcher_whip'):.2f}</div>"
        f"<div>{tag_html}</div>"
        f"<div style='font-size:10px;color:{C['text3']};font-style:italic;margin-top:3px'>{reason}</div>"
        f"</div>"
        f"<div style='text-align:right;flex-shrink:0'>"
        f"<div style='font-size:22px;font-weight:800;line-height:1'>{score:.0f}</div>"
        f"<div style='font-size:9px;color:{C['text3']}'>score</div>"
        f"<div style='font-size:13px;font-weight:800;color:{accent};margin-top:3px'>"
        f"{grade_for(p, kind)}</div></div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def rows_to_df(rows: List[Dict[str, Any]], cols: List[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    return df[[c for c in cols if c in df.columns]]


# ── SIDEBAR ─────────────────────────────────────────────────────────────────
if "slip" not in st.session_state:
    st.session_state.slip = []

# Watchlist persistence. The old site used localStorage, which Streamlit can't
# reach without a custom component, so the list lives in the URL query string
# instead: it survives reloads, and the URL can be bookmarked or sent to
# someone else with the same players already selected.
if "watch" not in st.session_state:
    raw = st.query_params.get("watch", "")
    st.session_state.watch = [w for w in raw.split("|") if w] if raw else []


def persist_watch() -> None:
    if st.session_state.watch:
        st.query_params["watch"] = "|".join(st.session_state.watch)
    elif "watch" in st.query_params:
        del st.query_params["watch"]

with st.sidebar:
    st.markdown("### ⚾ MLB HR Dashboard")
    slate = st.radio("Slate", ["today", "tomorrow"], horizontal=True, key="slate")
    if st.button("🔄 Refresh data", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    st.divider()

players = load_slate(slate)

if not players:
    st.error(
        f"No slate data found for **{slate}**.\n\n"
        f"Looked for `public/data/current/{slate}_slim.json` locally, then on the "
        f"`{DATA_BRANCH}` branch of `{GITHUB_REPO}`. If the bot hasn't published yet, "
        "check the **MLB HR Bot — Today** workflow in GitHub Actions."
    )
    st.stop()

with st.sidebar:
    teams = sorted({team_of(p) for p in players if team_of(p)})
    team_pick = st.multiselect("Team", teams)
    query = st.text_input("Search player / pitcher", "")
    lane_label = st.selectbox("Lane", [lbl for _, lbl in LANES])
    lane_key = next(k for k, lbl in LANES if lbl == lane_label)
    min_hr = st.slider("Min HR score", 0, 100, 0, step=5)
    confirmed_only = st.checkbox("Confirmed lineups only")
    aligned_only = st.checkbox("🧩 Aligned only")
    st.divider()

    # Auto-refresh. The old site polled every 45s while games were live and
    # every 5 min otherwise; live_mode is the same flag live_results_tracker.py
    # writes, so the cadence tracks reality instead of guessing.
    _res = load_json("public/data/current/results_live.json") or {}
    live_now = _res.get("live_mode") is True
    auto = st.checkbox("🔄 Auto-refresh", value=False,
                       help="45s while games are live, 5 min otherwise")
    if live_now:
        st.caption("🔴 Games in progress")

    if auto:
        # Deliberately a timed page reload rather than st.fragment: a fragment
        # that clears the cache and calls st.rerun re-enters itself and spins
        # the app in a tight loop (it hung the test harness outright). A plain
        # reload is bounded, and the 5-min cache TTL means the reload picks up
        # new bot output without hammering GitHub.
        interval_ms = (45 if live_now else 300) * 1000
        # st.html with unsafe_allow_javascript is the current API here --
        # st.components.v1.html is deprecated, and st.iframe only takes a src
        # URL, not inline markup.
        st.html(
            "<script>setTimeout(function(){window.parent.location.reload();},"
            f"{interval_ms});</script>",
            unsafe_allow_javascript=True,
        )

    if st.session_state.slip:
        st.markdown(f"**🎟️ Slip ({len(st.session_state.slip)})**")
        for item in st.session_state.slip:
            st.caption(f"• {item}")
        sc1, sc2 = st.columns(2)
        sc1.download_button("⬇️ Export", "\n".join(st.session_state.slip).encode(),
                            file_name=f"slip_{slate}.txt", mime="text/plain",
                            width="stretch")
        if sc2.button("Clear", width="stretch"):
            st.session_state.slip = []
            st.rerun()
    st.caption(f"{len(players)} players · cache {CACHE_TTL // 60} min")


def apply_filters(pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = pool
    if team_pick:
        out = [p for p in out if team_of(p) in team_pick]
    if query:
        q = query.lower()
        out = [p for p in out
               if q in f"{name_of(p)} {team_of(p)} {opp_of(p)} {txt(p, 'pitcher_name')}".lower()]
    if min_hr:
        out = [p for p in out if hr_score(p) >= min_hr]
    if confirmed_only:
        out = [p for p in out if p.get("lineup_confirmed")]
    if aligned_only:
        out = [p for p in out if is_aligned(p)]
    if lane_key != "all":
        out = [p for p in out if lane_pass(p, lane_key)]
    return out


view = apply_filters(players)

# ── HEADER ──────────────────────────────────────────────────────────────────
h1, h2 = st.columns([3, 2])
with h1:
    st.title(f"{slate.capitalize()}'s Slate")
    games = len({p.get("game_pk") for p in players})
    st.caption(f"{games} games · {len(players)} hitters · {len(view)} after filters")
with h2:
    hrs = [hr_score(p) for p in players]
    m = st.columns(4)
    m[0].metric("HR 80+", sum(1 for x in hrs if x >= 80))
    m[1].metric("HR 90+", sum(1 for x in hrs if x >= 90))
    m[2].metric("🧩 Aligned", sum(1 for p in players if is_aligned(p)))
    m[3].metric("Confirmed", sum(1 for p in players if p.get("lineup_confirmed")))

# Tab order matches the old site's lib/theme.js TABS list, so muscle memory
# carries over. Player is new: Streamlit has no modal, so what used to be
# PlayerModal is a tab instead.
(tab_games, tab_board, tab_hitshrr, tab_pitchers, tab_pairs, tab_bot,
 tab_pools, tab_scoreboard, tab_leaders, tab_results, tab_player,
 tab_watch, tab_spray, tab_guide) = st.tabs([
    "🗓️ Games", "🏆 HR Board", "💥 Hits & HRR", "⚾ Pitchers", "🎯 Pairs",
    "🤖 Bot", "🧩 Pools", "📊 Scoreboard", "🥇 Leaders", "✅ Results",
    "🔍 Player", "⭐ Watchlist", "💦 Spray", "📖 Guide",
])

# ── BOARD ───────────────────────────────────────────────────────────────────
with tab_board:
    c1, c2 = st.columns([2, 1])
    kind_label = c1.selectbox("Board type", ["HR", "HRR", "Hit", "TB (Contact)"])
    kind = {"HR": "hr", "HRR": "hrr", "Hit": "hit", "TB (Contact)": "tb"}[kind_label]
    top_n = c2.number_input("Show top", 5, 200, 25, step=5)

    ranked = sorted(view, key=lambda p: score_for(p, kind), reverse=True)[: int(top_n)]
    if not ranked:
        st.info("No players match these filters.")

    if ranked:
        v1, v2 = st.columns([3, 2])
        with v1:
            st.markdown(f"**Top 15 by {kind_label} score**")
            st.bar_chart(
                pd.DataFrame([{"Player": name_of(p), kind_label: round(score_for(p, kind), 1)}
                              for p in ranked[:15]]).set_index("Player"),
                height=300, color=C["orange"], horizontal=True,
            )
        with v2:
            st.markdown("**Score distribution — whole slate**")
            # Binned with pandas rather than st.plotly_chart: plotly isn't in
            # requirements.txt and pulling it in would slow every cold boot on
            # Streamlit Cloud just to draw one histogram.
            series = pd.Series([score_for(p, kind) for p in players])
            bins = pd.cut(series, bins=range(0, 105, 5), right=False)
            hist = bins.value_counts().sort_index()
            hist.index = [f"{int(iv.left)}" for iv in hist.index]
            st.bar_chart(pd.DataFrame({"players": hist}), height=300, color=C["cyan"])
            st.caption(f"median {series.median():.0f} · max {series.max():.0f}")

    for i, p in enumerate(ranked[:15], start=1):
        player_card(p, i, kind)

    if ranked:
        st.markdown("#### Full board")
        table = [{
            "Player": name_of(p), "Team": team_of(p), "Opp": opp_of(p),
            "Spot": p.get("lineup_spot"), "Role": tier_role(p),
            "Grade": grade_for(p, kind), "HR": round(hr_score(p), 1),
            "HRR": round(prod_score(p), 1), "Hit": round(hit_score(p), 1),
            "TB": round(tb_score(p), 1), "PMix": round(pmix_score(p), 1),
            "Damage": round(nn(p, "damage_conversion_score"), 1),
            "375+": int(recent375(p)), "IHR": round(ihr_val(p), 3),
            "SznHR": int(nn(p, "season_hr")), "Pitcher": txt(p, "pitcher_name"),
            "HR/9": round(nn(p, "pitcher_hr9"), 2),
        } for p in ranked]
        st.dataframe(pd.DataFrame(table), width="stretch", hide_index=True, height=480)
        st.download_button("⬇️ CSV", pd.DataFrame(table).to_csv(index=False).encode(),
                           file_name=f"mlb_{slate}_{kind}_board.csv", mime="text/csv")

# ── GAMES ───────────────────────────────────────────────────────────────────
with tab_games:
    by_game: Dict[Any, List[Dict[str, Any]]] = {}
    for p in view:
        by_game.setdefault(p.get("game_pk"), []).append(p)
    order = sorted(by_game.items(), key=lambda kv: max(hr_score(x) for x in kv[1]), reverse=True)

    # Slate-level view first: which games are worth attention at a glance.
    # Peaks (top HR / top HRR) say "is there a play here"; the averages across
    # every batter in the game say "is the whole lineup live or is it one guy",
    # which is what separates a real spot from a single outlier.
    if order:
        st.markdown("#### Slate at a glance")

        def med(vals: List[float]) -> float:
            vals = sorted(v for v in vals if math.isfinite(v))
            if not vals:
                return 0.0
            n = len(vals)
            return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

        def player_score(p: Dict[str, Any]) -> float:
            """One number per hitter: the median of his four HR-relevant scores.

            Median rather than mean on purpose -- a hitter with three strong
            marks and one weak one still reads strong, and a single inflated
            score can't drag the whole thing up on its own.
            """
            return med([
                hr_score(p), prod_score(p),
                nn(p, "hrw_score"), nn(p, "damage_conversion_score"),
            ])

        glance = []
        for _, gp in order:
            head = max(gp, key=hr_score)
            glance.append({
                "Game": f"{team_of(head)} vs {opp_of(head)}",
                # Median of every hitter's composite = the game's HR-chance
                # score. Reads "how live is this lineup", not "who's the one guy".
                "Game Score": round(med([player_score(x) for x in gp]), 1),
                "Batters": len(gp),
                "Top HR": round(max(hr_score(x) for x in gp), 1),
                "Top HRR": round(max(prod_score(x) for x in gp), 1),
                "Med HR": round(med([hr_score(x) for x in gp]), 1),
                "Med HRR": round(med([prod_score(x) for x in gp]), 1),
                "Med HRW": round(med([nn(x, "hrw_score") for x in gp]), 1),
                "Med DC": round(med([nn(x, "damage_conversion_score") for x in gp]), 1),
                "Pitcher": txt(head, "pitcher_name"),
                "P HR/9": round(nn(head, "pitcher_hr9"), 2),
                "Park HR": round(nn(head, "park_hr_factor", default=1.0), 2),
            })
        gdf = pd.DataFrame(glance).sort_values("Game Score", ascending=False)

        s = st.columns(6)
        s[0].metric("Games", len(gdf))
        s[1].metric("Best game score", f"{gdf['Game Score'].max():.1f}")
        s[2].metric("Slate median", f"{gdf['Game Score'].median():.1f}")
        s[3].metric("Top HR", f"{gdf['Top HR'].max():.0f}")
        s[4].metric("Top HRR", f"{gdf['Top HRR'].max():.0f}")
        s[5].metric("Med DC", f"{gdf['Med DC'].median():.1f}")

        metric_choice = st.radio(
            "Rank games by",
            ["Game Score", "Top HR", "Top HRR", "Med HR", "Med HRR", "Med HRW", "Med DC"],
            horizontal=True,
        )
        ranked_games = gdf.sort_values(metric_choice, ascending=False)
        st.bar_chart(
            ranked_games.set_index("Game")[[metric_choice]],
            height=320, color=C["orange"], horizontal=True,
        )
        st.caption(
            "Game Score = median across every hitter of that hitter's median "
            "HR / HRR / HRW / DC. Higher = more of the lineup is live for a homer."
        )

        # Heatmap: every game against every metric at once, so a game that's
        # strong across the board separates visually from one carried by a
        # single column.
        hm = ranked_games.set_index("Game")[
            ["Game Score", "Med HR", "Med HRR", "Med HRW", "Med DC", "Top HR", "Top HRR"]
        ]
        heatmap(hm, "Game x metric — hotter is better for hitters",
                height=max(280, 26 * len(hm) + 90), fmt="{:.0f}")

        st.dataframe(ranked_games, width="stretch", hide_index=True, height=min(520, 40 * len(gdf) + 40))

    st.divider()

    for gpk, gp in order:
        head = max(gp, key=hr_score)
        conf = "✅" if head.get("lineup_confirmed") else "◻︎"
        with st.expander(
            f"{conf}  {team_of(head)} vs {opp_of(head)} · {txt(head, 'venue_name')} · "
            f"top HR {hr_score(head):.0f} · {txt(head, 'pitcher_name')} "
            f"({txt(head, 'pitcher_throws')}) HR/9 {nn(head, 'pitcher_hr9'):.2f}"
        ):
            e = st.columns(6)
            e[0].metric("Temp", f"{nn(head, 'weather_temp_f'):.0f}°F" if head.get("weather_temp_f") else "—")
            e[1].metric("Wind", f"{nn(head, 'weather_wind_mph'):.0f} mph" if head.get("weather_wind_mph") else "—")
            e[2].metric("Park HR", f"{nn(head, 'park_hr_factor', default=1.0):.2f}")
            e[3].metric("Weather HR", f"{nn(head, 'weather_hr_effect_pct'):+.0f}%")
            e[4].metric("Roof", txt(head, "roof", default="—"))
            e[5].metric("Attack", txt(head, "pitcher_attack_tag", default="—"))
            for label, key in (("", "weather_label"), ("Wind: ", "wind_direction_label")):
                if txt(head, key):
                    st.caption(f"{label}{txt(head, key)}")

            # THE FIVE GAME PICKS — the bot stamps one player per game per role
            # (TOP / HR / HIT / HRR / CONTACT) in game_pick_role. This is the
            # same set the .txt report prints under each game header, and it
            # was the centrepiece of the old Games tab.
            picked = {}
            for p in gp:
                r = str(p.get("game_pick_role") or "").upper()
                if r in GAME_ROLE_LABEL and r not in picked:
                    picked[r] = p

            if picked:
                st.markdown("**Game picks**")
                for r in ("TOP", "HR", "HIT", "HRR", "CONTACT"):
                    if r in picked:
                        emoji, label, color = GAME_ROLE_LABEL[r]
                        player_card(picked[r], left_label=f"{emoji} {label.upper()}",
                                    left_color=color)
            else:
                st.caption("No stamped game picks for this game — showing top HR scores.")
                for p in sorted(gp, key=hr_score, reverse=True)[:4]:
                    player_card(p)

            with st.expander(f"Full lineup ({len(gp)})"):
                st.dataframe(pd.DataFrame([{
                    "Spot": p.get("lineup_spot"), "Player": name_of(p),
                    "Team": team_of(p), "B": txt(p, "bats", default="?"),
                    "Role": tier_role(p), "HR": round(hr_score(p), 1),
                    "HRR": round(prod_score(p), 1), "Hit": round(hit_score(p), 1),
                    "TB": round(tb_score(p), 1), "PMix": round(pmix_score(p), 1),
                    "⭐": "⭐" if p.get("weak_spot_flag") else "",
                } for p in sorted(gp, key=lambda x: nn(x, "lineup_spot", default=99.0))]),
                    width="stretch", hide_index=True)

# ── SCOREBOARD ──────────────────────────────────────────────────────────────
with tab_scoreboard:
    st.caption(f"{len(view)} batters · sortable — click any column header")
    board = [{
        "Player": name_of(p), "Team": team_of(p), "Opp": opp_of(p),
        "Role": tier_role(p), "HR": round(hr_score(p), 1),
        "Damage": round(nn(p, "damage_conversion_score"), 1),
        "PMatch": round(nn(p, "pitch_type_match_score"), 1),
        "HRR": round(prod_score(p), 1), "Hit": round(hit_score(p), 1),
        "TB": round(tb_score(p), 1), "PMix": round(pmix_score(p), 1),
        "375+": int(recent375(p)), "400+": int(recent400(p)),
        "IHR": round(ihr_val(p), 3), "K%": round(nn(p, "season_k_rate") * 100, 1),
        "Spot": p.get("lineup_spot"), "🧩": "🧩" if is_aligned(p) else "",
    } for p in view]
    st.dataframe(pd.DataFrame(board), width="stretch", hide_index=True, height=640)

# ── LEADERS ─────────────────────────────────────────────────────────────────
LEADER_STATS = {
    "HR Score": (hr_score, "{:.1f}"),
    "HRR Score": (prod_score, "{:.1f}"),
    "Hit Score": (hit_score, "{:.1f}"),
    "TB Score": (tb_score, "{:.1f}"),
    "Pitch Mix": (pmix_score, "{:.1f}"),
    "Damage Conversion": (lambda p: nn(p, "damage_conversion_score"), "{:.1f}"),
    "375+ count": (recent375, "{:.0f}"),
    "400+ count": (recent400, "{:.0f}"),
    "Ideal HR%": (lambda p: ihr_val(p) * 100, "{:.1f}%"),
    "Avg Exit Velo": (avg_ev, "{:.1f} mph"),
    "Max EV": (max_ev, "{:.1f} mph"),
    "Barrel %": (lambda p: barrel_rate(p) * 100, "{:.1f}%"),
    "Hard Hit %": (lambda p: hard_hit(p) * 100, "{:.1f}%"),
    "Pull %": (lambda p: pull_rate(p) * 100, "{:.1f}%"),
    "Launch Angle": (launch_angle, "{:.1f}°"),
    "Season HR": (lambda p: nn(p, "season_hr"), "{:.0f}"),
    "Season AVG": (lambda p: nn(p, "season_avg"), "{:.3f}"),
    "HR per PA": (lambda p: nn(p, "hr_per_pa"), "{:.4f}"),
}

with tab_leaders:
    l1, l2, l3, l4 = st.columns([2, 1, 1, 1])
    stat = l1.selectbox("Rank by", list(LEADER_STATS))
    min_pull = l2.number_input("Min Pull %", 0, 100, 0, step=5)
    min_la = l3.number_input("Min Launch°", 0, 45, 0, step=1)
    min_375 = l4.number_input("Min 375+", 0, 20, 0, step=1)

    getter, fmt = LEADER_STATS[stat]
    pool = [p for p in view
            if pull_rate(p) * 100 >= min_pull
            and launch_angle(p) >= min_la
            and recent375(p) >= min_375]
    # Everyone who has a value for this stat, not a top-N slice -- the table
    # below is the full board; only the chart is capped, because 200+ bars is
    # unreadable rather than informative.
    lead = sorted(((p, getter(p)) for p in pool), key=lambda x: x[1], reverse=True)
    lead = [(p, v) for p, v in lead if math.isfinite(v) and v > 0]

    if not lead:
        st.info("Not enough data for this leaderboard yet.")
    else:
        chart_n = st.slider("Players in chart", 10, min(60, len(lead)),
                            min(25, len(lead)), step=5)
        st.caption(f"{len(lead)} players ranked by {stat} — chart shows top {chart_n}, table shows all")
        st.bar_chart(
            pd.DataFrame([{"Player": f"{name_of(p)} ({team_of(p)})", stat: round(v, 3)}
                          for p, v in lead[:chart_n]]).set_index("Player"),
            height=max(320, 18 * chart_n), color=C["purple"], horizontal=True,
        )
        st.dataframe(pd.DataFrame([{
            "#": i, "Player": name_of(p) + (" 🧩" if is_aligned(p) else ""),
            "Team": f"{team_of(p)} vs {opp_of(p)}", "Role": tier_role(p),
            stat: fmt.format(v),
            "HR": round(hr_score(p), 1), "HRR": round(prod_score(p), 1),
            "Spot": p.get("lineup_spot"), "Pitcher": txt(p, "pitcher_name"),
        } for i, (p, v) in enumerate(lead, start=1)]),
            width="stretch", hide_index=True, height=620)

# ── PITCHERS ────────────────────────────────────────────────────────────────
def group_pitchers(pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Port of lib/data.js groupPitchers — one entry per starter with lineup."""
    by: Dict[Any, Dict[str, Any]] = {}
    for p in pool:
        pid = p.get("pitcher_id") or txt(p, "pitcher_name")
        if not pid:
            continue
        e = by.setdefault(pid, {
            "pitcher_id": p.get("pitcher_id"),
            "pitcher_name": txt(p, "pitcher_name", default="Unknown"),
            "throws": txt(p, "pitcher_throws", default="?"),
            "era": nn(p, "pitcher_era"), "hr9": nn(p, "pitcher_hr9"),
            "whip": nn(p, "pitcher_whip"), "k9": nn(p, "pitcher_k9"),
            "weak_side": txt(p, "pitcher_weak_side"),
            "xbh_lhb": p.get("pitcher_xbh_vs_lhb"), "xbh_rhb": p.get("pitcher_xbh_vs_rhb"),
            "attack": txt(p, "pitcher_attack_tag"),
            # The pitcher's own team is the batters' opponent, and vice versa.
            "team": opp_of(p), "facing": team_of(p),
            "venue": txt(p, "venue_name"), "game_time": p.get("game_time"),
            "confirmed": False, "lineup": [],
            # Keep one representative batter row: every pitcher_* field is
            # identical across the lineup facing him, so this is the cheapest
            # way to reach the full pitcher profile without a second lookup.
            "row": p,
        })
        if p.get("lineup_confirmed"):
            e["confirmed"] = True
        e["lineup"].append(p)
    for e in by.values():
        e["lineup"].sort(key=lambda x: nn(x, "lineup_spot", default=99.0))
        e["weak_spots"] = sum(1 for x in e["lineup"] if x.get("weak_spot_flag") is True)
    return sorted(by.values(), key=lambda e: (-e["weak_spots"], -e["hr9"]))


with tab_pitchers:
    pitchers = group_pitchers(view)
    if not pitchers:
        st.info("No pitcher data found yet.")
    else:
        sort_by = st.selectbox(
            "Sort by",
            ["Most weak spots", "Highest HR/9", "Highest WHIP", "Most hittable (attack)",
             "Worst barrel rate", "Game time"],
        )
        if sort_by == "Highest HR/9":
            pitchers.sort(key=lambda e: -e["hr9"])
        elif sort_by == "Highest WHIP":
            pitchers.sort(key=lambda e: -e["whip"])
        elif sort_by == "Most hittable (attack)":
            pitchers.sort(key=lambda e: -nn(e["row"], "pitcher_attack_score"))
        elif sort_by == "Worst barrel rate":
            pitchers.sort(key=lambda e: -nn(e["row"], "pitcher_barrel_allowed"))
        elif sort_by == "Game time":
            pitchers.sort(key=lambda e: str(e["game_time"] or ""))

        # Slate-wide comparison first: which starters are most attackable.
        pdf = pd.DataFrame([{
            "Pitcher": e["pitcher_name"],
            "Attack": round(nn(e["row"], "pitcher_attack_score"), 1),
            "HR/9": round(e["hr9"], 2),
            "WHIP": round(e["whip"], 2),
            "Barrel%": round(nn(e["row"], "pitcher_barrel_allowed") * 100, 1),
            "HardHit%": round(nn(e["row"], "pitcher_hardhit_allowed") * 100, 1),
            "Meatball%": round(nn(e["row"], "pitcher_meatball_pct") * 100, 1),
            "PullAir%": round(nn(e["row"], "pitcher_pullair_allowed_pct") * 100, 1),
            "Weak spots": e["weak_spots"],
        } for e in pitchers]).set_index("Pitcher")
        heatmap(pdf[["Attack", "Barrel%", "HardHit%", "Meatball%", "PullAir%"]],
                "Starter vulnerability — hotter is better for hitters",
                height=max(280, 26 * len(pdf) + 90))

        st.caption(f"{len(pitchers)} starters · expand for the full profile")
        for e in pitchers:
            r = e["row"]
            star = f" · ⭐ {e['weak_spots']} weak spot{'s' if e['weak_spots'] != 1 else ''}" if e["weak_spots"] else ""
            tag = txt(r, "pitcher_attack_tag")
            with st.expander(
                f"{e['pitcher_name']} ({e['throws']}HP) · {e['team']} vs {e['facing']} · "
                f"HR/9 {e['hr9']:.2f} · WHIP {e['whip']:.2f} · Attack "
                f"{nn(r, 'pitcher_attack_score'):.0f}{star}  {tag}"
            ):
                m = st.columns(6)
                m[0].metric("ERA", f"{e['era']:.2f}", f"L3 {nn(r, 'pitcher_l3_era'):.2f}")
                m[1].metric("HR/9", f"{e['hr9']:.2f}", f"L3 {nn(r, 'pitcher_l3_hr9'):.2f}")
                m[2].metric("WHIP", f"{e['whip']:.2f}", f"L3 {nn(r, 'pitcher_l3_whip'):.2f}")
                m[3].metric("K/9", f"{e['k9']:.2f}")
                m[4].metric("K rate", pct(nn(r, "pitcher_k_rate")))
                m[5].metric("FB velo", f"{nn(r, 'pitcher_fb_velo_delta'):+.2f}",
                            txt(r, "pitcher_fb_velo_status", default=""))

                v1, v2 = st.columns([1, 1])
                with v1:
                    # 0-100 scaling so contact quality, mistake rate and raw
                    # HR/9 sit on one axis. Bigger shape = easier to take deep.
                    axes = ["Attack", "Barrel", "Hard hit", "Meatball", "Pull air", "HR/9"]
                    vals = [
                        min(100, nn(r, "pitcher_attack_score")),
                        min(100, nn(r, "pitcher_barrel_allowed") * 100 * 6),
                        min(100, nn(r, "pitcher_hardhit_allowed") * 100 * 1.8),
                        min(100, nn(r, "pitcher_meatball_pct") * 100 * 3),
                        min(100, nn(r, "pitcher_pullair_allowed_pct") * 100 * 1.6),
                        min(100, e["hr9"] * 33),
                    ]
                    radar(axes, vals, C["orange"], "Vulnerability profile", height=300)
                with v2:
                    st.markdown("**Contact allowed**")
                    stat_table([
                        ("EV allowed", f"{nn(r, 'pitcher_ev_allowed'):.1f} mph"),
                        ("Barrel %", pct(nn(r, "pitcher_barrel_allowed"))),
                        ("Hard hit %", pct(nn(r, "pitcher_hardhit_allowed"))),
                        ("Fly-ball %", pct(nn(r, "pitcher_fb_rate"))),
                        ("Pull-air %", pct(nn(r, "pitcher_pullair_allowed_pct"))),
                        ("375+ / 400+ allowed", f"{int(nn(r, 'pitcher_375_allowed'))} / {int(nn(r, 'pitcher_400_allowed'))}"),
                        ("BABIP", f"{nn(r, 'pitcher_babip'):.3f}"),
                        ("Statcast BBE", f"{int(nn(r, 'pitcher_statcast_bbe'))}"),
                    ])

                s1, s2 = st.columns(2)
                with s1:
                    st.markdown("**Command / swing profile**")
                    stat_table([
                        ("Meatball %", pct(nn(r, "pitcher_meatball_pct"))),
                        ("Whiff %", pct(nn(r, "pitcher_whiff_pct"))),
                        ("SwStr %", pct(nn(r, "pitcher_swstr_pct"))),
                        ("Putaway %", pct(nn(r, "pitcher_putaway_pct"))),
                        ("1st-pitch strike %", pct(nn(r, "pitcher_first_pitch_strike_pct"))),
                        ("Spot damage", f"{nn(r, 'pitcher_spot_damage_score'):.0f} ({txt(r, 'pitcher_spot_damage_label', default='—')})"),
                        ("Zone damage", f"{nn(r, 'pitcher_zone_damage_score'):.0f} ({txt(r, 'pitcher_zone_damage_label', default='—')})"),
                    ])
                with s2:
                    st.markdown("**Platoon splits**")
                    weak = txt(r, "pitcher_weak_side", default="—")
                    stat_table([
                        ("Weak side", weak or "—"),
                        ("HR/9 vs LHB", f"{nn(r, 'pitcher_hr9_vs_lhb'):.2f}"),
                        ("HR/9 vs RHB", f"{nn(r, 'pitcher_hr9_vs_rhb'):.2f}"),
                        ("HR vs LHB / RHB", f"{int(nn(r, 'pitcher_hr_vs_lhb'))} / {int(nn(r, 'pitcher_hr_vs_rhb'))}"),
                        ("XBH vs LHB / RHB", f"{e['xbh_lhb'] or '—'} / {e['xbh_rhb'] or '—'}"),
                        ("Side SLG / OPS", f"{nn(r, 'pitcher_side_slug'):.3f} / {nn(r, 'pitcher_side_ops'):.3f}"),
                        ("Mix vs LHB", txt(r, "pitcher_primary_mix_vs_lhb", default="—")),
                        ("Mix vs RHB", txt(r, "pitcher_primary_mix_vs_rhb", default="—")),
                    ])

                usage = r.get("pitcher_pitch_usage_pct") or r.get("pitcher_arsenal") or {}
                if isinstance(usage, dict) and usage:
                    st.markdown(f"**Arsenal** — {txt(r, 'pitcher_arsenal_summary')}")
                    st.bar_chart(pd.DataFrame({"usage %": usage}), height=240, color=C["orange"])
                    if txt(r, "pitcher_mistake_pitch_v31"):
                        st.caption(f"Mistake pitch: {txt(r, 'pitcher_mistake_pitch_v31')}"
                                   + ("  ·  matches this hitter's damage pitch"
                                      if r.get("pitcher_mistake_match") else ""))

                st.markdown(f"**Opposing lineup ({len(e['lineup'])})**")
                ldf = pd.DataFrame([{
                    "Spot": b.get("lineup_spot"), "Batter": name_of(b),
                    "B": txt(b, "bats", default="?"),
                    "⭐": "⭐" if b.get("weak_spot_flag") else "",
                    "🎯": "🎯" if nn(b, "pitch_type_match_score") >= 80 else "",
                    "HR": round(hr_score(b), 1), "HRR": round(prod_score(b), 1),
                    "Hit": round(hit_score(b), 1), "PMix": round(pmix_score(b), 1),
                    "DC": round(nn(b, "damage_conversion_score"), 1),
                    "Role": tier_role(b),
                } for b in e["lineup"]])
                st.dataframe(ldf, width="stretch", hide_index=True)

                # Two different questions, so two separate heatmaps.
                #
                # (1) TODAY'S HITTERS: the model scores of whoever is batting
                #     in each spot right now. This is about the men, not the
                #     pitcher -- it was previously mislabelled "Threat by
                #     lineup spot", which implied the pitcher's own history.
                if "Spot" in ldf.columns and ldf["Spot"].notna().any():
                    lh = ldf.dropna(subset=["Spot"]).copy()
                    lh["Spot"] = lh["Spot"].astype(int).astype(str)
                    lh = lh.set_index("Spot")[["HR", "HRR", "Hit", "PMix", "DC"]]
                    heatmap(lh, "Today's hitters by lineup spot (model scores)",
                            height=max(240, 26 * len(lh) + 90))

                # (2) THE PITCHER'S OWN HISTORY: how he has actually been
                #     damaged by each spot in the order, from
                #     pitcher_lineup_spot_damage. This is the real "which
                #     spot hurts him" answer.
                spot_dmg = (load_detail("pitcher", e["pitcher_id"])
                            .get("pitcher_lineup_spot_damage") or {})
                if isinstance(spot_dmg, dict) and spot_dmg:
                    sd = pd.DataFrame([{
                        "Spot": str(v.get("spot", k)),
                        "Damage": nn(v, "damage_score"),
                        "SLG": nn(v, "slg") * 100,
                        "ISO": nn(v, "iso") * 100,
                        "HR rate": nn(v, "hr_rate") * 100,
                        "Hard hit": nn(v, "hard_hit_rate") * 100,
                        "Barrel": nn(v, "barrel_rate") * 100,
                        "_pa": nn(v, "pa"),
                        "_label": txt(v, "label", default=""),
                    } for k, v in sorted(spot_dmg.items(), key=lambda kv: str(kv[0]))
                        if isinstance(v, dict)])
                    if not sd.empty:
                        heatmap(
                            sd.set_index("Spot")[
                                ["Damage", "SLG", "ISO", "HR rate", "Hard hit", "Barrel"]],
                            f"{e['pitcher_name']} — damage allowed BY lineup spot (his own history)",
                            height=max(260, 26 * len(sd) + 90),
                        )
                        worst = sd.sort_values("Damage", ascending=False).iloc[0]
                        st.caption(
                            f"Most damaged in spot #{worst['Spot']} "
                            f"({worst['_label']}, {int(worst['_pa'])} PA). "
                            "SLG/ISO/rates shown ×100 so they share the colour scale."
                        )

                bp = txt(r, "bullpen_quality")
                if bp:
                    st.caption(
                        f"Bullpen behind him: {bp} · ERA {nn(r, 'bullpen_era'):.2f} · "
                        f"HR/9 {nn(r, 'bullpen_hr9'):.2f} · WHIP {nn(r, 'bullpen_whip'):.2f}"
                    )

# ── HITS / HRR ──────────────────────────────────────────────────────────────
with tab_hitshrr:
    st.caption("Production and contact profiles — hits, runs, RBI, extra bases.")
    hh1, hh2 = st.columns([2, 1])
    hh_kind = hh1.radio("Type", ["HRR (runs + RBI)", "Hit (base-hit floor)", "TB / XBH"], horizontal=True)
    hh_n = hh2.number_input("Show", 5, 100, 30, step=5, key="hhn")
    k = {"HRR (runs + RBI)": "hrr", "Hit (base-hit floor)": "hit", "TB / XBH": "tb"}[hh_kind]

    hh = sorted(view, key=lambda p: score_for(p, k), reverse=True)[: int(hh_n)]
    st.dataframe(pd.DataFrame([{
        "Player": name_of(p), "Team": team_of(p), "Opp": opp_of(p),
        "Spot": p.get("lineup_spot"), "Grade": grade_for(p, k),
        "Score": round(score_for(p, k), 1),
        "AVG": round(nn(p, "season_avg"), 3), "OBP": round(nn(p, "season_obp"), 3),
        "BABIP": round(nn(p, "babip"), 3), "K%": round(nn(p, "season_k_rate") * 100, 1),
        "L5 H": int(nn(p, "last5_hits")), "L5 XBH": int(nn(p, "last5_xbh")),
        "L5 R": int(nn(p, "last5_runs")), "L5 RBI": int(nn(p, "last5_rbi")),
        "PreOB": round(nn(p, "lineup_pre_onbase"), 3),
        "Post": round(nn(p, "lineup_post_convert"), 3),
        "Best non-HR": txt(p, "best_non_hr_category"),
    } for p in hh]), width="stretch", hide_index=True, height=560)

# ── PAIRS / POOLS ───────────────────────────────────────────────────────────
pair_payload = load_json("public/data/current/pair_builder_latest.json") or {}

with tab_pairs:
    pairs = pair_payload.get("recommended_pairs") or []
    if not pairs:
        st.info("No pair builder output published yet for this slate.")
    else:
        st.caption(f"Pair Builder · {pair_payload.get('date', '')} · {len(pairs)} pairs")
        for p in pairs:
            names = " + ".join(str(x.get("name", "?")) for x in (p.get("players") or []))
            st.markdown(
                f"<div class='pick-card'><b>{p.get('type', 'PAIR')}</b> "
                f"<span class='muted'>· score {n(p.get('pair_score')):.1f} · risk {p.get('risk', '—')}</span><br>"
                f"<b>{names}</b><br>{tags_html(p.get('tags'))}"
                f"<div class='muted'>{p.get('reason', '')}</div></div>",
                unsafe_allow_html=True,
            )
            if p.get("players"):
                st.dataframe(rows_to_df(p["players"], [
                    "name", "team", "opponent", "lineup_spot", "hr_score", "hrw_score",
                    "season_hr", "hr_per_pa", "pitcher_name", "pitcher_throws", "pitcher_hr9",
                ]), width="stretch", hide_index=True)

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
                f"<span class='muted'>· score {n(pool.get('pool_score')):.1f} · "
                f"risk {pool.get('risk', '—')}</span><br>{tags_html(pool.get('tags'))}"
                f"<div class='muted'>{pool.get('reason', '')}</div></div>",
                unsafe_allow_html=True,
            )
            if pool.get("players"):
                st.dataframe(rows_to_df(pool["players"], [
                    "name", "team", "opponent", "lineup_spot", "hr_score",
                    "hrw_score", "season_hr", "pitcher_name", "pitcher_hr9",
                ]), width="stretch", hide_index=True)

# ── RESULTS ─────────────────────────────────────────────────────────────────
with tab_results:
    which = st.radio("Results view", ["Live", "Final"], horizontal=True)
    rel = f"public/data/current/results_{'live' if which == 'Live' else 'final'}.json"
    res = load_json(rel) or {}
    rrows = res.get("results") or []

    if not rrows:
        st.info(f"No {which.lower()} results yet — grading runs after games finish.")
    else:
        rdf = pd.DataFrame(rrows)
        st.caption(f"{res.get('label', '')} · {res.get('date', '')} · {len(rdf)} graded picks")
        k = st.columns(5)
        k[0].metric("Graded", len(rdf))
        for i, (lbl, col) in enumerate(
            [("HRs", "got_hr"), ("Base hits", "got_base_hit"), ("XBH", "got_xbh")], start=1
        ):
            k[i].metric(lbl, int(pd.to_numeric(rdf.get(col, 0), errors="coerce").fillna(0).sum()))
        if "got_hr" in rdf.columns and len(rdf):
            rate = pd.to_numeric(rdf["got_hr"], errors="coerce").fillna(0).mean()
            k[4].metric("HR hit rate", f"{rate * 100:.1f}%")

        if "grade" in rdf.columns:
            st.markdown("##### Grade breakdown")
            st.dataframe(rdf["grade"].value_counts().rename_axis("grade").reset_index(name="count"),
                         width="stretch", hide_index=True)
        st.dataframe(rows_to_df(rrows, [
            "name", "team", "pick_type", "bet_type", "rank", "hr_score",
            "actual_hr", "actual_hits", "actual_tb", "actual_rbi", "actual_runs",
            "grade", "outcome_text", "game_status",
        ]), width="stretch", hide_index=True, height=520)

# ── PLAYER DETAIL ───────────────────────────────────────────────────────────
# Modelled on the old PlayerModal: identity header, pill row, then sub-tabs
# (Overview / EV Log / Pitch / Spray) instead of one long scroll.
with tab_player:
    if not view:
        st.info("No players match these filters.")
    else:
        opts = sorted(view, key=hr_score, reverse=True)
        labels = [f"{name_of(p)} ({team_of(p)}) — HR {hr_score(p):.0f}" for p in opts]
        idx = st.selectbox("Player", range(len(opts)), format_func=lambda i: labels[i])
        p = opts[idx]

        rc = role_config(p)
        role_label, role_color = rc if rc else (tier_role(p), tier_color(tier_role(p)))
        hrw = HRW_MAP.get(txt(p, "hrw_zone"))

        st.markdown(f"### {name_of(p)}")
        st.caption(
            f"{team_of(p)} vs {opp_of(p)} · Lineup #{p.get('lineup_spot', '—')} · "
            f"{txt(p, 'bats', default='?')}HB · vs {txt(p, 'pitcher_name', default='TBD')} "
            f"({txt(p, 'pitcher_throws', default='?')}HP) · {txt(p, 'venue_name')}"
        )

        head_pills = bubble(txt(p, "final_hr_role")[:1] or "•", role_label, role_color)
        head_pills += bubble("", f"Grade {grade_for(p, 'hr')}", C["text2"])
        if hrw:
            head_pills += bubble(hrw[0], f"HRW {nn(p, 'hrw_score'):.0f}", hrw[1])
        if nn(p, "last5_hr") > 0:
            head_pills += bubble("", f"L5 {int(nn(p, 'last5_hr'))}HR", C["orange"])
        if txt(p, "matchup_label"):
            head_pills += bubble("", txt(p, "matchup_label"), C["cyan"])
        if hard_hit(p) > 0:
            head_pills += bubble("", f"HH {hard_hit(p) * 100:.0f}%", C["green"])
        if p.get("weak_spot_flag"):
            head_pills += bubble("⭐", "Weak Spot", C["yellow"])
        if is_aligned(p):
            head_pills += bubble("🧩", "Aligned", C["purple"])
        st.markdown(head_pills, unsafe_allow_html=True)

        a1, a2, a3 = st.columns([1, 1, 4])
        if a1.button("➕ Add to slip", width="stretch"):
            st.session_state.slip.append(f"{name_of(p)} — {txt(p, 'best_bet_type', default='HR')}")
            st.rerun()
        if a2.button("⭐ Watch", width="stretch"):
            if name_of(p) not in st.session_state.watch:
                st.session_state.watch.append(name_of(p))
                persist_watch()
                st.rerun()

        detail = load_detail("batter", p.get("player_id"))
        # spray_chart is the canonical batted-ball list; contact_log and
        # batted_ball_log were byte-identical copies, so they're aliases here.
        bbe = detail.get("spray_chart") or []

        ov, evlog, pitchtab, spraytab = st.tabs(
            ["📊 Overview", "⚡ EV Log", "🎯 Pitch", "💦 Spray"]
        )

        with ov:
            o1, o2 = st.columns(2)
            with o1:
                st.markdown("**MODEL SCORES**")
                stat_table([
                    ("HR Score", f"{hr_score(p):.1f}"),
                    ("HRR Score", f"{prod_score(p):.1f}"),
                    ("Hit Score", f"{hit_score(p):.1f}"),
                    ("TB Score", f"{tb_score(p):.1f}"),
                    ("Pitch Mix", f"{pmix_score(p):.1f}"),
                    ("Damage Conversion", f"{nn(p, 'damage_conversion_score'):.1f}"),
                ])
                st.markdown("**RECENT DISTANCE**")
                stat_table([
                    ("350+ count", f"{int(recent350(p))}"),
                    ("375+ count", f"{int(recent375(p))}"),
                    ("400+ count", f"{int(recent400(p))}"),
                    ("Ideal HR %", f"{ihr_val(p) * 100:.1f}%"),
                ])
                st.markdown("**SPLITS**")
                stat_table([
                    ("vs RHP", f"{nn(p, 'avg_vs_rhp'):.3f}"),
                    ("vs LHP", f"{nn(p, 'avg_vs_lhp'):.3f}"),
                    ("L5 Hits", f"{int(nn(p, 'last5_hits'))}"),
                    ("L5 HR", f"{int(nn(p, 'last5_hr'))}"),
                    ("L5 XBH", f"{int(nn(p, 'last5_xbh'))}"),
                ])
            with o2:
                st.markdown("**BATTED BALL**")
                stat_table([
                    ("Avg EV", f"{avg_ev(p):.1f} mph"),
                    ("Max EV", f"{max_ev(p):.1f} mph"),
                    ("Barrel %", f"{barrel_rate(p) * 100:.0f}%"),
                    ("Hard Hit %", f"{hard_hit(p) * 100:.0f}%"),
                    ("Launch Angle", f"{launch_angle(p):.1f}°"),
                    ("Pull %", f"{pull_rate(p) * 100:.0f}%"),
                ])
                st.markdown("**SEASON**")
                stat_table([
                    ("AVG", f"{nn(p, 'season_avg'):.3f}"),
                    ("HR", f"{int(nn(p, 'season_hr'))}"),
                    ("PA", f"{int(nn(p, 'season_pa'))}"),
                    ("K Rate", f"{nn(p, 'season_k_rate') * 100:.0f}%"),
                    ("BABIP", f"{nn(p, 'babip'):.3f}"),
                    ("Games since HR", f"{int(nn(p, 'games_since_last_hr'))}"),
                ])
                st.markdown("**OPPOSING PITCHER**")
                stat_table([
                    ("Name", txt(p, "pitcher_name", default="—")),
                    ("Throws", txt(p, "pitcher_throws", default="—")),
                    ("HR/9", f"{nn(p, 'pitcher_hr9'):.1f}"),
                    ("WHIP", f"{nn(p, 'pitcher_whip'):.2f}"),
                    ("P-BABIP", f"{nn(p, 'pitcher_babip'):.3f}"),
                ])

            # Radar of the six model scores, with the slate median overlaid so
            # the shape reads as "vs everyone else today", not in a vacuum.
            axes = ["HR", "HRR", "Hit", "TB", "PMix", "DC"]
            mine = [hr_score(p), prod_score(p), hit_score(p), tb_score(p),
                    pmix_score(p), nn(p, "damage_conversion_score")]
            slate_med = [
                float(pd.Series([hr_score(x) for x in players]).median()),
                float(pd.Series([prod_score(x) for x in players]).median()),
                float(pd.Series([hit_score(x) for x in players]).median()),
                float(pd.Series([tb_score(x) for x in players]).median()),
                float(pd.Series([pmix_score(x) for x in players]).median()),
                float(pd.Series([nn(x, "damage_conversion_score") for x in players]).median()),
            ]
            radar(axes, mine, role_color, "Model score profile vs slate median",
                  height=360, second=("Slate median", slate_med, C["text3"]))

            for label, key in (("Why this HR score", "hr_reason"),
                               ("Pitch fit", "pitch_fit_summary"),
                               ("Park fit", "park_fit_summary"),
                               ("Risk", "risk_reason")):
                if txt(p, key):
                    st.markdown(f"**{label}** — {txt(p, key)}")

        # ── EV LOG ──────────────────────────────────────────────────────────
        with evlog:
            if not bbe:
                st.info("No batted-ball detail published for this player yet.")
            else:
                edf = pd.DataFrame(bbe)
                for col in ("ev", "launch_angle", "distance", "pitch_velocity"):
                    if col in edf.columns:
                        edf[col] = pd.to_numeric(edf[col], errors="coerce")
                if "date" in edf.columns:
                    edf = edf.sort_values("date", ascending=False)

                f1, f2, f3, f4 = st.columns(4)
                limit = f1.radio("Sample", [10, 15, 25, 50], index=2, horizontal=True)
                arm = f2.radio("Arm", ["All", "RHP", "LHP"], horizontal=True)
                pitches = sorted({str(x) for x in edf.get("pitch_type", pd.Series(dtype=str)).dropna()})
                pick_pitch = f3.selectbox("Pitch", ["All pitches"] + pitches)
                only = f4.selectbox("Show", ["All results", "Home runs", "Barrels",
                                             "Hard hit (95+)", "375+ ft"])

                q = edf
                if arm != "All" and "arm" in q.columns:
                    q = q[q["arm"].astype(str).str.upper().str.startswith(arm[0])]
                if pick_pitch != "All pitches" and "pitch_type" in q.columns:
                    q = q[q["pitch_type"].astype(str) == pick_pitch]
                if only == "Home runs" and "is_hr" in q.columns:
                    q = q[q["is_hr"].astype(bool)]
                elif only == "Barrels" and "is_barrel" in q.columns:
                    q = q[q["is_barrel"].astype(bool)]
                elif only == "Hard hit (95+)" and "ev" in q.columns:
                    q = q[q["ev"] >= 95]
                elif only == "375+ ft" and "distance" in q.columns:
                    q = q[q["distance"] >= 375]
                q = q.head(int(limit))

                k = st.columns(5)
                k[0].metric("BBE shown", len(q))
                if "ev" in q.columns and len(q):
                    k[1].metric("Avg EV", f"{q['ev'].mean():.1f}")
                    k[2].metric("Max EV", f"{q['ev'].max():.1f}")
                if "distance" in q.columns and len(q):
                    k[3].metric("Max dist", f"{q['distance'].max():.0f}")
                if "is_hr" in q.columns:
                    k[4].metric("HRs", int(q["is_hr"].astype(bool).sum()))

                show = [c for c in ["date", "pitcher", "arm", "pitch_name", "ev",
                                    "launch_angle", "distance", "pitch_velocity",
                                    "result", "trajectory"] if c in q.columns]
                disp = q[show].rename(columns={
                    "date": "Date", "pitcher": "Pitcher", "arm": "Arm",
                    "pitch_name": "Pitch", "ev": "EV", "launch_angle": "Angle",
                    "distance": "Dist", "pitch_velocity": "Velo",
                    "result": "Result", "trajectory": "Traj",
                })

                # Rendered as HTML rather than a styled DataFrame: pandas'
                # .style accessor needs jinja2, which isn't guaranteed on
                # Streamlit Cloud. This also reproduces the old EV Log's
                # per-cell colouring exactly -- EV green 95+ / red 85 and
                # under, distance green 375+ / red 300 and under.
                def cell(v: Any, kind: str = "") -> str:
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        return "<td>—</td>"
                    if not math.isfinite(fv):
                        return "<td>—</td>"
                    colr = ""
                    if kind == "ev":
                        colr = C["green"] if fv >= 95 else C["red"] if fv <= 85 else ""
                        shown = f"{fv:.1f}"
                    elif kind == "dist":
                        colr = C["green"] if fv >= 375 else C["red"] if fv <= 300 else ""
                        shown = f"{fv:.0f}"
                    else:
                        shown = f"{fv:.0f}"
                    style = f" style='color:{colr};font-weight:700'" if colr else ""
                    return f"<td{style}>{shown}</td>"

                rows_html = []
                for _, r in q.iterrows():
                    hr_mark = " 🔴" if r.get("is_hr") else ""
                    rows_html.append(
                        "<tr>"
                        f"<td>{r.get('date', '')}</td>"
                        f"<td>{r.get('pitcher', '')}</td>"
                        f"<td>{r.get('arm', '')}</td>"
                        f"<td style='color:{C['cyan']}'>{r.get('pitch_name', '')}</td>"
                        + cell(r.get("ev"), "ev")
                        + cell(r.get("launch_angle"))
                        + cell(r.get("distance"), "dist")
                        + cell(r.get("pitch_velocity"))
                        + f"<td>{r.get('result', '')}{hr_mark}</td>"
                        f"<td style='color:{C['text3']}'>{r.get('trajectory', '')}</td>"
                        "</tr>"
                    )
                st.markdown(
                    "<div style='max-height:440px;overflow:auto;border:1px solid "
                    f"{C['border']};border-radius:12px'>"
                    f"<table style='width:100%;border-collapse:collapse;"
                    f"font-family:{NUM_FONT};font-size:11px'>"
                    f"<thead><tr style='position:sticky;top:0;background:{C['bg3']};"
                    f"color:{C['text3']};text-align:left'>"
                    "<th>Date</th><th>Pitcher</th><th>Arm</th><th>Pitch</th><th>EV</th>"
                    "<th>Angle</th><th>Dist</th><th>Velo</th><th>Result</th><th>Traj</th>"
                    "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table></div>",
                    unsafe_allow_html=True,
                )
                st.caption("EV green 95+ · red 85− | Dist green 375+ · red 300− | 🔴 home run")

                st.markdown("**Contact quality by day**")
                cc1, cc2 = st.columns(2)
                with cc1:
                    candles(edf, "date", "ev", "Exit velocity", unit="mph")
                with cc2:
                    candles(edf, "date", "distance", "Distance", unit="ft")
                st.caption(
                    "Each candle is one day: opens on that day's first batted ball, "
                    "closes on its last, wick spans weakest to hardest contact."
                )

        # ── PITCH ───────────────────────────────────────────────────────────
        with pitchtab:
            prof = detail.get("batter_pitch_type_profile") or {}
            summary = detail.get("pitch_type_summary") or prof.get("pitch_type_summary")
            if isinstance(summary, list) and summary:
                sdf = pd.DataFrame(summary)
                cols = [c for c in ["pitch_type", "seen", "bbe", "avg_ev", "avg_la",
                                    "max_dist", "hr", "hr_per_bbe", "xbh",
                                    "hard_hit_pct", "hard_hit_rate"] if c in sdf.columns]
                st.markdown("**By pitch type**")
                # Heatmap normalises each metric to 0-100 across the pitch
                # types so they're comparable on one colour scale -- raw
                # avg_ev (~90) and hr_per_bbe (~0.03) can't share an axis.
                metrics = [c for c in ["avg_ev", "avg_la", "max_dist", "hr_per_bbe",
                                       "hard_hit_rate", "hard_hit_pct",
                                       "barrel_like_rate", "good_contact_rate"]
                           if c in sdf.columns]
                if metrics and "pitch_type" in sdf.columns:
                    hm = sdf.set_index("pitch_type")[metrics].apply(pd.to_numeric, errors="coerce")
                    norm = hm.copy()
                    for c in norm.columns:
                        lo, hi = norm[c].min(), norm[c].max()
                        norm[c] = 50.0 if hi == lo else (norm[c] - lo) / (hi - lo) * 100
                    heatmap(norm.round(0), "Damage by pitch type (0-100 within each column)",
                            height=max(240, 30 * len(norm) + 90))
                st.dataframe(sdf[cols], width="stretch", hide_index=True)
            else:
                st.info("No pitch-type profile published for this player yet.")

            arsenal = load_detail("pitcher", p.get("pitcher_id"))
            mix = (arsenal.get("pitcher_pitch_mix") or {}).get("usage") or {}
            if mix:
                st.markdown(f"**{txt(p, 'pitcher_name')} — pitch usage**")
                st.bar_chart(pd.DataFrame({"usage %": mix}), height=240, color=C["orange"])

        # ── SPRAY ───────────────────────────────────────────────────────────
        with spraytab:
            if not bbe:
                st.info("No spray data published for this player yet.")
            else:
                sdf = pd.DataFrame(bbe)
                for col in ("hc_x", "hc_y", "distance", "ev", "launch_angle"):
                    if col in sdf.columns:
                        sdf[col] = pd.to_numeric(sdf[col], errors="coerce")
                g1, g2 = st.columns(2)
                with g1:
                    st.caption("Spray map")
                    if {"hc_x", "hc_y"}.issubset(sdf.columns):
                        fld = sdf.dropna(subset=["hc_x", "hc_y"]).copy()
                        fld["x"] = fld["hc_x"] - 125.42
                        fld["y"] = 198.27 - fld["hc_y"]
                        st.scatter_chart(fld, x="x", y="y", height=340,
                                         color="result" if "result" in fld.columns else None,
                                         size="distance" if "distance" in fld.columns else None)
                with g2:
                    st.caption("Launch angle vs distance")
                    if {"launch_angle", "distance"}.issubset(sdf.columns):
                        st.scatter_chart(sdf, x="launch_angle", y="distance", height=340,
                                         color="result" if "result" in sdf.columns else None)
                if "lane" in sdf.columns:
                    st.caption("By field lane")
                    st.bar_chart(
                        pd.DataFrame({"batted balls": sdf["lane"].replace("", "—").value_counts()}),
                        height=220, color=C["green"])

# ── WATCHLIST ───────────────────────────────────────────────────────────────
def watch_card_html(p: Dict[str, Any]) -> str:
    """Compact grid card: badges, score + grade top-right, pills, stat line."""
    rc = role_config(p)
    role_label, role_color = rc if rc else (tier_role(p), tier_color(tier_role(p)))
    hrw = HRW_MAP.get(txt(p, "hrw_zone"))
    score = nn(p, "top_board_score_v2") or hr_score(p)

    badges = ""
    if hrw:
        badges += hrw[0]
    if p.get("weak_spot_flag"):
        badges += "⭐"
    if is_aligned(p):
        badges += "🧩"
    if nn(p, "pitch_type_match_score") >= 80:
        badges += "🎯"

    pills = bubble("", role_label, role_color)
    if txt(p, "best_use"):
        pills += bubble("", txt(p, "best_use")[:22], C["text2"])
    if is_aligned(p):
        pills += bubble("🧩", "Aligned Signals", C["purple"])
    extra = ""
    if txt(p, "matchup_label"):
        extra += bubble("", txt(p, "matchup_label"), C["cyan"])
    if nn(p, "pitch_type_match_score") >= 80:
        extra += bubble("", f"PMix: {txt(p, 'best_damage_pitch_v31', default='fit')}", C["cyan"])
    if pull_rate(p) >= 0.6:
        extra += bubble("", f"Pull {pull_rate(p) * 100:.0f}%", C["green"])
    if nn(p, "last5_hr") >= 2:
        extra += bubble("", f"L5 {int(nn(p, 'last5_hr'))}HR", C["orange"])

    return (
        f"<div style='background:{C['bg2']};border:1px solid {C['border']};"
        f"border-left:3px solid {role_color};border-radius:12px;padding:10px 12px;"
        f"margin-bottom:10px;height:100%'>"
        f"<div style='display:flex;justify-content:space-between;align-items:flex-start'>"
        f"<div style='min-width:0'>"
        f"<div style='font-size:13px;font-weight:700'>{badges} {name_of(p)}</div>"
        f"<div style='font-size:10px;color:{C['text3']};font-family:{NUM_FONT}'>"
        f"{team_of(p)} vs {opp_of(p)} · {p.get('lineup_spot', '—')} · "
        f"{txt(p, 'bats', default='?')}</div></div>"
        f"<div style='text-align:right;flex-shrink:0'>"
        f"<div style='font-size:20px;font-weight:800;color:{role_color};"
        f"font-family:{NUM_FONT};line-height:1'>{score:.0f}</div>"
        f"<div style='font-size:10px;color:{C['text3']}'>{grade_for(p, 'hr')}</div>"
        f"</div></div>"
        f"<div style='margin:6px 0 4px'>{pills}</div>"
        f"<div style='margin-bottom:6px'>{extra}</div>"
        f"<div style='font-size:10px;color:{C['text3']};font-family:{NUM_FONT}'>"
        f"BA {nn(p, 'season_avg'):.3f} · HR {int(nn(p, 'season_hr'))} · "
        f"K {nn(p, 'season_k_rate') * 100:.0f}% · BABIP {nn(p, 'babip'):.3f} · "
        f"WHIP {nn(p, 'pitcher_whip'):.1f}</div>"
        f"</div>"
    )


with tab_watch:
    if not st.session_state.watch:
        st.info("No players on your watchlist yet — add them from the Player tab.")
    else:
        watched = sorted(
            [p for p in players if name_of(p) in st.session_state.watch],
            key=hr_score, reverse=True,
        )
        hdr_l, hdr_r = st.columns([4, 1])
        hdr_l.markdown(f"### Watchlist\n{len(watched)} saved")
        if hdr_r.button("Clear All", width="stretch"):
            st.session_state.watch = []
            persist_watch()
            st.rerun()

        # Four across, like the old grid.
        per_row = 4
        for i in range(0, len(watched), per_row):
            cols = st.columns(per_row)
            for col, p in zip(cols, watched[i:i + per_row]):
                with col:
                    st.markdown(watch_card_html(p), unsafe_allow_html=True)
                    b1, b2 = st.columns([3, 1])
                    if b1.button("+ Add to Slip", key=f"slip_{name_of(p)}_{i}",
                                 width="stretch"):
                        st.session_state.slip.append(
                            f"{name_of(p)} — {txt(p, 'best_bet_type', default='HR')}")
                        st.rerun()
                    if b2.button("★", key=f"unwatch_{name_of(p)}_{i}", width="stretch",
                                 help="Remove from watchlist"):
                        st.session_state.watch = [
                            w for w in st.session_state.watch if w != name_of(p)]
                        persist_watch()
                        st.rerun()

        st.caption("Saved in the page URL — bookmark it and the list comes back.")

# ── BOT REPORT ──────────────────────────────────────────────────────────────
with tab_bot:
    txt_report = load_text(f"public/data/current/{slate}.txt") or load_text(f"public/data/{slate}.txt")
    if not txt_report:
        st.info("No text report published for this slate yet.")
    else:
        st.download_button("⬇️ Download report (.txt)", txt_report.encode(),
                           file_name=f"mlb_{slate}_report.txt", mime="text/plain")
        find = st.text_input("Filter report lines (blank = full report)", "")
        if find:
            keep = [ln for ln in txt_report.splitlines() if find.lower() in ln.lower()]
            st.code("\n".join(keep) or "No matching lines.", language="text")
        else:
            st.code(txt_report, language="text")

# ── SPRAY (full slate) ──────────────────────────────────────────────────────
with tab_spray:
    st.caption(
        "Batted-ball spray across the slate. Detail is fetched per player "
        "(~82 KB each), so pick a handful rather than loading everyone."
    )
    top_pool = sorted(view, key=hr_score, reverse=True)[:40]
    picks = st.multiselect(
        "Players", range(len(top_pool)),
        default=list(range(min(3, len(top_pool)))),
        format_func=lambda i: f"{name_of(top_pool[i])} ({team_of(top_pool[i])}) — HR {hr_score(top_pool[i]):.0f}",
    )
    frames = []
    for i in picks:
        pl = top_pool[i]
        det = load_detail("batter", pl.get("player_id"))
        for e in (det.get("spray_chart") or []):
            e = dict(e)
            e["player"] = name_of(pl)
            frames.append(e)

    if not frames:
        st.info(
            "No spray data for the selected players yet. Detail files publish "
            "with the next bot run."
        )
    else:
        sp = pd.DataFrame(frames)
        for col in ("hc_x", "hc_y", "distance", "ev", "launch_angle"):
            if col in sp.columns:
                sp[col] = pd.to_numeric(sp[col], errors="coerce")
        c1, c2 = st.columns(2)
        with c1:
            st.caption("Spray map")
            if {"hc_x", "hc_y"}.issubset(sp.columns):
                fld = sp.dropna(subset=["hc_x", "hc_y"]).copy()
                fld["x"] = fld["hc_x"] - 125.42
                fld["y"] = 198.27 - fld["hc_y"]
                st.scatter_chart(fld, x="x", y="y", color="player", height=380,
                                 size="distance" if "distance" in fld.columns else None)
        with c2:
            st.caption("Exit velocity vs distance")
            if {"ev", "distance"}.issubset(sp.columns):
                st.scatter_chart(sp, x="ev", y="distance", color="player", height=380)
        st.dataframe(
            sp[[c for c in ["player", "date", "pitch_type", "event", "bb_type",
                            "ev", "launch_angle", "distance", "lane"] if c in sp.columns]],
            width="stretch", hide_index=True, height=320,
        )

# ── GUIDE ───────────────────────────────────────────────────────────────────
with tab_guide:
    # The bot already writes a full legend at the end of every report, so this
    # stays in sync with the model automatically instead of drifting out of
    # date the way a hardcoded copy in the front end would.
    report = load_text(f"public/data/current/{slate}.txt") or ""
    if "LEGEND" in report:
        st.code(report[report.index("LEGEND"):], language="text")
    else:
        st.markdown("""
**Score keys**

- **HR** — home run score / power ceiling
- **HRR** — hits + runs + RBI production profile
- **Hit** — base-hit floor
- **TB** — total bases / XBH contact profile
- **PMix** — pitch-type matchup fit vs today's starter
- **HRW** — Home Run Window; timing score for today specifically
- **IHR** — ideal HR contact rate
- **Damage** — damage conversion score, the strongest single validated HR predictor
- **375+ / 400+** — recent tracked balls hit that far

**Lanes** filter the board to a specific angle: Strong HR, Value, Due,
Hot, Weak Pitcher, Weather/Park, Pitch Matchup, 🧩 Aligned, Avoid HR.

**🧩 Aligned** means weak-spot + pitch-match + real recent contact quality
all stack on the same hitter — the strongest validated combination.

Grades are the raw score banded: A+ 78 · A 70 · A- 62 · B+ 54 · B 46 · C+ below.
""")
