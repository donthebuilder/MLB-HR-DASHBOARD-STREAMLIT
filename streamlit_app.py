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
import datetime as dt
import json
import math
import re
import unicodedata
from difflib import get_close_matches
from statistics import median
from pathlib import Path
from zoneinfo import ZoneInfo
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
def load_pitcher_splits(pitcher_id: Any, slate_label: str = "today") -> Dict[str, Any]:
    """Same four split families for a starter, plus a log of every ball put
    in play against him. Written by the same bot under a `pitcher_` prefix."""
    if pitcher_id in (None, ""):
        return {}
    return load_json(
        f"public/data/current/splits/{slate_label}/pitcher_{pitcher_id}.json") or {}


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
# The 400ft counts only exist nested in bbe_profile -- no flat key is ever
# published, so the old chain resolved to 0 for every hitter on every slate
# and the "400+" column was permanently empty. 350/375 do have flat keys but
# fall back the same way for consistency.
recent375 = lambda p: (nn(p, "recent_375_num", "l20pa_375_num", "distance_375_num")
                       or n((p.get("bbe_profile") or {}).get("dist_375_plus")))
recent400 = lambda p: (nn(p, "recent_400_num", "l20pa_400_num", "distance_400_num",
                          "l25pa_400_plus")
                       or n((p.get("bbe_profile") or {}).get("dist_400_plus")))


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
    """Bot's conviction tier (💎 HR Bet / 📈 HR Lean / ...), not the type bucket."""
    return txt(p, "final_hr_role") or compact_role(p)


def tier_color(role: str) -> str:
    s = str(role or "")
    for token, key in (("💎", "orange"), ("📈", "orange"), ("🧲", "cyan"),
                       ("🧭", "blue"), ("🔭", "purple"), ("⛔", "red"),
                       # legacy glyphs from pre-rename payloads
                       ("🏆", "orange"), ("🔥", "orange"), ("🏁", "cyan"),
                       ("💠", "blue")):
        if token in s:
            return C[key]
    return {"Value HR": C["purple"], "HRR": C["cyan"], "Hit": C["purple"],
            "TB": C["green"], "Avoid HR": C["red"]}.get(s, C["orange"])


longest_score = lambda p: nn(p, "longest_hr_score")

_SUFFIX_RE = re.compile(r"\b(?:jr|sr|ii|iii|iv|v)\b\.?", re.I)
_JUNK_RE = re.compile(
    r"^\s*(?:\d+[.)]|[-*\u2022\u2023\u25aa\u2043])\s*"   # 1.  1)  -  *  bullets
    r"|\s*\((?:[A-Z]{2,3})\)\s*$"                            # trailing (NYY)
    r"|\s*[+-]\d{2,5}\s*$"                                    # trailing odds +450 / -110
    r"|\s+(?:vs\.?|@)\s+.*$",                                 # "Judge vs Cole"
    re.I,
)

_JUNK_RE = re.compile(
    r"^\s*(?:\d+[.)]|[-*\u2022\u2023\u25aa\u2043])\s*"   # 1.  1)  -  *  bullets
    r"|\s*\((?:[A-Z]{2,3})\)\s*$"                            # trailing (NYY)
    r"|\s*[+-]\d{2,5}\s*$"                                    # trailing odds +450 / -110
    r"|\s+(?:vs\.?|@)\s+.*$",                                 # "Judge vs Cole"
    re.I,
)

def norm_name(s: Any) -> str:
    """Normalise a player name for matching.

    The slate is full of names a pasted list will spell differently:
    Rodríguez, Acuña Jr., O'Hearn, Crow-Armstrong, Encarnacion-Strand. Strip
    accents, drop suffixes, flatten punctuation, and compare on that.
    """
    t = unicodedata.normalize("NFKD", str(s or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = _SUFFIX_RE.sub(" ", t)
    t = re.sub(r"[^a-z\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def parse_name_list(blob: str) -> List[str]:
    """Pull player names out of pasted text.

    People paste lists that came from somewhere else, so they arrive with
    ranking numbers, bullets, odds, team codes, one-per-line or comma joined.
    Strip the packaging and keep the names.
    """
    out: List[str] = []
    for chunk in re.split(r"[\n\r,;|\t]+", blob or ""):
        prev = None
        while chunk != prev:
            prev = chunk
            chunk = _JUNK_RE.sub("", chunk).strip()
        # Keep anything that looks like a person: at least one word of two or
        # more letters, and three letters total. Drops bare team codes ("NYY")
        # and stray numbers while keeping "J. Rodriguez" and "CJ Abrams".
        letters = re.sub(r"[^A-Za-z]", "", chunk)
        words = [w for w in re.split(r"\s+", chunk) if re.search(r"[A-Za-z]{2,}", w)]
        if len(letters) >= 3 and words and chunk.upper() != chunk.strip() or (
            len(letters) >= 3 and len(words) >= 1 and len(chunk.split()) >= 2
        ):
            out.append(chunk)
    # De-dupe, keep paste order.
    seen, uniq = set(), []
    for n in out:
        k = norm_name(n)
        if k and k not in seen:
            seen.add(k)
            uniq.append(n)
    return uniq

def match_players(names: List[str], pool: List[Dict[str, Any]]):
    """Match pasted names to slate players. Returns (hits, misses, ambiguous).

    Four passes, loosest last:
      exact     — normalised strings equal
      initial   — last name + first initial ("C. Abrams")
      partial   — one name contains the other ("Crow Armstrong", "Acuna")
      fuzzy     — spelling similarity, flagged in the output

    Anything that matches more than one player is reported as AMBIGUOUS rather
    than guessed at or silently dropped. Two J. Rodriguezes on one slate is a
    real thing, and calling that "not playing" would be a lie.
    """
    by_norm: Dict[str, Dict[str, Any]] = {}
    by_lastinit: Dict[str, List[Dict[str, Any]]] = {}
    norms: List[tuple] = []
    for p in pool:
        n = norm_name(name_of(p))
        if not n:
            continue
        by_norm.setdefault(n, p)
        norms.append((n, p))
        parts = n.split()
        if len(parts) >= 2:
            by_lastinit.setdefault(f"{parts[-1]}|{parts[0][0]}", []).append(p)

    hits, misses, ambiguous = [], [], []
    for raw in names:
        key = norm_name(raw)
        if not key:
            continue
        if key in by_norm:
            hits.append((raw, by_norm[key], "exact"))
            continue

        parts = key.split()
        if len(parts) >= 2:
            cands = by_lastinit.get(f"{parts[-1]}|{parts[0][0]}", [])
            if len(cands) == 1:
                hits.append((raw, cands[0], "initial"))
                continue
            if len(cands) > 1:
                ambiguous.append((raw, [name_of(c) for c in cands]))
                continue

        # Partial: pasted name is contained in a slate name or vice versa, on
        # whole-word boundaries so "ana" can't match "Santana".
        kw = set(key.split())
        part = [p for n, p in norms
                if kw and (kw <= set(n.split()) or set(n.split()) <= kw)]
        uniq = {name_of(p): p for p in part}
        if len(uniq) == 1:
            hits.append((raw, next(iter(uniq.values())), "partial"))
            continue
        if len(uniq) > 1:
            ambiguous.append((raw, sorted(uniq)))
            continue

        close = get_close_matches(key, list(by_norm), n=1, cutoff=0.85)
        if close:
            hits.append((raw, by_norm[close[0]], "fuzzy"))
        else:
            misses.append(raw)
    return hits, misses, ambiguous

def cross_board(p: Dict[str, Any]) -> float:
    """Median of Hit, DC, TB and HRR — the all-round score.

    Every other number on the board rewards a spike: a hitter can post a 96 HR
    score off one loud signal and be ordinary everywhere else. A median across
    four boards can't be moved by one outlier, so it answers a different
    question -- who is good at everything tonight, not who is loudest at one
    thing. Median rather than mean on purpose: with four values the mean still
    drags toward an extreme, the median doesn't.
    """
    return float(median([
        hit_score(p),
        nn(p, "damage_conversion_score"),
        tb_score(p),
        prod_score(p),
    ]))

def fair_american(rate_pct: Any) -> str:
    """Break-even American odds for a hit rate given in percent.

    A 33% hit rate is +203. Anything the book prices longer than that is
    positive expectation; anything shorter is not. The app has no odds feed,
    so this is the half of the comparison it CAN show.
    """
    try:
        r = float(rate_pct) / 100.0
    except (TypeError, ValueError):
        return "—"
    if not (0.0 < r < 1.0):
        return "—"
    return f"+{round(100 * (1 - r) / r)}" if r < 0.5 else f"-{round(100 * r / (1 - r))}"

dc_score = lambda p: nn(p, "damage_conversion_score")


def _minmax(value: Any, low: float, high: float) -> float:
    """Mirror of minmax_norm() in bots/mlb_dashboard.py."""
    if high <= low:
        return 0.5
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = low
    v = max(low, min(high, v))
    return (v - low) / (high - low)


def pitcher_damage_for(p: Dict[str, Any]) -> float:
    """Recompute the bot's pitcher_damage layer from published pitcher stats.

    pitcher_damage isn't published -- only its inputs are -- so the what-if
    swap below has to rebuild it to score an arm who isn't in this game. This
    mirrors the three branches in apply_model_v2_layers: full statcast +
    advanced, statcast only, and the HR/9 + WHIP fallback.
    """
    has_statcast = txt(p, "pitcher_statcast_status") == "ok" and nn(p, "pitcher_statcast_bbe") > 0
    has_advanced = txt(p, "pitcher_advanced_stats_status") == "ok"

    if has_statcast and has_advanced:
        return 100 * (
            0.18 * _minmax(p.get("pitcher_hr9", 1.10), 0.70, 2.00) +
            0.13 * _minmax(p.get("pitcher_barrel_allowed", 0.07), 0.03, 0.13) +
            0.11 * _minmax(p.get("pitcher_hardhit_allowed", 0.38), 0.30, 0.52) +
            0.08 * _minmax(p.get("pitcher_ev_allowed", 88.5), 86.0, 92.5) +
            0.04 * _minmax(p.get("pitcher_statcast_fb_rate", 0.34), 0.28, 0.48) +
            0.03 * _minmax(p.get("pitcher_375_allowed", 0), 0, 8) +
            0.12 * _minmax(p.get("pitcher_meatball_pct", 0.07), 0.040, 0.105) +
            0.09 * _minmax(p.get("pitcher_pullair_allowed_pct", 0.24), 0.16, 0.32) +
            0.14 * _minmax(p.get("pitcher_woba_against", 0.320), 0.290, 0.380) +
            0.08 * _minmax(p.get("pitcher_fip", 4.00), 2.50, 5.50)
        )
    if has_statcast:
        return 100 * (
            0.30 * _minmax(p.get("pitcher_hr9", 1.10), 0.70, 2.00) +
            0.22 * _minmax(p.get("pitcher_barrel_allowed", 0.07), 0.03, 0.13) +
            0.20 * _minmax(p.get("pitcher_hardhit_allowed", 0.38), 0.30, 0.52) +
            0.15 * _minmax(p.get("pitcher_ev_allowed", 88.5), 86.0, 92.5) +
            0.08 * _minmax(p.get("pitcher_statcast_fb_rate", 0.34), 0.28, 0.48) +
            0.05 * _minmax(p.get("pitcher_375_allowed", 0), 0, 8)
        )
    return 100 * (
        0.58 * _minmax(p.get("pitcher_hr9", 1.10), 0.70, 2.00) +
        0.42 * _minmax(p.get("pitcher_whip", 1.30), 1.05, 1.60)
    ) * 0.70


# Must track MODEL_WEIGHTS["hr_blend"]["pitcher_damage"] in the bot.
PITCHER_DAMAGE_WEIGHT = 0.15


def hr_score_vs_arm(p: Dict[str, Any], arm: Dict[str, Any]) -> float:
    """Estimated HR score for hitter `p` if `arm`'s pitcher started instead.

    Shifts ONLY the pitcher_damage term of the blend and holds the other
    fourteen constant. It is an estimate, not a re-run: the published
    hr_score already has post-blend gates and multipliers baked in, and those
    can't be reproduced here. Good enough to rank a what-if, not a substitute
    for the bot re-scoring the slate.
    """
    delta = pitcher_damage_for(arm) - pitcher_damage_for(p)
    return max(0.0, min(100.0, hr_score(p) + PITCHER_DAMAGE_WEIGHT * delta))


def score_for(p: Dict[str, Any], kind: str = "hr") -> float:
    return {"hrr": prod_score, "hit": hit_score, "tb": tb_score,
            "cross": cross_board, "dc": dc_score,
            "longest": longest_score}.get(kind, hr_score)(p)


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
# MODEL ROLES. Deliberately share no emoji with the pick types below --
# 🏆/🔥/🏁/💠 used to mean one thing as a role and a different thing as a
# pick, so a role badge read like a pick badge. The old glyphs are still
# mapped because published slates keep them until the bot re-runs.
ROLE_MAP = {
    "💎": ("HR Bet", "#f87171"),
    "📈": ("HR Lean", "#f97316"),
    "🧲": ("HRR / XBH", "#22d3ee"),
    "🧭": ("Contact", "#a78bfa"),
    "🔭": ("Power Watch", "#71717a"),
    "⛔": ("True Avoid", "#ef4444"),
    # legacy — pre-rename payloads
    "🏆": ("HR Bet", "#f87171"),
    "🔥": ("HR Lean", "#f97316"),
    "🏁": ("HRR / XBH", "#22d3ee"),
    "💠": ("Contact", "#a78bfa"),
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


# Old payloads carry the pre-rename glyph; normalise so the UI is consistent
# whichever slate is loaded.
ROLE_GLYPH_FIX = {"🏆": "💎", "🔥": "📈", "🏁": "🧲", "💠": "🧭"}


def role_glyph(p: Dict[str, Any]) -> str:
    raw = txt(p, "final_hr_role")
    if not raw:
        return ""
    return ROLE_GLYPH_FIX.get(raw[0], raw[0])


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
    st.plotly_chart(fig, width="stretch", key=_chart_key())


# Module level on purpose. This used to live inside `if order:` in the
# Games tab, which meant any tab rendering before that branch ran -- or a
# filter combination that emptied `order` -- hit a NameError.
# The slate payload has `team` and `opponent` but never says which is home.
# Every row does carry the venue, and a venue belongs to exactly one club, so
# the park name is what resolves it. Names match the PARKS table in
# bots/mlb_dashboard.py -- if a club moves or a park is renamed, fix both.
VENUE_HOME = {
    'Chase Field': 'ARI',
    'Sutter Health Park': 'ATH',
    'Truist Park': 'ATL',
    'Oriole Park at Camden Yards': 'BAL',
    'Fenway Park': 'BOS',
    'Wrigley Field': 'CHC',
    'Great American Ball Park': 'CIN',
    'Progressive Field': 'CLE',
    'Coors Field': 'COL',
    'Rate Field': 'CWS',
    'Comerica Park': 'DET',
    'Daikin Park': 'HOU',
    'Kauffman Stadium': 'KC',
    'Angel Stadium': 'LAA',
    'Dodger Stadium': 'LAD',
    'loanDepot park': 'MIA',
    'American Family Field': 'MIL',
    'Target Field': 'MIN',
    'Citi Field': 'NYM',
    'Yankee Stadium': 'NYY',
    'Citizens Bank Park': 'PHI',
    'PNC Park': 'PIT',
    'Petco Park': 'SD',
    'T-Mobile Park': 'SEA',
    'Oracle Park': 'SF',
    'Busch Stadium': 'STL',
    'George M. Steinbrenner Field': 'TB',
    'Globe Life Field': 'TEX',
    'Rogers Centre': 'TOR',
    'Nationals Park': 'WSH',
}


def home_away(gp: List[Dict[str, Any]]) -> tuple:
    """(away, home) abbreviations for a game. Falls back to slate order."""
    teams = []
    for x in gp:
        t = team_of(x)
        if t and t not in teams:
            teams.append(t)
    venue = txt(gp[0], "venue_name") if gp else ""
    host = VENUE_HOME.get(venue)
    if host and host in teams:
        away = next((t for t in teams if t != host), "")
        return away, host
    # Neutral site, unknown park, or a one-sided lineup pool.
    return (teams + ["", ""])[0], (teams + ["", ""])[1]


def _mm(value: Any, low: float, high: float) -> float:
    """minmax_norm from bots/mlb_dashboard.py."""
    if high <= low:
        return 0.5
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = low
    v = max(low, min(high, v))
    return (v - low) / (high - low)


def projected_hr_total(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Slate HR projection — the number the bot prints as "projected HRs 40-49".

    Mirrors projected_hr_total() in bots/mlb_dashboard.py line-for-line. The
    bot rounds the midpoint away into a low/high range before writing the .txt,
    so the only way to see it to one decimal is to recompute it here. Every
    input is published per player, so this is exact rather than an estimate --
    but it does mean the two have to be kept in step if the bot's weights move.
    """
    if not rows:
        return {"mid": 0.0, "low": 0, "high": 0, "grade": "No Slate",
                "top_profiles": 0, "weak_games": 0, "games": 0}

    games = max(1, len({r.get("game_pk") for r in rows}))

    def power_quality(r: Dict[str, Any]) -> float:
        tracked = max(1, int(nn(r, "recent_350_den")))
        return (
            0.30 * _mm(nn(r, "recent_ideal_hr_contact"), 0.04, 0.24) +
            0.22 * _mm(nn(r, "recent_350_num") / tracked, 0.08, 0.42) +
            0.18 * _mm(nn(r, "recent_375_num") / tracked, 0.02, 0.24) +
            0.13 * _mm(nn(r, "recent_fb_rate"), 0.25, 0.52) +
            0.10 * _mm(nn(r, "recent_pull_rate"), 0.28, 0.62) +
            0.07 * _mm(nn(r, "hrw_score"), 35, 75)
        )

    top_profiles = [r for r in rows
                    if hr_score(r) >= 34 and nn(r, "season_pa") >= 15]
    top_power = sorted(rows,
                       key=lambda r: hr_score(r) * 0.65 + 35 * power_quality(r),
                       reverse=True)[: max(20, games * 4)]
    hitter_component = sum(
        _mm(hr_score(r), 22, 58) * (0.72 + 0.28 * power_quality(r))
        for r in top_power)

    weak_games = set()
    for r in rows:
        bats = txt(r, "bats")
        side_hr9 = nn(r, "pitcher_hr9_vs_lhb") if bats == "L" else nn(r, "pitcher_hr9_vs_rhb")
        side_match = ((bats == "L" and txt(r, "pitcher_weak_side") == "LHB")
                      or (bats == "R" and txt(r, "pitcher_weak_side") == "RHB"))
        if (nn(r, "pitcher_hr9") >= 1.20 or side_hr9 >= 1.20
                or nn(r, "pitcher_fb_rate") >= 0.39 or side_match):
            weak_games.add(r.get("game_pk"))

    weakness = len(weak_games) * 0.42
    park = sum(_mm(nn(r, "park_factor", default=100.0), 96, 110)
               for r in top_power[: games * 2]) * 0.18
    mid = games * 1.05 + 0.34 * hitter_component + weakness + park
    spread = max(3.0, games * 0.28)
    low = max(0, round(mid - spread))
    grade = ("Strong" if mid >= games * 2.0 else
             "Medium" if mid >= games * 1.45 else "Light")
    return {"mid": mid, "low": low, "high": max(low + 1, round(mid + spread)),
            "grade": grade, "top_profiles": len(top_profiles),
            "weak_games": len(weak_games), "games": games}


# Module level: these used to live inside `with tab_games:`, so any other
# tab that wanted a start time hit a NameError when the Games branch
# hadn't run.
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


# Streamlit derives a chart's element id from its CONTENT, so two charts that
# happen to hold identical numbers collide -- which is what killed the
# tomorrow slate: two pitchers with no data both rendered an all-zero
# "Vulnerability profile" radar. A counter makes every chart unique without
# needing a hand-written key at each call site. It resets naturally because
# Streamlit re-executes this module top-to-bottom on every rerun, and the
# render order is deterministic, so ids stay stable between runs.
_CHART_SEQ = 0


def _chart_key(prefix: str = "chart") -> str:
    global _CHART_SEQ
    _CHART_SEQ += 1
    return f"{prefix}_{_CHART_SEQ}"


def med(vals: List[float]) -> float:
    vals = sorted(v for v in vals if math.isfinite(v))
    if not vals:
        return 0.0
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def hbar(labels: List[str], values: List[float], title: str = "",
         height: Optional[int] = None, fmt: str = "{:.1f}",
         ref: Optional[float] = None, ref_label: str = "median",
         subtitles: Optional[List[str]] = None, style: str = "heat") -> None:
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

    if style == "bar":
        # Bars are kept for the Results tab. A heat strip reads well when the
        # question is "who is hottest"; on Results the question is "how big
        # is this number", and a bar answers that at a glance where a shade
        # of green does not.
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
                         range=[0, (hi or 1) * 1.18])
        fig.update_yaxes(showgrid=False, zeroline=False,
                         tickfont=dict(size=11, color=C["text2"]))
        fig.update_layout(margin=dict(l=8, r=54, t=40 if title else 12, b=8),
                          bargap=0.28)
        st.plotly_chart(fig, width="stretch", key=_chart_key())
        return

    # Rendered as a one-column heat strip rather than bars, per request --
    # same ranking, same numbers, but colour carries the magnitude so these
    # read the same way as the matrix heatmaps everywhere else. Bar length
    # and cell colour encode the same single value, so nothing is lost.
    fig = go.Figure(go.Heatmap(
        z=[[v] for v in val], x=[""], y=lab,
        colorscale=GREEN_SCALE, showscale=False,
        zmin=lo, zmax=hi if hi != lo else lo + 1.0,
        text=[[fmt.format(v)] for v in val],
        texttemplate="%{text}",
        textfont=dict(size=11, color="#dfe6e9", family=NUM_FONT),
        hovertemplate="%{y}: %{z:.1f}<extra></extra>",
        xgap=0, ygap=2,
    ))
    h = height or max(220, 26 * len(lab) + 70)
    _layout(fig, h, title)
    fig.update_xaxes(showgrid=False, zeroline=False, showticklabels=False)
    fig.update_yaxes(showgrid=False, zeroline=False,
                     tickfont=dict(size=11, color=C["text2"]))
    fig.update_layout(margin=dict(l=8, r=14, t=40 if title else 12, b=8))
    st.plotly_chart(fig, width="stretch", key=_chart_key())
    if ref is not None:
        # The dotted reference line has no meaning on a heat strip, so the
        # comparison it used to draw is stated instead.
        st.caption(f"{ref_label}: {ref:.1f}")


def arsenal_pie(usage: Dict[str, Any], mistake: Any = None,
                height: int = 260) -> None:
    """Pitch mix as a donut. `mistake` is pulled out as an exploded slice."""
    items = [(str(k), n(v)) for k, v in (usage or {}).items() if n(v) > 0]
    if not items:
        st.caption("No pitch-mix data for this arm.")
        return
    items.sort(key=lambda kv: -kv[1])
    labels = [k for k, _ in items]
    vals = [v for _, v in items]
    PITCH_COLORS = [C["orange"], C["cyan"], C["purple"], C["green"],
                    C["yellow"], C["blue"], C["red"], C["text3"]]
    colors = [PITCH_COLORS[i % len(PITCH_COLORS)] for i in range(len(labels))]
    pull = [0.12 if mistake and k == str(mistake) else 0.0 for k in labels]
    fig = go.Figure(go.Pie(
        labels=labels, values=vals, hole=0.55, pull=pull, sort=False,
        marker=dict(colors=colors, line=dict(color=C["bg"], width=2)),
        textinfo="label+percent", textposition="outside",
        textfont=dict(size=11, color=C["text2"]),
        hovertemplate="%{label}: %{value:.1f}%<extra></extra>",
    ))
    _layout(fig, height, "")
    fig.update_layout(showlegend=False,
                      margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, width="stretch", key=_chart_key())


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
    st.plotly_chart(fig, width="stretch", key=_chart_key())


def bbe_trend(df: pd.DataFrame, val_col: str, label: str, unit: str = "",
              good_at: Optional[float] = None, height: int = 260) -> None:
    """Batted balls as dots over time with a rolling average.

    Replaces the candlestick view. A candle needs a meaningful open and close;
    batted balls have no order within a day, so the box was just "first ball
    hit" vs "last ball hit" -- no information. Dots show the real spread and
    the line shows whether he's actually trending.
    """
    if df.empty or val_col not in df.columns:
        st.caption(f"No {label.lower()} data.")
        return
    d = df[[c for c in ("date", val_col) if c in df.columns]].dropna().copy()
    if len(d) < 3:
        st.caption(f"Only {len(d)} tracked ball(s) — not enough for a trend.")
        return
    if "date" in d.columns:
        d = d.sort_values("date")
    d = d.reset_index(drop=True)
    idx = list(range(len(d)))
    roll = d[val_col].rolling(min(7, max(2, len(d) // 3)), min_periods=1).mean()

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=idx, y=d[val_col], mode="markers",
        marker=dict(size=7, color=C["cyan"], opacity=0.6, line=dict(width=0)),
        customdata=(d["date"] if "date" in d.columns else d[val_col]),
        hovertemplate="%{customdata}<br>%{y:.0f}" + unit + "<extra></extra>",
        name=label,
    ))
    fig.add_trace(go.Scatter(
        x=idx, y=roll, mode="lines", name="rolling avg",
        line=dict(color=C["orange"], width=2), hoverinfo="skip",
    ))
    if good_at is not None:
        fig.add_hline(y=good_at, line_dash="dot", line_color=C["green"])
    _layout(fig, height, f"{label} per batted ball — oldest to newest")
    fig.update_xaxes(showticklabels=False, showgrid=False)
    st.plotly_chart(fig, width="stretch", key=_chart_key())


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
    st.plotly_chart(fig, width="stretch", key=_chart_key())


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


def player_detail(p: Dict[str, Any], kp: str = "pl",
                  cmp_p: Optional[Dict[str, Any]] = None) -> None:
    """Everything the Player tab shows, renderable anywhere.

    The tab and the modal used to be two views of the same hitter: the tab
    had six sub-tabs of detail, the modal had a header and a stat line. This
    is the full view, so opening a player from any card gives you what you
    would have got by walking to the tab and finding him in a dropdown.

    `kp` namespaces every widget key -- the same player can legitimately be
    open in the tab and in a modal at once, and Streamlit rejects duplicate
    element keys.
    """
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

    a1, a2 = st.columns([1, 5])
    if a1.button("⭐ Watch", width="stretch", key=f"{kp}_watchbtn"):
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

    # Where he sits on the slate, not just his raw number. A 62 means
    # nothing until you know it's the 88th percentile tonight.
    BOARDS = [("HR", hr_score), ("Cross", cross_board), ("HRR", prod_score),
              ("Hit", hit_score), ("TB", tb_score),
              ("DC", lambda x: nn(x, "damage_conversion_score")),
              ("Longest", longest_score), ("HRW", lambda x: nn(x, "hrw_score"))]

    def pctile(fn, val: float) -> float:
        vals = [fn(x) for x in players]
        if not vals:
            return 0.0
        return 100.0 * sum(1 for v in vals if v <= val) / len(vals)

    pr = st.columns(len(BOARDS))
    for i, (lbl, fn) in enumerate(BOARDS):
        mine = fn(p)
        pct_ = pctile(fn, mine)
        delta = None
        if cmp_p is not None:
            delta = f"{mine - fn(cmp_p):+.1f} vs {cmp_pick.split()[-1]}"
        else:
            delta = f"{mine - med([fn(x) for x in players]):+.1f} vs med"
        pr[i].metric(lbl, f"{mine:.1f}", delta=delta, delta_color="normal",
                     help=f"{pct_:.0f}th percentile on tonight's slate.")

    if cmp_p is not None:
        radar(
            [b[0] for b in BOARDS],
            [b[1](p) for b in BOARDS],
            title=f"{name_of(p)} vs {name_of(cmp_p)}",
            second=(name_of(cmp_p), [b[1](cmp_p) for b in BOARDS], C["cyan"]),
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
            limit = f1.radio("Sample", [10, 15, 25, 50], index=2, horizontal=True, key=f"{kp}_w1")
            arm = f2.radio("Arm", ["All", "RHP", "LHP"], horizontal=True, key=f"{kp}_w2")
            pitches = sorted({str(x) for x in edf.get("pitch_type", pd.Series(dtype=str)).dropna()})
            pick_pitch = f3.selectbox("Pitch", ["All pitches"] + pitches, key=f"{kp}_w3")
            only = f4.selectbox("Show", ["All results", "Home runs", "Barrels",
                                         "Hard hit (95+)", "375+ ft"],
                                key=f"{kp}_evshow")

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
            hbar([str(k) for k in mix], [n(v) for v in mix.values()],
                 height=max(180, 26 * len(mix) + 60), fmt="{:.1f}%")

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
                _lane = sdf["lane"].replace("", "—").value_counts()
                hbar([str(i) for i in _lane.index], [float(v) for v in _lane.values],
                     height=max(180, 26 * len(_lane) + 60), fmt="{:.0f}")

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


@st.dialog("Player", width="large")
def player_modal(p: Dict[str, Any]) -> None:
    """Full player detail in a popup — the same content as the Player tab.

    The dialog decorator belongs HERE, not on player_detail: the tab renders
    the detail inline, and only a card click should pop it. With the
    decorator on player_detail the Player tab itself opened as a modal that
    stayed up across reruns.
    """
    # game_pk disambiguates a doubleheader, where one player_id appears twice.
    player_detail(p, kp=f"modal_{p.get('player_id') or name_of(p)}"
                     f"_{p.get('game_pk', '')}")


def open_player_picker(rows: List[Dict[str, Any]], where: str,
                       label: str = "Open a player") -> None:
    """Pick any hitter from the table above and pop his full detail.

    A dataframe can't carry a button per row, so this sits underneath one.
    Every board gets the same affordance instead of detail being reachable
    only from the card views and the Player tab.
    """
    if not rows:
        return
    # A doubleheader lists the same hitter twice; dedupe so the dropdown has
    # one entry per player rather than two identical-looking ones.
    seen: set = set()
    uniq = []
    for x in rows:
        k = norm_name(name_of(x))
        if k and k not in seen:
            seen.add(k)
            uniq.append(x)
    names = [name_of(x) for x in uniq]
    c1, c2 = st.columns([3, 1])
    pick = c1.selectbox(label, ["—"] + names, key=f"opick_{where}")
    if c2.button("🔍 Detail", key=f"obtn_{where}", width="stretch",
                 disabled=(pick == "—")):
        target = next((x for x in uniq if name_of(x) == pick), None)
        if target is not None:
            player_modal(target)


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
        if st.button("View details",
                     key=f"open_{open_key}_{p.get('player_id')}"
                         f"_{p.get('game_pk', '')}_{rank or 0}",
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
            # Season HR totals and the platoon split. HR/9 is a rate, so a
            # reliever-length sample and a full season look identical on the
            # card; the raw count is what tells you how much season is behind
            # the number, and the L/R split is what tells you which side of
            # the plate it came from.
            "hr_allowed": p.get("pitcher_hr_allowed"),
            "hr_lhb": p.get("pitcher_hr_vs_lhb"), "hr_rhb": p.get("pitcher_hr_vs_rhb"),
            "hr9_lhb": p.get("pitcher_hr9_vs_lhb"), "hr9_rhb": p.get("pitcher_hr9_vs_rhb"),
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
    "375+": "Batted balls hit 375+ feet over his last 8 games played. Raw "
            "power showing up in games.",
    "400+": "Batted balls hit 400+ feet over his last 8 games played. "
            "Genuine no-doubt distance.",
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
        + (
            "<div style='display:flex;gap:14px;margin-top:7px;font-family:"
            + NUM_FONT + "'>"
            + "".join(
                f"<div><div style='font-size:8.5px;color:{C['text3']};"
                f"letter-spacing:.05em'>{lbl}</div>"
                f"<div style='font-size:13px;font-weight:700;color:{colr}'>"
                f"{val}</div></div>"
                for lbl, val, colr in (
                    ("HR ALLOWED", f"{int(n(e.get('hr_allowed')))}", C["text"]),
                    ("VS LHB",
                     f"{int(n(e.get('hr_lhb')))}"
                     + (f" · {n(e.get('hr9_lhb')):.2f}" if e.get("hr9_lhb") is not None else ""),
                     C["orange"] if n(e.get("hr9_lhb")) > n(e.get("hr9_rhb")) else C["text2"]),
                    ("VS RHB",
                     f"{int(n(e.get('hr_rhb')))}"
                     + (f" · {n(e.get('hr9_rhb')):.2f}" if e.get("hr9_rhb") is not None else ""),
                     C["orange"] if n(e.get("hr9_rhb")) > n(e.get("hr9_lhb")) else C["text2"]),
                )
            )
            + "</div>"
            if e.get("hr_allowed") is not None else ""
        )
        + f"<div style='font-size:10px;color:{C['text3']};margin-top:7px'>"
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
        _u = row.get("pitcher_pitch_usage_pct") or row.get("pitcher_arsenal") or {}
        if isinstance(_u, dict) and _u:
            arsenal_pie(_u, row.get("pitcher_mistake_pitch_v31"), height=230)

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
                "Spot": spot, "PA": int(n(v.get("pa"))),
                "AVG": round(n(v.get("avg")), 3),
                "SLG": round(n(v.get("slg")), 3),
                "ISO": round(n(v.get("iso")), 3),
                "HR": int(n(v.get("hr"))), "XBH": int(n(v.get("xbh"))),
                "HR%": round(n(v.get("hr_rate")) * 100, 1),
                "XBH%": round(n(v.get("xbh_rate")) * 100, 1),
                "K%": round(n(v.get("k_rate")) * 100, 1),
                "HardHit%": round(n(v.get("hard_hit_rate")) * 100, 1),
                "Damage": round(n(v.get("damage_score")), 1),
            })
        if rows:
            sdf = pd.DataFrame(rows).sort_values("Spot")
            st.markdown("**Where he gets hurt in the order**")
            # Was OPS bars, but the payload has no `ops` key -- every spot
            # rendered 0.000. These are the fields that actually exist.
            _hm = sdf.set_index(sdf.apply(
                lambda r: f"{int(r['Spot'])}-hole ({int(r['PA'])} PA)", axis=1
            ))[["Damage", "HR%", "XBH%", "HardHit%", "K%"]]
            heatmap(_hm, "Damage allowed by lineup spot",
                    height=max(260, 30 * len(_hm) + 90))
            st.caption(
                "**Damage** is the bot's own composite for that slot — the best "
                "single read on where he gets beaten. **K%** is the one column "
                "where bright is good *for him*. Watch PA: a big number on 18 "
                "plate appearances is noise, not a trend."
            )
            st.dataframe(sdf, width="stretch", hide_index=True)

    # Splits and contact log, matching what the batter modal carries.
    psp = load_pitcher_splits(e.get("pitcher_id"), slate)
    if psp:
        with st.expander("📅 Situational splits — day/night, home/away, W/L, weekday"):
            render_pitcher_splits(psp)

    p_bbe = bbe_frame((psp or {}).get("contact_log"))
    if not p_bbe.empty:
        with st.expander(f"⚡ Contact allowed — last {min(30, len(p_bbe))} balls in play"):
            # On a pitcher's log the "pitcher" column actually holds the
            # BATTER, so relabel it -- otherwise every row repeats the
            # pitcher's own name and tells you nothing.
            if "batter" in p_bbe.columns:
                p_bbe = p_bbe.rename(columns={"pitcher": "_own"})
                p_bbe["pitcher"] = p_bbe["batter"]
            pitches = sorted({str(x) for x in p_bbe.get("pitch_name", [])
                              if str(x) not in ("", "nan")})
            if pitches:
                pick_pitch = st.selectbox(
                    "Filter by pitch", ["All pitches"] + pitches,
                    key=f"pbbe_{e.get('pitcher_id')}",
                    help="See who did damage against one specific pitch.")
                if pick_pitch != "All pitches":
                    p_bbe = p_bbe[p_bbe["pitch_name"].astype(str) == pick_pitch]
            if p_bbe.empty:
                st.caption("No batted balls against that pitch.")
                st.stop() if False else None
            recent = p_bbe.head(30)
            q = st.columns(4)
            if "ev" in recent.columns:
                q[0].metric("Avg EV against", f"{recent['ev'].mean():.1f}")
                q[1].metric("Max EV", f"{recent['ev'].max():.1f}")
            if "distance" in recent.columns:
                q[2].metric("Max dist", f"{recent['distance'].max():.0f}")
            if "is_hr" in recent.columns:
                q[3].metric("HRs", int(recent["is_hr"].astype(bool).sum()))
            st.markdown(contact_log_html(recent, max_height=320), unsafe_allow_html=True)
            st.caption(
                CONTACT_LOG_LEGEND
                + " On a pitcher the **Pitcher** column is the batter who hit it, "
                  "and **Arm** is which side he swings from."
            )
            cl, cr = st.columns(2)
            with cl:
                candles(p_bbe, "date", "ev", "Exit velo allowed by day",
                        height=260, unit="mph")
            with cr:
                candles(p_bbe, "date", "distance", "Distance allowed by day",
                        height=260, unit="ft")

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
    st.plotly_chart(fig, width="stretch", key=_chart_key())


def split_table_html(df: pd.DataFrame, pitcher: bool = False) -> str:
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
        if col in lower_better:
            pos = 1.0 - pos
        bg = ramp[min(len(ramp) - 1, int(pos * len(ramp)))]
        fg = "#06281a" if pos >= 0.6 else C["text"]
        return f"background:{bg};color:{fg};font-weight:700"

    # A pitching line reads the other way round: ERA/WHIP/HR9 are all
    # "lower is better", so those columns shade on the REVERSED ramp while
    # K/9 keeps the normal one. Getting that wrong would paint a 6.00 ERA
    # bright green.
    if pitcher:
        rate_cols = [c for c in ("ERA", "WHIP", "HR/9", "BB/9", "BAA", "K/9")
                     if c in df.columns]
        lower_better = {"ERA", "WHIP", "HR/9", "BB/9", "BAA"}
        cnt_cols = [c for c in ("IP", "BF", "H", "HR", "ER", "BB", "K")
                    if c in df.columns]
        first_label, first_fmt = "G-IP", None
    else:
        rate_cols = [c for c in ("AVG", "OBP", "SLG", "OPS", "ISO")
                     if c in df.columns]
        lower_better = set()
        cnt_cols = [c for c in ("HR", "XBH", "R", "RBI", "BB", "K")
                    if c in df.columns]
        first_label, first_fmt = "H-AB", None

    pad = "padding:5px 8px"
    head = (
        f"<th style='text-align:left;{pad}'>SPLIT</th>"
        f"<th style='{pad}'>{first_label}</th>"
        + "".join(f"<th style='{pad}'>{c}</th>" for c in rate_cols)
        + "".join(f"<th style='{pad}'>{c}</th>" for c in cnt_cols)
    )

    rows = []
    for idx in df.index:
        if pitcher:
            lead = f"{int(num(idx, 'G'))}-{num(idx, 'IP'):.1f}"
            thin = num(idx, "BF") < 40      # under ~1.5 starts
        else:
            lead = f"{int(num(idx, 'H'))}-{int(num(idx, 'AB'))}"
            thin = num(idx, "PA") < 25      # too few PA to read anything into
        name_style = f"color:{C['text3']}" if thin else f"color:{C['text']}"
        cells = "".join(
            f"<td style='{pad};{'' if thin else shade(idx, c)}'>"
            f"{num(idx, c):{'.3f' if c in ('AVG', 'OBP', 'SLG', 'OPS', 'ISO', 'BAA') else '.2f'}}</td>"
            for c in rate_cols
        )
        rows.append(
            f"<tr style='border-top:1px solid {C['border']}'>"
            f"<td style='text-align:left;{pad};{name_style};font-weight:600'>{idx}"
            + (f"<span style='color:{C['text3']};font-size:9px'> · thin</span>"
               if thin else "")
            + "</td>"
            f"<td style='{pad};color:{C['text2']}'>{lead}</td>"
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


def pitcher_split_chart(df: pd.DataFrame, title: str, height: int = 300) -> None:
    """Deviation-from-baseline bars for a pitcher, on HR/9.

    Same idea as the hitter chart but the polarity is flipped: for a pitcher,
    giving up FEWER homers is the good outcome, so bars point right (light
    green) when his HR/9 is BELOW his own baseline. HR/9 is the axis rather
    than ERA because this is a home-run dashboard -- ERA moves on bloop
    singles and bullpen luck, HR/9 is the thing being asked about.
    """
    if df is None or df.empty or "HR/9" not in df.columns:
        return
    hr9 = pd.to_numeric(df["HR/9"], errors="coerce")
    ip = pd.to_numeric(df["IP"], errors="coerce") if "IP" in df.columns else pd.Series(
        [0.0] * len(df), index=df.index)
    bf = pd.to_numeric(df["BF"], errors="coerce") if "BF" in df.columns else ip * 4

    total_ip = float(ip.sum())
    base = float((hr9 * ip).sum() / total_ip) if total_ip else float(hr9.mean())

    labels, deltas, texts, colors, hovers = [], [], [], [], []
    for idx in df.index:
        v, innings, faced = float(hr9.get(idx, 0)), float(ip.get(idx, 0)), float(bf.get(idx, 0))
        # Negated so "fewer homers allowed" points right, like every other
        # right-is-good bar on the site.
        delta = base - v
        labels.append(f"{idx}   {innings:.1f} IP")
        deltas.append(delta)
        texts.append(f"{v:.2f}")
        if faced < 40:
            colors.append("#3f6b52")
        else:
            colors.append("#b7f7c9" if delta >= 0 else "#0f6b3c")
        hovers.append(f"{idx}<br>HR/9 {v:.2f} (baseline {base:.2f})"
                      f"<br>{innings:.1f} IP · {int(faced)} batters faced")

    fig = go.Figure(go.Bar(
        x=deltas, y=labels, orientation="h", marker=dict(color=colors),
        text=texts, textposition="outside",
        textfont=dict(size=10, color=C["text2"], family=NUM_FONT),
        hovertext=hovers, hoverinfo="text",
    ))
    fig.add_vline(x=0, line=dict(color=C["text3"], width=1, dash="dot"))
    fig.add_annotation(x=0, y=1.06, yref="paper",
                       text=f"his overall {base:.2f} HR/9",
                       showarrow=False, font=dict(size=9, color=C["text3"]))
    span = max(0.25, float(max(abs(v) for v in deltas)) * 1.55) if deltas else 0.5
    _layout(fig, height, title)
    fig.update_xaxes(range=[-span, span], zeroline=False, showticklabels=False,
                     showgrid=False)
    fig.update_yaxes(autorange="reversed", tickfont=dict(size=10))
    st.plotly_chart(fig, width="stretch", key=_chart_key())


def render_pitcher_splits(sp: Dict[str, Any]) -> None:
    """Starter's day/night, home/away, weekday and W/L splits."""
    if not sp:
        st.info(
            "No situational splits published for this starter yet — they land "
            "with the next **Player Splits** workflow run."
        )
        return
    st.caption(
        f"{int(nn(sp, 'games_logged'))} games logged · season {sp.get('season', '')} — "
        "bars run right when he gives up **fewer** homers than his own "
        "average in that situation. Faded bars are under 40 batters faced."
    )
    for key, title in SPLIT_FAMILIES:
        df = _split_frame(sp.get(key) or {}, key)
        if df is None:
            continue
        pitcher_split_chart(df, title, height=max(190, 42 * len(df) + 95))
        st.markdown(split_table_html(df, pitcher=True), unsafe_allow_html=True)


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
    # The bot flags a hitter whose lineup spot this pitcher has been beaten in
    # ("allowed 2 HR to the #1 spot in 32 PA, .679 SLG"). ~50 of 215 on a
    # typical slate, so it's a real cut rather than a rounding error.
    weakspot_only = st.checkbox("⭐ Weak spot starts only", key="f_weak")
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
    if weakspot_only:
        out = [p for p in out if p.get("weak_spot_flag")]
    if lane_key != "all":
        out = [p for p in out if lane_pass(p, lane_key)]
    return out


view = apply_filters(players)


def board_search(rows: List[Dict[str, Any]], key: str,
                 label: str = "Search this board") -> List[Dict[str, Any]]:
    """Per-tab search box.

    The sidebar query already filters `view` globally, so search technically
    worked everywhere -- but on a top-N board you had to leave the board,
    scroll the sidebar, type, and come back. This is the same match logic
    scoped to one tab, so the ranking you were reading stays where it was.
    """
    q = st.text_input(
        label, "", key=key,
        placeholder="name, team, opponent or pitcher",
    ).strip().lower()
    if not q:
        return rows
    return [p for p in rows
            if q in f"{name_of(p)} {team_of(p)} {opp_of(p)} "
                    f"{txt(p, 'pitcher_name')}".lower()]


def best_non_hr_label(p: Dict[str, Any]) -> str:
    """Which non-HR board this hitter actually profiles for.

    enrich_signal_pills_and_best_non_hr() in the bot only writes
    best_non_hr_category for rows flagged true_avoid_hr, and leaves it as ""
    for everyone else. On the Hits/HRR tab -- where every row is a non-HR play
    by definition -- that meant the column came back blank for almost the
    whole board. Falls back to the same three scores the bot ranks, so the
    column populates without needing a pipeline re-run.
    """
    cat = txt(p, "best_non_hr_category")
    if cat and cat != "none":
        return {"hits": "Hits", "hrr": "HRR",
                "contact": "Contact/TB"}.get(cat, cat.title())
    lbl, val = max(
        (("Hits", hit_score(p)), ("HRR", prod_score(p)), ("Contact/TB", tb_score(p))),
        key=lambda t: t[1],
    )
    return f"{lbl} ({val:.0f})" if val >= 55 else "—"

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
        ("Weak spot starts only", bool(weakspot_only)),
        (f"Lane: {lane_key}", lane_key != "all"),
    ) if on
]
if _active_filters:
    with st.sidebar:
        st.caption("**Active filters** — " + " · ".join(_active_filters))
        if st.button("↩︎ Reset filters", width="stretch", key="resetfilters"):
            for k in ("f_team", "f_query", "f_minhr", "f_conf", "f_aligned",
                      "f_lane", "f_weak"):
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

# Projected vs actual. Before first pitch this is all projection; as games go
# final the actuals fill in beside it, so the header stops being a pre-game
# artefact and starts tracking the slate.
_res_now = (load_json("public/data/current/results_live.json")
            or load_json("public/data/current/results_final.json") or {})
_res_rows = _res_now.get("results") or []
_settled_now = [r for r in _res_rows if int(nn(r, "is_final"))]
_actual_hr = len(_res_now.get("merged_homers") or [])
_cap = _res_now.get("hr_capture_report") or {}
_slate_hr = int(nn(_cap, "total_hrs_on_slate"))

# The bot's own slate projection, recomputed so it can be shown to one
# decimal -- the .txt only ever prints the rounded low-high range.
_proj = projected_hr_total(players)
_proj_hr = _proj["mid"]

m = st.columns(6)
m[0].metric("Games", games)
m[1].metric("Hitters", len(view))
m[2].metric(
    "HR projected", f"{_proj_hr:.1f}",
    delta=(f"{_slate_hr - _proj_hr:+.1f} vs actual" if _slate_hr else None),
    delta_color="normal",
    help=f"The bot's slate projection — printed in the report as "
         f"\"projected HRs {_proj['low']}\u2013{_proj['high']}\", shown here to "
         f"one decimal. Power grade: {_proj['grade']}. "
         f"{_proj['top_profiles']} top HR profiles, {_proj['weak_games']} weak "
         "pitcher spots. Delta compares to the real slate total once games start.",
)
m[3].metric(
    "HR actual", f"{_slate_hr:.1f}" if _slate_hr else "—",
    delta=(f"{_actual_hr} on the board" if _actual_hr else None),
    delta_color="off",
    help="Home runs hit on this slate so far, and how many were by someone "
         "the model had on the board.",
)
m[4].metric("🧩 Aligned", sum(1 for p in players if is_aligned(p)))
m[5].metric(
    "✅ Confirmed", sum(1 for p in players if p.get("lineup_confirmed")),
    delta=(f"{len(_settled_now)} picks settled" if _settled_now else None),
    delta_color="off",
)
# ── SLATE LEAN ─────────────────────────────────────────────────────────────
# Which way tonight actually tilts. Comparing the boards' raw medians would be
# meaningless -- they aren't calibrated to each other, and hit_score sits ~12
# points above hr_score on an ordinary night. So each board is scored against
# ITS OWN normal, measured over 34 graded days:
#   hr_score      median 49.9  (sd 17.1)
#   hit_score     median 61.3  (sd  4.3)
#   contact_score median 48.1  (sd  5.7)
# The lean is whichever board sits furthest above its own baseline in standard
# deviations, so "high for a HR night" and "high for a hits night" are on the
# same footing.
# score_for() has no "contact" kind -- it silently falls back to hr_score --
# so the contact board is read straight off the field it lives in.
_LEAN_BASE = {
    "HR": (lambda x: hr_score(x), 49.86, 17.11),
    "hits": (lambda x: hit_score(x), 61.30, 4.28),
    "bases": (lambda x: nn(x, "contact_score"), 48.12, 5.71),
}
# The baselines were measured on GRADED PICKS -- roughly 90 hitters a night,
# the top of each board -- not on all 260-odd bats in the slate. Taking the
# median of the whole slate would compare two different populations and make
# every night look far below normal. So today is measured over a matched
# top-90 slice of each board.
_LEAN_N = 90
_lean_z = {}
for _lbl, (_fn, _mu, _sd) in _LEAN_BASE.items():
    try:
        _vals = [_fn(x) for x in players]
        _vals = sorted((v for v in _vals if isinstance(v, (int, float)) and v),
                       reverse=True)[:_LEAN_N]
        if len(_vals) < 20:
            continue
        _m = float(med(_vals))
    except Exception:
        continue
    _lean_z[_lbl] = (_m - _mu) / (_sd or 1.0)
if _lean_z:
    # A lean is a RELATIVE call -- which of the three to favour tonight --
    # so it keys off the gap between the top two boards, not the top board's
    # absolute level. The absolute level is reported separately, because
    # "favour hits" on a night when everything is down still matters.
    _ordered = sorted(_lean_z, key=lambda k: -_lean_z[k])
    _best = _ordered[0]
    _gap = _lean_z[_best] - (_lean_z[_ordered[1]] if len(_ordered) > 1 else 0.0)
    if _gap < 0.4:
        _lean_txt = "**balanced** — no board stands out tonight"
    elif _gap < 1.0:
        _lean_txt = f"leans **{_best}** (mild)"
    else:
        _lean_txt = f"leans **{_best}**"
    _overall = sum(_lean_z.values()) / len(_lean_z)
    _lean_txt += (" · whole slate **quiet**" if _overall < -0.75
                  else " · whole slate **live**" if _overall > 0.75 else "")
    st.caption(
        f"Slate {_lean_txt}  ·  "
        + " · ".join(f"{k} {_lean_z[k]:+.1f}sd" for k in ("HR", "hits", "bases")
                     if k in _lean_z)
        + "  — each board against its own 34-day normal. HR swings widest "
          "(sd 17) because the scoring model changed mid-season, so treat an "
          "HR lean as the softest of the three."
    )

st.caption(
    f"Projected **{_proj['low']}\u2013{_proj['high']}** HRs · power grade "
    f"**{_proj['grade']}** · {_proj['top_profiles']} top HR profiles · "
    f"{_proj['weak_games']} weak pitcher spots  ·  "
    f"HR 80+: {sum(1 for x in hrs if x >= 80)} · "
    f"HR 90+: {sum(1 for x in hrs if x >= 90)}"
)

st.divider()

# Tab order matches the old site's lib/theme.js TABS list, so muscle memory
# carries over. Player is new: Streamlit has no modal, so what used to be
# PlayerModal is a tab instead.
#
# Labels are kept SHORT on purpose. At 16 tabs the full names ("Pair History",
# "Scoreboard", "Watchlist") overflowed the strip, so Streamlit collapsed the
# tail behind a scroll arrow -- Spray and Guide were unreachable without
# noticing the little chevron. These fit on one row.
(tab_games, tab_board, tab_long, tab_due, tab_hitshrr, tab_pitchers, tab_pairs,
 tab_bot, tab_pools, tab_pairhist, tab_scoreboard, tab_leaders, tab_player,
 tab_watch, tab_spray, tab_results, tab_guide) = st.tabs([
    "🗓️ Games", "🏆 HR", "🚀 Longest", "💣 Due", "💥 Hits",
    "⚾ Pitchers", "🎯 Pairs", "🤖 Bot", "🧩 Pools", "🧬 History",
    "📊 Board", "🥇 Leaders", "🔍 Player", "⭐ Watch", "💦 Spray",
    "✅ Results", "📖 Guide",
])

# ── BOARD ───────────────────────────────────────────────────────────────────
with tab_board:
    c1, c2, c3 = st.columns([2, 1, 2])
    kind_label = c1.selectbox(
        "Board type",
        ["HR", "DC (damage)", "Longest (distance)", "Cross (all-round)",
         "HRR", "Hit", "TB (Base)"])
    kind = {"HR": "hr", "DC (damage)": "dc", "Longest (distance)": "longest",
            "Cross (all-round)": "cross", "HRR": "hrr", "Hit": "hit",
            "TB (Base)": "tb"}[kind_label]
    top_n = c2.number_input("Show top", 5, 200, 25, step=5)
    with c3:
        hr_pool = board_search(view, "hr_board_q")

    ranked = sorted(hr_pool, key=lambda p: score_for(p, kind), reverse=True)[: int(top_n)]
    if not ranked:
        st.info("No players match these filters.")

    if ranked:
        # The profile heatmap is the primary chart now. A single ranked
        # column only says WHO is on top; the profile says WHY, and the
        # score is already its first column, so the ranking isn't lost.
        if True:
            # Same shape as the Due profile: the board tells you WHO, this
            # tells you WHY -- which of the inputs is actually carrying each
            # name. Scaled so the columns are comparable side by side.
            hm_hr = pd.DataFrame([{
                "Player": name_of(p),
                "Score": score_for(p, kind),
                "HRW": nn(p, "hrw_score"),
                "DC": nn(p, "damage_conversion_score"),
                "PMix": pmix_score(p),
                "Barrel": barrel_rate(p) * 100,
                "P HR/9": nn(p, "pitcher_hr9") * 30,
            } for p in ranked[:15]]).set_index("Player")
            heatmap(hm_hr, f"Top 15 by {kind_label} score — full profile",
                    height=max(360, 26 * min(15, len(ranked)) + 120))
            st.caption(
                "Each column is scaled independently, so a bright cell means "
                "*high for this slate* on that input — not that the numbers "
                "are comparable across columns. P HR/9 is ×30 to fit the scale."
            )

        with st.expander("Score distribution — whole slate"):
            st.markdown("**Score distribution — whole slate**")
            # Binned with pandas rather than st.plotly_chart: plotly isn't in
            # requirements.txt and pulling it in would slow every cold boot on
            # Streamlit Cloud just to draw one histogram.
            series = pd.Series([score_for(p, kind) for p in players])
            bins = pd.cut(series, bins=range(0, 105, 5), right=False)
            hist = bins.value_counts().sort_index()
            hist.index = [f"{int(iv.left)}" for iv in hist.index]
            hbar([str(i) for i in hist.index], [float(v) for v in hist.values],
                 height=max(220, 24 * len(hist) + 60), fmt="{:.0f}")
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



    # First pitch order matters when you're actually playing the slate --
    # you need to know what locks in twenty minutes, which a strength
    # ranking can't tell you.
    # Always first-pitch order. The game panels are the point of this page,
    # and you read a slate chronologically -- re-ranking them by score made
    # you hunt for the 7:05 game you were actually about to bet.
    order = sorted(by_game.items(), key=lambda kv: game_start(kv[1]))

    # Slate-level view first: which games are worth attention at a glance.
    # Peaks (top HR / top HRR) say "is there a play here"; the averages across
    # every batter in the game say "is the whole lineup live or is it one guy",
    # which is what separates a real spot from a single outlier.
    if order:
        st.markdown("#### Slate at a glance")


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
                "Med Hit": round(med([hit_score(x) for x in gp]), 1),
                "Med TB": round(med([tb_score(x) for x in gp]), 1),
                "Pitcher": txt(head, "pitcher_name"),
                "P HR/9": round(nn(head, "pitcher_hr9"), 2),
                "Park HR": round(nn(head, "park_hr_factor", default=1.0), 2),
            })
        gdf = pd.DataFrame(glance).sort_values("Game Score", ascending=False)

        # Slate at a glance. "Best game score 61.2" told you a number on a
        # scale nobody knows. These answer questions instead: how much power
        # is on tonight, WHICH game is the one, how much of the slate is
        # actually confirmed, and where the exploitable spots are.
        # gdf is already sorted by Game Score descending, so the best game is
        # simply the first row. (This previously used gdf.iloc[idxmax()],
        # mixing a label from idxmax with positional .iloc indexing -- after
        # the sort those disagree, so it reported the wrong game entirely.)
        _best_g = gdf.iloc[0] if len(gdf) else None
        _conf = sum(1 for p in view if p.get("lineup_confirmed"))
        _hot = sum(1 for p in view if hr_score(p) >= 70)
        s = st.columns(6)
        s[0].metric("Games", len(gdf))
        s[1].metric("Projected HRs", f"{_proj_hr:.1f}",
                    f"{_proj['low']}–{_proj['high']} range", delta_color="off")
        s[2].metric("Best game",
                    str(_best_g["Game"]) if _best_g is not None else "—",
                    f"score {_best_g['Game Score']:.1f}" if _best_g is not None else None,
                    delta_color="off")
        s[3].metric("Hitters 70+", _hot,
                    f"{100 * _hot / max(1, len(view)):.0f}% of slate", delta_color="off")
        s[4].metric("Lineups confirmed",
                    f"{100 * _conf / max(1, len(view)):.0f}%",
                    f"{_conf} of {len(view)}", delta_color="off")
        s[5].metric("⭐ Weak spots",
                    sum(1 for p in view if p.get("weak_spot_flag")))

        # The heatmap and the table below are supporting material, so they
        # get one fixed ordering instead of a control -- every column is on
        # the heatmap anyway, and it sorts on click.
        ranked_games = gdf.sort_values("Game Score", ascending=False)

        # The "Games ranked by" chart is gone -- the sorted table and the
        # per-metric heatmap directly below say the same thing with more
        # detail, so it was a third rendering of one ordering.
        # Radar removed per request -- the per-metric heatmap directly
        # below answers the same "what shape is this game" question in a
        # form that reads at a glance and matches the rest of the site.
        hm = ranked_games.set_index("Game")[
            ["Game Score", "Med HR", "Med HRR", "Med HRW", "Med DC", "Med Hit",
             "Med TB", "Top HR", "Top HRR"]
        ]
        heatmap(hm, "Game x metric — hotter is better for hitters",
                height=max(280, 26 * len(hm) + 90), fmt="{:.0f}")


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

    # Every starter on the slate, as swap candidates for the what-if below.
    # Only starters are published -- true bullpen arms never are, so a
    # reliever can only be approximated by a similar starter.
    arms_by_name: Dict[str, Dict[str, Any]] = {}
    for _p in players:
        _n = txt(_p, "pitcher_name")
        if _n and _n not in arms_by_name:
            arms_by_name[_n] = _p
    arm_names = sorted(arms_by_name)

    slate_gs_med = med(list(game_score_by_pk.values())) if game_score_by_pk else 0.0

    with st.expander("What the header numbers mean"):
        st.markdown(
            "**GS — Game Score.** For each hitter take the median of his four "
            "board scores (HR, HRR, HRW, DC); then take the median of *those* "
            "across every hitter in the lineup.\n\n"
            "Two medians on purpose. The inner one stops a hitter who spikes on "
            "a single board from looking live; the outer one stops one big bat "
            "from carrying a dead lineup. So GS answers **is this whole lineup "
            "dangerous**, not *is there one guy here*.\n\n"
            "**▲ / ▽** — above or below the slate's own median GS.\n\n"
            "**Med HRW** — median HR-window score: how much of this lineup the "
            "model reads as being in a hot window right now.\n\n"
            "**⭐ n** — hitters whose lineup spot this starter has already been "
            "beaten in.\n\n"
            "Matchups read **away @ home**."
        )

    for gpk, gp in order:
        head = max(gp, key=hr_score)
        conf = "✅" if head.get("lineup_confirmed") else "◻︎"
        gs = game_score_by_pk.get(gpk, 0.0)
        edge = "▲" if gs >= slate_gs_med else "▽"
        n_weak = sum(1 for x in gp if x.get("weak_spot_flag"))
        away, home = home_away(gp)
        matchup = (f"{away} @ {home}" if away and home
                   else f"{team_of(head)} vs {opp_of(head)}")
        med_hrw = med([nn(x, "hrw_score") for x in gp])
        with st.expander(
            f"{conf}  {local_time(gp)}   ·   {matchup}   ·   "
            f"GS {gs:.1f} {edge}   ·   Med HRW {med_hrw:.0f}"
            + (f"   ·   ⭐{n_weak}" if n_weak else "")
            + f"   ·   {txt(head, 'venue_name')}"
        ):
            # Where this game sits on each board, not just Game Score.
            g1 = st.columns(8)
            g1[0].metric("Med HR", f"{med([hr_score(x) for x in gp]):.1f}")
            g1[1].metric("Med Hit", f"{med([hit_score(x) for x in gp]):.1f}")
            g1[2].metric("Med HRR", f"{med([prod_score(x) for x in gp]):.1f}")
            g1[3].metric("Med DC", f"{med([nn(x, 'damage_conversion_score') for x in gp]):.1f}")
            # Cross-board base score: the median of each hitter's hit / DC /
            # TB / HRR scores, then the median of that across the game. One
            # number for "how live is this lineup overall", rather than
            # reading four boards to find out.
            g1[6].metric("Med base", f"{med([cross_board(x) for x in gp]):.1f}")
            g1[4].metric("Med TB", f"{med([tb_score(x) for x in gp]):.1f}")
            g1[5].metric("Top Longest", f"{max(longest_score(x) for x in gp):.1f}")
            g1[7].metric("⭐ Weak spots", n_weak)


            # Five names ranked on one score only told you the order, and the
            # order was already obvious from the lineup table. This says what
            # each of them is actually good at, so you can see at a glance
            # whether the game's best bat is a power play or a contact play.
            top5 = sorted(gp, key=hr_score, reverse=True)[:5]
            hm_g5 = pd.DataFrame([{
                "Player": name_of(x),
                "HR": hr_score(x),
                "Hit": hit_score(x),
                "HRR": prod_score(x),
                "TB": tb_score(x),
                "DC": nn(x, "damage_conversion_score"),
                "HRW": nn(x, "hrw_score"),
                "P HR/9": nn(x, "pitcher_hr9") * 30,
            } for x in top5]).set_index("Player")
            heatmap(hm_g5, "Top 5 bats in this game — what each one is for",
                    height=max(240, 30 * len(hm_g5) + 110), fmt="{:.0f}")
            st.caption(
                "Columns scale independently, so bright means high *for this "
                "slate*. A bright HR row with a dark Hit row is a swing-hard "
                "bat; the reverse is a table-setter. P HR/9 is ×30 to fit."
            )

            # Wind gets a real block like every other number. st.metric can't
            # render rotated HTML, so the bearing is snapped to the nearest of
            # eight compass glyphs -- wind_deg is the direction it blows FROM,
            # so +180 points it where the ball actually gets carried.
            _wdeg = head.get("wind_deg", head.get("weather_wind_deg"))
            _wlab = txt(head, "wind_direction_label")
            _wmph = nn(head, "weather_wind_mph", "wind_mph")
            if _wdeg is not None:
                _glyph = "↑↗→↘↓↙←↖"[int(((float(_wdeg) + 180) % 360) / 45 + 0.5) % 8]
            else:
                _glyph = ""
            e = st.columns(6)
            e[0].metric("Temp", f"{nn(head, 'weather_temp_f'):.0f}°F" if head.get("weather_temp_f") else "—")
            e[1].metric("Wind", f"{_glyph} {_wmph:.0f} mph" if _wmph else "—",
                        _wlab or None, delta_color="off")
            e[2].metric("Park HR", f"{nn(head, 'park_hr_factor', default=1.0):.2f}")
            e[3].metric("Weather HR", f"{nn(head, 'weather_hr_effect_pct'):+.0f}%")
            e[4].metric("Roof", txt(head, "roof", default="—"))
            e[5].metric("Attack", txt(head, "pitcher_attack_tag", default="—"))
            if txt(head, "weather_label"):
                st.caption(txt(head, "weather_label"))
            # Wind arrow. wind_deg is the compass direction the wind blows
            # FROM (meteorological convention), so the arrow has to point at
            # deg + 180 to show where the ball gets pushed. The label is
            # already computed relative to the park's CF bearing, so the two
            # together read as "out to CF ->" without needing a field diagram.
            _wdeg = head.get("wind_deg", head.get("weather_wind_deg"))
            _wlab = txt(head, "wind_direction_label")
            # BOTH starters, side by side. The header only ever named the
            # pitcher facing the strongest hitter, so half of every matchup
            # was invisible -- you could see that CHC's lineup was live
            # without ever learning who PIT was sending out.
            # ── WHAT-IF: swap the arm ──────────────────────────────────
            # For TBD starters, or when the bot's listed starter gets pulled
            # early and the game is really a bullpen game.
            cur_arm = txt(head, "pitcher_name")
            wi_default = arm_names.index(cur_arm) + 1 if cur_arm in arm_names else 0
            wi = st.selectbox(
                "What if a different arm pitched?",
                ["— listed starter —"] + arm_names,
                index=wi_default, key=f"whatif_{gpk}",
                help="Estimates the HR-score shift from swapping the pitcher. "
                     "Only starters on today's slate are available -- bullpen "
                     "arms aren't in the published data.",
            )
            if wi != "— listed starter —" and wi != cur_arm:
                arm = arms_by_name[wi]
                a1, a2, a3 = st.columns(3)
                a1.metric("HR/9", f"{nn(arm, 'pitcher_hr9'):.2f}",
                          delta=f"{nn(arm, 'pitcher_hr9') - nn(head, 'pitcher_hr9'):+.2f}")
                a2.metric("Barrel% allowed",
                          f"{nn(arm, 'pitcher_barrel_allowed') * 100:.1f}%",
                          delta=f"{(nn(arm, 'pitcher_barrel_allowed') - nn(head, 'pitcher_barrel_allowed')) * 100:+.1f}pp")
                a3.metric("Pitcher damage", f"{pitcher_damage_for(arm):.1f}",
                          delta=f"{pitcher_damage_for(arm) - pitcher_damage_for(head):+.1f}")

                wi_rows = sorted(gp, key=hr_score, reverse=True)[:10]
                st.dataframe(pd.DataFrame([{
                    "Player": name_of(x), "Team": team_of(x),
                    "HR now": round(hr_score(x), 1),
                    f"HR vs {wi}": round(hr_score_vs_arm(x, arm), 1),
                    "Shift": round(hr_score_vs_arm(x, arm) - hr_score(x), 1),
                } for x in wi_rows]), width="stretch", hide_index=True)
                st.caption(
                    f"Estimate only. Moves the pitcher_damage term "
                    f"({PITCHER_DAMAGE_WEIGHT:.0%} of the HR blend) and holds the other "
                    "fourteen fixed — it does not re-run the model, so gates and "
                    "post-blend multipliers aren't reflected."
                )
                st.divider()

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
                # Default to Both. Opening on one club meant the other
                # lineup was one click away but invisible, so half of every
                # game went unread.
                _lu_opts = ["Both"] + teams_here
                pick_team = st.radio(
                    "Lineup", _lu_opts, horizontal=True, index=0,
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
                "HRW": round(nn(p, "hrw_score"), 1),
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

            # Streamlit dataframe rows aren't clickable, so every hitter in
            # the order gets here through a picker instead -- otherwise only
            # the five stamped game picks above could be opened, and the
            # other thirteen names in the game were dead text.
            if lineup_rows:
                _lu_sorted = sorted(
                    lineup_rows,
                    key=lambda x: (team_of(x), nn(x, "lineup_spot", default=99.0)),
                )
                _lu_c1, _lu_c2 = st.columns([4, 1])
                _who = _lu_c1.selectbox(
                    "Open a hitter",
                    _lu_sorted,
                    format_func=lambda x: (
                        f"{int(nn(x, 'lineup_spot', default=0)) or '—'}. "
                        f"{name_of(x)} ({team_of(x)}) · HR {hr_score(x):.1f}"
                    ),
                    key=f"luwho_{gpk}",
                    label_visibility="collapsed",
                )
                if _lu_c2.button("Open", width="stretch", key=f"luopen_{gpk}"):
                    player_modal(_who)


    st.divider()
    # Dessert: the game panels above are the meal. These two are what
    # you read after, to check the slate agreed with what you just saw.
    # ── PROJECTED OUTPUT BY GAME ───────────────────────────────────
    # Scores are ranks: a 78 only means "above a 62". These are
    # PROJECTIONS -- each hitter's score is mapped through the observed
    # hit rate for its band across 34 graded days (3,265 player-days),
    # then summed over the lineup. So a cell reads "this game projects
    # 2.4 home runs", not "this game scores 61".
    CALIB = {
        "Proj HR":    ("hr",      {0: 12.8, 40: 15.0, 55: 15.3, 70: 18.7, 85: 16.1}),
        "Proj hits":  ("hit",     {0: 61.8, 40: 59.5, 55: 63.0, 70: 65.4, 85: 72.0}),
        "Proj XBH":   ("contact", {0: 29.1, 40: 29.8, 55: 32.8, 70: 27.2, 85: 36.4}),
        "Proj bases": ("contact", {0: 37.8, 40: 37.5, 55: 41.6, 70: 34.3, 85: 45.5}),
    }

    def _rate(score: float, table: Dict[int, float]) -> float:
        edge = 0
        for cut in sorted(table):
            if score >= cut:
                edge = cut
        return table[edge] / 100.0

    def _sc(p, kind):
        if kind == "contact":
            return nn(p, "contact_score")
        return score_for(p, kind)

    # Per game, or split into the two lineups that make it up. A 3.0-HR
    # game is a different bet if one side is carrying 2.2 of it.
    proj_view = st.radio(
        "Projection view", ["By game", "By team"], horizontal=True,
        key="projview", label_visibility="collapsed",
    )
    # PITCHER ADJUSTMENT. The batter scores already carry some matchup
    # signal, so applying the arm's full effect on top would double-count --
    # the multipliers are square-rooted to take roughly half, and clamped so
    # one extreme arm can't swamp a lineup. League anchors: HR/9 1.20,
    # opponent AVG .245. An ace now suppresses the whole game and a
    # batting-practice arm lifts it, which is what was missing when a Skenes
    # start projected the same as a spot starter.
    def _arm_mult(grp, stat, anchor, lo=0.62, hi=1.55):
        vals = [nn(p, stat) for p in grp]
        vals = [v for v in vals if isinstance(v, (int, float)) and v > 0]
        if not vals:
            return 1.0
        return max(lo, min(hi, (sorted(vals)[len(vals) // 2] / anchor) ** 0.5))

    def _adjust(grp, col, base):
        if col in ("Proj HR", "Proj XBH", "Proj bases"):
            return base * _arm_mult(grp, "pitcher_hr9", 1.20)
        if col == "Proj hits":
            return base * _arm_mult(grp, "pitcher_avg_against", 0.245)
        return base

    buckets: Dict[Any, List[Dict[str, Any]]] = {}
    bucket_time: Dict[Any, str] = {}
    for gk, gp2 in by_game_full.items():
        _t0 = game_start(gp2)
        if proj_view == "By team":
            for p in gp2:
                t = team_of(p)
                lbl = f"{t} vs {opp_of(p)}" if t else txt(p, "venue_name", default=str(gk))
                buckets.setdefault(lbl, []).append(p)
                # Both halves of a game share its start time, so the two
                # sides stay side by side instead of being flung apart by
                # whichever lineup happens to project higher.
                bucket_time[lbl] = f"{_t0}|{t}"
        else:
            h2 = gp2[0]
            lbl = (f"{team_of(h2)} @ {opp_of(h2)}" if team_of(h2)
                   else txt(h2, "venue_name", default=str(gk)))
            buckets.setdefault(lbl, []).extend(gp2)
            bucket_time[lbl] = _t0

    proj_rows = []
    for lbl, grp in buckets.items():
        row = {"Game" if proj_view == "By game" else "Team": lbl}
        for col, (kind, table) in CALIB.items():
            base = sum(_rate(_sc(p, kind), table) for p in grp)
            row[col] = round(_adjust(grp, col, base), 2)
        row["_t"] = bucket_time.get(lbl, "")
        proj_rows.append(row)
    if proj_rows:
        _idx = "Game" if proj_view == "By game" else "Team"
        pdf = (pd.DataFrame(proj_rows).sort_values("_t")
               .drop(columns=["_t"]).set_index(_idx))
        heatmap(pdf, f"Projected output {proj_view.lower()} — expected count, not a score",
                height=max(280, 26 * len(pdf) + 90), fmt="{:.1f}")
        st.caption(
            "Each hitter's board score is converted to the rate that band "
            "actually produced over 34 graded days, then summed across "
            "the lineup. **Proj HR** and **Proj hits** rest on bands that "
            "climb cleanly with score, so they carry real signal. "
            "Totals are then scaled by the arm they face — HR/9 against a "
            "1.20 league anchor, opponent AVG against .245, square-rooted so "
            "the pitcher effect already inside the batter scores isn't "
            "counted twice. **Proj XBH** and **Proj bases** come off the contact board, "
            "whose bands do *not* climb with score — those two columns "
            "are close to lineup-size times a constant, so read them as "
            "opportunity, not edge."
        )

    # Seasoning: every number here is already on the heatmap above, so it
    # is collapsed rather than taking a screen of its own.
    with st.expander("Every game, every metric — sortable table"):
        st.dataframe(ranked_games, width="stretch", hide_index=True,
                     height=min(520, 40 * len(gdf) + 40))

# ── SCOREBOARD ──────────────────────────────────────────────────────────────
with tab_scoreboard:
    st.subheader("Scoreboard")
    st.caption(
        f"Every one of the {len(view)} hitters on the slate, all scores in one "
        "grid. Sort by several columns at once with the controls below the "
        "trackers — clicking a header only ever sorts by one."
    )
    # ── LIVE HR TRACKER ────────────────────────────────────────────────
    # The grid below is a pre-game object. This is the same slate scored
    # against reality as it happens, with each hitter's board rank next to
    # him -- the fastest read on whether the model is having a good night.
    _rank_by = {}
    for _i, _p in enumerate(sorted(view, key=hr_score, reverse=True), 1):
        _key = _p.get("player_id") or name_of(_p)
        _rank_by.setdefault(_key, _i)
    # A hitter picked in three tiers is three rows in the results payload;
    # listing him three times makes one homer look like three.
    _gone_seen, _gone = set(), []
    for _r in _res_rows:
        if not int(nn(_r, "actual_hr")):
            continue
        _gk = _r.get("player_id") or txt(_r, "name")
        if _gk in _gone_seen:
            continue
        _gone_seen.add(_gk)
        _gone.append(_r)
    _sb_a, _sb_b = st.columns(2)
    with _sb_a:
        st.markdown(f"**💥 Gone yard** ({len(_gone)})")
        if _gone:
            _gone_rows = []
            for r in sorted(_gone, key=lambda x: _rank_by.get(
                    x.get("player_id") or txt(x, "name"), 999)):
                _k = r.get("player_id") or txt(r, "name")
                _rk = _rank_by.get(_k)
                _gone_rows.append({
                    "Board": f"#{_rk}" if _rk else "—",
                    "Player": txt(r, "name"),
                    "Team": txt(r, "team"),
                    "HR": int(nn(r, "actual_hr")),
                    "HR score": round(nn(r, "hr_score"), 1),
                    "Role": txt(r, "pick_type", default="—"),
                })
            st.dataframe(pd.DataFrame(_gone_rows), width="stretch",
                         hide_index=True)
            _hit_top15 = sum(1 for g in _gone_rows
                             if g["Board"] != "—" and int(g["Board"][1:]) <= 15)
            st.caption(
                f"{_hit_top15} of {len(_gone_rows)} came from the top 15 of "
                "the board."
            )
        elif _res_rows:
            st.caption("Nobody on the slate has gone deep yet.")
        else:
            st.caption("Results load once the first games are underway.")
    with _sb_b:
        # ── WEAK SPOTS ─────────────────────────────────────────────────
        # Every lineup spot the model flagged, grouped by the arm that has
        # to face it. Sorted by how much damage that spot does to him, so
        # the top row is the single most exploitable matchup on the slate.
        _ws = [p for p in view if p.get("weak_spot_flag")]
        st.markdown(f"**⭐ Weak spots** ({len(_ws)})")
        if _ws:
            _by_arm = {}
            for p in _ws:
                _by_arm.setdefault(txt(p, "pitcher_name", default="TBD"), []).append(p)
            _ws_rows = []
            for _arm, _grp in _by_arm.items():
                _grp = sorted(_grp, key=lambda x: nn(x, "lineup_spot", default=99.0))
                _ws_rows.append({
                    "Pitcher": _arm,
                    "HR/9": round(nn(_grp[0], "pitcher_hr9"), 2),
                    "Spots": ", ".join(
                        str(int(nn(x, "lineup_spot", default=0))) for x in _grp),
                    "Hitters": ", ".join(name_of(x) for x in _grp),
                    "Damage": round(nn(_grp[0], "pitcher_spot_damage_score"), 1),
                    "Best HR": round(max(hr_score(x) for x in _grp), 1),
                })
            _ws_rows.sort(key=lambda r: -r["Damage"])
            st.dataframe(pd.DataFrame(_ws_rows), width="stretch",
                         hide_index=True)
            st.caption(
                "Damage is how hard that pitcher gets hit in those spots. "
                "Sorted hardest first."
            )
        else:
            st.caption("No weak spots flagged on this slate.")

    st.divider()

    sb_pool = board_search(view, "scoreboard_q")
    board = [{
        "Player": name_of(p), "Team": team_of(p), "Opp": opp_of(p),
        "Spot": p.get("lineup_spot"), "Role": tier_role(p),
        "HR": round(hr_score(p), 1), "HRR": round(prod_score(p), 1),
        "Hit": round(hit_score(p), 1), "TB": round(tb_score(p), 1),
        "Damage": round(nn(p, "damage_conversion_score"), 1),
        "Cross": round(cross_board(p), 1),
        "Longest": round(longest_score(p), 1),
        "PMix": round(pmix_score(p), 1),
        "PMatch": round(nn(p, "pitch_type_match_score"), 1),
        "375+": int(recent375(p)), "400+": int(recent400(p)),
        "IHR": round(ihr_val(p), 3), "K%": round(nn(p, "season_k_rate") * 100, 1),
        "Pitcher": txt(p, "pitcher_name"),
        "P HR/9": round(nn(p, "pitcher_hr9"), 2),
        "🧩": "🧩" if is_aligned(p) else "",
    } for p in sb_pool]
    bdf = pd.DataFrame(board)

    if bdf.empty:
        st.info("No players match your filters — clear them in the sidebar.")
    else:
        sb1, sb2, sb3 = st.columns([2, 2, 1])
        # Streamlit's grid only sorts by one column at a time -- clicking a
        # second header replaces the first. So the sort happens here instead:
        # pick columns in priority order and the frame is sorted before it is
        # handed to the grid. Ties on the first key break on the second, and
        # so on.
        sb_sort = sb1.multiselect(
            "Sort by (in order)",
            ["HR", "Cross", "Longest", "HRR", "Hit", "TB", "Damage",
             "PMix", "PMatch", "375+", "400+", "IHR", "K%", "Spot"],
            default=["HR"], key="sbsort",
            help="Add more than one to break ties — e.g. HR then Cross sorts "
                 "by HR, and equal HR scores fall back to Cross.",
        )
        sb_asc = sb1.checkbox(
            "Low to high", value=False, key="sbasc",
            help="Applies to every sort column. Spot sorts low-to-high by "
                 "default since batting order 1 is the top of the lineup.",
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
        _keys = [c for c in sb_sort if c in bdf.columns] or ["HR"]
        # Spot is the one column where "best" means smallest.
        _asc = [sb_asc if c != "Spot" else (not sb_asc) for c in _keys]
        out = bdf.sort_values(_keys, ascending=_asc)[keep]
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
        open_player_picker(sb_pool, "scoreboard")
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
                    # A pitch mix is parts of a whole, so a pie reads it
                    # faster than bars -- you want "how much of the time does
                    # he throw the pitch this guy crushes", not a ranking.
                    arsenal_pie(usage, r.get("pitcher_mistake_pitch_v31"))
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
                    "HRW": round(nn(b, "hrw_score"), 1),
                    "DC": round(nn(b, "damage_conversion_score"), 1),
                    "Role": tier_role(b),
                } for b in e["lineup"]])
                st.dataframe(ldf, width="stretch", hide_index=True)
                open_player_picker(e["lineup"], f"opp_{e.get('pitcher_id')}",
                                   "Open a hitter from this lineup")

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


# ── LONGEST HR ──────────────────────────────────────────────────────────────
# Distance, not probability. longest_hr_score answers "who hits the farthest
# ball tonight", which is a different question from "who is most likely to go
# deep" -- a 40%-to-homer guy who hits 390ft doesn't win this board.
def carry_factor(p: Dict[str, Any]) -> float:
    """Multiplier on raw distance from park dimensions and air.

    park_dist_factor is the ballpark's own carry. weather_hr_effect_pct is
    the bot's temperature/wind adjustment, expressed as a % swing on HR rate;
    a ball carries roughly a third as much in distance terms as it does in
    HR-rate terms, so it's damped before being applied here.
    """
    park = nn(p, "park_dist_factor", default=1.0) or 1.0
    weather_pct = nn(p, "weather_hr_effect_pct", default=0.0)
    return float(park * (1.0 + (weather_pct / 100.0) * 0.33))


def longest_adj(p: Dict[str, Any]) -> float:
    """longest_hr_score after park + air carry."""
    return max(0.0, min(100.0, longest_score(p) * carry_factor(p)))


def bbe_tracked(p: Dict[str, Any]) -> int:
    return int(nn(p, "recent_distance_tracked", "recent_350_den", "recent_pa_window"))


with tab_long:
    st.subheader("🚀 Longest HR")
    st.caption(
        "Who hits the **farthest** ball tonight — a distance board, not a "
        "probability board. Different question from the HR tab, and it "
        "regularly disagrees with it."
    )

    with st.expander("How this is calculated"):
        st.markdown(
            "`longest_hr_score` blends four recent-window distance signals:\n\n"
            "| Weight | Signal | Why |\n|---|---|---|\n"
            "| 35% | rate of **400ft+** batted balls | tail events predict tail events |\n"
            "| 25% | recent **average exit velo** | the engine |\n"
            "| 22% | rate of **350ft+** batted balls | deep-contact volume |\n"
            "| 18% | recent **average distance** | overall carry |\n\n"
            "Small samples are shrunk toward a neutral 40, reaching full trust "
            "at 20 tracked batted-ball events — so a hitter with 3 tracked BBE "
            "cannot post an 85 on four good swings.\n\n"
            "**Carry adjustment** (this page only) multiplies that by the park's "
            "`park_dist_factor` and a damped share of the weather HR effect. "
            "Air and altitude move distance, but less than they move HR rate, "
            "so the weather term is scaled to a third before it is applied."
        )

    lc1, lc2, lc3 = st.columns([2, 1, 2])
    use_carry = lc1.radio("Rank by", ["Adjusted for park + air", "Raw score"],
                          horizontal=True, key="long_mode")
    long_n = lc2.number_input("Show top", 5, 100, 25, step=5, key="long_n")
    with lc3:
        long_pool = board_search(view, "long_q")

    min_bbe = st.slider(
        "Minimum tracked batted balls", 0, 30, 0, step=5, key="long_bbe",
        help="Below ~20 the score is shrunk toward 40. Raise this to see only "
             "hitters with a sample behind the number.",
    )
    long_pool = [p for p in long_pool if bbe_tracked(p) >= min_bbe]

    keyfn = longest_adj if use_carry.startswith("Adjusted") else longest_score
    ranked_long = sorted(long_pool, key=keyfn, reverse=True)[: int(long_n)]

    if not ranked_long:
        st.info("No hitters match these filters.")
    else:
        m = st.columns(4)
        m[0].metric("Top score", f"{keyfn(ranked_long[0]):.1f}")
        m[1].metric("Slate median",
                    f"{med([longest_score(p) for p in players]):.1f}")
        m[2].metric("Best carry",
                    f"{max(carry_factor(p) for p in ranked_long):.2f}×",
                    help="Park distance factor × damped weather effect.")
        m[3].metric("Thin samples",
                    sum(1 for p in ranked_long if bbe_tracked(p) < 20),
                    help="Fewer than 20 tracked BBE — score is shrunk toward 40.")

        hm_long = pd.DataFrame([{
            "Player": name_of(p),
            "Adj": longest_adj(p),
            "Raw": longest_score(p),
            "400+": recent400(p) * 20,
            "375+": recent375(p) * 12,
            "Avg EV": nn(p, "recent_ev", "l25pa_avg_ev"),
            "Barrel": barrel_rate(p) * 100,
            "Carry": carry_factor(p) * 60,
        } for p in ranked_long[:15]]).set_index("Player")
        heatmap(hm_long, "Distance profile (scaled)",
                height=max(280, 30 * len(hm_long) + 90))
        st.caption(
            "Columns are scaled independently, so bright means *high for this "
            "slate* on that input. 400+/375+/Carry are multiplied only to sit "
            "on a comparable scale — the real values are in the table above."
        )

        if True:
            st.markdown("**Distance score vs HR score**")
            st.caption(
                "Hitters far from the diagonal are the interesting ones: high "
                "distance and low HR score is a big-fly-or-nothing bat."
            )
            sc = pd.DataFrame({
                "Longest": [round(keyfn(p), 1) for p in ranked_long],
                "HR": [round(hr_score(p), 1) for p in ranked_long],
                "Player": [name_of(p) for p in ranked_long],
            })
            st.scatter_chart(sc, x="HR", y="Longest", height=300)

        st.markdown("#### The board")
        st.dataframe(pd.DataFrame([{
            "": "✅" if p.get("lineup_confirmed") else "◻︎",
            "Player": name_of(p), "Team": team_of(p), "Opp": opp_of(p),
            "Adj": round(longest_adj(p), 1),
            "Raw": round(longest_score(p), 1),
            "Carry": round(carry_factor(p), 2),
            "HR": round(hr_score(p), 1),
            "Rank": int(nn(p, "longest_hr_rank")) or None,
            # The four raw inputs behind the score.
            "400+": int(recent400(p)),
            "375+": int(nn(p, "recent_375_num")),
            "350+": int(nn(p, "recent_350_num")),
            "BBE": bbe_tracked(p),
            "Avg EV": round(nn(p, "recent_ev", "l25pa_avg_ev"), 1),
            "Barrel%": round(nn(p, "recent_barrel_rate") * 100, 1),
            "Park dist": round(nn(p, "park_dist_factor", default=1.0), 2),
            "Wx HR": f"{nn(p, 'weather_hr_effect_pct'):+.0f}%",
            "Pitcher": txt(p, "pitcher_name"),
            "Venue": txt(p, "venue_name"),
        } for p in ranked_long]), width="stretch", hide_index=True, height=520)

        # Same profile grid as the HR and Hits boards -- the table tells you
        # the order, this tells you which input is carrying each name.

        open_player_picker(ranked_long, "longest")

        st.download_button(
            "⬇️ CSV",
            pd.DataFrame([{"Player": name_of(p), "Team": team_of(p),
                           "Adj": round(longest_adj(p), 1),
                           "Raw": round(longest_score(p), 1),
                           "Carry": round(carry_factor(p), 2),
                           "BBE": bbe_tracked(p)} for p in ranked_long]
                         ).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"mlb_{slate}_longest.csv", mime="text/csv", key="longcsv")

        st.caption(
            "**Adj** = raw × carry. **Carry** under 1.00 is a park/air "
            "combination that suppresses distance. **BBE** under 20 means the "
            "raw score is partly the neutral 40, not measured power."
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

    pool = [p for p in board_search(view, "due_board_q")
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

        if True:
            hm = pd.DataFrame([{
                "Player": name_of(p),
                "Due": nn(p, "hr_due_score"),
                "Drought": nn(p, "games_since_last_hr"),
                "HR gap": due_gap(p) * 20,
                "HR/PA": nn(p, "hr_per_pa") * 1000,
                "Barrel": barrel_rate(p) * 100,
                # A drought only matters if tonight is a good spot to end it,
                # so the two matchup inputs sit alongside the due signals.
                "DC": nn(p, "damage_conversion_score"),
                "P HR/9": nn(p, "pitcher_hr9") * 30,
            } for p in ranked_due[:15]]).set_index("Player")
            heatmap(hm, "Due profile (scaled)",
                    height=max(360, 26 * min(15, len(ranked_due)) + 120))

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

    hh1, hh2, hh3 = st.columns([2, 1, 2])
    hh_kind = hh1.radio(
        "Type", ["HRR (runs + RBI)", "Hit (base-hit floor)", "Base / XBH"],
        horizontal=True)
    hh_n = hh2.number_input("Show", 5, 100, 30, step=5, key="hhn")
    k = {"HRR (runs + RBI)": "hrr", "Hit (base-hit floor)": "hit",
         "Base / XBH": "tb"}[hh_kind]
    with hh3:
        hh_pool = board_search(view, "hitshrr_q")

    # Lineup slot decides how much of this board is even reachable: you can't
    # drive runs with nobody on, and you can't score without someone behind
    # you. These two filters are the difference between a good bat and a good
    # SPOT, which is what an HRR play actually is.
    f1, f2, f3 = st.columns(3)
    top_slot = f1.checkbox("Top 5 in the order only", key="hh_top5")
    min_preob = f2.slider("Min on-base ahead of him", 0.0, 0.50, 0.0, 0.05,
                          key="hh_preob",
                          help="lineup_pre_onbase — chance someone is on when "
                               "he bats. Drives RBI.")
    min_post = f3.slider("Min conversion behind him", 0.0, 0.60, 0.0, 0.05,
                         key="hh_post",
                         help="lineup_post_convert — chance the bats after him "
                              "bring him around. Drives runs scored.")
    if top_slot:
        hh_pool = [p for p in hh_pool if nn(p, "lineup_spot", default=99) <= 5]
    hh_pool = [p for p in hh_pool
               if nn(p, "lineup_pre_onbase") >= min_preob
               and nn(p, "lineup_post_convert") >= min_post]

    hh = sorted(hh_pool, key=lambda p: score_for(p, k), reverse=True)[: int(hh_n)]

    if not hh:
        st.info("No hitters match these filters.")
    else:
        slate_med = med([score_for(p, k) for p in players])
        m = st.columns(4)
        m[0].metric(f"Top {hh_kind.split(' ')[0]}", f"{score_for(hh[0], k):.1f}")
        m[1].metric("Slate median", f"{slate_med:.1f}")
        m[2].metric("Grade A or better",
                    sum(1 for p in hh if grade_for(p, k) in ("A+", "A")))
        m[3].metric("Top-5 spots", sum(1 for p in hh
                                       if nn(p, "lineup_spot", default=99) <= 5))

        # The profile grid leads the page -- Score is its first column, so
        # the ranking is still there, and the rest of the row says why.
        if True:
            # Bat and spot in one grid. A great bat with dark PreOB/Post cells
            # is a good hitter in a dead lineup slot -- the exact thing this
            # board should stop you doing.
            hm_hh = pd.DataFrame([{
                "Player": name_of(p),
                "Score": score_for(p, k),
                "Hit": hit_score(p),
                "HRR": prod_score(p),
                "TB": tb_score(p),
                "PreOB": nn(p, "lineup_pre_onbase") * 100,
                "Post": nn(p, "lineup_post_convert") * 100,
            } for p in hh[:15]]).set_index("Player")
            heatmap(hm_hh, "Bat and spot (scaled)",
                    height=max(360, 26 * min(15, len(hh)) + 120))
            st.caption(
                "**PreOB** and **Post** are the lineup around him — men on "
                "when he bats, and the bats behind him. A bright Hit column "
                "next to dark PreOB/Post is a good bat in a dead slot."
            )

        st.markdown("#### The board")
        st.dataframe(pd.DataFrame([{
            "": "✅" if p.get("lineup_confirmed") else "◻︎",
            "Player": name_of(p), "Team": team_of(p), "Opp": opp_of(p),
            "Spot": p.get("lineup_spot"), "Grade": grade_for(p, k),
            "Score": round(score_for(p, k), 1),
            "HRR": round(prod_score(p), 1), "Hit": round(hit_score(p), 1),
            "TB": round(tb_score(p), 1),
            "HRW": round(nn(p, "hrw_score"), 1),
            "AVG": round(nn(p, "season_avg"), 3),
            "OBP": round(nn(p, "season_obp"), 3),
            "BABIP": round(nn(p, "babip"), 3),
            "K%": round(nn(p, "season_k_rate") * 100, 1),
            "L5 H": int(nn(p, "last5_hits")), "L5 XBH": int(nn(p, "last5_xbh")),
            "L5 R": int(nn(p, "last5_runs")), "L5 RBI": int(nn(p, "last5_rbi")),
            "PreOB": round(nn(p, "lineup_pre_onbase"), 3),
            "Post": round(nn(p, "lineup_post_convert"), 3),
            "Best non-HR": best_non_hr_label(p),
            "Pitcher": txt(p, "pitcher_name"),
        } for p in hh]), width="stretch", hide_index=True, height=520)

        open_player_picker(hh, "hitshrr")

        st.download_button(
            "⬇️ CSV",
            pd.DataFrame([{"Player": name_of(p), "Team": team_of(p),
                           "Score": round(score_for(p, k), 1),
                           "Spot": p.get("lineup_spot")} for p in hh]
                         ).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"mlb_{slate}_{k}.csv", mime="text/csv", key="hhcsv")

        st.caption(
            "**PreOB** is the chance someone is on base when he hits — that is "
            "what turns contact into RBI. **Post** is the chance the bats "
            "behind him bring him home. A great bat in the 9 hole is a worse "
            "HRR play than a decent bat hitting 3rd."
        )

# ── LIVE HR STATE ───────────────────────────────────────────────────────────
# Who has ALREADY gone deep on this slate. The grader publishes it every hour
# and the Results tab was the only thing reading it, which made every other
# board a pre-game artefact -- you could be staring at a pair card at 9pm with
# no idea half of it had already cashed.
@st.cache_data(ttl=CACHE_TTL)
def homered_today() -> Dict[str, int]:
    """{normalised player name: HR count today}. Empty before first pitch."""
    res = (load_json("public/data/current/results_live.json")
           or load_json("public/data/current/results_final.json") or {})
    out: Dict[str, int] = {}
    for h in (res.get("merged_homers") or []):
        nm = norm_name(h.get("name"))
        if nm:
            base = h.get("base_row") or {}
            out[nm] = max(out.get(nm, 0), int(nn(base, "actual_hr")) or 1)
    # merged_homers only covers players on the sheet; the raw rows catch the rest.
    for r in (res.get("results") or []):
        if int(nn(r, "actual_hr")):
            nm = norm_name(r.get("name"))
            if nm:
                out[nm] = max(out.get(nm, 0), int(nn(r, "actual_hr")))
    return out


def deep_mark(name: Any, live: Dict[str, int]) -> str:
    """💥 / 💥×2 if this player has already homered today."""
    c = live.get(norm_name(name), 0)
    return "" if not c else ("💥" if c == 1 else f"💥×{c}")


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
        live_hr = homered_today()

        def _members(rec):
            return rec.get("players") or []

        def _deep(rec):
            return sum(1 for x in _members(rec)
                       if live_hr.get(norm_name(x.get("name")), 0))

        half = [p for p in pairs if _deep(p) == 1]

        c1, c2 = st.columns([3, 2])
        c1.caption(f"Pair Builder · {pair_payload.get('date', '')} · {len(pairs)} pairs")
        only_half = c2.toggle(
            f"💥 Half already in ({len(half)})", key="pair_half",
            disabled=not half,
            help="One man has homered, the other is still live.",
        ) if half else False

        shown = half if only_half else sorted(
            pairs, key=lambda x: (-_deep(x), -n(x.get("pair_score"))))

        if half and not only_half:
            st.caption(f"💥 {len(half)} pair(s) are half in — shown first.")

        for p in shown:
            nd = _deep(p)
            names = " + ".join(
                str(x.get("name", "?"))
                + (" 💥" if live_hr.get(norm_name(x.get("name")), 0) else "")
                for x in _members(p))
            label = str(p.get("type", "PAIR"))
            if nd == 1:
                label += " · HALF IN"
            elif nd >= 2:
                label += " · BOTH DEEP"
            combo_card(label, names, n(p.get("pair_score")),
                       p.get("risk"), p.get("tags"), str(p.get("reason") or ""),
                       _members(p), C["green"] if nd else C["purple"])

with tab_pools:
    p4 = pair_payload.get("pools_4man") or []
    p6 = pair_payload.get("pools_6man") or []
    if not p4 and not p6:
        st.info("No pools published yet for this slate.")
    else:
        live_hr = homered_today()

        def pool_deep(pool):
            return sum(1 for pl in (pool.get("players") or [])
                       if live_hr.get(norm_name(pl.get("name")
                                                or pl.get("player_name")), 0))

        total_deep = sum(pool_deep(x) for x in p4 + p6)
        if total_deep:
            st.caption(
                f"💥 marks a hitter who has already homered on this slate. "
                f"Pools are ordered by how many are already in."
            )

        for title, pools, accent in (("4-man pools", p4, C["cyan"]),
                                     ("6-man pools", p6, C["orange"])):
            if not pools:
                continue
            st.markdown(f"#### {title}")
            if len(pools) > 1:
                hbar([str(pool.get("name", "Pool")) for pool in pools],
                     [n(pool.get("pool_score")) for pool in pools],
                     f"{title} ranked by pool score", fmt="{:.1f}")
            for pool in sorted(pools, key=lambda x: (-pool_deep(x),
                                                     -n(x.get("pool_score")))):
                plist = pool.get("players") or []
                nd = pool_deep(pool)
                combo_card(
                    f"{len(plist)}-MAN POOL" + (f" · {nd} DEEP" if nd else ""),
                    str(pool.get("name", "Pool")),
                    n(pool.get("pool_score")), pool.get("risk"),
                    pool.get("tags"), str(pool.get("reason") or ""),
                    plist, C["green"] if nd else accent)

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

        # The live link: pair history is a season table until you cross it
        # with tonight's homers.
        live_hr = homered_today()
        half_only = st.toggle(
            "💥 One half already homered", key="ph_half", disabled=not live_hr,
            help="Pairs where exactly one hitter has gone deep tonight — the "
                 "other is the live side of a historically correlated duo.",
        ) if live_hr else False

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

            deep = [1 if live_hr.get(norm_name(nm), 0) else 0 for nm in names]
            n_deep = sum(deep)
            if half_only and n_deep != 1:
                continue
            if n_deep == 1:
                flag, still = "💥", (names[deep.index(0)] if 0 in deep else "")
            elif n_deep >= 2:
                flag, still = "✅", ""
            else:
                flag, still = ("🟢" if both_live else ""), ""

            rows.append({
                "": flag,
                "Still live": still,
                "Pair": " + ".join(
                    nm + (" 💥" if live_hr.get(norm_name(nm), 0) else "")
                    for nm in names),
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
            hdf = pd.DataFrame(rows)
            # Half-in first: at 9pm that's the only row you're looking for.
            hdf["_rank"] = hdf[""].map(
                lambda x: 0 if x == "💥" else 1 if x == "✅" else 2)
            hdf = hdf.sort_values(
                ["_rank", "Same-day HRs", "Same-game HRs"],
                ascending=[True, False, False]).drop(columns=["_rank"])
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
# Laid out to read like the nightly results report rather than a generic
# dataframe: bettable headline first, then HR capture, then who actually went
# deep with the same emoji tags the .txt uses, then the detail table.

# The pick-type emoji vocabulary from live_results_tracker's summary text.
PICK_EMOJI = {
    "TOP15": "🏆", "TOP": "🔥", "HR": "🧨",
    "HRR": "🏁", "HIT": "💠", "CONTACT": "⚾", "TB": "⚾",
}
PICK_LABEL = {
    "TOP15": "Top 15 Board", "TOP": "Top Picks", "HR": "HR Picks",
    "HRR": "HRR Picks", "HIT": "Hit Picks", "CONTACT": "Contact Picks",
    "TB": "Contact Picks",
}
PICK_ORDER = ["TOP15", "TOP", "HR", "HRR", "HIT", "CONTACT", "TB"]


# What each tier was picked to DO. Mirrors DESIGNED_OUTCOME in
# bots/live_results_tracker.py -- keep the two in step.
DESIGNED_OUTCOME = {
    "TOP15": "HR",
    "HR": "HR",
    "HIT": "1+ hit",
    "HRR": "2+ H+R+RBI",
    "CONTACT": "2+ TB or XBH",
    "TB": "2+ TB or XBH",
    "TOP": "most productive of our picks in his game",
}


def designed_hit(r: Dict[str, Any], game_rows: List[Dict[str, Any]]) -> Optional[int]:
    """Did this pick do the specific job it was picked for?

    The grader stamps `designed_hit` from tonight onward; this recomputes it
    for results published before that, so the column isn't blank on history.
    Returns None for a pick that hasn't settled.
    """
    if r.get("designed_hit") is not None:
        return int(r["designed_hit"])
    if not int(nn(r, "is_final")):
        return None
    pt = str(r.get("pick_type", "")).upper()
    tb = int(nn(r, "actual_tb"))
    hrr = int(nn(r, "hrr_total")) or (int(nn(r, "actual_hits"))
                                      + int(nn(r, "actual_runs"))
                                      + int(nn(r, "actual_rbi")))
    if pt in ("TOP15", "HR"):
        return 1 if int(nn(r, "got_hr")) else 0
    if pt == "HIT":
        return 1 if int(nn(r, "got_base_hit")) else 0
    if pt == "HRR":
        return 1 if hrr >= 2 else 0
    if pt in ("CONTACT", "TB"):
        return 1 if (tb >= 2 or int(nn(r, "got_xbh"))) else 0
    if pt == "TOP":
        peers = [g for g in game_rows
                 if g.get("player_id") != r.get("player_id")]
        if not peers:
            return 1 if tb else 0
        if hrr == 0 and tb == 0:
            return 0
        best_hrr = max(int(nn(g, "hrr_total")) or (int(nn(g, "actual_hits"))
                       + int(nn(g, "actual_runs")) + int(nn(g, "actual_rbi")))
                       for g in peers)
        best_tb = max(int(nn(g, "actual_tb")) for g in peers)
        return 1 if (hrr >= best_hrr or tb >= best_tb) else 0
    return None


def pick_badge(pick_type: Any) -> str:
    k = str(pick_type or "").upper()
    return f"{PICK_EMOJI.get(k, '•')} {PICK_LABEL.get(k, k or '—')}"


with tab_results:
    # ── ALL-TIME (backtest) ────────────────────────────────────────────────
    # Today's hit rates are one slate. The bands on a single day run to n=2 and
    # read as 100%, which is noise wearing a percentage sign. backtest_report.py
    # aggregates every graded day; this is where that lands.
    # .title() turns HR_PICKS into "Hr Picks". These are acronyms, not words.
    BT_TIER_LABELS = {
        "TOP_15_BOARD": "Top 15 Board", "TOP_PICKS": "Top Picks",
        "HR_PICKS": "HR Picks", "HRR_PICKS": "HRR Picks",
        "HIT_PICKS": "Hit Picks", "CONTACT_PICKS": "Contact Picks",
    }
    bt = load_json("public/data/current/backtest_summary.json") or {}
    bt_summary = bt.get("summary") or {}
    bt_days = bt.get("per_day") or {}

    # Today vs yesterday, not live vs final. Live/final was an artifact of how
    # the grader runs, not a question anyone actually asks -- both files are
    # always the same slate, and "final" just means the in-progress games were
    # skipped. Today reads the rolling live file (which settles as games end);
    # yesterday reads the dated graded file the nightly publish carries over.
    which = st.radio("Results view", ["Today", "Yesterday"], horizontal=True)
    if which == "Today":
        res = (load_json("public/data/current/results_live.json")
               or load_json("public/data/current/results_final.json") or {})
    else:
        y = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        res = load_json(f"public/data/current/graded_results_{y}.json") or {}
        # The grader also writes the date into the payload; if the file is
        # stale or missing entirely, say which day we looked for.
        if not res:
            res = {}
    rrows = res.get("results") or []

    if not rrows:
        if which == "Yesterday":
            y = (dt.date.today() - dt.timedelta(days=1)).isoformat()
            st.info(
                f"No graded file published for **{y}** yet. Yesterday's view "
                "reads `graded_results_<date>.json`, which the nightly grading "
                "run publishes after the last game goes final."
            )
        else:
            st.info("No results yet — grading runs hourly once games start.")
    else:
        rdf = pd.DataFrame(rrows)
        for c in ("got_hr", "got_base_hit", "got_xbh", "actual_hr", "actual_hits",
                  "actual_tb", "actual_rbi", "actual_runs", "hr_score", "rank"):
            if c in rdf.columns:
                rdf[c] = pd.to_numeric(rdf[c], errors="coerce").fillna(0)

        graded_mask = (rdf["grade"].astype(str).str.upper() != "PENDING"
                       if "grade" in rdf.columns else pd.Series(True, index=rdf.index))
        n_graded, n_pending = int(graded_mask.sum()), int((~graded_mask).sum())

        st.markdown(f"### {res.get('label', 'Results')}")
        st.caption(f"{res.get('date', '')} · {len(rdf)} picks tracked · "
                   f"{n_graded} settled, {n_pending} pending")

        if n_graded == 0:
            st.info(
                f"All {n_pending} picks are still **PENDING** — no game on this "
                "slate has started yet. Hit rates fill in as games go final; "
                "grading runs hourly from 11am Phoenix."
            )

        # ── BETTABLE RESULTS ───────────────────────────────────────────────
        # The three lines that open the .txt report, as metrics.
        st.markdown("#### Bettable results")
        bet_cols = st.columns(4)
        for i, key in enumerate(["TOP15", "TOP", "HR"]):
            sub = rdf[rdf["pick_type"].astype(str).str.upper() == key] if "pick_type" in rdf else rdf.iloc[0:0]
            n_tot = len(sub)
            n_hr = int(sub.get("got_hr", pd.Series(dtype=float)).sum()) if n_tot else 0
            bet_cols[i].metric(
                f"{PICK_EMOJI[key]} {PICK_LABEL[key]}",
                f"{n_hr}/{n_tot}" if n_tot else "—",
                delta=f"{n_hr / n_tot * 100:.1f}%" if n_tot else None,
                delta_color="off",
            )
        if "got_base_hit" in rdf.columns and n_graded:
            bet_cols[3].metric(
                "Base hit accuracy",
                f"{rdf.loc[graded_mask, 'got_base_hit'].mean() * 100:.1f}%",
                help="Full sheet, settled picks only.",
            )
        else:
            bet_cols[3].metric("Base hit accuracy", "—")

        # ── HR CAPTURE ─────────────────────────────────────────────────────
        cap = res.get("hr_capture_report") or {}
        if cap:
            st.markdown("#### HR capture")
            cc = st.columns(4)
            cc[0].metric("Slate HRs", cap.get("total_hrs_on_slate", 0))
            cc[1].metric("On the sheet", cap.get("caught_hrs_on_sheet", 0))
            cc[2].metric("Capture rate", f"{nn(cap, 'hr_capture_pct'):.1f}%",
                         help="Share of the slate's homers hit by someone the model had on the board at all.")
            cc[3].metric("Missed entirely", cap.get("missed_hrs_not_on_sheet", 0),
                         help="Homers by players who were never on the sheet.")

        # ── GOING YARD ─────────────────────────────────────────────────────
        # merged_homers already carries the emoji tags the .txt prints
        # ('🏆#7', '🔥'). The app never used them until now.
        homers = res.get("merged_homers") or []
        if homers:
            st.markdown("#### 💥 Going yard")
            hr_recs = []
            for h in homers:
                base = h.get("base_row") or {}
                hr_recs.append({
                    "Player": h.get("name", "—"),
                    "Team": h.get("team", ""),
                    "Tags": " ".join(h.get("tags") or []) or "—",
                    "Model role": txt(base, "final_hr_role") or "—",
                    "HR": int(nn(base, "actual_hr")),
                    "Ft": int(nn(h, "longest_ft")) or None,
                    "EV": round(nn(h, "max_ev_mph"), 1) or None,
                    "HR score": round(nn(base, "hr_score"), 1),
                    "Longest": round(nn(base, "longest_hr_score"), 1) or None,
                    "Line": txt(base, "outcome_text"),
                })
            hr_recs.sort(key=lambda r: (-r["HR"], -r["HR score"]))

            # ── LONGEST ────────────────────────────────────────────────
            # Distance comes off the play feed (hitData.totalDistance), so it
            # only exists once the grader has seen the at-bat. Statcast misses
            # the odd one; those homers are absent rather than shown as 0.
            with_dist = [h for h in homers if nn(h, "longest_ft")]
            if with_dist:
                top = max(with_dist, key=lambda h: nn(h, "longest_ft"))
                st.markdown("##### 📏 Longest so far")
                d1, d2, d3 = st.columns([2, 1, 1])
                d1.metric(f"{top.get('name')}", f"{int(nn(top, 'longest_ft'))} ft",
                          help="Longest home run by anyone on the board tonight.")
                d2.metric("Max EV",
                          f"{nn(top, 'max_ev_mph'):.1f} mph"
                          if nn(top, "max_ev_mph") else "—")
                d3.metric("Tracked",
                          f"{len(with_dist)}/{len(homers)}",
                          help="Homers with a Statcast distance.")
                hbar([str(h.get("name")) for h in
                      sorted(with_dist, key=lambda h: -nn(h, "longest_ft"))[:10]],
                     [int(nn(h, "longest_ft")) for h in
                      sorted(with_dist, key=lambda h: -nn(h, "longest_ft"))[:10]],
                     "Longest HRs tonight (ft)", fmt="{:.0f}", style="bar")
                st.caption(
                    "Feeds the Longest board: compare these to who the model "
                    "actually had ranked, and the distance weights become "
                    "testable instead of assumed."
                )
                st.divider()

            hy1, hy2 = st.columns([3, 2])
            with hy1:
                st.dataframe(pd.DataFrame(hr_recs), width="stretch",
                             hide_index=True, height=min(430, 36 * len(hr_recs) + 40))
            with hy2:
                # Which buckets are actually producing the homers tonight.
                tally: Dict[str, int] = {}
                for h in homers:
                    for t in (h.get("tags") or []):
                        e = str(t)[:1]
                        for k, em in PICK_EMOJI.items():
                            if em == e:
                                tally[k] = tally.get(k, 0) + 1
                                break
                tally = {k: v for k, v in tally.items() if v}
                if tally:
                    ordered = [k for k in PICK_ORDER if k in tally]
                    hbar([f"{PICK_EMOJI[k]} {PICK_LABEL[k]}" for k in ordered],
                         [tally[k] for k in ordered],
                         "HRs by pick type", fmt="{:.0f}", style="bar")
                    best = max(ordered, key=lambda k: tally[k])
                    st.caption(
                        f"Best HR-producing category tonight: "
                        f"**{PICK_EMOJI[best]} {PICK_LABEL[best]}** ({tally[best]})"
                    )

            missed = cap.get("missed_homer_entries") or []
            if missed:
                with st.expander(f"Missed HRs — not on the sheet ({len(missed)})"):
                    st.dataframe(pd.DataFrame([{
                        "Player": m.get("name"), "Team": m.get("team"),
                        "HR": m.get("hr", 1),
                    } for m in missed]), width="stretch", hide_index=True)
        elif n_graded:
            st.markdown("#### 💥 Going yard")
            st.caption("Nobody on the board has gone deep yet tonight.")

        st.divider()

        # ── CALIBRATION ────────────────────────────────────────────────────
        if "grade" in rdf.columns:
            gc = rdf["grade"].astype(str).str.upper().value_counts()
            gl, gr = st.columns([2, 3])
            with gl:
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
                st.plotly_chart(fig, width="stretch", key=_chart_key())
            with gr:
                if n_graded:
                    g = rdf.loc[graded_mask].copy()
                    # One hitter can be picked in several tiers on the same
                    # slate (TOP15 and HR and HRR, say). Each of those is a
                    # separate row, so counting them all inflates every band's
                    # n and lets one player's result land three times. Across
                    # the graded backlog that was 388 rows for 313 real
                    # player-days. Calibration is a per-player question.
                    _idcol = "player_id" if "player_id" in g.columns else "name"
                    if _idcol in g.columns:
                        g = g.drop_duplicates(subset=[_idcol], keep="first")
                    g["band"] = pd.cut(g["hr_score"], [0, 40, 55, 70, 85, 101],
                                       labels=["<40", "40-55", "55-70", "70-85", "85+"],
                                       right=False)
                    by_band = g.groupby("band", observed=True)["got_hr"].agg(["mean", "size"])
                    by_band = by_band[by_band["size"] > 0]
                    if len(by_band):
                        hbar([f"{b}  (n={int(r['size'])})" for b, r in by_band.iterrows()],
                             [float(r["mean"]) * 100 for _, r in by_band.iterrows()],
                             "HR hit rate by model score band", fmt="{:.0f}%", style="bar")
                        st.caption("If the model is working, these climb left to right.")
                else:
                    st.caption("Hit-rate-by-score-band appears once picks settle.")

        # ── DESIGNED OUTCOME ───────────────────────────────────────────────
        # "Did it homer" is the wrong question for five of the six tiers. A
        # HIT pick was picked to get a hit; grading it on HR makes a 92% tier
        # look like a 20% one.
        if "pick_type" in rdf.columns and n_graded:
            _rows = rdf.to_dict("records")
            _by_game: Dict[Any, List[Dict[str, Any]]] = {}
            for _r in _rows:
                _by_game.setdefault(_r.get("game_pk"), []).append(_r)
            for _r in _rows:
                _r["_dh"] = designed_hit(_r, _by_game.get(_r.get("game_pk"), []))

            _settled = [r for r in _rows if r["_dh"] is not None]
            if _settled:
                st.markdown("#### Did each pick do its job?")
                _hit = sum(r["_dh"] for r in _settled)
                _hr_only = sum(int(nn(r, "got_hr")) for r in _settled)
                dm = st.columns(3)
                dm[0].metric("Designed outcome hit",
                             f"{_hit}/{len(_settled)}",
                             delta=f"{_hit / len(_settled) * 100:.1f}%",
                             delta_color="off")
                dm[1].metric("If graded on HR only",
                             f"{_hr_only}/{len(_settled)}",
                             delta=f"{_hr_only / len(_settled) * 100:.1f}%",
                             delta_color="off",
                             help="What the board looks like when every tier "
                                  "is judged on home runs.")
                dm[2].metric("Tiers tracked",
                             len({str(r.get('pick_type', '')).upper()
                                  for r in _settled}))

                do_rows = []
                for kk in PICK_ORDER:
                    g = [r for r in _settled
                         if str(r.get("pick_type", "")).upper() == kk]
                    if not g:
                        continue
                    got = sum(r["_dh"] for r in g)
                    do_rows.append({
                        "": PICK_EMOJI[kk],
                        "Pick type": PICK_LABEL[kk],
                        "Needs": DESIGNED_OUTCOME.get(kk, "—"),
                        "Hit": f"{got}/{len(g)}",
                        "Rate": f"{got / len(g) * 100:.1f}%",
                        "HR%": f"{sum(int(nn(r, 'got_hr')) for r in g) / len(g) * 100:.1f}%",
                    })
                if do_rows:
                    st.dataframe(pd.DataFrame(do_rows), width="stretch",
                                 hide_index=True)
                    hbar([f"{r['']} {r['Pick type']}" for r in do_rows],
                         [float(r["Rate"].rstrip("%")) for r in do_rows],
                         "Designed-outcome hit rate by tier", fmt="{:.0f}%", style="bar")
                # Model roles that never became a game-sheet pick. Without
                # this they vanish from grading entirely -- the model had an
                # opinion and nothing recorded whether it was right.
                _roles = {}
                for _r in _settled:
                    _rl = txt(_r, "final_hr_role")
                    if _rl:
                        _roles.setdefault(_rl, []).append(_r)
                if _roles:
                    st.dataframe(pd.DataFrame([{
                        "Model role": _rl,
                        "N": len(_g),
                        "HR": sum(int(nn(x, "got_hr")) for x in _g),
                        "HR%": f"{sum(int(nn(x, 'got_hr')) for x in _g) / len(_g) * 100:.1f}%",
                        "Job done%": f"{sum(x['_dh'] for x in _g) / len(_g) * 100:.1f}%",
                    } for _rl, _g in sorted(
                        _roles.items(), key=lambda kv: -len(kv[1]))]),
                        width="stretch", hide_index=True)
                    st.caption(
                        "Model role is the bot's conviction tier — separate "
                        "from which game sheet he landed on."
                    )

                st.caption(
                    "**TOP** is relative — it's picked as the best overall play "
                    "in its game, so it only counts if it out-produced the "
                    "other picks from that same game on HRR or total bases. "
                    "We only see our own picks, so that means best *of the ones "
                    "we tracked*."
                )
                st.divider()

        if "pick_type" in rdf.columns and n_graded:
            st.markdown("#### Player type performance")
            pt_rows = []
            for k in PICK_ORDER:
                sub = rdf[(rdf["pick_type"].astype(str).str.upper() == k) & graded_mask]
                if sub.empty:
                    continue
                pt_rows.append({
                    "": PICK_EMOJI[k],
                    "Pick type": PICK_LABEL[k],
                    "N": len(sub),
                    "HR": int(sub["got_hr"].sum()),
                    "HR %": f"{sub['got_hr'].mean() * 100:.1f}%",
                    "1+ Hit %": f"{sub['got_base_hit'].mean() * 100:.1f}%"
                                if "got_base_hit" in sub else "—",
                    "XBH %": f"{sub['got_xbh'].mean() * 100:.1f}%"
                             if "got_xbh" in sub else "—",
                })
            if pt_rows:
                st.dataframe(pd.DataFrame(pt_rows), width="stretch", hide_index=True)

        st.divider()

        # ── ALL PICKS ──────────────────────────────────────────────────────
        st.markdown("#### Every pick")
        sort_key = rdf["grade"].astype(str).str.upper().eq("PENDING").astype(int)
        rdf = rdf.assign(_pending=sort_key).sort_values(
            ["_pending", "actual_hr", "actual_tb"],
            ascending=[True, False, False],
        ).drop(columns=["_pending"])

        f1, f2 = st.columns([3, 2])
        show_only = f1.radio(
            "Show", ["All", "Settled only", "Hit a HR", "Pending"],
            horizontal=True, key="resfilter",
        )
        type_opts = ["All types"] + [PICK_LABEL[k] for k in PICK_ORDER
                                     if k in set(rdf["pick_type"].astype(str).str.upper())]
        type_pick = f2.selectbox("Pick type", type_opts, key="restype")

        v = rdf
        if show_only == "Settled only":
            v = v[v["grade"].astype(str).str.upper() != "PENDING"]
        elif show_only == "Hit a HR":
            v = v[v.get("got_hr", 0) > 0]
        elif show_only == "Pending":
            v = v[v["grade"].astype(str).str.upper() == "PENDING"]
        if type_pick != "All types":
            keys = [k for k in PICK_ORDER if PICK_LABEL[k] == type_pick]
            v = v[v["pick_type"].astype(str).str.upper().isin(keys)]

        if v.empty:
            st.caption("Nothing matches that filter yet.")
        else:
            disp = pd.DataFrame([{
                "": PICK_EMOJI.get(str(r.get("pick_type", "")).upper(), "•"),
                "Player": r.get("name"), "Team": r.get("team"),
                "Pick": PICK_LABEL.get(str(r.get("pick_type", "")).upper(),
                                       r.get("pick_type")),
                # A hitter can hold a per-game role AND a slate-wide Top 15
                # rank at the same time -- they're selected by different
                # passes. Showing every role he holds stops it looking like
                # the pick "changed" after he homered.
                "All roles": " + ".join(sorted({
                    PICK_LABEL.get(str(o.get("pick_type", "")).upper(),
                                   str(o.get("pick_type")))
                    for o in rdf.to_dict("records")
                    if o.get("player_id") == r.get("player_id")})),
                # The model's own conviction tier, independent of whether he
                # was picked for a game sheet. A 💎 HR Bet who never made a
                # sheet still tells you the model liked him.
                "Model role": txt(r, "final_hr_role") or "—",
                "Rank": int(nn(r, "rank")) or None,
                "HR score": round(nn(r, "hr_score"), 1),
                "HR": int(nn(r, "actual_hr")),
                "H": int(nn(r, "actual_hits")),
                "TB": int(nn(r, "actual_tb")),
                "RBI": int(nn(r, "actual_rbi")),
                "R": int(nn(r, "actual_runs")),
                "Grade": r.get("grade"),
                "Job": ({1: "✓", 0: "✗"}.get(
                    designed_hit(r, [g for g in rdf.to_dict("records")
                                     if g.get("game_pk") == r.get("game_pk")]), "")),
                "Line": r.get("outcome_text"),
            } for r in v.to_dict("records")])
            st.dataframe(disp, width="stretch", hide_index=True, height=480)
            st.download_button(
                "⬇️ CSV", v.to_csv(index=False).encode(),
                f"mlb_results_{res.get('date', which.lower())}.csv",
                "text/csv", key="rescsv",
            )

    # ── ALL-TIME TIER TABLE ────────────────────────────────────────────────
    st.divider()
    if bt_summary:
        st.markdown("#### All-time by tier")
        dates = sorted(d for d in bt_days if isinstance(d, str))
        span = f" · {dates[0]} to {dates[-1]}" if dates else ""
        st.caption(f"{len(bt_days)} graded day(s){span}")

        bt_rows = []
        for tier, d in bt_summary.items():
            if not isinstance(d, dict):
                continue
            rate = d.get("hr_rate_pct")
            pool = d.get("total_pool_size")
            # backtest_report aggregates these per tier from the graded
            # per-pick JSON, so they cover past slates too rather than only
            # days graded after the report started printing all six.
            # Pooled is the headline (every pick counts equally); the average
            # of daily rates is shown in its own table below.
            met = d.get("pooled_metrics") or d.get("avg_metrics") or {}

            def _m(*names):
                for nm in names:
                    if met.get(nm) is not None:
                        return f"{met[nm]}%"
                return "—"

            bt_rows.append({
                "Tier": BT_TIER_LABELS.get(tier, tier.replace("_", " ").title()),
                "HRs": d.get("total_hr_count", 0),
                "Pool": pool if pool else "—",
                "HR rate": f"{rate}%" if rate is not None else "—",
                "Fair odds": fair_american(rate) if rate else "—",
                "Did its job": _m("Did its job"),
                "HR": _m("HR"),
                "1+ Hit": _m("1+ Hit"),
                "XBH": _m("XBH"),
                "2+ TB": _m("2+ TB"),
                "1+ HRR": _m("1+ HRR"),
                "2+ HRR": _m("2+ HRR"),
                "3+ HRR": _m("3+ HRR"),
                "Days": d.get("days_seen", 0),
            })
        bt_rows.sort(key=lambda r: (r["HR rate"] == "—",
                                    -(float(r["HR rate"].rstrip("%"))
                                      if r["HR rate"] != "—" else 0)))
        st.dataframe(pd.DataFrame(bt_rows), width="stretch", hide_index=True)

        acc = bt.get("overall_base_hit_accuracy")
        if acc is not None:
            st.caption(f"Base-hit accuracy across all graded days: **{acc}%**")
        st.caption(
            "Fair odds are the break-even American price implied by that hit "
            "rate. Anything priced longer than fair is where the edge is — "
            "the board doesn't know prices yet, so that comparison is manual."
        )
    else:
        st.caption(
            "All-time tier performance appears here once the nightly backtest "
            "publishes `backtest_summary.json`. It aggregates every graded day, "
            "so the rates above stop being single-slate noise."
        )

# ── PLAYER DETAIL ───────────────────────────────────────────────────────────
# Modelled on the old PlayerModal: identity header, pill row, then sub-tabs
# (Overview / EV Log / Pitch / Spray) instead of one long scroll.
with tab_player:
    if not view:
        st.info("No players match these filters.")
    else:
        pc1, pc2 = st.columns([3, 2])
        opts = sorted(view, key=hr_score, reverse=True)
        labels = [f"{name_of(p)} ({team_of(p)}) — HR {hr_score(p):.0f}" for p in opts]
        idx = pc1.selectbox("Player", range(len(opts)),
                            format_func=lambda i: labels[i], key="pl_pick")
        p = opts[idx]
        # Head-to-head. Reading one player's numbers tells you nothing without
        # something to read them against.
        cmp_names = ["— slate median —"] + [name_of(x) for x in opts
                                            if name_of(x) != name_of(p)]
        cmp_pick = pc2.selectbox("Compare with", cmp_names, key="pl_cmp")
        cmp_p = next((x for x in opts if name_of(x) == cmp_pick), None)

        player_detail(p, kp="pl", cmp_p=cmp_p)

def watch_badges(p: Dict[str, Any]) -> str:
    """The emoji run shown on a watch card.

    Lives on its own so the card and the CSV export read from one place --
    they were going to drift the first time a badge rule changed.
    """
    hrw = HRW_MAP.get(txt(p, "hrw_zone"))
    badges = ""
    if hrw:
        badges += hrw[0]
    if p.get("weak_spot_flag"):
        badges += "⭐"
    if is_aligned(p):
        badges += "🧩"
    if nn(p, "pitch_type_match_score") >= 80:
        badges += "🎯"
    return badges


def watch_card_html(p: Dict[str, Any]) -> str:
    """Compact grid card: badges, score + grade top-right, pills, stat line."""
    rc = role_config(p)
    role_label, role_color = rc if rc else (tier_role(p), tier_color(tier_role(p)))
    score = nn(p, "top_board_score_v2") or hr_score(p)
    badges = watch_badges(p)

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
    # ── CROSS-REFERENCE ────────────────────────────────────────────────────
    # Paste a list from anywhere -- someone else's picks, a DFS slate, a
    # group chat -- and see what the model says about those exact names.
    with st.expander("📋 Cross-reference a list of players", expanded=not st.session_state.watch):
        st.caption(
            "Paste names one per line or comma-separated. Ranking numbers, "
            "bullets, odds and team codes are stripped automatically."
        )
        blob = st.text_area(
            "Names", height=130, key="xref_blob",
            placeholder="Aaron Judge\nShohei Ohtani +410\n3. Kyle Schwarber (PHI)",
        )
        names = parse_name_list(blob)
        if names:
            # Match against the whole slate, not `view` -- sidebar filters
            # shouldn't silently hide someone you explicitly asked about.
            hits, misses, ambiguous = match_players(names, players)
            st.caption(
                f"{len(names)} name(s) read · {len(hits)} matched · "
                f"{len(ambiguous)} ambiguous · {len(misses)} not found"
            )

            if hits:
                xdf = pd.DataFrame([{
                    "": "✅" if p.get("lineup_confirmed") else "◻︎",
                    "Player": name_of(p),
                    "As pasted": raw if norm_name(raw) != norm_name(name_of(p)) else "",
                    "Match": how,
                    "Team": team_of(p), "Opp": opp_of(p),
                    "Spot": p.get("lineup_spot"),
                    "HR": round(hr_score(p), 1),
                    "Cross": round(cross_board(p), 1),
                    "HRR": round(prod_score(p), 1),
                    "Hit": round(hit_score(p), 1),
                    "TB": round(tb_score(p), 1),
                    "DC": round(nn(p, "damage_conversion_score"), 1),
                    "Grade": grade_for(p, "hr"),
                    "Role": tier_role(p),
                    "Pitcher": txt(p, "pitcher_name"),
                    "P HR/9": round(nn(p, "pitcher_hr9"), 2),
                    "Park HR": round(nn(p, "park_hr_factor", default=1.0), 2),
                } for raw, p, how in hits])
                st.dataframe(xdf, width="stretch", hide_index=True,
                             height=min(460, 36 * len(xdf) + 42))

                x1, x2, x3 = st.columns(3)
                x1.metric("Best HR score", f"{max(hr_score(p) for _, p, _ in hits):.1f}")
                x2.metric("Median HR score",
                          f"{median([hr_score(p) for _, p, _ in hits]):.1f}",
                          help="Slate median is "
                               f"{median([hr_score(p) for p in players]):.1f}.")
                x3.metric("Confirmed",
                          f"{sum(1 for _, p, _ in hits if p.get('lineup_confirmed'))}/{len(hits)}")

                open_player_picker([p for _, p, _ in hits], "xref")

                b1, b2 = st.columns([1, 3])
                if b1.button("⭐ Add all to watchlist", width="stretch", key="xref_watch"):
                    added = 0
                    for _, p, _ in hits:
                        if name_of(p) not in st.session_state.watch:
                            st.session_state.watch.append(name_of(p))
                            added += 1
                    persist_watch()
                    st.success(f"Added {added} player(s).")
                    st.rerun()
                b2.download_button(
                    "⬇️ CSV", xdf.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"mlb_{slate}_crossref.csv", mime="text/csv",
                    width="stretch", key="xref_csv",
                )

                if any(how == "fuzzy" for _, _, how in hits):
                    st.caption(
                        "⚠️ Rows marked **fuzzy** were matched on spelling "
                        "similarity — check those before trusting them."
                    )

            if ambiguous:
                st.warning(
                    "**Ambiguous — more than one player matches:**\n\n"
                    + "\n".join(f"- `{raw}` → {', '.join(c)}" for raw, c in ambiguous)
                    + "\n\nAdd a first name to disambiguate."
                )

            if misses:
                st.info(
                    "**Not on this slate:** " + ", ".join(misses) +
                    "  \nUsually means they're not in a confirmed lineup, not "
                    "playing today, or the spelling is too far off to match."
                )

    if not st.session_state.watch:
        st.info(
            "No players on your watchlist yet. Add them with the ⭐ Watch "
            "button on the Player tab, or from any player pop-up. Your list "
            "is saved in the page URL, so bookmarking or sharing that link "
            "carries the same players with it."
        )
    else:
        watched_all = [p for p in players if name_of(p) in st.session_state.watch]
        # Names on the list that aren't on tonight's slate — otherwise they
        # silently vanish and you assume you never added them.
        off_slate = [w for w in st.session_state.watch
                     if not any(name_of(p) == w for p in players)]

        live_hr = homered_today()
        SORTS = {
            "HR score": hr_score, "Cross": cross_board, "Longest": longest_score,
            "HRR": prod_score, "Hit": hit_score,
            "DC": lambda x: nn(x, "damage_conversion_score"),
            # game_start returns an ISO string ("9999" when unknown), so
            # a plain string sort is chronological and puts TBD last.
            "First pitch": lambda x: game_start([x]),
        }
        hdr_l, hdr_s, hdr_x, hdr_r = st.columns([2, 1, 1, 1])
        hdr_l.markdown(f"### Watchlist\n{len(watched_all)} on the slate"
                       + (f" · {len(off_slate)} not playing" if off_slate else ""))
        wl_sort = hdr_s.selectbox("Sort by", list(SORTS), key="wl_sort")
        watched = sorted(watched_all, key=SORTS[wl_sort], reverse=True)
        # Doubleheaders put the same hitter on the slate twice, under two
        # game_pks. Two identical cards is confusing and, because the card key
        # was built from his name, it also collided. Keep his best row.
        _seen: set = set()
        _dedup = []
        for _p in watched:
            _k = norm_name(name_of(_p))
            if _k in _seen:
                continue
            _seen.add(_k)
            _dedup.append(_p)
        _dh = len(watched) - len(_dedup)
        watched = _dedup

        # How the list is actually doing, as a group.
        if watched:
            wm = st.columns(4)
            wm[0].metric("Best HR", f"{max(hr_score(x) for x in watched):.1f}")
            wm[1].metric("Median HR", f"{med([hr_score(x) for x in watched]):.1f}",
                         delta=f"{med([hr_score(x) for x in watched]) - med([hr_score(x) for x in players]):+.1f} vs slate",
                         delta_color="normal")
            wm[2].metric("Confirmed",
                         f"{sum(1 for x in watched if x.get('lineup_confirmed'))}/{len(watched)}")
            _deep = sum(1 for x in watched if live_hr.get(norm_name(name_of(x)), 0))
            wm[3].metric("💥 Already deep", _deep or "—",
                         help="Watchlist hitters who have homered tonight.")

            hbar([name_of(x) + (" 💥" if live_hr.get(norm_name(name_of(x)), 0) else "")
                  for x in watched],
                 [round(SORTS[wl_sort](x), 1) for x in watched],
                 f"Watchlist by {wl_sort}",
                 ref=float(med([SORTS[wl_sort](x) for x in players])),
                 ref_label="slate median")

        if watched:
            open_player_picker(watched, "watchlist")
        if off_slate:
            st.caption("Not on this slate: " + ", ".join(off_slate))

        # Name / HR / emojis, in board order. Two shapes because they get used
        # two ways: CSV for a spreadsheet, plain text for pasting into a post.
        wl_rows = [{
            "Player": name_of(p),
            "HR": round(hr_score(p), 1),
            "Emojis": watch_badges(p)
                      + (" 💥" if live_hr.get(norm_name(name_of(p)), 0) else ""),
            "Role": tier_role(p),
            "Team": team_of(p),
            "Opp": opp_of(p),
            "Cross": round(cross_board(p), 1),
            "Longest": round(longest_score(p), 1),
            "DC": round(nn(p, "damage_conversion_score"), 1),
            "First pitch": local_time([p]),
            "Pitcher": txt(p, "pitcher_name"),
            "P HR/9": round(nn(p, "pitcher_hr9"), 2),
        } for p in watched]
        wl_text = "\n".join(
            f"{r['Emojis']} {r['Player']} — {r['HR']}".strip() for r in wl_rows)

        hdr_x.download_button(
            "⬇️ CSV",
            pd.DataFrame(wl_rows).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"mlb_{slate}_watchlist.csv",
            mime="text/csv",
            width="stretch",
            help="Name, HR score and badges for everyone on the list",
        )
        if hdr_r.button("Clear All", width="stretch"):
            st.session_state.watch = []
            persist_watch()
            st.rerun()

        with st.expander("📋 Copy as text"):
            st.code(wl_text, language=None)

        # Four across, like the old grid.
        if _dh:
            st.caption(f"{_dh} hitter(s) are on a doubleheader — showing their "
                       "stronger game.")

        per_row = 4
        for i in range(0, len(watched), per_row):
            cols = st.columns(per_row)
            for j, (col, p) in enumerate(zip(cols, watched[i:i + per_row])):
                with col:
                    st.markdown(watch_card_html(p), unsafe_allow_html=True)
                    # Keyed on position, not on the player -- a name or an id
                    # can repeat, an index in this list cannot.
                    if st.button("★ Remove", key=f"unwatch_{i + j}",
                                 width="stretch",
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
