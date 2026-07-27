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

import datetime as dt
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

# Discrete steps off the same ramp, for HTML cells. Plotly can interpolate
# GREEN_SCALE itself; hand-built tables can't, so they sample it here and
# every table on the site ends up using the identical six shades.
RAMP6 = ["#06251a", "#0b4b30", "#12783f", "#2f9e52", "#4cb96a", "#b7f7c9"]


def ramp_color(v: Any, lo: float, hi: float) -> Optional[str]:
    """Map a value onto the green ramp between two anchors.

    Returns None for anything unparseable so callers can fall through to a
    plain cell rather than painting a misleading colour on missing data.
    """
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fv):
        return None
    span = hi - lo
    pos = 0.0 if span <= 0 else (fv - lo) / span
    pos = max(0.0, min(1.0, pos))
    return RAMP6[min(len(RAMP6) - 1, int(pos * len(RAMP6)))]


def ink_for(bg: str) -> str:
    """Readable text colour on a ramp swatch — the top two shades are light
    enough that white text disappears on them."""
    return "#06281a" if bg in (RAMP6[-1], RAMP6[-2]) else "#e8ecef"

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
def load_splits(player_id: Any, slate_label: str = "today") -> Dict[str, Any]:
    """Day/night, home/away, day-of-week and win/loss splits for one hitter.

    Built by bots/player_splits.py from MLB's gameLog endpoint -- the scoring
    bot itself only carries vs-RHP/LHP, so none of this exists in the slate.
    """
    if player_id in (None, ""):
        return {}
    return load_json(f"public/data/current/splits/{slate_label}/{player_id}.json") or {}


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_detail(kind: str, ident: Any, slate_label: str = "today") -> Dict[str, Any]:
    """One player's or pitcher's heavy logs. ~82 KB, fetched only on demand.

    Detail lives under a per-slate folder. They used to share one directory,
    which meant a pitcher starting on both days had today's file overwritten
    by tomorrow's. Falls back to the old flat path so the app keeps working
    against a data branch published before that fix.
    """
    if ident in (None, ""):
        return {}
    return (load_json(f"public/data/current/detail/{slate_label}/{kind}_{ident}.json")
            or load_json(f"public/data/current/detail/{kind}_{ident}.json")
            or {})


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


# ── "DOES HE GET HURT IN THE N-HOLE?" ───────────────────────────────────────
# Every batter row carries pitcher_spot_damage_score: the damage this pitcher
# has allowed in THAT batter's lineup spot. So the slate-wide baseline for
# each spot can be built from the payload already in memory -- no extra
# fetches -- and any single answer can be judged three ways at once:
# against the spot's own history, against the pitcher's other spots, and
# against what every other starter allows in that same spot today.
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def spot_baseline(slate_label: str) -> Dict[int, float]:
    by: Dict[int, List[float]] = {}
    for r in load_slate(slate_label):
        sp, v = r.get("lineup_spot"), r.get("pitcher_spot_damage_score")
        if sp not in (None, "") and v is not None:
            by.setdefault(int(sp), []).append(float(v))
    return {k: float(pd.Series(v).median()) for k, v in by.items() if v}


def spot_answer(spot_row: Dict[str, Any], all_spots: Dict[str, Any],
                baseline: Dict[int, float], spot: int) -> Dict[str, Any]:
    """Verdict for one pitcher in one lineup spot."""
    dmg = nn(spot_row, "damage_score")
    pa = int(nn(spot_row, "pa"))
    label = txt(spot_row, "label", default="Unknown")

    others = [nn(v, "damage_score") for k, v in all_spots.items()
              if isinstance(v, dict) and str(k) != str(spot)]
    own_med = float(pd.Series(others).median()) if others else 0.0
    ranked = sorted(
        [(int(v.get("spot", k)), nn(v, "damage_score"))
         for k, v in all_spots.items() if isinstance(v, dict)],
        key=lambda x: -x[1])
    rank = next((i for i, (sp, _) in enumerate(ranked, 1) if sp == spot), None)

    league = baseline.get(spot, 0.0)

    # Sample honesty first -- 8 PA can't answer anything, and the labels
    # themselves get shaky below ~15.
    if pa < 10:
        verdict, colr = "NOT ENOUGH DATA", C["text3"]
    elif label in ("HOT", "WARM") or (dmg >= 50 and dmg > own_med + 12):
        verdict, colr = "YES — he gets hurt here", C["red"]
    elif dmg <= 15 or label == "PITCHER ADV":
        verdict, colr = "NO — pitcher's advantage", C["green"]
    else:
        verdict, colr = "NEUTRAL", C["text2"]

    return {
        "verdict": verdict, "color": colr, "damage": dmg, "pa": pa,
        "label": label, "rank": rank, "own_med": own_med, "league": league,
        "vs_own": dmg - own_med, "vs_league": dmg - league,
        "slg": nn(spot_row, "slg"), "iso": nn(spot_row, "iso"),
        "hr_rate": nn(spot_row, "hr_rate"), "hard_hit": nn(spot_row, "hard_hit_rate"),
        "barrel": nn(spot_row, "barrel_rate"), "hr": int(nn(spot_row, "hr")),
        "reason": txt(spot_row, "reason"),
    }


def render_spot_answer(a: Dict[str, Any], pitcher_name: str, spot: int) -> None:
    st.markdown(
        f"<div style='background:{C['bg2']};border:1px solid {C['border']};"
        f"border-left:4px solid {a['color']};border-radius:12px;padding:14px 16px;"
        f"margin:6px 0 10px'>"
        f"<div style='font-size:11px;color:{C['text3']};letter-spacing:.05em'>"
        f"DOES {pitcher_name.upper()} GET HURT IN THE {spot}-HOLE?</div>"
        f"<div style='font-size:22px;font-weight:800;color:{a['color']};margin:4px 0 2px'>"
        f"{a['verdict']}</div>"
        f"<div style='font-size:11px;color:{C['text2']};font-family:{NUM_FONT}'>"
        f"damage {a['damage']:.1f} · {a['label']} · {a['pa']} PA · "
        f"ranks #{a['rank']} of 9 among his own spots</div></div>",
        unsafe_allow_html=True,
    )
    k = st.columns(4)
    k[0].metric("Damage in spot", f"{a['damage']:.1f}",
                f"{a['vs_own']:+.1f} vs his other spots")
    k[1].metric("vs slate median", f"{a['league']:.1f}", f"{a['vs_league']:+.1f}")
    k[2].metric("SLG / ISO allowed", f"{a['slg']:.3f}", f"ISO {a['iso']:.3f}")
    k[3].metric("HR / hard-hit", f"{a['hr']} HR", f"HH {a['hard_hit'] * 100:.0f}%")
    if a["reason"]:
        st.caption(a["reason"])
    if a["pa"] < 15:
        st.caption(
            f"⚠️ {a['pa']} PA is a thin sample — treat this as a lean, not a read."
        )


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

# Score cutoffs matching the bot report's own legend (🚀 70+, ⚡ 60-69,
# 🌤️ 50-59, 🧊 under 50), with 🌋 for the volatile-hot top end.
HRW_BANDS = [
    (80.0, "volatile_hot", "VOLATILE"),
    (70.0, "strong_capped", "STRONG"),
    (60.0, "sweet_spot", "SWEET SPOT"),
    (50.0, "watch", "BUILDING"),
    (0.0, "cold", "COLD"),
]


def hrw_badge(p: Dict[str, Any]) -> Optional[tuple]:
    """(emoji, colour, word, score) for a hitter's HR Window — always resolves.

    Everywhere else on the site HRW is looked up purely by the `hrw_zone`
    string, so any row where the bot didn't stamp that field renders with no
    HRW at all and the reader can't tell "weak timing" apart from "not
    measured". The score is present far more often than the zone label, so
    this falls back to banding the score itself using the report's own
    published cutoffs.
    """
    score = nn(p, "hrw_score")
    zone = txt(p, "hrw_zone")
    if zone in HRW_MAP:
        word = next((w for _, z, w in HRW_BANDS if z == zone), zone.replace("_", " ").upper())
        emoji, colr = HRW_MAP[zone]
        return (emoji, colr, word, score)
    if score <= 0:
        return None
    for cut, z, word in HRW_BANDS:
        if score >= cut:
            emoji, colr = HRW_MAP[z]
            return (emoji, colr, word, score)
    return None

# The five game picks, in the order they're shown: TOP, HR, HIT, HRR, TB.
# "CONTACT" is the bot's internal key for what the board calls TB -- it stamps
# game_pick_role="CONTACT" for the total-bases play, so the key has to stay as
# the bot writes it while the label reads TB like everywhere else in the app.
GAME_ROLE_LABEL = {
    "TOP": ("🔥", "Top", "#f97316"),
    "HR": ("🧨", "HR", "#f87171"),
    "HIT": ("💠", "Hit", "#a78bfa"),
    "HRR": ("🏁", "HRR", "#22d3ee"),
    "CONTACT": ("⚾", "TB", "#34d399"),
}
GAME_ROLE_ORDER = ("TOP", "HR", "HIT", "HRR", "CONTACT")

# Which score each pick is actually being picked ON. Showing the HR score on a
# TB pick made the cards look wrong -- the TB guy would sit at 61 next to an
# HR pick at 96 and read as strictly worse, when he's the best TB play in the
# game. Each tile now leads with its own category's number.
GAME_ROLE_SCORE = {
    "TOP": "hr", "HR": "hr", "HIT": "hit", "HRR": "hrr", "CONTACT": "tb",
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


def hbar(labels: List[str], values: List[float], title: str = "",
         height: Optional[int] = None, fmt: str = "{:.1f}",
         ref: Optional[float] = None, ref_label: str = "median",
         subtitles: Optional[List[str]] = None) -> None:
    """Horizontal ranked bars — the readable replacement for st.bar_chart.

    st.bar_chart gives no value labels, doesn't reliably preserve sort order,
    and paints every bar one flat colour, so a 14-game ranking came out as an
    unreadable stack of same-coloured strips. This sorts descending, shades
    each bar along the green ramp by its own value, prints the number at the
    end of the bar, and can drop a reference line for the median.
    """
    if not labels:
        st.caption("Nothing to chart.")
        return
    order = sorted(zip(labels, values), key=lambda x: x[1])  # plotly draws bottom-up
    lab = [a for a, _ in order]
    val = [b for _, b in order]
    lo, hi = min(val), max(val)
    span = (hi - lo) or 1.0

    # Brightest = best. Sampled from the same ramp the heatmaps use.
    ramp = ["#0b4b30", "#12783f", "#2f9e52", "#4cb96a", "#7fd894", "#b7f7c9"]
    colors = [ramp[min(len(ramp) - 1, int((v - lo) / span * len(ramp)))] for v in val]

    fig = go.Figure(go.Bar(
        x=val, y=lab, orientation="h",
        marker=dict(color=colors, line=dict(width=0)),
        text=[fmt.format(v) for v in val],
        textposition="outside",
        textfont=dict(size=11, color=C["text"], family=NUM_FONT),
        hovertemplate="%{y}: %{x:.1f}<extra></extra>",
        cliponaxis=False,
    ))
    if ref is not None:
        fig.add_vline(x=ref, line=dict(color=C["text3"], width=1, dash="dot"),
                      annotation_text=f"{ref_label} {ref:.1f}",
                      annotation_position="top",
                      annotation_font=dict(size=10, color=C["text3"]))
    h = height or max(220, 30 * len(lab) + 70)
    _layout(fig, h, title)
    fig.update_xaxes(showgrid=False, zeroline=False, showticklabels=False,
                     range=[0, hi * 1.18])
    fig.update_yaxes(showgrid=False, zeroline=False,
                     tickfont=dict(size=11, color=C["text2"]))
    fig.update_layout(margin=dict(l=8, r=54, t=40 if title else 12, b=8),
                      bargap=0.28)
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
        # Both directions are green, light for a day that finished hotter
        # than it started and dark for one that faded. Trading platforms use
        # red for "down" because down means losing money; a day where a
        # hitter's last ball was softer than his first isn't bad, it's just
        # a shape. Red here also collided with the one place this site does
        # mean danger by it.
        increasing=dict(line=dict(color=RAMP6[-1], width=1),
                        fillcolor=RAMP6[-1]),
        decreasing=dict(line=dict(color=RAMP6[1], width=1),
                        fillcolor=RAMP6[1]),
        hovertext=[f"{n} batted ball{'s' if n != 1 else ''}" for n in agg["n"]],
    ))
    # Median line gives the candles something to be read against -- without
    # it you can see the day-to-day shape but not whether any of it is good.
    med = float(d[val_col].median())
    fig.add_hline(y=med, line=dict(color=C["text3"], width=1, dash="dot"),
                  annotation_text=f"median {med:.0f}{(' ' + unit) if unit else ''}",
                  annotation_position="top left",
                  annotation_font=dict(size=9, color=C["text3"]))
    fig.update_layout(xaxis_rangeslider_visible=False)
    _layout(fig, height, title)
    fig.update_yaxes(title_text=unit)
    st.plotly_chart(fig, width="stretch")


def bbe_frame(bbe: Any) -> pd.DataFrame:
    """Batted-ball list -> numeric DataFrame, newest first."""
    if not bbe:
        return pd.DataFrame()
    df = pd.DataFrame(bbe)
    for col in ("ev", "launch_angle", "distance", "pitch_velocity"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "date" in df.columns:
        df = df.sort_values("date", ascending=False)
    return df


def contact_log_html(df: pd.DataFrame, max_height: int = 440) -> str:
    """Every batted ball, shaded so hard contact is visible at a glance.

    This used to bucket EV and distance into three states and paint the bad
    one RED. Two problems: red is reserved on this site for "genuinely bad",
    not for a 300-foot flyout, and three buckets threw away all the
    resolution between them -- a 94 mph ball and an 86 mph ball rendered
    identically as "not coloured". Both columns now sit on the same green
    ramp as every other table here, so the whole log reads as a heat map
    and a hot streak is a block of pale cells.

    Anchors: EV 80->108 mph and distance 180->440 ft, which is roughly the
    playable range of a batted ball. Below the floor everything is the
    darkest shade, which is correct -- a 70 mph grounder and a 60 mph
    grounder are equally uninteresting.

    Still hand-built HTML rather than pandas .style, which needs jinja2 and
    isn't guaranteed on Streamlit Cloud.
    """
    if df is None or df.empty:
        return ""

    pad = "padding:4px 8px"

    def heat(v: Any, lo: float, hi: float, fmt: str) -> str:
        bg = ramp_color(v, lo, hi)
        if bg is None:
            return f"<td style='{pad};color:{C['text3']}'>—</td>"
        return (
            f"<td style='{pad};background:{bg};color:{ink_for(bg)};"
            f"font-weight:700;text-align:right'>{fmt.format(float(v))}</td>"
        )

    def plain(v: Any, fmt: str = "{:.0f}") -> str:
        try:
            fv = float(v)
            if not math.isfinite(fv):
                raise ValueError
        except (TypeError, ValueError):
            return f"<td style='{pad};color:{C['text3']}'>—</td>"
        return f"<td style='{pad};color:{C['text2']};text-align:right'>{fmt.format(fv)}</td>"

    rows_html = []
    for i, (_, r) in enumerate(df.iterrows()):
        is_hr = bool(r.get("is_hr"))
        # Home runs get an orange left rail and a chip. The old 🔴 emoji sat
        # inside the result text where it was easy to scroll straight past.
        rail = f"border-left:3px solid {C['orange']}" if is_hr else "border-left:3px solid transparent"
        zebra = "background:rgba(255,255,255,.02)" if i % 2 else ""
        chip = (
            f"<span style='background:{C['orange']};color:#1a1205;font-size:8.5px;"
            f"font-weight:800;padding:1px 5px;border-radius:3px;margin-left:6px'>HR</span>"
            if is_hr else ""
        )
        rows_html.append(
            f"<tr style='{rail};{zebra}'>"
            f"<td style='{pad};color:{C['text3']};white-space:nowrap'>{r.get('date', '')}</td>"
            f"<td style='{pad};color:{C['text2']};white-space:nowrap'>{r.get('pitcher', '')}</td>"
            f"<td style='{pad};color:{C['text3']}'>{r.get('arm', '')}</td>"
            f"<td style='{pad};color:{C['cyan']};white-space:nowrap'>{r.get('pitch_name', '')}</td>"
            + heat(r.get("ev"), 80, 108, "{:.1f}")
            + plain(r.get("launch_angle"), "{:.0f}°")
            + heat(r.get("distance"), 180, 440, "{:.0f}")
            + plain(r.get("pitch_velocity"))
            + f"<td style='{pad};color:{C['text']};white-space:nowrap'>"
              f"{r.get('result', '')}{chip}</td>"
            f"<td style='{pad};color:{C['text3']}'>{r.get('trajectory', '')}</td>"
            "</tr>"
        )

    heads = ["Date", "Pitcher", "Arm", "Pitch", "EV", "Angle", "Dist", "Velo",
             "Result", "Traj"]
    head_html = "".join(
        f"<th style='{pad};text-align:{'right' if h in ('EV', 'Angle', 'Dist', 'Velo') else 'left'};"
        f"font-weight:600'>{h}</th>"
        for h in heads
    )
    return (
        f"<div style='max-height:{max_height}px;overflow:auto;border:1px solid "
        f"{C['border']};border-radius:12px'>"
        f"<table style='width:100%;border-collapse:collapse;"
        f"font-family:{NUM_FONT};font-size:11px'>"
        f"<thead><tr style='position:sticky;top:0;z-index:1;background:{C['bg3']};"
        f"color:{C['text3']};font-size:9.5px;letter-spacing:.04em'>{head_html}</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table></div>"
    )


CONTACT_LOG_LEGEND = (
    "Exit velo and distance are shaded on the site's green ramp — **pale green "
    "is hard contact, dark green is weak**. Orange rail and HR chip mark balls "
    "that left the yard."
)


def tags_html(tags: Any, limit: int = 6) -> str:
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    if not isinstance(tags, list):
        return ""
    return "".join(
        f"<span class='pill' style='color:{C['text2']}'>{t}</span>" for t in tags[:limit] if t
    )


@st.dialog("Player", width="large")
def player_modal(p: Dict[str, Any]) -> None:
    """Real popup, the way the old PlayerModal worked — open it from any card
    instead of leaving the tab and re-finding the player in a dropdown."""
    rc = role_config(p)
    role_label, role_color = rc if rc else (tier_role(p), tier_color(tier_role(p)))
    hrw = HRW_MAP.get(txt(p, "hrw_zone"))

    st.markdown(f"### {name_of(p)}")
    st.caption(
        f"{team_of(p)} vs {opp_of(p)} · Lineup #{p.get('lineup_spot', '—')} · "
        f"{txt(p, 'bats', default='?')}HB · vs {txt(p, 'pitcher_name', default='TBD')} "
        f"({txt(p, 'pitcher_throws', default='?')}HP)"
    )
    pills = bubble(txt(p, "final_hr_role")[:1] or "•", role_label, role_color)
    pills += bubble("", f"Grade {grade_for(p, 'hr')}", C["text2"])
    if hrw:
        pills += bubble(hrw[0], f"HRW {nn(p, 'hrw_score'):.0f}", hrw[1])
    if nn(p, "last5_hr") > 0:
        pills += bubble("", f"L5 {int(nn(p, 'last5_hr'))}HR", C["orange"])
    if txt(p, "matchup_label"):
        pills += bubble("", txt(p, "matchup_label"), C["cyan"])
    if p.get("weak_spot_flag"):
        pills += bubble("⭐", "Weak Spot", C["yellow"])
    if is_aligned(p):
        pills += bubble("🧩", "Aligned", C["purple"])
    st.markdown(pills, unsafe_allow_html=True)

    m = st.columns(6)
    m[0].metric("HR", f"{hr_score(p):.0f}")
    m[1].metric("HRR", f"{prod_score(p):.0f}")
    m[2].metric("Hit", f"{hit_score(p):.0f}")
    m[3].metric("TB", f"{tb_score(p):.0f}")
    m[4].metric("PMix", f"{pmix_score(p):.0f}")
    m[5].metric("DC", f"{nn(p, 'damage_conversion_score'):.0f}")

    c1, c2 = st.columns(2)
    with c1:
        stat_table([
            ("Season AVG", f"{nn(p, 'season_avg'):.3f}"),
            ("Season HR / PA", f"{int(nn(p, 'season_hr'))} / {int(nn(p, 'season_pa'))}"),
            ("HR per PA", f"{nn(p, 'hr_per_pa'):.4f}"),
            ("Games since HR", f"{int(nn(p, 'games_since_last_hr'))}"),
            ("Due score", f"{nn(p, 'hr_due_score'):.1f} ({txt(p, 'hr_due_tag', default='—')})"),
            ("L5", f"{int(nn(p, 'last5_hits'))}H / {int(nn(p, 'last5_hr'))}HR / {int(nn(p, 'last5_xbh'))}XBH"),
        ])
    with c2:
        stat_table([
            ("Avg / Max EV", f"{avg_ev(p):.1f} / {max_ev(p):.1f}"),
            ("Hard hit / Barrel", f"{pct(hard_hit(p))} / {pct(barrel_rate(p))}"),
            ("Launch angle", f"{launch_angle(p):.1f}°"),
            ("375+ / 400+", f"{int(recent375(p))} / {int(recent400(p))}"),
            ("Pitcher HR/9 · WHIP", f"{nn(p, 'pitcher_hr9'):.2f} · {nn(p, 'pitcher_whip'):.2f}"),
            ("Park / Weather HR", f"{nn(p, 'park_hr_factor', default=1.0):.2f} · {nn(p, 'weather_hr_effect_pct'):+.0f}%"),
        ])

    axes = ["HR", "HRR", "Hit", "TB", "PMix", "DC"]
    radar(axes, [hr_score(p), prod_score(p), hit_score(p), tb_score(p),
                 pmix_score(p), nn(p, "damage_conversion_score")],
          role_color, "Score profile", height=300)

    # Situational splits: charts, and all four families including day of week.
    # This used to be three raw tables with the weekday breakdown missing
    # entirely, even though the splits bot has been publishing it all along.
    if load_splits(p.get("player_id"), slate):
        with st.expander("📅 Situational splits — day/night, home/away, W/L, weekday"):
            render_splits(p, slate, compact=True)

    # Contact log. The modal was the one place a player's batted balls
    # couldn't be seen -- you had to close it, go to the Player tab and
    # re-find him, which defeats the point of a modal.
    mbbe = bbe_frame((load_detail("batter", p.get("player_id"), slate) or {}).get("spray_chart"))
    if not mbbe.empty:
        with st.expander(f"⚡ Contact log — last {min(20, len(mbbe))} batted balls"):
            recent = mbbe.head(20)
            q = st.columns(4)
            if "ev" in recent.columns:
                q[0].metric("Avg EV", f"{recent['ev'].mean():.1f}")
                q[1].metric("Max EV", f"{recent['ev'].max():.1f}")
            if "distance" in recent.columns:
                q[2].metric("Max dist", f"{recent['distance'].max():.0f}")
            if "is_hr" in recent.columns:
                q[3].metric("HRs", int(recent["is_hr"].astype(bool).sum()))
            st.markdown(contact_log_html(recent, max_height=300), unsafe_allow_html=True)
            st.caption(CONTACT_LOG_LEGEND)
            cl, cr = st.columns(2)
            with cl:
                candles(mbbe, "date", "ev", "Exit velocity by day", height=260, unit="mph")
            with cr:
                candles(mbbe, "date", "distance", "Distance by day", height=260, unit="ft")

    if txt(p, "simple_reason_1"):
        st.caption(txt(p, "simple_reason_1"))
    if txt(p, "hr_reason"):
        st.caption(txt(p, "hr_reason"))

    b1, b2 = st.columns(2)
    if b1.button("➕ Add to slip", width="stretch", key=f"mslip_{name_of(p)}"):
        st.session_state.slip.append(f"{name_of(p)} — {txt(p, 'best_bet_type', default='HR')}")
        st.rerun()
    if b2.button("⭐ Watch", width="stretch", key=f"mwatch_{name_of(p)}"):
        if name_of(p) not in st.session_state.watch:
            st.session_state.watch.append(name_of(p))
            persist_watch()
        st.rerun()


def player_card(
    p: Dict[str, Any],
    rank: Optional[int] = None,
    kind: str = "hr",
    left_label: str = "",
    left_color: str = "",
    open_key: str = "",
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
    # Opens the modal. Streamlit can't attach a click to the card HTML itself,
    # so this sits directly under it as the equivalent of clicking the card.
    if open_key:
        if st.button("View details", key=f"open_{open_key}_{p.get('player_id')}_{rank or 0}",
                     width="stretch"):
            player_modal(p)


# Moved above the tab bodies: the Games tab now calls this to show both
# starters, and tab bodies execute at import time -- leaving the def down
# in the Pitchers section made it a NameError on first render.
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


# Plain-English definitions, in one place and reused everywhere. Written for
# someone who has never read a stat line: what the number means, and which
# direction is good. Every table and metric that uses one of these names
# points back here rather than assuming the reader already knows.
SCORE_HELP = {
    "HR": "Home-run score, 0-100. How likely this hitter is to go deep today. Higher is better.",
    "HRR": "Home run + Runs score, 0-100. Broader production — runs scored and driven in, not just homers.",
    "Hit": "Hit score, 0-100. How likely he is to get at least one base hit. This is the safest of the scores.",
    "TB": "Total-bases score, 0-100. Doubles, triples and homers — extra-base damage rather than singles.",
    "Damage": "Damage conversion, 0-100. When he does square a ball up, how often it turns into real damage.",
    "PMix": "Pitch-mix score, 0-100. How well his swing matches the pitches this particular starter throws.",
    "PMatch": "Pitch-type match, 0-100. Same idea as PMix, focused on his single best pitch type.",
    "375+": "Batted balls hit 375 feet or more recently. Raw power showing up in games.",
    "400+": "Batted balls hit 400+ feet recently. Genuine no-doubt distance.",
    "IHR": "Ideal HR rate — share of his batted balls hit at both the speed and angle that produce homers.",
    "K%": "Strikeout rate. HIGHER IS WORSE for a hitter — he has to put the ball in play to hit a homer.",
    "Spot": "Where he bats in the order, 1-9. Earlier spots get more plate appearances.",
    "P HR/9": "Home runs the opposing starter allows per 9 innings. League average ≈ 1.2; higher is better for hitters.",
}

GLOSSARY_MD = """
**The five scores** — all 0–100, all "higher is better", all built by the bot
from that day's matchup:

| Score | Plain English |
|---|---|
| **HR** | Chance he hits a home run today |
| **HRR** | Chance he scores or drives in runs |
| **Hit** | Chance he gets at least one base hit — the safest score |
| **TB** | Chance of extra bases (doubles, triples, homers) |
| **Damage** | When he connects, how often it actually hurts |

**Reading the colours.** Light green is good, dark green is weak. That holds
everywhere on the site — charts, heat maps and tables all use the same scale,
so you never have to check a legend.

**Supporting numbers**

- **PMix / PMatch** — does his swing match what this pitcher throws?
- **375+ / 400+** — how many balls he's recently hit that far. Real power, in games.
- **IHR** — share of his contact hit at both the speed *and* angle that make homers.
- **K%** — strikeout rate. This is the one where **higher is worse**.
- **HR/9** — homers the opposing starter gives up per 9 innings. Around 1.2 is
  average, so anything well above that is a pitcher you want to attack.

**The one habit worth having:** always check the sample size before you trust a
split. A .400 average over 9 plate appearances is noise; over 200 it's a
player. Anywhere the site shows you a rate, it shows you the PA count next to
it for exactly this reason.
"""


def hr9_verdict(hr9: float) -> tuple:
    """Plain-English read on a starter's home-run rate.

    League average sits around 1.2 HR/9. Someone who has never looked at a
    pitching line has no way to know whether 1.84 is good or bad, so the
    number is always shown with a word next to it.
    """
    if hr9 <= 0:
        return ("—", C["text3"], "no data")
    if hr9 >= 1.8:
        return ("VERY HITTABLE", C["red"], "gives up homers far more than most starters")
    if hr9 >= 1.4:
        return ("HITTABLE", "#fb923c", "gives up more homers than average")
    if hr9 >= 1.0:
        return ("AVERAGE", C["yellow"], "roughly league-average for homers")
    if hr9 >= 0.7:
        return ("TOUGH", "#4cb96a", "harder than most to take deep")
    return ("VERY TOUGH", C["green"], "one of the hardest starters to homer off")


def pitcher_strip(e: Dict[str, Any]) -> str:
    """Compact starter card: who's pitching and how homer-friendly he is."""
    verdict, vcolor, _ = hr9_verdict(n(e.get("hr9")))
    weak = e.get("weak_side") or "—"
    return (
        f"<div style='background:rgba(255,255,255,.03);border:1px solid {C['border']};"
        f"border-left:3px solid {vcolor};border-radius:10px;padding:10px 12px'>"
        f"<div style='display:flex;justify-content:space-between;align-items:baseline'>"
        f"<div><span style='font-size:14px;font-weight:700'>{e.get('pitcher_name', 'TBD')}</span>"
        f"<span style='font-size:10px;color:{C['text3']};margin-left:6px'>"
        f"{e.get('throws', '?')}HP · {e.get('team', '')}</span></div>"
        f"<span style='font-size:9px;font-weight:800;letter-spacing:.06em;color:{vcolor}'>"
        f"{verdict}</span></div>"
        f"<div style='display:flex;gap:14px;margin-top:7px;font-family:{NUM_FONT}'>"
        + "".join(
            f"<div><div style='font-size:8.5px;color:{C['text3']};letter-spacing:.05em'>"
            f"{lbl}</div><div style='font-size:15px;font-weight:800;color:{colr}'>{val}</div></div>"
            for lbl, val, colr in (
                ("HR/9", f"{n(e.get('hr9')):.2f}", vcolor),
                ("ERA", f"{n(e.get('era')):.2f}", C["text"]),
                ("WHIP", f"{n(e.get('whip')):.2f}", C["text"]),
                ("K/9", f"{n(e.get('k9')):.1f}", C["text"]),
            )
        )
        + "</div>"
        f"<div style='font-size:10px;color:{C['text3']};margin-top:7px'>"
        f"Weak side: <b style='color:{C['text2']}'>{weak}</b>"
        + (f" · {e.get('attack')}" if e.get("attack") else "")
        + (f" · <span style='color:{C['yellow']}'>⭐ {e['weak_spots']} weak spot"
           f"{'s' if e.get('weak_spots', 0) != 1 else ''} in the order</span>"
           if e.get("weak_spots") else "")
        + "</div></div>"
    )


@st.dialog("Pitcher", width="large")
def pitcher_modal(e: Dict[str, Any]) -> None:
    """Starter detail without leaving the Games tab.

    Previously the only way to see a pitcher's arsenal or where he gets hurt
    in the order was to leave, go to the Pitchers tab and find him again.
    """
    row = e.get("row") or {}
    verdict, vcolor, blurb = hr9_verdict(n(e.get("hr9")))

    st.markdown(f"### {e.get('pitcher_name', 'TBD')}")
    st.caption(
        f"{e.get('throws', '?')}HP · {e.get('team', '')} vs {e.get('facing', '')} · "
        f"{e.get('venue', '')}"
    )
    st.markdown(
        f"<span style='font-size:11px;font-weight:800;color:{vcolor}'>{verdict}</span>"
        f"<span style='font-size:11px;color:{C['text3']}'> — {blurb}</span>",
        unsafe_allow_html=True,
    )

    k = st.columns(5)
    k[0].metric("HR/9", f"{n(e.get('hr9')):.2f}", help="Home runs allowed per 9 innings. League average ≈ 1.2 — higher is better for hitters.")
    k[1].metric("ERA", f"{n(e.get('era')):.2f}", help="Earned runs per 9 innings. Higher = easier to score on.")
    k[2].metric("WHIP", f"{n(e.get('whip')):.2f}", help="Walks + hits per inning. Above ~1.30 means traffic on the bases.")
    k[3].metric("K/9", f"{n(e.get('k9')):.1f}", help="Strikeouts per 9. High K/9 means fewer balls in play, which caps homer chances.")
    k[4].metric("Weak side", e.get("weak_side") or "—", help="The batter handedness this pitcher struggles with most.")

    if txt(row, "pitcher_arsenal_summary"):
        st.markdown(f"**Arsenal** — {txt(row, 'pitcher_arsenal_summary')}")

    # Where he gets hurt in the batting order — the "does he get hurt in the
    # 4-hole" question, answerable right here instead of two tabs away.
    det = load_detail("pitcher", e.get("pitcher_id"), slate)
    spots = det.get("pitcher_lineup_spot_damage") or {}
    if isinstance(spots, dict) and spots:
        rows = []
        for k_, v in spots.items():
            if not isinstance(v, dict):
                continue
            try:
                spot = int(str(k_).strip())
            except ValueError:
                continue
            rows.append({
                "Spot": spot, "PA": int(n(v.get("pa"))), "HR": int(n(v.get("hr"))),
                "AVG": round(n(v.get("avg")), 3), "SLG": round(n(v.get("slg")), 3),
                "OPS": round(n(v.get("ops")), 3),
            })
        if rows:
            sdf = pd.DataFrame(rows).sort_values("Spot")
            st.markdown("**Where he gets hurt in the order**")
            hbar([f"{int(r['Spot'])}-hole  ({int(r['PA'])} PA)" for _, r in sdf.iterrows()],
                 [float(r["OPS"]) for _, r in sdf.iterrows()],
                 "OPS allowed by lineup spot", fmt="{:.3f}")
            st.caption(
                "OPS allowed to each batting-order slot. Longer, lighter bars "
                "are the spots he's been beaten from. Watch the PA count — a "
                "big number on 8 plate appearances is noise, not a trend."
            )
            st.dataframe(sdf, width="stretch", hide_index=True)

    if e.get("lineup"):
        st.markdown(f"**Lineup facing him ({len(e['lineup'])})**")
        st.dataframe(pd.DataFrame([{
            "Spot": x.get("lineup_spot"), "Player": name_of(x),
            "B": txt(x, "bats", default="?"), "HR": round(hr_score(x), 1),
            "HRR": round(prod_score(x), 1), "Hit": round(hit_score(x), 1),
            "TB": round(tb_score(x), 1),
            "⭐": "⭐" if x.get("weak_spot_flag") else "",
        } for x in e["lineup"]]), width="stretch", hide_index=True)


def game_pick_tile(p: Dict[str, Any], role: str) -> str:
    """One game pick as a compact tile.

    The Games tab used to stack full-width player_cards, so a single game ran
    five screens deep and comparing its picks meant scrolling back and forth.
    These sit side by side: the whole game reads at a glance, and the detail
    that was on the wide card lives in the modal a click away.
    """
    emoji, label, color = GAME_ROLE_LABEL[role]
    kind = GAME_ROLE_SCORE.get(role, "hr")
    score = score_for(p, kind)
    grade = grade_for(p, kind)

    hrw = hrw_badge(p)
    flags = ""
    if p.get("weak_spot_flag"):
        flags += "<span title='Weak spot'>⭐</span>"
    if is_aligned(p):
        flags += "<span title='Aligned'>🧩</span>"
    if nn(p, "last5_hr") > 0:
        flags += f"<span style='color:{C['orange']}'>L5 {int(nn(p, 'last5_hr'))}HR</span>"

    # HRW as a fifth bar in the same stack as HR/HRR/HIT/TB, coloured by its
    # zone. It was a tinted callout block, which made it shout louder than
    # the four scores above it; as a bar it reads as one more number in the
    # same row of numbers, which is what it is.
    hrw_row = bar("HRW", hrw[3], 100, hrw[1]) if hrw else ""

    return (
        f"<div style='background:rgba(255,255,255,.03);border:1px solid {C['border']};"
        f"border-top:3px solid {color};border-radius:10px;padding:9px 11px;"
        f"height:100%'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<span style='font-size:10px;font-weight:800;letter-spacing:.07em;color:{color}'>"
        f"{emoji} {label.upper()}</span>"
        f"<span style='font-size:11px;font-weight:800;color:{color}'>{grade}</span></div>"
        f"<div style='font-size:13px;font-weight:700;line-height:1.25;margin:5px 0 1px'>"
        f"{name_of(p)}</div>"
        f"<div style='font-size:9.5px;color:{C['text3']};margin-bottom:6px'>"
        f"spot {p.get('lineup_spot', '—')} · {txt(p, 'bats', default='?')}HB · "
        f"{int(nn(p, 'season_hr'))} HR</div>"
        f"<div style='font-family:{NUM_FONT};font-size:24px;font-weight:800;"
        f"line-height:1'>{score:.0f}"
        f"<span style='font-size:9px;color:{C['text3']};font-weight:600;"
        f"margin-left:4px'>{label.upper()}</span></div>"
        + f"<div style='margin-top:7px'>{bar('HR', hr_score(p), 100, '#f97316')}"
        f"{bar('HRR', prod_score(p), 100, '#22d3ee')}"
        f"{bar('HIT', hit_score(p), 100, '#a78bfa')}"
        f"{bar('TB', tb_score(p), 100, '#34d399')}"
        + hrw_row
        + "</div>"
        + (f"<div style='font-size:9.5px;display:flex;gap:6px;margin-top:4px;"
           f"color:{C['text3']}'>{flags}</div>" if flags else "")
        + "</div>"
    )


def rows_to_df(rows: List[Dict[str, Any]], cols: List[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    return df[[c for c in cols if c in df.columns]]


# player_splits.py abbreviates ("Mon"), but older payloads spelled the day out.
# Both are listed so the calendar ordering holds either way; anything that
# matches neither is left in whatever order the payload had.
DOW_ORDER = ["Mon", "Monday", "Tue", "Tuesday", "Wed", "Wednesday",
             "Thu", "Thursday", "Fri", "Friday", "Sat", "Saturday",
             "Sun", "Sunday"]

SPLIT_FAMILIES = [
    ("day_night", "Day vs Night"),
    ("home_away", "Home vs Away"),
    ("win_loss", "Team Wins vs Losses"),
    ("day_of_week", "By Day of Week"),
]


def _split_frame(data: Dict[str, Any], key: str) -> Optional[pd.DataFrame]:
    """Split payload -> DataFrame, with day-of-week in calendar order.

    Sorting matters here: the JSON preserves whatever order the gameLog
    happened to produce, so without this the weekday chart came out as
    Thursday, Monday, Saturday... which is unreadable as a trend.
    """
    if not data:
        return None
    df = pd.DataFrame(data).T
    if df.empty:
        return None
    if key == "day_of_week":
        present = [d for d in DOW_ORDER if d in df.index]
        if present:
            df = df.loc[present]
    return df


def split_chart(df: pd.DataFrame, title: str, height: int = 300) -> None:
    """Each split as a deviation from the player's own baseline.

    This was a grouped bar chart of AVG/OBP/SLG/OPS side by side. It was
    accurate and hard to read: four bars per bucket, twelve bars for a
    weekday chart, and the actual question -- "is he BETTER on this day or
    worse?" -- meant eyeballing tiny differences between adjacent bars and
    holding the comparison in your head.

    Now there's one row per bucket and one bar per row, drawn left or right
    of a centre line at the player's overall OPS. Right and light = better
    than his normal self, left and dark = worse. The raw OPS and the sample
    size sit on the row, so nothing is lost, and a split with 9 PA is
    visibly not a split with 200.
    """
    if df is None or df.empty:
        return
    d = df.copy()
    if "OPS" not in d.columns:
        return
    ops = pd.to_numeric(d["OPS"], errors="coerce")
    pa = pd.to_numeric(d["PA"], errors="coerce") if "PA" in d.columns else pd.Series(
        [0] * len(d), index=d.index)
    hr = pd.to_numeric(d["HR"], errors="coerce") if "HR" in d.columns else pd.Series(
        [0] * len(d), index=d.index)

    # Baseline is PA-weighted, so it's the player's true overall rate rather
    # than the average of his buckets -- otherwise a 9-PA Tuesday would pull
    # the centre line as hard as a 200-PA home split.
    total_pa = float(pa.sum())
    base = float((ops * pa).sum() / total_pa) if total_pa else float(ops.mean())

    labels, deltas, texts, colors, hovers = [], [], [], [], []
    for idx in d.index:
        o, p_, h = float(ops.get(idx, 0)), float(pa.get(idx, 0)), float(hr.get(idx, 0))
        delta = o - base
        labels.append(f"{idx}   {int(p_)} PA")
        deltas.append(delta)
        texts.append(f"{o:.3f}")
        # Light green above baseline, dark green below -- the site's
        # convention. Thin sample gets muted so it can't shout.
        if p_ < 25:
            colors.append("#3f6b52")
        else:
            colors.append("#b7f7c9" if delta >= 0 else "#0f6b3c")
        hovers.append(
            f"{idx}<br>OPS {o:.3f} ({delta:+.3f} vs {base:.3f})"
            f"<br>{int(p_)} PA · {int(h)} HR"
        )

    fig = go.Figure(go.Bar(
        x=deltas, y=labels, orientation="h",
        marker=dict(color=colors),
        text=texts, textposition="outside",
        textfont=dict(size=10, color=C["text2"], family=NUM_FONT),
        hovertext=hovers, hoverinfo="text",
    ))
    fig.add_vline(x=0, line=dict(color=C["text3"], width=1, dash="dot"))
    fig.add_annotation(
        x=0, y=1.06, yref="paper", text=f"his overall {base:.3f}",
        showarrow=False, font=dict(size=9, color=C["text3"]),
    )
    span = max(0.08, float(max(abs(v) for v in deltas)) * 1.55) if deltas else 0.1
    _layout(fig, height, title)
    fig.update_xaxes(range=[-span, span], zeroline=False, showticklabels=False,
                     showgrid=False)
    fig.update_yaxes(autorange="reversed", tickfont=dict(size=10))
    st.plotly_chart(fig, width="stretch")


def split_table_html(df: pd.DataFrame) -> str:
    """Split line in the shape a baseball reader expects: H-AB, then rates,
    then the counting stats.

    Modelled on a standard splits table, with two deliberate differences.
    First, the rate cells are shaded on the site's light-good / dark-green-bad
    scale rather than the usual red-to-green, so this table matches every
    other chart here instead of introducing a second colour language. Second,
    the shading is relative to THIS player's own best and worst split, not to
    a league scale -- the question being asked is "where is he strongest",
    not "is he good".

    Hand-built HTML rather than pandas .style because that needs jinja2,
    which isn't guaranteed on Streamlit Cloud.
    """
    if df is None or df.empty:
        return ""

    def num(idx, col, default=0.0):
        try:
            return float(df.loc[idx, col]) if col in df.columns else default
        except (TypeError, ValueError):
            return default

    ramp = ["#0b4b30", "#12783f", "#2f9e52", "#4cb96a", "#7fd894", "#b7f7c9"]

    def shade(idx, col):
        """Colour a rate cell by where it sits between this player's worst
        and best value for that stat."""
        if col not in df.columns:
            return ""
        vals = [num(i, col) for i in df.index]
        lo, hi = min(vals), max(vals)
        if hi <= lo:
            return ""
        pos = (num(idx, col) - lo) / (hi - lo)
        bg = ramp[min(len(ramp) - 1, int(pos * len(ramp)))]
        fg = "#06281a" if pos >= 0.6 else C["text"]
        return f"background:{bg};color:{fg};font-weight:700"

    cnt_cols = [c for c in ("HR", "XBH", "R", "RBI", "BB", "K") if c in df.columns]
    pad = "padding:5px 8px"
    head = (
        f"<th style='text-align:left;{pad}'>SPLIT</th>"
        f"<th style='{pad}'>H-AB</th>"
        + "".join(f"<th style='{pad}'>{c}</th>"
                  for c in ("AVG", "OBP", "SLG", "OPS", "ISO"))
        + "".join(f"<th style='{pad}'>{c}</th>" for c in cnt_cols)
    )

    rows = []
    for idx in df.index:
        h, ab, pa = int(num(idx, "H")), int(num(idx, "AB")), int(num(idx, "PA"))
        thin = pa < 25          # too few PA to read anything into
        name_style = f"color:{C['text3']}" if thin else f"color:{C['text']}"
        cells = "".join(
            f"<td style='{pad};{'' if thin else shade(idx, c)}'>{num(idx, c):.3f}</td>"
            for c in ("AVG", "OBP", "SLG", "OPS", "ISO")
        )
        rows.append(
            f"<tr style='border-top:1px solid {C['border']}'>"
            f"<td style='text-align:left;{pad};{name_style};font-weight:600'>{idx}"
            + (f"<span style='color:{C['text3']};font-size:9px'> · thin</span>"
               if thin else "")
            + "</td>"
            f"<td style='{pad};color:{C['text2']}'>{h}-{ab}</td>"
            + cells
            + "".join(f"<td style='{pad};color:{C['text2']}'>{int(num(idx, c))}</td>"
                      for c in cnt_cols)
            + "</tr>"
        )

    return (
        f"<div style='overflow:auto;border:1px solid {C['border']};border-radius:10px;"
        "margin:2px 0 14px'>"
        f"<table style='width:100%;border-collapse:collapse;font-family:{NUM_FONT};"
        "font-size:11px;text-align:right'>"
        f"<thead><tr style='background:{C['bg3']};color:{C['text3']};"
        f"font-size:9.5px;letter-spacing:.04em'>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def render_splits(p: Dict[str, Any], slate_label: str, compact: bool = False) -> None:
    """Situational splits as charts. `compact` drops the backing tables so the
    same renderer can be dropped into the player modal without burying it."""
    sp = load_splits(p.get("player_id"), slate_label)
    if not sp:
        st.info(
            "No situational splits published for this hitter yet — they land "
            "with the next **Player Splits** workflow run."
        )
        return

    st.caption(
        f"{int(nn(sp, 'games_logged'))} games logged · season {sp.get('season', '')} — "
        "bars run right of the line when he hits **better** than his own "
        "season average in that situation, left when he hits worse. Faded "
        "bars are under 25 plate appearances: too small to trust."
    )
    for key, title in SPLIT_FAMILIES:
        df = _split_frame(sp.get(key) or {}, key)
        if df is None:
            continue
        split_chart(df, title, height=max(190, 42 * len(df) + 95))
        st.markdown(split_table_html(df), unsafe_allow_html=True)
        # The "full table" expander that used to sit here is gone: the split
        # table above now carries the same columns in a readable order, so
        # the expander was the same numbers twice, once unformatted.


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
    # Explicit keys so the "Reset filters" button below can clear them all in
    # one go -- without keys Streamlit auto-generates names that can't be
    # popped from session_state.
    team_pick = st.multiselect("Team", teams, key="f_team")
    query = st.text_input("Search player / pitcher", "", key="f_query")
    lane_label = st.selectbox("Lane", [lbl for _, lbl in LANES], key="f_lane")
    lane_key = next(k for k, lbl in LANES if lbl == lane_label)
    min_hr = st.slider("Min HR score", 0, 100, 0, step=5, key="f_minhr")
    confirmed_only = st.checkbox("Confirmed lineups only", key="f_conf")
    aligned_only = st.checkbox("🧩 Aligned only", key="f_aligned")
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

# A filter combination that matches nobody used to leave every tab showing
# "No players match these filters" with no hint as to WHICH filter did it or
# how to undo it -- easy to hit with Aligned-only plus a min HR score, and it
# looks exactly like the data failing to load.
_active_filters = [
    lbl for lbl, on in (
        (f"Team: {', '.join(team_pick)}" if team_pick else "", bool(team_pick)),
        (f"Search: “{query}”", bool(query)),
        (f"Min HR ≥ {min_hr}", bool(min_hr)),
        ("Confirmed lineups only", bool(confirmed_only)),
        ("Aligned only", bool(aligned_only)),
        (f"Lane: {lane_key}", lane_key != "all"),
    ) if on
]
if _active_filters:
    with st.sidebar:
        st.caption("**Active filters** — " + " · ".join(_active_filters))
        if st.button("↩︎ Reset filters", width="stretch", key="resetfilters"):
            for k in ("f_team", "f_query", "f_minhr", "f_conf", "f_aligned", "f_lane"):
                st.session_state.pop(k, None)
            st.rerun()

# ── HEADER ──────────────────────────────────────────────────────────────────
# Metrics sit BELOW the title on their own full-width row. They used to share
# a two-column split with it, and because st.title is so much taller than a
# metric, the numbers rendered level with the top of the page and got clipped
# by the header bar -- every screenshot showed "HR 80+" cut in half.
st.title(f"{slate.capitalize()}'s Slate")
games = len({p.get("game_pk") for p in players})
filtered_out = len(players) - len(view)
st.caption(
    f"{games} games · {len(players)} hitters"
    + (f" · **{filtered_out} hidden by filters**" if filtered_out else "")
)

hrs = [hr_score(p) for p in players]
m = st.columns(6)
m[0].metric("Games", games)
m[1].metric("Hitters", len(view))
m[2].metric("HR 80+", sum(1 for x in hrs if x >= 80))
m[3].metric("HR 90+", sum(1 for x in hrs if x >= 90))
m[4].metric("🧩 Aligned", sum(1 for p in players if is_aligned(p)))
m[5].metric("✅ Confirmed", sum(1 for p in players if p.get("lineup_confirmed")))

st.divider()

# Tab order matches the old site's lib/theme.js TABS list, so muscle memory
# carries over. Player is new: Streamlit has no modal, so what used to be
# PlayerModal is a tab instead.
#
# Labels are kept SHORT on purpose. At 16 tabs the full names ("Pair History",
# "Scoreboard", "Watchlist") overflowed the strip, so Streamlit collapsed the
# tail behind a scroll arrow -- Spray and Guide were unreachable without
# noticing the little chevron. These fit on one row.
(tab_games, tab_board, tab_due, tab_hitshrr, tab_pitchers, tab_pairs, tab_bot,
 tab_pools, tab_pairhist, tab_scoreboard, tab_leaders, tab_results, tab_player,
 tab_watch, tab_spray, tab_guide) = st.tabs([
    "🗓️ Games", "🏆 HR", "💣 Due", "💥 Hits", "⚾ Pitchers",
    "🎯 Pairs", "🤖 Bot", "🧩 Pools", "🧬 History", "📊 Board",
    "🥇 Leaders", "✅ Results", "🔍 Player", "⭐ Watch", "💦 Spray", "📖 Guide",
])

# ── BOARD ───────────────────────────────────────────────────────────────────
with tab_board:
    c1, c2 = st.columns([2, 1])
    kind_label = c1.selectbox("Board type", ["HR", "HRR", "Hit", "TB (Base)"])
    kind = {"HR": "hr", "HRR": "hrr", "Hit": "hit", "TB (Base)": "tb"}[kind_label]
    top_n = c2.number_input("Show top", 5, 200, 25, step=5)

    ranked = sorted(view, key=lambda p: score_for(p, kind), reverse=True)[: int(top_n)]
    if not ranked:
        st.info("No players match these filters.")

    if ranked:
        v1, v2 = st.columns([3, 2])
        with v1:
            hbar([name_of(p) for p in ranked[:15]],
                 [round(score_for(p, kind), 1) for p in ranked[:15]],
                 f"Top 15 by {kind_label} score",
                 ref=float(pd.Series([score_for(x, kind) for x in players]).median()),
                 ref_label="slate median")
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
        player_card(p, i, kind, open_key='board')

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
    # Deliberately collapsed. Someone who already knows the board never opens
    # it; someone who doesn't would otherwise be looking at "Med HRW 41.5"
    # with no idea what any of it means or where to start.
    with st.expander("🆕 New here? Start with this", expanded=False):
        st.markdown(
            "**What this site does.** Every morning a bot scores each hitter "
            "on today's slate for how likely he is to homer, get a hit, or do "
            "damage. Everything you see is built from that.\n\n"
            "**How to use this page in 30 seconds:**\n\n"
            "1. The chart below ranks today's games. Longer, lighter bars are "
            "better games for hitters.\n"
            "2. Open the top game. You'll get both starting pitchers and five "
            "picks — the best home-run bet, the safest hit, and so on.\n"
            "3. Click **Details** on anyone to see why the bot likes him.\n\n"
            "**The only rule that matters:** a great-looking rate on a tiny "
            "sample isn't real. Wherever the site shows a rate, it shows the "
            "number of plate appearances behind it. Check that first."
        )
        st.markdown(GLOSSARY_MD)

    by_game: Dict[Any, List[Dict[str, Any]]] = {}
    for p in view:
        by_game.setdefault(p.get("game_pk"), []).append(p)


    def game_start(rows: List[Dict[str, Any]]) -> str:
        """ISO start time for a game, or a far-future string so games with no
        time sort last instead of jumping to the front of a chronological
        list."""
        for r in rows:
            t = str(r.get("game_time") or "").strip()
            if t:
                return t
        return "9999"

    def local_time(rows: List[Dict[str, Any]]) -> str:
        """Start time as Phoenix local, e.g. '10:35 AM'. The feed stores UTC
        with a Z suffix; Phoenix is UTC-7 all year, so this is a fixed shift
        with no DST branch to get wrong."""
        raw = game_start(rows)
        if raw == "9999":
            return "TBD"
        try:
            base = dt.datetime.strptime(raw.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return "TBD"
        return (base - dt.timedelta(hours=7)).strftime("%-I:%M %p")

    # First pitch order matters when you're actually playing the slate --
    # you need to know what locks in twenty minutes, which a strength
    # ranking can't tell you.
    game_sort = st.radio(
        "Order games by", ["Best first", "First pitch"],
        horizontal=True, key="gameorder",
        help="Best first ranks by the strongest hitter in the game. "
             "First pitch puts them in start-time order.",
    )
    if game_sort == "First pitch":
        order = sorted(by_game.items(), key=lambda kv: game_start(kv[1]))
    else:
        order = sorted(by_game.items(),
                       key=lambda kv: max(hr_score(x) for x in kv[1]), reverse=True)

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

        rl, rr = st.columns([3, 2])
        with rl:
            hbar(
                ranked_games["Game"].tolist(),
                ranked_games[metric_choice].tolist(),
                f"Games ranked by {metric_choice}",
                ref=float(ranked_games[metric_choice].median()), ref_label="slate median",
            )
        with rr:
            # Radar of the top games across every metric at once. The bar chart
            # answers "which game is best on ONE metric"; this answers "what
            # SHAPE is that game" -- a game that's 50 on everything and a game
            # carried by a single 96 both rank high on a bar and look nothing
            # alike here. Slate median is drawn as a filled baseline so a
            # game's edges read as above or below par without doing the maths.
            radar_axes = ["Game Score", "Med HR", "Med HRR", "Med HRW", "Med DC", "Top HR"]
            top_n = ranked_games.head(3)
            fig = go.Figure()
            fig.add_trace(go.Scatterpolar(
                r=[float(gdf[a].median()) for a in radar_axes] + [float(gdf[radar_axes[0]].median())],
                theta=radar_axes + [radar_axes[0]],
                fill="toself", name="slate median",
                line=dict(color=C["text3"], width=1, dash="dot"),
                fillcolor="rgba(148,163,184,0.10)",
            ))
            ramp = [C["orange"], C["cyan"], C["purple"]]
            for i, (_, row) in enumerate(top_n.iterrows()):
                fig.add_trace(go.Scatterpolar(
                    r=[float(row[a]) for a in radar_axes] + [float(row[radar_axes[0]])],
                    theta=radar_axes + [radar_axes[0]],
                    name=str(row["Game"]), line=dict(color=ramp[i % len(ramp)], width=2),
                ))
            fig.update_layout(
                polar=dict(
                    bgcolor="rgba(0,0,0,0)",
                    radialaxis=dict(visible=True, range=[0, 100], gridcolor=C["border"],
                                    tickfont=dict(size=8, color=C["text3"])),
                    angularaxis=dict(gridcolor=C["border"],
                                     tickfont=dict(size=9, color=C["text2"])),
                ),
            )
            _layout(fig, 360, f"Top 3 by {metric_choice} — shape vs slate")
            fig.update_layout(showlegend=True,
                              legend=dict(orientation="h", y=-0.12, x=0,
                                          font=dict(size=9), bgcolor="rgba(0,0,0,0)"))
            st.plotly_chart(fig, width="stretch")

        st.caption(
            "Game Score = median across every hitter of that hitter's median "
            "HR / HRR / HRW / DC. Higher = more of the lineup is live for a homer. "
            "On the radar, a wide even shape is a live lineup top to bottom; a "
            "single long spike is one hitter carrying the game."
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

    # Game Score per game, so the header can carry the same number the ranking
    # chart above is sorted by -- otherwise you rank games by Game Score, then
    # open one and have no idea what its score was.
    game_score_by_pk = {
        pk: float(pd.Series([
            pd.Series([hr_score(x), prod_score(x), nn(x, "hrw_score"),
                       nn(x, "damage_conversion_score")]).median()
            for x in rows
        ]).median())
        for pk, rows in by_game.items()
    }

    for gpk, gp in order:
        head = max(gp, key=hr_score)
        conf = "✅" if head.get("lineup_confirmed") else "◻︎"
        gs = game_score_by_pk.get(gpk, 0.0)
        with st.expander(
            f"{conf}  {local_time(gp)}   ·   {team_of(head)} vs {opp_of(head)}   ·   "
            f"Game Score {gs:.1f}   ·   top HR {hr_score(head):.0f}   ·   "
            f"{txt(head, 'venue_name')}"
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

            # BOTH starters, side by side. The header only ever named the
            # pitcher facing the strongest hitter, so half of every matchup
            # was invisible -- you could see that CHC's lineup was live
            # without ever learning who PIT was sending out.
            st.markdown("**Starting pitchers**")
            game_arms = group_pitchers(gp)
            pcols = st.columns(max(1, len(game_arms)))
            for pc, arm in zip(pcols, game_arms):
                with pc:
                    st.markdown(pitcher_strip(arm), unsafe_allow_html=True)
                    if st.button("Pitcher details", width="stretch",
                                 key=f"gp_{gpk}_{arm.get('pitcher_id')}"):
                        pitcher_modal(arm)
            st.caption(
                "HR/9 is home runs allowed per 9 innings — league average is "
                "about 1.2, so higher means easier to take deep."
            )

            # THE GAME PICKS — the bot stamps players per game per role in
            # game_pick_role. Roles are NOT one-per-game: TOP/HR/CONTACT get
            # one each but HIT and HRR get TWO apiece, exactly as the .txt
            # report prints them. Collecting into a dict keyed by role kept
            # only the first of each and silently dropped two cards per game.
            picked: Dict[str, List[Dict[str, Any]]] = {}
            for p in gp:
                r = str(p.get("game_pick_role") or "").upper()
                if r in GAME_ROLE_LABEL:
                    picked.setdefault(r, []).append(p)

            if picked:
                # Exactly one pick per role: TOP, HR, HIT, HRR, TB -- five
                # tiles, one row, always the same five columns in the same
                # order. The bot stamps TWO players for HIT and HRR, which is
                # why this used to spill to a second row and give some games
                # seven cards and others five; the extras are the runners-up
                # and they're all still in the lineup table below. Keeping the
                # best of each by that role's OWN score, not by HR score --
                # the second HIT pick often out-scores the first on HR while
                # being the worse hit play.
                flat = []
                for r in GAME_ROLE_ORDER:
                    cands = picked.get(r) or []
                    if cands:
                        flat.append(
                            (r, max(cands, key=lambda x, _r=r: score_for(x, GAME_ROLE_SCORE[_r])))
                        )
                st.markdown(f"**Game picks** ({len(flat)})")
                cols = st.columns(5)
                for col, (r, p) in zip(cols, flat):
                    with col:
                        st.markdown(game_pick_tile(p, r), unsafe_allow_html=True)
                        if st.button("Details", width="stretch",
                                     key=f"gt_{gpk}_{r}_{p.get('player_id')}"):
                            player_modal(p)
            else:
                st.caption("No stamped game picks for this game — showing top HR scores.")
                top4 = sorted(gp, key=hr_score, reverse=True)[:5]
                cols = st.columns(5)
                for col, p in zip(cols, top4):
                    with col:
                        st.markdown(game_pick_tile(p, "HR"), unsafe_allow_html=True)
                        if st.button("Details", width="stretch",
                                     key=f"gf_{gpk}_{p.get('player_id')}"):
                            player_modal(p)

            # Lineups are split by team. Both clubs used to share one table
            # sorted by batting order, so it ran 1,1,2,2,3,3... -- two
            # different #3 hitters facing two different pitchers on adjacent
            # rows. You had to read the Team column on every line to know
            # whose order you were looking at.
            teams_here = sorted({team_of(p) for p in gp if team_of(p)})
            st.markdown("**Lineups**")
            if len(teams_here) > 1:
                pick_team = st.radio(
                    "Lineup", teams_here + ["Both"], horizontal=True,
                    key=f"lu_{gpk}", label_visibility="collapsed",
                )
            else:
                pick_team = teams_here[0] if teams_here else "Both"

            lineup_rows = gp if pick_team == "Both" else [
                p for p in gp if team_of(p) == pick_team
            ]
            opp_pitcher = txt(
                max(lineup_rows, key=hr_score) if lineup_rows else head, "pitcher_name"
            )
            if pick_team != "Both" and opp_pitcher:
                st.caption(f"{pick_team} vs {opp_pitcher} · {len(lineup_rows)} hitters")

            lineup_tbl = pd.DataFrame([{
                "Spot": p.get("lineup_spot"), "Player": name_of(p),
                "Team": team_of(p), "B": txt(p, "bats", default="?"),
                "Role": tier_role(p), "HR": round(hr_score(p), 1),
                "HRR": round(prod_score(p), 1), "Hit": round(hit_score(p), 1),
                "TB": round(tb_score(p), 1), "PMix": round(pmix_score(p), 1),
                "DC": round(nn(p, "damage_conversion_score"), 1),
                "Due": round(nn(p, "hr_due_score"), 1),
                "⭐": "⭐" if p.get("weak_spot_flag") else "",
            } for p in sorted(lineup_rows,
                              key=lambda x: (team_of(x),
                                             nn(x, "lineup_spot", default=99.0)))])
            # Team column is noise once you've filtered to one club.
            if pick_team != "Both" and "Team" in lineup_tbl.columns:
                lineup_tbl = lineup_tbl.drop(columns=["Team"])
            st.dataframe(lineup_tbl, width="stretch", hide_index=True)

# ── SCOREBOARD ──────────────────────────────────────────────────────────────
with tab_scoreboard:
    st.subheader("Scoreboard")
    st.caption(
        f"Every one of the {len(view)} hitters on the slate, all scores in one "
        "sortable grid. Click any column header to sort by it."
    )
    board = [{
        "Player": name_of(p), "Team": team_of(p), "Opp": opp_of(p),
        "Spot": p.get("lineup_spot"), "Role": tier_role(p),
        "HR": round(hr_score(p), 1), "HRR": round(prod_score(p), 1),
        "Hit": round(hit_score(p), 1), "TB": round(tb_score(p), 1),
        "Damage": round(nn(p, "damage_conversion_score"), 1),
        "PMix": round(pmix_score(p), 1),
        "PMatch": round(nn(p, "pitch_type_match_score"), 1),
        "375+": int(recent375(p)), "400+": int(recent400(p)),
        "IHR": round(ihr_val(p), 3), "K%": round(nn(p, "season_k_rate") * 100, 1),
        "Pitcher": txt(p, "pitcher_name"),
        "P HR/9": round(nn(p, "pitcher_hr9"), 2),
        "🧩": "🧩" if is_aligned(p) else "",
    } for p in view]
    bdf = pd.DataFrame(board)

    if bdf.empty:
        st.info("No players match your filters — clear them in the sidebar.")
    else:
        sb1, sb2, sb3 = st.columns([2, 2, 1])
        sb_sort = sb1.selectbox(
            "Sort by", ["HR", "HRR", "Hit", "TB", "Damage", "PMix", "PMatch",
                        "375+", "400+", "IHR", "K%", "Spot"], key="sbsort",
        )
        sb_cols = sb2.multiselect(
            "Extra columns", ["PMix", "PMatch", "375+", "400+", "IHR", "K%",
                              "Pitcher", "P HR/9"],
            default=["375+", "IHR", "Pitcher"], key="sbcols",
            help="The five scores are always shown. Add the rest as you need them.",
        )
        sb_top = sb3.checkbox("Top 50 only", value=False, key="sbtop")

        keep = (["Player", "Team", "Opp", "Spot", "Role",
                 "HR", "HRR", "Hit", "TB", "Damage"]
                + [c for c in sb_cols if c not in ("Damage",)] + ["🧩"])
        keep = list(dict.fromkeys(c for c in keep if c in bdf.columns))
        out = bdf.sort_values(sb_sort, ascending=(sb_sort == "Spot"))[keep]
        if sb_top:
            out = out.head(50)

        # Scores render as in-cell bars rather than bare numbers. On a 260-row
        # grid a column of decimals gives you nothing at a glance -- you have
        # to read and compare every one. Bars make the shape of the board
        # visible while keeping the exact value on the cell.
        colcfg = {
            c: st.column_config.ProgressColumn(
                c, format="%.1f", min_value=0, max_value=100,
                help=SCORE_HELP.get(c),
            )
            for c in ("HR", "HRR", "Hit", "TB", "Damage", "PMix", "PMatch")
            if c in out.columns
        }
        colcfg["Player"] = st.column_config.TextColumn("Player", pinned=True)

        st.dataframe(out, width="stretch", hide_index=True, height=620,
                     column_config=colcfg)
        st.download_button(
            "⬇️ CSV", out.to_csv(index=False).encode(),
            f"mlb_{slate}_scoreboard.csv", "text/csv", key="sbcsv",
        )
        with st.expander("What do these columns mean?"):
            st.markdown(GLOSSARY_MD)

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
        hbar([f"{name_of(p)} ({team_of(p)})" for p, _ in lead[:chart_n]],
             [round(v, 3) for _, v in lead[:chart_n]],
             f"Top {chart_n} — {stat}",
             fmt="{:.3f}" if lead[0][1] < 5 else "{:.1f}")
        st.dataframe(pd.DataFrame([{
            "#": i, "Player": name_of(p) + (" 🧩" if is_aligned(p) else ""),
            "Team": f"{team_of(p)} vs {opp_of(p)}", "Role": tier_role(p),
            stat: fmt.format(v),
            "HR": round(hr_score(p), 1), "HRR": round(prod_score(p), 1),
            "Spot": p.get("lineup_spot"), "Pitcher": txt(p, "pitcher_name"),
        } for i, (p, v) in enumerate(lead, start=1)]),
            width="stretch", hide_index=True, height=620)

# ── PITCHERS ────────────────────────────────────────────────────────────────
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
                spot_dmg = (load_detail("pitcher", e["pitcher_id"], slate)
                            .get("pitcher_lineup_spot_damage") or {})

                # Direct answer: pick a spot, get a verdict.
                if isinstance(spot_dmg, dict) and spot_dmg:
                    avail = sorted(int(v.get("spot", k)) for k, v in spot_dmg.items()
                                   if isinstance(v, dict))
                    if avail:
                        pick_spot = st.radio(
                            "Does he get hurt in the …", avail, horizontal=True,
                            format_func=lambda x: f"{x}-hole",
                            key=f"spotq_{e['pitcher_id']}",
                        )
                        srow = next((v for k, v in spot_dmg.items()
                                     if isinstance(v, dict)
                                     and int(v.get("spot", k)) == pick_spot), {})
                        if srow:
                            render_spot_answer(
                                spot_answer(srow, spot_dmg, spot_baseline(slate), pick_spot),
                                e["pitcher_name"], pick_spot,
                            )
                            here = [b for b in e["lineup"]
                                    if nn(b, "lineup_spot") == pick_spot]
                            if here:
                                b = here[0]
                                st.caption(
                                    f"Batting {pick_spot} today: **{name_of(b)}** "
                                    f"({txt(b, 'bats', default='?')}HB) — HR {hr_score(b):.0f} · "
                                    f"HRR {prod_score(b):.0f} · {tier_role(b)}"
                                )

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

# ── DUE BOARD ───────────────────────────────────────────────────────────────
# The bot's DUE BOMBER concept: hitters overdue for a homer. hr_due_score and
# hr_due_tag come straight from the model; games_since_last_hr and the
# expected-vs-actual HR gap over the recent PA window supply the evidence.
with tab_due:
    st.caption(
        "Hitters the model says are overdue — real power that hasn't converted "
        "recently. Drought alone isn't a signal, so this pairs it with the bot's "
        "due score and the gap between expected and actual HRs."
    )

    d1, d2, d3, d4 = st.columns(4)
    min_due = d1.slider("Min due score", 0, 100, 0, step=5)
    min_drought = d2.number_input("Min games since HR", 0, 60, 0, step=1)
    tag_opts = ["All"] + sorted({txt(p, "hr_due_tag") for p in players if txt(p, "hr_due_tag")})
    tag_pick = d3.selectbox("Due tag", tag_opts)
    due_n = d4.number_input("Show", 5, 200, 30, step=5, key="duen")

    def due_gap(p: Dict[str, Any]) -> float:
        """Expected minus actual HRs over the recent window: positive means the
        contact quality has been there and the homers haven't followed."""
        return nn(p, "expected_hrs_recent_window") - nn(p, "recent_hr_window")

    pool = [p for p in view
            if nn(p, "hr_due_score") >= min_due
            and nn(p, "games_since_last_hr") >= min_drought
            and (tag_pick == "All" or txt(p, "hr_due_tag") == tag_pick)]
    ranked_due = sorted(pool, key=lambda p: (nn(p, "hr_due_score"), due_gap(p)),
                        reverse=True)[: int(due_n)]

    if not ranked_due:
        st.info("No hitters match these due filters.")
    else:
        k = st.columns(5)
        k[0].metric("Qualifying", len(pool))
        k[1].metric("Due Elite Power",
                    sum(1 for p in players if txt(p, "hr_due_tag") == "Due Elite Power"))
        k[2].metric("Longest drought",
                    f"{max(int(nn(p, 'games_since_last_hr')) for p in players)} g")
        k[3].metric("Median due score",
                    f"{pd.Series([nn(p, 'hr_due_score') for p in players]).median():.1f}")
        k[4].metric("Biggest HR gap", f"{max(due_gap(p) for p in players):+.2f}")

        v1, v2 = st.columns([3, 2])
        with v1:
            hbar([f"{name_of(p)} ({team_of(p)})" for p in ranked_due[:15]],
                 [round(nn(p, "hr_due_score"), 1) for p in ranked_due[:15]],
                 "Most overdue",
                 ref=float(pd.Series([nn(x, "hr_due_score") for x in players]).median()),
                 ref_label="slate median")
        with v2:
            hm = pd.DataFrame([{
                "Player": name_of(p),
                "Due": nn(p, "hr_due_score"),
                "Drought": nn(p, "games_since_last_hr"),
                "HR gap": due_gap(p) * 20,
                "HR/PA": nn(p, "hr_per_pa") * 1000,
                "Barrel": barrel_rate(p) * 100,
            } for p in ranked_due[:15]]).set_index("Player")
            heatmap(hm, "Due profile (scaled)", height=320)

        for i, p in enumerate(ranked_due[:12], start=1):
            player_card(p, i, open_key="due")

        st.markdown("#### Full due board")
        due_tbl = pd.DataFrame([{
            "Player": name_of(p), "Team": team_of(p), "Opp": opp_of(p),
            "Spot": p.get("lineup_spot"),
            "Due score": round(nn(p, "hr_due_score"), 1),
            "Tag": txt(p, "hr_due_tag"),
            "Games since HR": int(nn(p, "games_since_last_hr")),
            "Exp HR": round(nn(p, "expected_hrs_recent_window"), 2),
            "Actual HR": int(nn(p, "recent_hr_window")),
            "HR gap": round(due_gap(p), 2),
            "PA window": int(nn(p, "recent_pa_window")),
            "HR/PA": round(nn(p, "hr_per_pa"), 4),
            "PA per HR": round(nn(p, "pa_per_hr"), 1),
            "Tier": txt(p, "hr_pa_tier"),
            "HR score": round(hr_score(p), 1),
            "Barrel%": round(barrel_rate(p) * 100, 1),
            "Pitcher": txt(p, "pitcher_name"),
            "P HR/9": round(nn(p, "pitcher_hr9"), 2),
        } for p in ranked_due])
        st.dataframe(due_tbl, width="stretch", hide_index=True, height=520)
        st.download_button("⬇️ CSV", due_tbl.to_csv(index=False).encode(),
                           file_name=f"mlb_{slate}_due_board.csv", mime="text/csv")
        st.caption(
            "HR gap = expected HRs minus actual over the recent PA window. "
            "Positive means the contact has been there without the results."
        )


# ── HITS / HRR ──────────────────────────────────────────────────────────────
with tab_hitshrr:
    st.subheader("Hits & HRR")
    st.caption(
        "The non-homer plays: base-hit floor, total bases, and HRR "
        "(runs + RBI). Use this when the HR board is thin."
    )
    hh1, hh2 = st.columns([2, 1])
    hh_kind = hh1.radio("Type", ["HRR (runs + RBI)", "Hit (base-hit floor)", "Base / XBH"], horizontal=True)
    hh_n = hh2.number_input("Show", 5, 100, 30, step=5, key="hhn")
    k = {"HRR (runs + RBI)": "hrr", "Hit (base-hit floor)": "hit", "Base / XBH": "tb"}[hh_kind]

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

RISK_COLOR = {"low": C["green"], "lower": C["green"], "medium": C["yellow"],
              "mid": C["yellow"], "high": C["red"], "higher": C["red"]}


def risk_color(risk: Any) -> str:
    return RISK_COLOR.get(str(risk or "").strip().lower(), C["text3"])


def combo_player_html(pl: Dict[str, Any]) -> str:
    """One player tile inside a pair or pool card.

    Replaces the raw dataframe these used to render as. A table of eleven
    numeric columns is technically complete and completely unreadable at a
    glance -- the point of a pair card is to see, in one look, who the two
    guys are and how hard each of them is hitting.
    """
    hr = n(pl.get("hr_score"))
    hrw = n(pl.get("hrw_score"))
    spot = pl.get("lineup_spot") or "—"
    return (
        f"<div style='flex:1;min-width:190px;background:rgba(255,255,255,.03);"
        f"border:1px solid {C['border']};border-radius:10px;padding:9px 11px'>"
        f"<div style='font-size:13px;font-weight:700;line-height:1.2'>"
        f"{pl.get('name', '?')}</div>"
        f"<div style='font-size:10px;color:{C['text3']};margin-bottom:6px'>"
        f"{pl.get('team', '')} vs {pl.get('opponent', '')} · spot {spot} · "
        f"{int(n(pl.get('season_hr')))} HR</div>"
        f"{bar('HR', hr, 100, C['orange'])}"
        f"{bar('HRW', hrw, 100, C['cyan'])}"
        f"<div style='font-size:10px;color:{C['text3']};margin-top:5px'>"
        f"vs {pl.get('pitcher_name', 'TBD')} "
        f"({pl.get('pitcher_throws', '?')}) · HR/9 {n(pl.get('pitcher_hr9')):.2f}</div>"
        f"</div>"
    )


def combo_card(title: str, subtitle: str, score: float, risk: Any,
               tags: Any, reason: str, players: List[Dict[str, Any]],
               accent: str) -> None:
    """Shared renderer for a pair card and a pool card -- same shape, different
    player count, so they may as well not drift apart."""
    tiles = "".join(combo_player_html(pl) for pl in (players or []))
    st.markdown(
        f"<div style='border:1px solid {C['border']};border-left:3px solid {accent};"
        f"border-radius:12px;padding:12px 14px;margin-bottom:12px;"
        f"background:rgba(255,255,255,.02)'>"
        f"<div style='display:flex;align-items:baseline;justify-content:space-between;"
        f"gap:10px;flex-wrap:wrap'>"
        f"<div><span style='font-size:10px;font-weight:800;letter-spacing:.07em;"
        f"color:{accent}'>{title}</span>"
        f"<div style='font-size:15px;font-weight:700;margin-top:2px'>{subtitle}</div></div>"
        f"<div style='text-align:right'>"
        f"<div style='font-family:{NUM_FONT};font-size:20px;font-weight:800;"
        f"line-height:1'>{score:.1f}</div>"
        f"<div style='font-size:9px;color:{risk_color(risk)};font-weight:700;"
        f"text-transform:uppercase'>{risk or '—'} risk</div></div></div>"
        f"<div style='margin:8px 0 4px'>{tags_html(tags, limit=8)}</div>"
        f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:8px'>{tiles}</div>"
        + (f"<div style='font-size:11px;color:{C['text3']};font-style:italic;"
           f"margin-top:9px'>{reason}</div>" if reason else "")
        + "</div>",
        unsafe_allow_html=True,
    )


with tab_pairs:
    pairs = pair_payload.get("recommended_pairs") or []
    if not pairs:
        st.info("No pair builder output published yet for this slate.")
    else:
        ph1, ph2 = st.columns([3, 2])
        ph1.caption(f"Pair Builder · {pair_payload.get('date', '')} · {len(pairs)} pairs")
        types = sorted({str(p.get("type", "PAIR")) for p in pairs})
        pick_type = ph2.selectbox("Type", ["All types"] + types, key="pairtype")

        shown = [p for p in pairs
                 if pick_type == "All types" or str(p.get("type", "PAIR")) == pick_type]

        # Score comparison up top: which pairs the model actually likes is the
        # first question, and it was previously only answerable by reading a
        # score out of every card's subtitle one at a time.
        if len(shown) > 1:
            hbar([" + ".join(str(x.get("name", "?")).split()[-1]
                             for x in (p.get("players") or []))
                  for p in shown],
                 [n(p.get("pair_score")) for p in shown],
                 "Pairs ranked by pair score", fmt="{:.1f}")

        for p in shown:
            names = " + ".join(str(x.get("name", "?")) for x in (p.get("players") or []))
            combo_card(str(p.get("type", "PAIR")), names, n(p.get("pair_score")),
                       p.get("risk"), p.get("tags"), str(p.get("reason") or ""),
                       p.get("players") or [], C["purple"])

with tab_pools:
    p4 = pair_payload.get("pools_4man") or []
    p6 = pair_payload.get("pools_6man") or []
    if not p4 and not p6:
        st.info("No pools published yet for this slate.")
    for title, pools, accent in (("4-man pools", p4, C["cyan"]),
                                 ("6-man pools", p6, C["orange"])):
        if not pools:
            continue
        st.markdown(f"#### {title}")
        if len(pools) > 1:
            hbar([str(pool.get("name", "Pool")) for pool in pools],
                 [n(pool.get("pool_score")) for pool in pools],
                 f"{title} ranked by pool score", fmt="{:.1f}")
        for pool in pools:
            plist = pool.get("players") or []
            combo_card(f"{len(plist)}-MAN POOL", str(pool.get("name", "Pool")),
                       n(pool.get("pool_score")), pool.get("risk"),
                       pool.get("tags"), str(pool.get("reason") or ""),
                       plist, accent)

# ── PAIR HISTORY ────────────────────────────────────────────────────────────
# The Pair History Bot has been publishing pair_history_summary.json on a
# schedule since the migration, but nothing in the app ever read it -- there
# was no tab, so the whole dataset was invisible. This is the search over it.
with tab_pairhist:
    st.subheader("Pair History")
    st.caption(
        "Which two hitters have actually gone deep on the SAME DAY this "
        "season — built from real HR events, not projections."
    )
    hist = load_json("public/data/current/pair_history_summary.json") or {}
    top_pairs = hist.get("top_pairs") or []

    if not top_pairs:
        st.info(
            "No pair history published yet. It's built by the **Pair History "
            "Bot** workflow — run it from the Actions tab and it'll appear here."
        )
    else:
        hm = st.columns(4)
        hm[0].metric("Pairs tracked", f"{int(n(hist.get('pair_count'))):,}")
        hm[1].metric("HR events", f"{int(n(hist.get('hr_event_count'))):,}")
        hm[2].metric("Games checked", f"{int(n(hist.get('games_checked'))):,}")
        hm[3].metric("Season", str(hist.get("season", "—")))
        st.caption(
            f"{hist.get('start_date', '')} → {hist.get('end_date', '')} · "
            f"showing top {len(top_pairs)} pairs by same-day HR history"
        )

        # Cross-reference against today's slate. This is the whole point of
        # keeping pair history: a duo that has gone deep together nine times
        # is only actionable if BOTH of them are in a lineup today. Without
        # this the tab was a season trivia table you had to check by hand.
        today_names = {name_of(p).lower(): p for p in players}

        s1, s2, s3, s4 = st.columns([3, 2, 2, 2])
        query = s1.text_input(
            "Search by player", placeholder="e.g. Judge, Ohtani, Schwarber",
            key="phq",
            help="Matches either player in the pair. Leave blank to see them all.",
        )
        min_hits = s2.slider("Min same-day HRs", 1, 10, 2, key="phmin")
        same_game_only = s3.checkbox("Same game only", key="phsg",
                                     help="Both HRs hit in the same ballgame.")
        playing_only = s4.checkbox(
            "Both playing today", value=True, key="phtoday",
            help="Only pairs where both hitters are in a lineup on this slate.",
        )

        def pair_names(rec: Dict[str, Any]) -> List[str]:
            return [str(x.get("name") or x.get("player_name") or "")
                    for x in (rec.get("players") or [])]

        q = (query or "").strip().lower()
        rows = []
        for rec in top_pairs:
            names = [nm for nm in pair_names(rec) if nm]
            if q and not any(q in nm.lower() for nm in names):
                continue
            same_day = int(n(rec.get("same_day_hr_count_season")))
            same_game = int(n(rec.get("same_game_hr_count")))
            if same_day < min_hits:
                continue
            if same_game_only and same_game < 1:
                continue

            live = [today_names.get(nm.lower()) for nm in names]
            both_live = all(x is not None for x in live) and len(live) == 2
            if playing_only and not both_live:
                continue

            rows.append({
                "": "🟢" if both_live else "",
                "Pair": " + ".join(names),
                "Teams": " / ".join(
                    str(x.get("team") or "?") for x in (rec.get("players") or [])),
                "Same-day HRs": same_day,
                "Same-game HRs": same_game,
                "Career": int(n(rec.get("same_day_hr_count_career"))),
                "Last hit": rec.get("last_same_day_hr") or "—",
                # Today's model scores for each half, so a historically hot
                # pair that both rate badly today is visibly not a play.
                "HR today": (round(sum(hr_score(x) for x in live) / 2, 1)
                             if both_live else None),
                "Boost": round(n(rec.get("history_boost")), 2),
            })

        if not rows:
            st.warning(
                "No pairs match those filters."
                + (" Try unticking **Both playing today** — most historical "
                   "pairs won't both be in action on any given slate."
                   if playing_only else "")
            )
        else:
            hdf = pd.DataFrame(rows).sort_values(
                ["Same-day HRs", "Same-game HRs"], ascending=False)
            live_n = int((hdf[""] == "🟢").sum())
            st.caption(
                f"{len(hdf)} pairs match · **{live_n}** with both hitters in a "
                "lineup today (🟢)"
            )

            top15 = hdf.head(15)
            ch1, ch2 = st.columns(2)
            with ch1:
                hbar(top15["Pair"].tolist()[::-1],
                     top15["Same-day HRs"].tolist()[::-1],
                     "Most same-day homers together", fmt="{:.0f}")
            with ch2:
                live_rows = hdf[hdf["HR today"].notna()].head(15)
                if len(live_rows):
                    hbar(live_rows["Pair"].tolist()[::-1],
                         live_rows["HR today"].tolist()[::-1],
                         "Today's average HR score — live pairs", fmt="{:.1f}")
                else:
                    st.caption(
                        "No pairs currently have both hitters on the slate, so "
                        "there's nothing to score for today."
                    )

            st.dataframe(
                hdf, width="stretch", hide_index=True, height=440,
                column_config={
                    "": st.column_config.TextColumn("Live", width="small",
                                                    help="🟢 = both hitters play today"),
                    "HR today": st.column_config.ProgressColumn(
                        "HR today", format="%.1f", min_value=0, max_value=100,
                        help="Average of the two hitters' HR scores on this slate.",
                    ),
                    "Same-day HRs": st.column_config.NumberColumn(
                        "Same-day HRs",
                        help="Times both went deep on the same calendar day this season.",
                    ),
                    "Same-game HRs": st.column_config.NumberColumn(
                        "Same-game HRs",
                        help="Times both homered in the same ballgame — the rarer, stronger signal.",
                    ),
                },
            )
            st.caption(
                "Same-day means both homered somewhere that day. Same-game "
                "means they did it in the same ballpark, in the same game — "
                "rarer, and the stronger signal of the two. History is "
                "context, not a prediction: check today's HR scores before "
                "acting on it."
            )
            st.download_button(
                "⬇️ CSV", hdf.to_csv(index=False).encode(),
                "pair_history.csv", "text/csv", key="phcsv")

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
        for c in ("got_hr", "got_base_hit", "got_xbh", "actual_hr", "actual_hits",
                  "actual_tb", "actual_rbi", "actual_runs", "hr_score", "rank"):
            if c in rdf.columns:
                rdf[c] = pd.to_numeric(rdf[c], errors="coerce").fillna(0)

        graded_mask = (rdf["grade"].astype(str).str.upper() != "PENDING"
                       if "grade" in rdf.columns else pd.Series(True, index=rdf.index))
        n_graded, n_pending = int(graded_mask.sum()), int((~graded_mask).sum())

        st.caption(f"{res.get('label', '')} · {res.get('date', '')} · {len(rdf)} picks tracked")

        # An all-PENDING board used to render as a wall of zeros that looked
        # like the model went 0-for-120. It didn't -- the games hadn't
        # started. Say so plainly before showing a single number.
        if n_graded == 0:
            st.info(
                f"All {n_pending} picks are still **PENDING** — no game on this "
                "slate has started yet. Hit rates fill in as games go final; "
                "grading runs hourly from 11am Phoenix."
            )

        k = st.columns(5)
        k[0].metric("Picks", len(rdf), delta=f"{n_graded} settled" if n_graded else None)
        for i, (lbl, col) in enumerate(
            [("HRs", "got_hr"), ("Base hits", "got_base_hit"), ("XBH", "got_xbh")], start=1
        ):
            k[i].metric(lbl, int(rdf.get(col, pd.Series(dtype=float)).sum()))
        # Rate over SETTLED picks only. Dividing by every pick including the
        # unplayed ones dragged the hit rate to 0.0% all afternoon and made a
        # good slate look like a disaster.
        if "got_hr" in rdf.columns and n_graded:
            rate = rdf.loc[graded_mask, "got_hr"].mean()
            k[4].metric("HR hit rate", f"{rate * 100:.1f}%",
                        help=f"Of the {n_graded} settled picks.")
        else:
            k[4].metric("HR hit rate", "—", help="Fills in once games go final.")

        if "grade" in rdf.columns:
            gc = rdf["grade"].astype(str).str.upper().value_counts()
            gl, gr = st.columns([2, 3])
            with gl:
                # Grades on a fixed good-to-bad colour scale rather than the
                # value-count table that was here. PENDING is deliberately
                # grey, not green -- it's not an outcome.
                GRADE_COLOR = {
                    "HR": C["green"], "WIN": C["green"], "HIT": "#4cb96a",
                    "XBH": "#7fd894", "PARTIAL": C["yellow"],
                    "MISS": C["red"], "LOSS": C["red"], "PENDING": C["text3"],
                }
                fig = go.Figure(go.Bar(
                    x=gc.values, y=gc.index, orientation="h",
                    marker_color=[GRADE_COLOR.get(g, C["cyan"]) for g in gc.index],
                    text=gc.values, textposition="outside",
                    textfont=dict(size=10, color=C["text2"]),
                ))
                _layout(fig, max(200, 34 * len(gc) + 80), "Grade breakdown")
                fig.update_xaxes(showgrid=False, showticklabels=False)
                st.plotly_chart(fig, width="stretch")
            with gr:
                if n_graded:
                    # Did the model's ranking actually predict anything? Hit
                    # rate by score band answers that in one look; the raw
                    # table never did.
                    g = rdf.loc[graded_mask].copy()
                    g["band"] = pd.cut(g["hr_score"], [0, 40, 55, 70, 85, 101],
                                       labels=["<40", "40-55", "55-70", "70-85", "85+"],
                                       right=False)
                    by_band = g.groupby("band", observed=True)["got_hr"].agg(["mean", "size"])
                    by_band = by_band[by_band["size"] > 0]
                    if len(by_band):
                        hbar([f"{b}  (n={int(r['size'])})" for b, r in by_band.iterrows()],
                             [float(r["mean"]) * 100 for _, r in by_band.iterrows()],
                             "HR hit rate by model score band", fmt="{:.0f}%")
                        st.caption(
                            "If the model is working, these climb left to right."
                        )
                else:
                    st.caption("Hit-rate-by-score-band appears once picks settle.")

        # Settled picks first — the whole point of opening this tab is seeing
        # what cashed, and those rows were previously buried under 100+
        # PENDING lines sorted by rank.
        sort_key = rdf["grade"].astype(str).str.upper().eq("PENDING").astype(int)
        rdf = rdf.assign(_pending=sort_key).sort_values(
            ["_pending", "actual_hr", "actual_tb"],
            ascending=[True, False, False],
        ).drop(columns=["_pending"])

        show_only = st.radio(
            "Show", ["All", "Settled only", "Hit a HR", "Pending"],
            horizontal=True, key="resfilter",
        )
        v = rdf
        if show_only == "Settled only":
            v = rdf[rdf["grade"].astype(str).str.upper() != "PENDING"]
        elif show_only == "Hit a HR":
            v = rdf[rdf.get("got_hr", 0) > 0]
        elif show_only == "Pending":
            v = rdf[rdf["grade"].astype(str).str.upper() == "PENDING"]

        if v.empty:
            st.caption("Nothing matches that filter yet.")
        else:
            st.dataframe(rows_to_df(v.to_dict("records"), [
                "name", "team", "pick_type", "bet_type", "rank", "hr_score",
                "actual_hr", "actual_hits", "actual_tb", "actual_rbi", "actual_runs",
                "grade", "outcome_text",
            ]), width="stretch", hide_index=True, height=480)
            st.download_button(
                "⬇️ CSV", v.to_csv(index=False).encode(),
                f"mlb_results_{which.lower()}_{res.get('date', '')}.csv",
                "text/csv", key="rescsv",
            )

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

        detail = load_detail("batter", p.get("player_id"), slate)
        # spray_chart is the canonical batted-ball list; contact_log and
        # batted_ball_log were byte-identical copies, so they're aliases here.
        bbe = detail.get("spray_chart") or []

        ov, evlog, pitchtab, spraytab, splitstab, zonetab = st.tabs(
            ["📊 Overview", "⚡ EV Log", "🎯 Pitch", "💦 Spray",
             "📅 Splits", "🔥 Zones & Maps"]
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

            # The same spot question, answered automatically for THIS hitter's
            # own lineup slot -- the version of it you actually care about
            # when you're looking at a player rather than a pitcher.
            _spot = p.get("lineup_spot")
            if _spot not in (None, ""):
                _sd = (load_detail("pitcher", p.get("pitcher_id"), slate)
                       .get("pitcher_lineup_spot_damage") or {})
                _row = next((v for k, v in _sd.items()
                             if isinstance(v, dict)
                             and int(v.get("spot", k)) == int(_spot)), {})
                if _row:
                    render_spot_answer(
                        spot_answer(_row, _sd, spot_baseline(slate), int(_spot)),
                        txt(p, "pitcher_name", default="This pitcher"), int(_spot),
                    )
                elif nn(p, "pitcher_spot_damage_score"):
                    # Detail file missing, but the row itself still carries the
                    # score and the bot's own reason string.
                    st.caption(
                        f"Spot #{int(_spot)} vs {txt(p, 'pitcher_name')}: damage "
                        f"{nn(p, 'pitcher_spot_damage_score'):.1f} "
                        f"({txt(p, 'pitcher_spot_damage_label', default='—')}) — "
                        f"{txt(p, 'pitcher_spot_damage_reason')}"
                    )

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

                # Single shared renderer -- this tab used to carry its own
                # inline copy of the table, so the modal and the Player tab
                # could (and did) drift apart on colouring.
                st.markdown(contact_log_html(q), unsafe_allow_html=True)
                st.caption(CONTACT_LOG_LEGEND)

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

            arsenal = load_detail("pitcher", p.get("pitcher_id"), slate)
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
        # ── SPLITS ──────────────────────────────────────────────────────────
        with splitstab:
            render_splits(p, slate)

        # ── ZONES & MAPS ────────────────────────────────────────────────────
        with zonetab:
            st.markdown("**Where this pitcher gets hurt**")
            pdet = load_detail("pitcher", p.get("pitcher_id"), slate)

            # Order zones (top 1-3 / middle 4-6 / bottom 7-9) come straight
            # from the bot as pitcher_lineup_zone_damage.
            zd = pdet.get("pitcher_lineup_zone_damage") or {}
            if isinstance(zd, dict) and zd:
                zrows = []
                for k, v in zd.items():
                    if not isinstance(v, dict):
                        continue
                    zrows.append({
                        "Zone": f"{str(k).title()} ({'-'.join(str(x) for x in (v.get('spots') or []))})",
                        "Damage": nn(v, "damage_score"), "SLG": nn(v, "slg") * 100,
                        "ISO": nn(v, "iso") * 100, "HR rate": nn(v, "hr_rate") * 100,
                        "Hard hit": nn(v, "hard_hit_rate") * 100,
                        "Barrel": nn(v, "barrel_rate") * 100,
                        "_pa": int(nn(v, "pa")), "_label": txt(v, "label"),
                    })
                if zrows:
                    zdf = pd.DataFrame(zrows).set_index("Zone")
                    heatmap(zdf[["Damage", "SLG", "ISO", "HR rate", "Hard hit", "Barrel"]],
                            "Damage allowed by batting-order zone", height=260)
                    worst = zdf.sort_values("Damage", ascending=False).iloc[0]
                    st.caption(
                        f"Most damaged in the {zdf['Damage'].idxmax()} of the order "
                        f"({worst['_label']}, {int(worst['_pa'])} PA). "
                        "Rate stats ×100 to share the colour scale."
                    )
            else:
                st.caption("No batting-order zone data for this pitcher.")

            # Spray-density map: the closest thing to a hot-zone map the data
            # supports. A true strike-zone heat map needs plate_x/plate_z per
            # pitch, which the bot doesn't currently collect -- only landing
            # coordinates (hc_x/hc_y), which is where the ball ended up.
            st.markdown("**Batted-ball density map**")
            bmap = load_detail("batter", p.get("player_id"), slate).get("spray_chart") or []
            if bmap:
                mdf = pd.DataFrame(bmap)
                for c in ("hc_x", "hc_y", "distance", "ev", "launch_angle"):
                    if c in mdf.columns:
                        mdf[c] = pd.to_numeric(mdf[c], errors="coerce")
                if {"hc_x", "hc_y"}.issubset(mdf.columns):
                    fld = mdf.dropna(subset=["hc_x", "hc_y"]).copy()
                    fld["x"] = fld["hc_x"] - 125.42
                    fld["y"] = 198.27 - fld["hc_y"]
                    # Bin the field into a grid and count -- that turns the
                    # scatter into an actual density map.
                    fld["xb"] = pd.cut(fld["x"], bins=8)
                    fld["yb"] = pd.cut(fld["y"], bins=8)
                    grid = (fld.pivot_table(index="yb", columns="xb", values="x",
                                            aggfunc="count", observed=False)
                            .fillna(0).iloc[::-1])
                    grid.index = [f"{int(iv.mid)}" for iv in grid.index]
                    grid.columns = [f"{int(iv.mid)}" for iv in grid.columns]
                    heatmap(grid, "Where he hits the ball (count per field cell)",
                            height=340)
                if "lane" in mdf.columns and "distance" in mdf.columns:
                    lane = mdf.groupby("lane").agg(
                        balls=("lane", "size"),
                        avg_dist=("distance", "mean"),
                        avg_ev=("ev", "mean") if "ev" in mdf.columns else ("distance", "mean"),
                    )
                    lane = lane[lane.index != ""]
                    if not lane.empty:
                        heatmap(lane.round(1), "By field lane", height=240, fmt="{:.0f}")
            else:
                st.caption("No batted-ball detail for this hitter yet.")


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
        st.info(
            "No players on your watchlist yet. Add them with the ⭐ Watch "
            "button on the Player tab, or from any player pop-up. Your list "
            "is saved in the page URL, so bookmarking or sharing that link "
            "carries the same players with it."
        )
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
        det = load_detail("batter", pl.get("player_id"), slate)
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
