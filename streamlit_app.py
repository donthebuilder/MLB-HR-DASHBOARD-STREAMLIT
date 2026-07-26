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

# Palette ported from lib/theme.js
C = {
    "orange": "#f59e0b", "cyan": "#22d3ee", "purple": "#a78bfa",
    "green": "#34d399", "red": "#f87171", "blue": "#60a5fa",
    "yellow": "#fbbf24", "text2": "#9aa4b2", "text3": "#6b7280",
}

st.markdown(
    """
    <style>
      .block-container {padding-top: 1.4rem; padding-bottom: 3rem;}
      [data-testid="stMetricValue"] {font-size: 1.4rem;}
      .pick-card {
        border: 1px solid rgba(250,250,250,.14); border-radius: 10px;
        padding: .7rem .95rem; margin-bottom: .55rem;
        background: rgba(255,255,255,.03);
      }
      .pill {
        display:inline-block; padding:2px 9px; margin:2px 4px 2px 0;
        border-radius:999px; font-size:.72rem; font-weight:700;
        border:1px solid currentColor;
      }
      .muted {opacity:.72; font-size:.85rem;}
      .grade {font-weight:800; font-size:1.05rem;}
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
if "watch" not in st.session_state:
    st.session_state.watch = []

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
    if st.session_state.slip:
        st.markdown(f"**🎟️ Slip ({len(st.session_state.slip)})**")
        for item in st.session_state.slip:
            st.caption(f"• {item}")
        if st.button("Clear slip", width="stretch"):
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

tabs = st.tabs([
    "🏆 Board", "🗓️ Games", "📊 Scoreboard", "🥇 Leaders", "⚾ Pitchers",
    "💥 Hits/HRR", "🎯 Pairs", "🧩 Pools", "✅ Results", "🔍 Player",
    "⭐ Watchlist", "🤖 Bot Report", "📖 Guide",
])

# ── BOARD ───────────────────────────────────────────────────────────────────
with tabs[0]:
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
                height=300, color="#f97316", horizontal=True,
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
            st.bar_chart(pd.DataFrame({"players": hist}), height=300, color="#22d3ee")
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
with tabs[1]:
    by_game: Dict[Any, List[Dict[str, Any]]] = {}
    for p in view:
        by_game.setdefault(p.get("game_pk"), []).append(p)
    order = sorted(by_game.items(), key=lambda kv: max(hr_score(x) for x in kv[1]), reverse=True)

    # Slate-level view first: which games are worth attention at a glance.
    if order:
        st.markdown("#### Slate at a glance — top HR score by game")
        chart = pd.DataFrame([{
            "Game": f"{team_of(max(gp, key=hr_score))} vs {opp_of(max(gp, key=hr_score))}",
            "Top HR": round(max(hr_score(x) for x in gp), 1),
        } for _, gp in order]).set_index("Game")
        st.bar_chart(chart, height=260, color="#f97316")

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
with tabs[2]:
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

with tabs[3]:
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
    lead = sorted(((p, getter(p)) for p in pool), key=lambda x: x[1], reverse=True)
    lead = [(p, v) for p, v in lead if math.isfinite(v) and v > 0][:25]

    if not lead:
        st.info("Not enough data for this leaderboard yet.")
    else:
        st.caption(f"Top {len(lead)} by {stat}")
        st.bar_chart(
            pd.DataFrame([{"Player": f"{name_of(p)} ({team_of(p)})", stat: round(v, 3)}
                          for p, v in lead]).set_index("Player"),
            height=520, color="#a78bfa", horizontal=True,
        )
        st.dataframe(pd.DataFrame([{
            "#": i, "Player": name_of(p) + (" 🧩" if is_aligned(p) else ""),
            "Team": f"{team_of(p)} vs {opp_of(p)}", "Role": tier_role(p),
            stat: fmt.format(v),
        } for i, (p, v) in enumerate(lead, start=1)]),
            width="stretch", hide_index=True, height=560)

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
        })
        if p.get("lineup_confirmed"):
            e["confirmed"] = True
        e["lineup"].append(p)
    for e in by.values():
        e["lineup"].sort(key=lambda x: nn(x, "lineup_spot", default=99.0))
        e["weak_spots"] = sum(1 for x in e["lineup"] if x.get("weak_spot_flag") is True)
    return sorted(by.values(), key=lambda e: (-e["weak_spots"], -e["hr9"]))


with tabs[4]:
    pitchers = group_pitchers(view)
    if not pitchers:
        st.info("No pitcher data found yet.")
    else:
        sort_by = st.selectbox("Sort by", ["Most weak spots", "Highest HR/9", "Highest WHIP", "Game time"])
        if sort_by == "Highest HR/9":
            pitchers.sort(key=lambda e: -e["hr9"])
        elif sort_by == "Highest WHIP":
            pitchers.sort(key=lambda e: -e["whip"])
        elif sort_by == "Game time":
            pitchers.sort(key=lambda e: str(e["game_time"] or ""))

        st.caption(f"{len(pitchers)} starters · expand to see the opposing lineup")
        for e in pitchers:
            star = f" · ⭐ {e['weak_spots']} weak spot{'s' if e['weak_spots'] != 1 else ''}" if e["weak_spots"] else ""
            with st.expander(
                f"{e['pitcher_name']} ({e['throws']}HP) · {e['team']} vs {e['facing']} · "
                f"HR/9 {e['hr9']:.2f} · WHIP {e['whip']:.2f}{star}"
            ):
                m = st.columns(5)
                m[0].metric("ERA", f"{e['era']:.2f}")
                m[1].metric("HR/9", f"{e['hr9']:.2f}")
                m[2].metric("WHIP", f"{e['whip']:.2f}")
                m[3].metric("K/9", f"{e['k9']:.2f}" if e["k9"] else "—")
                m[4].metric("Weak side", e["weak_side"] or "—")
                if e["attack"]:
                    st.caption(f"Attack profile: {e['attack']}")
                if e["xbh_lhb"] is not None or e["xbh_rhb"] is not None:
                    st.caption(f"XBH allowed — vs LHB {e['xbh_lhb'] or '—'} · vs RHB {e['xbh_rhb'] or '—'}")

                st.dataframe(pd.DataFrame([{
                    "Spot": b.get("lineup_spot"), "Batter": name_of(b),
                    "B": txt(b, "bats", default="?"),
                    "⭐": "⭐" if b.get("weak_spot_flag") else "",
                    "🎯": "🎯" if nn(b, "pitch_type_match_score") > 0 else "",
                    "HR": round(hr_score(b), 1), "HRR": round(prod_score(b), 1),
                    "Role": tier_role(b),
                } for b in e["lineup"]]), width="stretch", hide_index=True)

                arsenal = load_detail("pitcher", e["pitcher_id"])
                mix = (arsenal.get("pitcher_pitch_mix") or {}).get("usage") or {}
                if mix:
                    st.caption("Pitch usage")
                    st.bar_chart(pd.DataFrame({"usage %": mix}))

# ── HITS / HRR ──────────────────────────────────────────────────────────────
with tabs[5]:
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

with tabs[6]:
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

with tabs[7]:
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
with tabs[8]:
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
with tabs[9]:
    if not view:
        st.info("No players match these filters.")
    else:
        opts = sorted(view, key=hr_score, reverse=True)
        labels = [f"{name_of(p)} ({team_of(p)}) — HR {hr_score(p):.0f}" for p in opts]
        idx = st.selectbox("Player", range(len(opts)), format_func=lambda i: labels[i])
        p = opts[idx]

        st.markdown(f"### {name_of(p)} {'🧩' if is_aligned(p) else ''}")
        st.caption(
            f"{team_of(p)} vs {opp_of(p)} · bats {txt(p, 'bats', default='?')} · "
            f"spot {p.get('lineup_spot', '—')} · "
            f"{'confirmed' if p.get('lineup_confirmed') else 'projected'} lineup · "
            f"{txt(p, 'venue_name')}"
        )

        d = st.columns(6)
        d[0].metric("HR", f"{hr_score(p):.0f}", grade_for(p, "hr"))
        d[1].metric("HRR", f"{prod_score(p):.0f}")
        d[2].metric("Hit", f"{hit_score(p):.0f}")
        d[3].metric("TB", f"{tb_score(p):.0f}")
        d[4].metric("HRW", f"{nn(p, 'hrw_score'):.0f}")
        d[5].metric("Damage", f"{nn(p, 'damage_conversion_score'):.0f}")

        st.markdown(pills_html(signal_pills(p) + ([risk_pill(p)] if risk_pill(p) else [])),
                    unsafe_allow_html=True)

        if st.button("➕ Add to slip"):
            st.session_state.slip.append(f"{name_of(p)} — {txt(p, 'best_bet_type', default='HR')}")
        if st.button("⭐ Add to watchlist"):
            if name_of(p) not in st.session_state.watch:
                st.session_state.watch.append(name_of(p))

        cA, cB = st.columns(2)
        with cA:
            st.markdown("**Season / recent**")
            st.dataframe(pd.DataFrame([
                {"Stat": "Season AVG", "Value": f"{nn(p, 'season_avg'):.3f}"},
                {"Stat": "Season OPS", "Value": f"{nn(p, 'season_ops'):.3f}"},
                {"Stat": "Season ISO", "Value": f"{nn(p, 'season_iso'):.3f}"},
                {"Stat": "Season HR / PA", "Value": f"{int(nn(p, 'season_hr'))} / {int(nn(p, 'season_pa'))}"},
                {"Stat": "HR per PA", "Value": f"{nn(p, 'hr_per_pa'):.4f} ({txt(p, 'hr_pa_tier')})"},
                {"Stat": "Games since HR", "Value": f"{int(nn(p, 'games_since_last_hr'))}"},
                {"Stat": "L5", "Value": f"{int(nn(p, 'last5_hits'))}H / {int(nn(p, 'last5_hr'))}HR / {int(nn(p, 'last5_xbh'))}XBH"},
                {"Stat": "K rate", "Value": pct(nn(p, "season_k_rate"))},
            ]), width="stretch", hide_index=True)
        with cB:
            st.markdown("**Batted ball / matchup**")
            st.dataframe(pd.DataFrame([
                {"Stat": "Avg EV", "Value": f"{avg_ev(p):.1f}"},
                {"Stat": "Max EV", "Value": f"{max_ev(p):.1f}"},
                {"Stat": "Launch angle", "Value": f"{launch_angle(p):.1f}°"},
                {"Stat": "Hard hit / Barrel", "Value": f"{pct(hard_hit(p))} / {pct(barrel_rate(p))}"},
                {"Stat": "Pull rate", "Value": pct(pull_rate(p))},
                {"Stat": "350+ / 375+ / 400+", "Value": f"{int(recent350(p))} / {int(recent375(p))} / {int(recent400(p))}"},
                {"Stat": "Pitcher", "Value": f"{txt(p, 'pitcher_name')} ({txt(p, 'pitcher_throws')})"},
                {"Stat": "Pitcher HR/9 · WHIP", "Value": f"{nn(p, 'pitcher_hr9'):.2f} · {nn(p, 'pitcher_whip'):.2f}"},
            ]), width="stretch", hide_index=True)

        for label, key in (("Why this HR score", "hr_reason"), ("Pitch fit", "pitch_fit_summary"),
                           ("Park fit", "park_fit_summary"), ("Risk", "risk_reason"),
                           ("Advanced", "advanced_reason")):
            if txt(p, key):
                st.markdown(f"**{label}** — {txt(p, key)}")

        detail = load_detail("batter", p.get("player_id"))
        spray = detail.get("spray_chart") or []
        if spray:
            st.markdown(f"**Batted balls ({len(spray)} tracked)**")
            sdf = pd.DataFrame(spray)
            for col in ("ev", "launch_angle", "distance", "hc_x", "hc_y"):
                if col in sdf.columns:
                    sdf[col] = pd.to_numeric(sdf[col], errors="coerce")

            g1, g2 = st.columns(2)
            with g1:
                st.caption("Spray map — where the ball landed")
                if {"hc_x", "hc_y"}.issubset(sdf.columns):
                    # Statcast hit coords: home plate sits at (125.42, 198.27)
                    # and y grows downward, so shift to the plate and flip y to
                    # get a normal field orientation (CF up, LF left).
                    field = sdf.dropna(subset=["hc_x", "hc_y"]).copy()
                    field["x"] = field["hc_x"] - 125.42
                    field["y"] = 198.27 - field["hc_y"]
                    st.scatter_chart(field, x="x", y="y", height=330,
                                     color="event" if "event" in field.columns else None,
                                     size="distance" if "distance" in field.columns else None)
                else:
                    st.caption("No hit coordinates in this sample.")
            with g2:
                st.caption("Launch angle vs distance")
                if {"launch_angle", "distance"}.issubset(sdf.columns):
                    st.scatter_chart(sdf, x="launch_angle", y="distance", height=330,
                                     color="event" if "event" in sdf.columns else None)

            if "lane" in sdf.columns:
                st.caption("Batted balls by field lane")
                lane_counts = sdf["lane"].replace("", "—").value_counts()
                st.bar_chart(pd.DataFrame({"batted balls": lane_counts}),
                             height=220, color="#34d399")

            show = [c for c in ["date", "pitch_type", "event", "bb_type", "ev",
                                "launch_angle", "distance", "lane", "spray_side"]
                    if c in sdf.columns]
            st.dataframe(sdf[show].head(60), width="stretch", hide_index=True, height=300)
        else:
            st.caption(
                "No batted-ball detail published for this player yet — the detail "
                "files land with the next bot run."
            )

# ── WATCHLIST ───────────────────────────────────────────────────────────────
with tabs[10]:
    if not st.session_state.watch:
        st.info("No players on your watchlist yet — add them from the Player tab.")
    else:
        watched = [p for p in players if name_of(p) in st.session_state.watch]
        st.caption(f"{len(watched)} players watched")
        for p in sorted(watched, key=hr_score, reverse=True):
            player_card(p)
        if st.button("Clear watchlist"):
            st.session_state.watch = []
            st.rerun()

# ── BOT REPORT ──────────────────────────────────────────────────────────────
with tabs[11]:
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

# ── GUIDE ───────────────────────────────────────────────────────────────────
with tabs[12]:
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
