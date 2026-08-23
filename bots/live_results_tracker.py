#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas



# STREAMLIT MIGRATION (2026-07-25): the Next.js `app/` directory used to be
# the marker for "this is the dashboard repo". The site is Streamlit now and
# app/ is gone, so every sync would have silently bailed out with
# "Website repo not found". Accept either marker.
def _is_dashboard_repo(p) -> bool:
    from pathlib import Path as _P
    p = _P(p)
    return (p / "streamlit_app.py").exists() or (p / "app").exists()


MLB_BASE = "https://statsapi.mlb.com/api/v1.1/game"
TIMEOUT = 30
ROOT_DIR = Path(__file__).resolve().parent
OUT_DIR = ROOT_DIR / "outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
TODAY = dt.date.today()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", "--"):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "--"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def pct(items: List[Dict[str, Any]], key: str) -> float:
    if not items:
        return 0.0
    return round(100 * sum(int(x.get(key, 0)) for x in items) / len(items), 1)


def load_rows(date_str: str) -> List[Dict[str, Any]]:
    """Load the breakdown JSON for a date, even if today/tomorrow bots use different prefixes."""
    candidates = [
        OUT_DIR / f"mlb_breakdown_today_{date_str}.json",
        OUT_DIR / f"mlb_breakdown_tomorrow_{date_str}.json",
        OUT_DIR / f"mlb_daily_breakdown_final_{date_str}.json",
        OUT_DIR / f"mlb_today_breakdown_{date_str}.json",
        OUT_DIR / f"mlb_today_slate_breakdown_{date_str}.json",
        OUT_DIR / f"mlb_tomorrow_early_breakdown_{date_str}.json",
        OUT_DIR / f"mlb_tomorrow_breakdown_{date_str}.json",
        OUT_DIR / f"tomorrow_early_breakdown_{date_str}.json",
    ]

    for path in candidates:
        if path.exists():
            print(f"Loaded picks file: {path}")
            return json.loads(path.read_text(encoding="utf-8"))

    matches = sorted(
        [
            p for p in OUT_DIR.glob(f"*{date_str}*.json")
            if "graded_results" not in p.name and "live_graded_results" not in p.name
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if matches:
        path = matches[0]
        print(f"Loaded picks file: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    tried = "\n".join(f"- {c}" for c in candidates)
    raise FileNotFoundError(
        f"Could not find a breakdown JSON for {date_str}.\n"
        f"Tried:\n{tried}\n"
        f"Also searched outputs/*{date_str}*.json"
    )


# ── ROADMAP STEP 9B TASK 1 (2026-08-22): grade against the LOCKED run's
# features, not the currently-published slate ────────────────────────────
#
# The defect, confirmed at the code level in
# claude/moonshot-sol-verdict-graded-archive-leak.md: load_rows() above
# reads the CURRENTLY PUBLISHED slate (fetched by fetch_picks_for_grading.py
# from {label}_slim.json), and today.yml keeps rebuilding that slate into
# the evening. A rebuild after first pitch calls the live MLB StatsAPI
# gamelog/season-stats endpoints fresh -- with no as-of-date parameter -- so
# fields like last5_hr and games_since_last_hr come back reflecting that
# night's already-played games, not the pre-game state a predictive feature
# needs to be.
#
# pick_lock.py already locks one run_id per game_pk into
# prediction_of_record (por_log_<date>.jsonl, fetched best-effort by
# fetch_picks_for_grading.py) the moment that game's first pitch passes --
# almost always an earlier run than whatever is published by the time
# grading runs. That run's own feature values are in
# prediction_log_<run_id>.jsonl (also fetched best-effort, one file per
# distinct locked run_id). apply_locked_features() joins the two and
# overlays what the locked run captured onto each row, stamping
# feature_snapshot so nothing downstream mistakes an unjoined row for a
# trustworthy one.
#
# SCOPE, stated plainly rather than implied: prediction_log's schema
# (bots/mlb_dashboard.py build_prediction_log_lines) is deliberately
# slimmer than the full graded-row shape trim_row() keeps -- it carries
# `scores` and `components` (task 3's additions, now including
# games_since_last_hr from this task), not every SLOT_FIELDS key. This fix
# overlays every field prediction_log currently captures -- which covers
# the two headline leaking fields Claim A's evidence named,
# recent_form_last5_hr and games_since_last_hr, plus hr_score/config_hash
# provenance and the rest of task 3's additions. A SLOT_FIELDS key with no
# entry in _LOCKED_SCORE_MAP/_LOCKED_COMPONENT_MAP below is not yet
# captured at lock time and keeps whatever the currently-published slate
# has -- still exposed to the leak -- until it's added to
# build_prediction_log_lines's components dict, the same additive,
# no-score-changing pattern task 3 and this task both use. Worth doing as a
# fast follow, not bundled here to keep this change reviewable.

# flat SLOT_FIELDS key -> key inside a locked prediction_log row's `scores`
# dict (bots/mlb_dashboard.py build_prediction_log_lines's "Moonshot scores"
# block).
_LOCKED_SCORE_MAP: Dict[str, str] = {
    "hr_score": "hr",
    "overall_score": "overall",
    "hit_score": "hit",
    "hrr_score": "hrr",
    "contact_score": "contact",
    "hrw_score": "hrw",
    "multi_hit_score": "multi_hit",
}

# flat SLOT_FIELDS key -> key inside a locked prediction_log row's
# `components` dict. Only fields with a genuine 1:1 match are listed --
# e.g. components' recent_form_last10_hr/l20pa_hr/l20pa_xbh have no
# SLOT_FIELDS counterpart at all (SLOT_FIELDS only ever kept last5_hr/
# last5_xbh/last10_xbh), so they are not (and should not be) mapped here.
_LOCKED_COMPONENT_MAP: Dict[str, str] = {
    "last5_hr": "recent_form_last5_hr",
    "last5_xbh": "recent_form_last5_xbh",
    "games_since_last_hr": "games_since_last_hr",
}


def load_locked_run_ids(date_str: str, fetch_dir: Path) -> Dict[int, str]:
    """{game_pk: run_id} from a locally-fetched por_log_<date_str>.jsonl
    (see fetch_picks_for_grading.py). Missing/unreadable file -> {} -- every
    row then grades as feature_snapshot="unavailable", never as an error."""
    out: Dict[int, str] = {}
    path = fetch_dir / f"por_log_{date_str}.jsonl"
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            gp, rid = obj.get("game_pk"), obj.get("run_id")
            if gp is None or not rid:
                continue
            try:
                out[int(gp)] = str(rid)
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return out


def load_locked_prediction_rows(run_ids: Iterable[str], fetch_dir: Path,
                                run_id_by_game: Optional[Dict[int, str]] = None) -> Dict[Tuple[int, int], Dict[str, Any]]:
    """{(game_pk, player_id): prediction_log row} for every distinct locked
    run_id whose prediction_log_<run_id>.jsonl was fetched locally. A
    run_id with no local file (aged out of prediction_log's retention
    window, or the fetch failed) simply contributes no matches for its
    games -- those rows grade as feature_snapshot="unavailable" downstream,
    never silently treated as locked.

    CROSS-GAME GUARD (2026-08-22, 9b-fix defect B). A prediction_log file
    carries EVERY rated player on the slate, not just the players in the
    games that locked to that run. Without the guard below, one late-locked
    game's run sprayed its features across every OTHER game on the slate --
    games already Final when that run was generated -- and stamped them all
    feature_snapshot="locked". Observed in production on the very first
    night 9b ran (2026-08-22): por_log held one locked game (823422, locked
    22:18Z, locked_late=True) and graded_results carried 11 rows across two
    DIFFERENT, already-Final games (823509, 823743), every one stamped
    "locked" with that run's post-game features. That is the leak this
    module exists to close, wearing the clean stamp -- worse than the honest
    "unavailable" path, which at least tells downstream analysis which rows
    to exclude. When run_id_by_game is provided, a row only joins if its own
    game_pk actually locked to the run_id whose file it came from."""
    out: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for rid in run_ids:
        path = fetch_dir / f"prediction_log_{rid}.jsonl"
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            # The header line (run_id/generated_at/git_sha/config_hashes/
            # model_versions/schema_version/env) has no player_id -- it is
            # skipped the same way any other malformed/irrelevant line
            # would be, no special-casing needed.
            pid, gp = obj.get("player_id"), obj.get("game_pk")
            if pid is None or gp is None:
                continue
            try:
                gp_i, pid_i = int(gp), int(pid)
            except (TypeError, ValueError):
                continue
            # Defect B guard: this row's game must have LOCKED to this
            # run -- a slate-wide prediction_log also carries every other
            # game's players, and those rows must not join.
            if run_id_by_game is not None and run_id_by_game.get(gp_i) != rid:
                continue
            out[(gp_i, pid_i)] = obj
    return out


def apply_locked_features(
    rows: List[Dict[str, Any]], date_str: str, fetch_dir: Path,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Overlay each row's leak-prone fields with the LOCKED run's values.

    Returns (rows, dropped_locked_rows):
      - rows: same length/order/membership as the input -- the currently-
        published slate, unchanged in shape, each row stamped
        feature_snapshot="locked" (fields overlaid from the join) or
        "unavailable" (no locked match found; existing values, which may
        still be post-game, are left as-is -- never silently upgraded to
        "locked" without an actual match).
      - dropped_locked_rows: minimal synthetic rows for (game_pk,
        player_id) pairs present in the locked join but ABSENT from `rows`
        entirely -- a player later dropped from a rebuilt slate. This is
        the SECOND defect roadmap step 9b/23 name in
        build_hr_capture_report (rows = load_rows(date) undercounts
        hr_capture_pct for exactly this reason). Deliberately NOT merged
        into the returned `rows` list itself: these synthetic rows carry
        only what the locked run captured, not the full slate-row shape
        build_tracking_slots' pick-selection logic expects, so they are
        handed back separately for callers that only need slate
        MEMBERSHIP (hr_capture_report) rather than a pickable row.
    """
    run_id_by_game = load_locked_run_ids(date_str, fetch_dir)
    locked_by_key = load_locked_prediction_rows(
        set(run_id_by_game.values()), fetch_dir, run_id_by_game)

    def _overlay(dst: Dict[str, Any], locked: Dict[str, Any], gp: int) -> None:
        scores = locked.get("scores") or {}
        for flat_key, score_key in _LOCKED_SCORE_MAP.items():
            if score_key in scores:
                dst[flat_key] = scores[score_key]
        components = locked.get("components") or {}
        for flat_key, comp_key in _LOCKED_COMPONENT_MAP.items():
            if comp_key in components:
                dst[flat_key] = components[comp_key]
        if locked.get("config_hash"):
            dst["config_hash"] = locked["config_hash"]
        dst["feature_snapshot"] = "locked"
        dst["locked_run_id"] = locked.get("run_id") or run_id_by_game.get(gp)

    seen_keys: set = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            gp = int(row.get("game_pk"))
            pid = int(row.get("player_id"))
        except (TypeError, ValueError):
            out.append(row)
            continue
        seen_keys.add((gp, pid))
        locked = locked_by_key.get((gp, pid))
        if locked is None:
            row = dict(row)
            row["feature_snapshot"] = "unavailable"
            out.append(row)
            continue
        row = dict(row)
        _overlay(row, locked, gp)
        out.append(row)

    dropped: List[Dict[str, Any]] = []
    for (gp, pid), locked in locked_by_key.items():
        if (gp, pid) in seen_keys:
            continue
        drop_row: Dict[str, Any] = {
            "player_id": pid,
            "game_pk": gp,
            "name": locked.get("player"),
            "team": locked.get("team"),
        }
        _overlay(drop_row, locked, gp)
        dropped.append(drop_row)

    return out, dropped


def write_json_and_aliases(main_path: Path, payload: Any, alias_paths: Iterable[Path]) -> None:
    """Write canonical tracker JSON plus compatibility aliases."""
    text = json.dumps(payload, indent=2)
    main_path.write_text(text, encoding="utf-8")
    for alias in alias_paths:
        if alias == main_path:
            continue
        alias.write_text(text, encoding="utf-8")


def write_text_and_aliases(main_path: Path, text: str, alias_paths: Iterable[Path]) -> None:
    """Write canonical tracker TXT plus compatibility aliases."""
    main_path.write_text(text, encoding="utf-8")
    for alias in alias_paths:
        if alias == main_path:
            continue
        alias.write_text(text, encoding="utf-8")


# ── WEBSITE REPO SYNC FIX V3 ─────────────────────────────────────────────────

def _find_dashboard_repo() -> Path:
    """
    Find the MLB-HR-DASHBOARD website repo.

    Priority:
      1. GitHub Actions environment — the repo IS the workspace
      2. MLB_DASHBOARD_DIR env var, if set and valid
      3. Search common Mac locations
      4. Recursive scan of standard dirs
      5. Fallback to old hard-coded path
    """
    # 1. Running inside GitHub Actions? The repo IS where we are.
    # GITHUB_WORKSPACE is set by Actions and points at the checked-out repo.
    gh_workspace = os.environ.get("GITHUB_WORKSPACE", "").strip()
    if gh_workspace:
        p = Path(gh_workspace)
        if p.exists() and _is_dashboard_repo(p):
            return p
    # Fallback for Actions: walk up from this script's location
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        if _is_dashboard_repo(parent) and (parent / "public").exists():
            return parent

    # 2. Honor env override
    env_path = os.environ.get("MLB_DASHBOARD_DIR", "").strip()
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists() and _is_dashboard_repo(p):
            return p

    # 3. Search common Mac locations
    home = Path.home()
    candidates = [
        home / "Documents" / "GitHub" / "MLB-HR-DASHBOARD",
        home / "Documents" / "GitHub" / "MLB-HR-Dashboard",
        home / "Documents" / "GitHub" / "mlb-hr-dashboard",
        home / "Documents" / "GitHub" / "MLB HR MODEL",
        home / "Documents" / "GitHub" / "MLB-HR-MODEL",
        home / "Downloads" / "MLB-HR-DASHBOARD",
        home / "Downloads" / "mlb_hr_bot_starter",
        home / "Desktop" / "MLB-HR-DASHBOARD",
        home / "Projects" / "MLB-HR-DASHBOARD",
        Path("/Volumes/DONX/USERS/Kingdondondon/Documents/GitHub/MLB-HR-DASHBOARD"),
        Path("/Volumes/DONX/USERS/Kingdondondon/Documents/GitHub/MLB HR MODEL"),
        Path("/Volumes/DONX/USERS/Kingdondondon/Downloads/MLB-HR-DASHBOARD"),
        Path("/Volumes/DONX/USERS/Kingdondondon/Downloads/mlb_hr_bot_starter"),
    ]
    for c in candidates:
        if c.exists() and _is_dashboard_repo(c):
            return c

    # 4. Recursive scan
    for top in [home / "Documents" / "GitHub", home / "Documents",
                home / "Downloads", home / "Desktop"]:
        if not top.exists():
            continue
        try:
            for sub in top.iterdir():
                if not sub.is_dir():
                    continue
                name = sub.name.lower()
                if ("mlb" in name and "dashboard" in name) or name == "mlb_hr_bot_starter":
                    if _is_dashboard_repo(sub):
                        return sub
        except (PermissionError, OSError):
            continue

    # 5. Final fallback
    return Path("/Volumes/DONX/USERS/Kingdondondon/Documents/GitHub/MLB-HR-DASHBOARD")


DASHBOARD_REPO = _find_dashboard_repo()

def _sync_copy(src: Path, dest: Path) -> bool:
    try:
        if not src.exists():
            print(f"⚠️ Website sync missing source: {src}")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"📁 Website copy: {src.name} → {dest}")
        return True
    except Exception as exc:
        print(f"⚠️ Website copy failed: {src} → {dest}: {exc}")
        return False

def _sync_read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default

def _discord_urls() -> list:
    """DISCORD_WEBHOOK accepts MULTIPLE webhook URLs separated by commas,
    whitespace or newlines (2026-08-08 — second server added). One secret,
    every room gets every post; a failure on one URL never blocks the rest."""
    raw = os.environ.get("DISCORD_WEBHOOK", "")
    return [u.strip() for u in raw.replace(",", "\n").split() if u.strip().startswith("http")]


def _post_discord_payload(payload: dict) -> None:
    for url in _discord_urls():
        try:
            import urllib.request
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "moonshot-bot"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            print(f"discord post failed: {exc}")


_HL_CACHE: dict = {}

def _hr_highlight_url(game_pk, batter_name: str) -> str:
    """Best-effort link to the HR highlight clip for this batter in this
    game, from MLB's game content feed. Any miss returns '' and the digest
    line simply ships without a video — never worth failing a post over."""
    try:
        if not game_pk or not batter_name:
            return ""
        if game_pk not in _HL_CACHE:
            import urllib.request
            u = f"https://statsapi.mlb.com/api/v1/game/{game_pk}/content"
            with urllib.request.urlopen(u, timeout=10) as r:
                _HL_CACHE[game_pk] = json.loads(r.read().decode("utf-8"))
        items = ((((_HL_CACHE[game_pk] or {}).get("highlights") or {}).get("highlights") or {}).get("items") or [])
        last = batter_name.split()[-1].lower()
        for it in items:
            text = f"{it.get('headline','')} {it.get('title','')} {it.get('description','')}".lower()
            if last in text and ("homer" in text or "home run" in text or "hr" in text.split()):
                pbs = it.get("playbacks") or []
                best = ""
                for pb in pbs:
                    pb = pb if isinstance(pb, dict) else {}   # shape guard, see bots/check_shapes.py
                    u2 = str(pb.get("url", ""))
                    if u2.endswith(".mp4"):
                        best = u2  # later entries are higher bitrate
                if best:
                    return best
        return ""
    except Exception:
        return ""


def _post_discord(msg: str) -> None:
    """Fire-and-forget alert to a Discord webhook. Set the DISCORD_WEBHOOK
    secret in the repo and pass it through the workflow env; without it this
    is a silent no-op. The notification lives in Discord's infrastructure —
    the cheapest way live news reaches a pocket without the site growing a
    server (2026-08-06)."""
    for url in _discord_urls():
        try:
            import urllib.request
            req = urllib.request.Request(
                url,
                data=json.dumps({"content": msg[:1900]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "moonshot-bot"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            print(f"discord post failed: {exc}")


def send_pitching_change_alerts(game_cache: Dict[int, Dict[str, Any]], rows: List[Dict[str, Any]]) -> None:
    """🚪 PEN DOOR alerts (2026-08-08, Donovan: "add a discord noti for when
    the pitchers change in the game").

    The live feed marks every change explicitly — eventType
    'pitching_substitution' with a written description ("Pitching Change:
    Evan Sisk replaces Carmen Mlodzinski.") and a UTC startTime; VERIFIED on
    a real game feed before this was written. This job runs hourly, so the
    honest framing is "changes in the last hour", batched into one post.

    Dedupe without state: the window is the PREVIOUS full clock hour
    [tick-1h, tick). Every hourly run sees a disjoint window, so no change
    is ever posted twice and none is skipped (a change landing between the
    tick and this run's start simply ships next hour).

    Why bettors care enough to ping a phone: the door opening is the HR
    window opening — our own graded pen numbers say relievers bleed homers
    late. Each line names which of tonight's picks are on the attacking
    side of the new arm."""
    if not _discord_urls():
        return
    now = dt.datetime.now(dt.UTC)
    tick = now.replace(minute=0, second=0, microsecond=0)
    w_start, w_end = tick - dt.timedelta(hours=1), tick

    picks_by_game: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        try:
            picks_by_game.setdefault(int(r.get("game_pk") or 0), []).append(r)
        except (TypeError, ValueError):
            continue

    lines: List[str] = []
    for game_pk, feed in game_cache.items():
        gteams = (feed.get("gameData", {}) or {}).get("teams", {}) or {}
        home_ab = (gteams.get("home") or {}).get("abbreviation", "")
        away_ab = (gteams.get("away") or {}).get("abbreviation", "")
        for play in ((feed.get("liveData", {}) or {}).get("plays", {}) or {}).get("allPlays", []) or []:
            about = play.get("about") or {}
            for ev in play.get("playEvents") or []:
                det = ev.get("details") or {}
                if det.get("eventType") != "pitching_substitution":
                    continue
                ts = str(ev.get("startTime") or "")
                try:
                    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if not (w_start <= t < w_end):
                    continue
                half = str(about.get("halfInning") or "")
                inning = about.get("inning")
                pitching_ab = home_ab if half == "top" else away_ab
                batting_ab = away_ab if half == "top" else home_ab
                desc = str(det.get("description") or "").rstrip(".")
                our = [str(r.get("name") or "").split()[-1]
                       for r in picks_by_game.get(game_pk, [])
                       if str(r.get("team") or "").upper() == batting_ab][:3]
                line = f"🚪 **{pitching_ab}** pen, {half} {inning}: {desc}"
                if our:
                    line += f" — our bats attacking: {', '.join(our)}"
                lines.append(line)

    if not lines:
        return
    header = (f"🚪 **PEN DOORS — last hour** ({len(lines)} change{'s' if len(lines) != 1 else ''})\n"
              "Fresh arms are where the late homers live — full pen workloads on the site.")
    _post_discord(header + "\n" + "\n".join(lines[:15]))
    print(f"pen-door alert: {len(lines)} change(s) posted")


def _render_night_card(tally: dict, date_str: str) -> bytes:
    """The nightly receipt as an IMAGE (2026-08-06) — an artifact that can be
    posted, screenshotted and shared, and that nobody can retroactively edit.
    Site palette, per-category records at their own bars, overall line."""
    from PIL import Image, ImageDraw, ImageFont
    W, H = 940, 96 + 74 * max(1, len(tally)) + 96
    img = Image.new("RGB", (W, H), (9, 9, 11))
    d = ImageDraw.Draw(img)
    def font(sz, bold=True):
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
                     else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",):
            try:
                return ImageFont.truetype(path, sz)
            except Exception:
                pass
        return ImageFont.load_default()
    ORANGE = (249, 115, 22); GREEN = (74, 222, 128); DIM = (140, 140, 148); TXT = (235, 235, 238)
    CAT_COL = {"TOP": (252, 211, 77), "HR": (251, 146, 60), "HIT": (96, 165, 250),
               "HRR": (34, 211, 238), "CONTACT": (167, 139, 250)}
    d.rectangle([0, 0, W, 6], fill=ORANGE)
    d.text((36, 30), "MOONSHOT — NIGHT RECEIPTS", font=font(30), fill=TXT)
    d.text((36, 68), f"{date_str} · every pick graded against its own bar · locked at first pitch",
           font=font(15, False), fill=DIM)
    y = 118
    tot_ok = tot_n = 0
    BARS = {"TOP": "homered", "HR": "homered", "HIT": "got a hit",
            "HRR": "2+ H+R+RBI", "CONTACT": "2+ total bases"}
    for role in ("TOP", "HR", "HIT", "HRR", "CONTACT"):
        if role not in tally:
            continue
        ok, n = tally[role]; tot_ok += ok; tot_n += n
        pct = (100.0 * ok / n) if n else 0.0
        col = CAT_COL.get(role, DIM)
        d.text((36, y), role, font=font(24), fill=col)
        d.text((36, y + 30), BARS.get(role, ""), font=font(13, False), fill=DIM)
        bx, bw = 260, 460
        d.rounded_rectangle([bx, y + 8, bx + bw, y + 34], 6, fill=(30, 30, 34))
        if n:
            d.rounded_rectangle([bx, y + 8, bx + int(bw * min(1.0, ok / n)), y + 34], 6, fill=col)
        d.text((bx + bw + 24, y + 2), f"{ok}/{n}", font=font(26), fill=TXT)
        d.text((bx + bw + 118, y + 10), f"{pct:.0f}%", font=font(18), fill=GREEN if pct >= 50 else DIM)
        y += 74
    d.line([36, y + 6, W - 36, y + 6], fill=(40, 40, 44), width=2)
    tp = (100.0 * tot_ok / tot_n) if tot_n else 0.0
    d.text((36, y + 22), f"NIGHT: {tot_ok}/{tot_n} · {tp:.0f}%", font=font(26),
           fill=GREEN if tp >= 50 else ORANGE)
    d.text((W - 330, y + 30), "moonshot-mlb.vercel.app", font=font(15, False), fill=DIM)
    import io
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return buf.getvalue()


def _post_discord_file(png: bytes, filename: str, content: str) -> None:
    for url in _discord_urls():
        try:
            import urllib.request, uuid
            boundary = uuid.uuid4().hex
            pj = json.dumps({"content": content}).encode()
            body = b""
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n".encode() + pj + b"\r\n"
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n".encode() + png + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "moonshot-bot"})
            urllib.request.urlopen(req, timeout=15)
        except Exception as exc:
            print(f"discord file post failed: {exc}")


def _webhook_transitions(old_payload, new_payload, date_str: str = "") -> None:
    """Diff the previous published live results against the new ones and post
    ONE Discord digest per grading run (2026-08-06, expanded on request).
    Covers: homers by designated picks, picks clearing their own bar, picks
    going final without it, multi-HR nights, pool one-away/cashed, pair
    cashed, and a full per-category report line once the night is (mostly)
    final. Transitions only — a state already announced never repeats — and
    everything batches into a single message so "a lot of stuff" arrives as
    one hourly digest instead of a pager meltdown."""
    try:
        def slots_of(p):
            out = {}
            for sl in ((p or {}).get("graded_slots") or (p or {}).get("results") or []):
                nm = str(sl.get("name", "")).strip()
                role = str(sl.get("game_pick_role") or sl.get("pick_type") or "").split("/")[0].strip().upper()
                if nm:
                    out[(nm.lower(), role)] = sl
            return out

        def bar_cleared(sl, role):
            if sl is None:
                return None
            # A pick with ZERO at-bats never had his chance — late scratch
            # after the lock, rainout, ejection before a PA. Grading that a
            # miss punishes a bet that never existed; it grades as nothing
            # (2026-08-06, "so it can be true").
            if int(sl.get("actual_ab") or 0) == 0:
                return None
            h = int(sl.get("actual_hits") or 0); hr = int(sl.get("actual_hr") or 0)
            combo = h + int(sl.get("actual_runs") or 0) + int(sl.get("actual_rbi") or 0)
            tb = int(sl.get("actual_tb") or 0)
            if role in ("HR", "TOP"):
                return hr >= 1
            if role == "HIT":
                return h >= 1
            if role == "HRR":
                return combo >= 2
            if role in ("CONTACT", "TB"):
                return tb >= 2
            return None

        old_s, new_s = slots_of(old_payload), slots_of(new_payload)
        lines = []

        # ── picks: homers, bars cleared, final without it ──
        hr_lines, clear_lines, dead_lines, multi_lines, bases_lines = [], [], [], [], []
        for (nm, role), sl in new_s.items():
            if not role:
                continue
            osl = old_s.get((nm, role))
            hr_n, hr_o = int(sl.get("actual_hr") or 0), int((osl or {}).get("actual_hr") or 0)
            name = str(sl.get("name", nm)).strip() or nm.title()
            if hr_n > hr_o:
                extra = f" — that makes {hr_n}" if hr_n > 1 else ""
                clip = _hr_highlight_url(sl.get("game_pk"), name)
                watch = f"  [▶ watch]({clip})" if clip else ""
                _pg = 0.0
                for _k in ("hrw_score", "top_board_score_v2", "overall_score"):
                    try:
                        _pg = float(sl.get(_k) or 0)
                    except Exception:
                        _pg = 0.0
                    if _pg:
                        break
                _called = f", called pregame at {_pg:.0f}" if _pg else ""
                hr_lines.append(f"💥 **{name}** ({role} pick{_called}) went deep{extra}{watch}")
                # Social "pick cashed" post — same transition, same run, no
                # extra fetch. Isolated try/except so a social-pipeline
                # failure never costs us this Discord line (2026-08-22).
                try:
                    from bots.social import pick_cashed as _pick_cashed
                    _pick_cashed.queue_hr(name=name, role=role, date_str=date_str or "",
                                           team=sl.get("team"), opponent=sl.get("opponent"),
                                           hr_score=_pg or None, hr_count=hr_n,
                                           called_pregame=_pg or None)
                except Exception as _sexc:
                    print(f"  · social pick-cashed skipped for {name}: {_sexc}")
            if hr_n >= 2 and hr_o < 2:
                multi_lines.append(f"🚀 **{name}**: {hr_n} HR tonight")
            # Big-bases nights (2026-08-06): 4+ TB crossing, gated so a fresh
            # homer doesn't double-announce — pure bases surges get their own
            # line, HR-driven ones are already covered above.
            tb_n, tb_o = int(sl.get("actual_tb") or 0), int((osl or {}).get("actual_tb") or 0)
            if tb_n >= 4 and tb_o < 4 and hr_n == hr_o:
                h_ = int(sl.get("actual_hits") or 0); ab_ = int(sl.get("actual_ab") or 0)
                bases_lines.append(f"🧨 **{name}** ({role}) — {tb_n} TB night ({h_}-{ab_})")
            c_n, c_o = bar_cleared(sl, role), bar_cleared(osl, role) if osl else None
            if c_n is True and c_o is not True and hr_n == hr_o:
                h = int(sl.get("actual_hits") or 0); ab = int(sl.get("actual_ab") or 0)
                clear_lines.append(f"✓ {name} clears the {role} bar ({h}-{ab})")
            fin_n = int(sl.get("is_final") or 0) == 1
            fin_o = osl is not None and int(osl.get("is_final") or 0) == 1
            if fin_n and not fin_o and c_n is False:
                dead_lines.append(f"✗ {name} — {role} pick final without it")
        # ── ALWAYS-ON LAYERS (2026-08-06, "give that good update") ──
        # Tonight's full pick-homer board, ranked by the bot's own score —
        # shown whenever the NEW list is thin, so a digest never reads empty
        # while bombs already sit on the board.
        def _score(sl):
            for k in ("hrw_score", "top_board_score_v2", "overall_score"):
                try:
                    v = float(sl.get(k) or 0)
                    if v:
                        return v
                except Exception:
                    pass
            return 0.0
        tonight = []
        for (nm, role), sl in new_s.items():
            if role and int(sl.get("actual_hr") or 0) >= 1:
                name = str(sl.get("name", nm)).strip() or nm.title()
                tonight.append((int(sl.get("actual_hr") or 0), _score(sl), name, role))
        tonight.sort(key=lambda t: (-t[0], -t[1]))
        tonight_lines = [
            f"💣 **{name}** ({role}){f' — {hrn} HR' if hrn > 1 else ''}{f' · score {sc:.0f}' if sc else ''}"
            for hrn, sc, name, role in tonight[:8]
        ]

        # Live picks status — cleared / did the job SO FAR, every digest.
        live_tally = {}
        for (nm, role), sl in new_s.items():
            if not role or int(sl.get("actual_ab") or 0) == 0:
                continue
            c = bar_cleared(sl, role)
            if c is None:
                continue
            okc, n = live_tally.get(role, (0, 0))
            live_tally[role] = (okc + (1 if c else 0), n + 1)
        status_line = " · ".join(
            f"**{r}** {okc}/{n}" for r, (okc, n) in sorted(live_tally.items())
        ) if live_tally else ""

        # receipts-first: totals across every judgeable pick RIGHT NOW
        _tot_ok = sum(okc for okc, _n2 in live_tally.values())
        _tot_n = sum(_n2 for _okc2, _n2 in live_tally.values())
        _missed_final = sum(
            1 for (_nm3, _r3), _sl3 in new_s.items()
            if _r3 and int(_sl3.get("is_final") or 0) == 1 and bar_cleared(_sl3, _r3) is False
        )
        receipts_line = None
        if _tot_n:
            receipts_line = (
                f"📌 locked at first pitch, never edited · so far **{_tot_ok}/{_tot_n}** cleared their bar"
                + (f" · **{_missed_final}** finished without it" if _missed_final else "")
                + " — misses post here the same as bombs"
            )

        sections = []
        if receipts_line:
            sections.append(("🧾 THE RECORD, FIRST", [receipts_line]))
        if hr_lines:
            sections.append(("💥 WENT DEEP", hr_lines[:8]))
        if len(hr_lines) < 3 and tonight_lines:
            sections.append(("🏆 TONIGHT SO FAR — by score", tonight_lines))
        if multi_lines:
            sections.append(("🚀 MULTI-HR", multi_lines[:4]))
        if bases_lines:
            sections.append(("🧨 BIG BASES", bases_lines[:5]))
        if clear_lines:
            sections.append(("✓ BARS CLEARED", clear_lines[:10]))
        if dead_lines:
            sections.append(("✗ DIDN'T GET THERE", dead_lines[:8]))

        # ── pools ──
        def pools(p):
            out = {}
            for pl in (((p or {}).get("pair_pool_results") or {}).get("graded_pools") or []):
                out[str(pl.get("label"))] = (
                    int(pl.get("hr_count") or 0), int(pl.get("total_count") or 0),
                    [str(m.get("name")) for m in (pl.get("players") or []) if isinstance(m, dict)],
                    set(str(x).lower() for x in (pl.get("homer_names") or [])),
                )
            return out
        ticket_lines = []
        oldp, newp = pools(old_payload), pools(new_payload)
        for label, (hit, tot, members, homered) in newp.items():
            if not tot:
                continue
            old_hit = oldp.get(label, (0,))[0]
            if hit >= tot and old_hit < tot:
                ticket_lines.append(f"💰 **POOL CASHED — {label}**: all {tot} went deep")
                try:
                    from bots.social import pick_cashed as _pick_cashed
                    _pick_cashed.queue_pool_cashed(label=label, members=list(members), date_str=date_str or "")
                except Exception as _sexc:
                    print(f"  · social pick-cashed skipped for pool {label}: {_sexc}")
            elif hit == tot - 1 and old_hit < tot - 1:
                missing = [m for m in members if m.lower() not in homered]
                ticket_lines.append(f"🎟 {label} · **{hit}/{tot}** — one swing away ({', '.join(missing[:3])})")

        # ── pairs ──
        def hr_names(p):
            slots = (p or {}).get("graded_slots") or (p or {}).get("results") or []
            return set(str(sl.get("name", "")).lower() for sl in slots
                       if int(sl.get("actual_hr") or 0) >= 1 or int(sl.get("got_hr") or 0) >= 1)
        old_hr, new_hr = hr_names(old_payload), hr_names(new_payload)
        for pr in (((new_payload or {}).get("pair_pool_results") or {}).get("all_pairs") or []):
            a_n = str((pr.get("a") or {}).get("name", ""))
            b_n = str((pr.get("b") or {}).get("name", ""))
            if not a_n or not b_n:
                continue
            if a_n.lower() in new_hr and b_n.lower() in new_hr and not (a_n.lower() in old_hr and b_n.lower() in old_hr):
                ticket_lines.append(f"💰 **PAIR CASHED — {a_n} + {b_n}** ({pr.get('label', 'pair')})")
                try:
                    from bots.social import pick_cashed as _pick_cashed
                    _pick_cashed.queue_pair_cashed(a=a_n, b=b_n, label=str(pr.get("label", "pair")),
                                                    date_str=date_str or "")
                except Exception as _sexc:
                    print(f"  · social pick-cashed skipped for pair {a_n}+{b_n}: {_sexc}")

        # ── night wrap: per-category record, once, when grading turns final ──
        def final_share(p):
            slots = (p or {}).get("graded_slots") or (p or {}).get("results") or []
            fin = sum(1 for sl in slots if int(sl.get("is_final") or 0) == 1)
            return (fin / len(slots)) if slots else 0.0
        if status_line:
            sections.append(("📋 PICKS SO FAR — did the job", [status_line]))
        tally = {}
        if final_share(new_payload) >= 0.95 and final_share(old_payload) < 0.95:
            # simple per-role tally
            tally = {}
            for (nm, role), sl in new_s.items():
                if not role:
                    continue
                c = bar_cleared(sl, role)
                if c is None:
                    continue
                ok, n = tally.get(role, (0, 0))
                tally[role] = (ok + (1 if c else 0), n + 1)
            if tally:
                parts = [f"**{r}** {ok}/{n}" for r, (ok, n) in sorted(tally.items())]
                sections.append(("🧾 NIGHT WRAP", [" · ".join(parts)]))
                # the shareable artifact: tonight's receipts as an image
                try:
                    _png = _render_night_card(tally, date_str or "tonight")
                    _post_discord_file(_png, f"receipts_{date_str or 'night'}.png",
                                       "🧾 **Night receipts** — locked, graded, done.")
                except Exception as _cexc:
                    print(f"night card skipped: {_cexc}")
        if ticket_lines:
            sections.append(("🎫 TICKETS", ticket_lines[:8]))

        if not sections:
            return
        # One rich embed per digest: sectioned, color-coded, timestamped —
        # not a wall of words. Cashes turn it gold, homers green, otherwise
        # the site's ember orange. Footer carries the running pick record.
        cashed = any("CASHED" in ln for _, ls in sections for ln in ls)
        went_deep = any(t.startswith("💥") for t, _ in sections)
        color = 0xF5C242 if cashed else (0x4ADE80 if went_deep else 0xF97316)
        desc_parts = []
        for title, ls in sections:
            desc_parts.append(f"**{title}**\n" + "\n".join(ls))
        desc = "\n\n".join(desc_parts)[:4000]
        src_tally = tally or live_tally
        ok_t = sum(ok for ok, n in src_tally.values()) if src_tally else None
        n_t = sum(n for ok, n in src_tally.values()) if src_tally else None
        footer = (f"picks {ok_t}/{n_t} on their own bars tonight" if n_t else "moonshot live digest") \
            + " · stats & analysis, not financial or betting advice"
        _post_discord_payload({
            "embeds": [{
                "title": "📡 Moonshot — live digest",
                "description": desc,
                "color": color,
                "footer": {"text": footer},
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            }],
        })
    except Exception as exc:
        print(f"webhook transitions skipped: {exc}")


def _sync_write_json(path: Path, payload) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"⚠️ Could not update {path}: {exc}")

def _sync_git_best_effort(repo: Path, message: str) -> None:
    # In GitHub Actions, the workflow handles the commit + push itself.
    # Don't try to do it here — it would either fail (no user.name set) or
    # interfere with the workflow's commit logic.
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        print("✅ Files staged in public/data — workflow will commit and push.")
        return
    if not (repo / ".git").exists():
        print(f"✅ Website files copied locally. Open GitHub Desktop for: {repo}")
        return
    try:
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], text=True, capture_output=True)
        if not status.stdout.strip():
            print("✅ Website repo already clean after sync.")
            return
        subprocess.run(["git", "-C", str(repo), "add", "public/data"], check=False)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", message], check=False)
        pushed = subprocess.run(["git", "-C", str(repo), "push"], text=True, capture_output=True)
        if pushed.returncode == 0:
            print("✅ Synced + pushed website results to GitHub. Vercel should redeploy.")
        else:
            print("⚠️ Website results copied, but git push failed. Use GitHub Desktop to commit/push.")
            if pushed.stderr:
                print(pushed.stderr.strip())
    except Exception as exc:
        print(f"⚠️ Website results copied, but auto git failed: {exc}")
        print("Open GitHub Desktop → commit → push.")

def sync_results_to_website_repo_v2(date_str: str, live_mode: bool, json_path: Path, txt_path: Path, pdf_path: Path) -> None:
    data_dir = DASHBOARD_REPO / "public" / "data"
    if not _is_dashboard_repo(DASHBOARD_REPO):
        print(f"⚠️ Website repo not found at {DASHBOARD_REPO}")
        print(f"   Tried env (GITHUB_WORKSPACE, MLB_DASHBOARD_DIR) and common Mac paths.")
        print(f"   To debug: run `pwd` and verify a folder with app/ + public/ exists.")
        return
    print(f"📍 Website repo: {DASHBOARD_REPO}")

    results_dir = data_dir / "results"
    current_dir = data_dir / "current"
    role = "live" if live_mode else "final"
    active_json = "results_live.json" if live_mode else "results_final.json"
    active_txt = "results_live.txt" if live_mode else "results_final.txt"
    legacy_prefix = "live_graded_results" if live_mode else "graded_results"

    # Discord transitions: compare against what was already public BEFORE the
    # new results overwrite it (live runs only — finals repeat nothing new).
    if live_mode:
        try:
            _old_pub = current_dir / active_json
            _oldp = json.loads(_old_pub.read_text(encoding="utf-8")) if _old_pub.exists() else None
            _newp = json.loads(Path(json_path).read_text(encoding="utf-8"))
            _webhook_transitions(_oldp, _newp, date_str)
        except Exception as _wexc:
            print(f"webhook diff skipped: {_wexc}")

    targets = [
        (json_path, data_dir / active_json),
        (txt_path, data_dir / active_txt),
        (json_path, current_dir / active_json),
        (txt_path, current_dir / active_txt),
        (json_path, results_dir / f"{legacy_prefix}_{date_str}.json"),
        (txt_path, results_dir / f"{legacy_prefix}_{date_str}.txt"),
        (json_path, data_dir / f"{legacy_prefix}_{date_str}.json"),
        (txt_path, data_dir / f"{legacy_prefix}_{date_str}.txt"),
    ]
    if pdf_path and pdf_path.exists():
        targets.append((pdf_path, results_dir / f"{legacy_prefix}_{date_str}.pdf"))

    copied = 0
    for src, dest in targets:
        copied += 1 if _sync_copy(src, dest) else 0

    results_index = results_dir / "index.json"
    ridx = _sync_read_json(results_index, {})
    if not isinstance(ridx, dict):
        ridx = {}
    files = ridx.get("files", [])
    if not isinstance(files, list):
        files = []
    history_name = f"{legacy_prefix}_{date_str}.json"
    files = list(dict.fromkeys([history_name] + files))[:80]
    _sync_write_json(results_index, {"files": files, "updated": date_str, "active": active_json})

    root_index = data_dir / "index.json"
    idx = _sync_read_json(root_index, {})
    if not isinstance(idx, dict):
        idx = {}
    results = idx.get("results", [])
    if not isinstance(results, list):
        results = []
    add_results = [active_json, f"results/{history_name}", history_name]
    idx["results"] = list(dict.fromkeys(add_results + results))[:80]
    idx.setdefault("current", idx.get("current", {}))
    idx.setdefault("files", idx.get("files", []))
    idx.setdefault("history", idx.get("history", []))
    _sync_write_json(root_index, idx)

    print(f"✅ Dashboard synced results ({role}): {copied} files copied into website repo.")
    _sync_git_best_effort(DASHBOARD_REPO, f"Update MLB {role} results {date_str}")
# ─────────────────────────────────────────────────────────────────────────────

def fetch_game_feed(game_pk: int) -> Dict[str, Any]:
    resp = requests.get(f"{MLB_BASE}/{game_pk}/feed/live", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_player_batting_line(game_feed: Dict[str, Any], player_id: int) -> Dict[str, Any]:
    teams = game_feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    for side in ("home", "away"):
        players = teams.get(side, {}).get("players", {}) or {}
        pdata = players.get(f"ID{player_id}")
        if pdata:
            batting = pdata.get("stats", {}).get("batting", {}) or {}
            # ── WAS HE PULLED, OR DID HE COME IN LATE? (2026-08-22) ────────
            # Both cases mean the pick never got a fair test, and both are
            # derivable from the boxscore this function already has -- no
            # extra request.
            #
            # MLB's battingOrder is a 3-digit string: "400" is the starter in
            # the 4 slot, "401" the first man to replace him there, "402" the
            # second. So a trailing "00" means he STARTED, and any teammate
            # sharing his first two digits with a non-"00" suffix means
            # somebody took his slot -- pinch-hit for, pinch-run for, or
            # pulled. A non-"00" suffix on his own order means HE was the
            # substitute.
            #
            # What this deliberately does NOT claim: that it can see an
            # INJURY. The boxscore does not say why a man left the game. A
            # player removed for a pinch-hitter and one removed with a
            # hamstring look identical here, so both are reported as
            # "replaced" rather than one being guessed at as "hurt".
            _order = str(pdata.get("battingOrder") or "")
            _gs = pdata.get("gameStatus") or {}
            was_substitute = bool(_gs.get("isSubstitute")) or (
                len(_order) == 3 and not _order.endswith("00"))
            was_replaced = False
            if len(_order) == 3 and _order.endswith("00"):
                for other in players.values():
                    if not isinstance(other, dict) or other is pdata:
                        continue
                    o = str(other.get("battingOrder") or "")
                    if len(o) == 3 and o[:-2] == _order[:-2] and not o.endswith("00"):
                        was_replaced = True
                        break
            # Extract homer events for this player (2026-08-11: JOB 1)
            hr_events = []
            plays = ((game_feed.get("liveData") or {}).get("plays") or {}).get("allPlays") or []
            for play in plays:
                result = play.get("result") or {}
                if str(result.get("eventType") or result.get("event") or "").lower() not in (
                        "home_run", "home run"):
                    continue
                batter = ((play.get("matchup") or {}).get("batter") or {})
                if safe_int(batter.get("id"), 0) != player_id:
                    continue
                # Extract homer details
                hit = {}
                for ev in reversed(play.get("playEvents") or []):
                    if ev.get("hitData"):
                        hit = ev["hitData"]
                        break
                pitcher = ((play.get("matchup") or {}).get("pitcher") or {})
                hr_events.append({
                    "inning": safe_int(play.get("about", {}).get("inning"), None),
                    "pitcher_id": safe_int(pitcher.get("id"), None),
                    "pitcher_name": pitcher.get("fullName", ""),
                    "launch_speed": safe_float(hit.get("launchSpeed"), None) if hit.get("launchSpeed") is not None else None,
                    "launch_angle": safe_float(hit.get("launchAngle"), None) if hit.get("launchAngle") is not None else None,
                    "total_distance": safe_float(hit.get("totalDistance"), None) if hit.get("totalDistance") is not None else None,
                    "pitch_type": "",
                    "event": result.get("event", ""),
                })
            return {
                "hits": safe_int(batting.get("hits"), 0),
                "hr": safe_int(batting.get("homeRuns"), 0),
                "runs": safe_int(batting.get("runs"), 0),
                "rbi": safe_int(batting.get("rbi"), 0),
                "tb": safe_int(batting.get("totalBases"), 0),
                "ab": safe_int(batting.get("atBats"), 0),
                # Docket #1-3 (2026-08-05): these four were sitting unread in
                # the same dict. actual_k makes K-risk auditable for the first
                # time; actual_bb stops CONTACT picks being graded failures
                # for walking twice.
                "k": safe_int(batting.get("strikeOuts"), 0),
                "bb": safe_int(batting.get("baseOnBalls"), 0),
                # Plate appearances, not at-bats. Every book that states a
                # batter-prop threshold states it in PA (FanDuel: "one plate
                # appearance"), and a walk is a plate appearance. Falls back to
                # AB+BB when the boxscore omits the field, rather than a zero
                # that would read as "never came up".
                "pa": safe_int(batting.get("plateAppearances"),
                               safe_int(batting.get("atBats"), 0) + safe_int(batting.get("baseOnBalls"), 0)),
                "doubles": safe_int(batting.get("doubles"), 0),
                "triples": safe_int(batting.get("triples"), 0),
                "hr_events": hr_events,  # JOB 1: per-homer batted-ball capture
                "was_substitute": was_substitute,
                "was_replaced": was_replaced,
            }
    return {
        "hits": 0, "hr": 0, "runs": 0, "rbi": 0, "tb": 0, "ab": 0, "k": 0, "bb": 0,
        "pa": 0, "doubles": 0, "triples": 0, "hr_events": [],
        "was_substitute": False, "was_replaced": False,
    }




def hr_distances_from_game(game_feed: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """{batter_id: {"longest": ft, "distances": [...], "max_ev": mph}} for HRs.

    The boxscore only carries a home-run COUNT. Distance lives on the play
    itself, in liveData.plays.allPlays[].hitData -- the same feed we already
    download, so this costs no extra request. Statcast occasionally omits
    hitData on a play (tracking gap); those homers simply have no distance
    rather than a zero, so they can't drag a leaderboard down.
    """
    out: Dict[int, Dict[str, Any]] = {}
    plays = ((game_feed.get("liveData") or {}).get("plays") or {}).get("allPlays") or []
    for play in plays:
        result = play.get("result") or {}
        if str(result.get("eventType") or result.get("event") or "").lower() not in (
                "home_run", "home run"):
            continue
        batter = ((play.get("matchup") or {}).get("batter") or {})
        pid = safe_int(batter.get("id"), 0)
        if not pid:
            continue
        # hitData sits on the last playEvent of the at-bat.
        hit = {}
        for ev in reversed(play.get("playEvents") or []):
            if ev.get("hitData"):
                hit = ev["hitData"]
                break
        dist = safe_float(hit.get("totalDistance"), 0.0)
        ev_mph = safe_float(hit.get("launchSpeed"), 0.0)
        # PITCH CAPTURE (2026-07-31). pitchData sits on the same playEvent as
        # hitData and costs no extra request. Without it there is no way to
        # test whether breaking balls really produce more 400+ contact than
        # they are thrown -- and crucially, storing EVERY homer here gives the
        # short ones too, which is the comparison group a highlight feed can
        # never provide.
        pitch_ev = None
        for evt in reversed(play.get("playEvents") or []):
            if evt.get("hitData"):
                pitch_ev = evt
                break
        pd_ = (pitch_ev or {}).get("pitchData") or {}
        details = (pitch_ev or {}).get("details") or {}
        ptype = ((details.get("type") or {}).get("description")
                 or (details.get("type") or {}).get("code") or "")
        pvelo = safe_float((pd_.get("startSpeed") if pd_ else None), 0.0)
        spin = safe_float(((pd_.get("breaks") or {}).get("spinRate")
                           if pd_ else None), 0.0)

        rec = out.setdefault(pid, {"longest": 0.0, "distances": [], "max_ev": 0.0,
                                   "launch_angle": None, "hrs": []})
        if dist > 0:
            rec["distances"].append(round(dist))
            rec["longest"] = max(rec["longest"], round(dist))
        if ev_mph > 0:
            rec["max_ev"] = max(rec["max_ev"], round(ev_mph, 1))
        la = safe_float(hit.get("launchAngle"), None) if hit.get("launchAngle") is not None else None
        if la is not None and (rec["launch_angle"] is None or dist >= rec["longest"]):
            rec["launch_angle"] = round(la, 1)
        # One row per home run, so each is analysable on its own rather than
        # collapsed into a per-player maximum.
        rec.setdefault("hrs", []).append({
            "dist": round(dist) if dist > 0 else None,
            "ev": round(ev_mph, 1) if ev_mph > 0 else None,
            "la": round(la, 1) if la is not None else None,
            "pitch": str(ptype) or None,
            "pitch_velo": round(pvelo, 1) if pvelo > 0 else None,
            "spin": int(spin) if spin > 0 else None,
        })
    return out


def get_all_homers_from_game(game_feed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return every player who homered in this game, including untracked players."""
    homers: List[Dict[str, Any]] = []
    dist_by_pid = hr_distances_from_game(game_feed)
    teams = game_feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    game_data = game_feed.get("gameData", {}) or {}
    team_meta = game_data.get("teams", {}) or {}
    for side in ("home", "away"):
        team_abbr = ((team_meta.get(side) or {}).get("abbreviation") or side.upper())
        players = teams.get(side, {}).get("players", {}) or {}
        for key, pdata in players.items():
            batting = pdata.get("stats", {}).get("batting", {}) or {}
            hr = safe_int(batting.get("homeRuns"), 0)
            if hr <= 0:
                continue
            person = pdata.get("person", {}) or {}
            pid = safe_int(person.get("id"), 0)
            d = dist_by_pid.get(pid) or {}
            homers.append({
                "player_id": pid,
                "name": person.get("fullName", f"Player {pid}"),
                "team": team_abbr,
                "hr": hr,
                # None, not 0, when Statcast didn't track it.
                "longest_ft": (d.get("longest") or None),
                "distances_ft": d.get("distances") or [],
                "max_ev_mph": (d.get("max_ev") or None),
                "launch_angle": d.get("launch_angle"),
            })
    return homers


def build_hr_capture_report(rows: List[Dict[str, Any]], game_cache: Dict[int, Dict[str, Any]], actual_by_pid: Dict[Tuple[int, int], Dict[str, int]]) -> Dict[str, Any]:
    """Compare every HR hit on the slate against every player included in the model output."""
    tracked_player_ids = {int(r["player_id"]) for r in rows}
    tracked_by_pid = {int(r["player_id"]): r for r in rows}

    all_homer_entries: List[Dict[str, Any]] = []
    for game_pk, feed in game_cache.items():
        for h in get_all_homers_from_game(feed):
            h["game_pk"] = int(game_pk)
            all_homer_entries.append(h)

    total_hrs = sum(safe_int(h.get("hr"), 0) for h in all_homer_entries)
    caught_entries = [h for h in all_homer_entries if int(h.get("player_id", 0)) in tracked_player_ids]
    caught_hrs = sum(safe_int(h.get("hr"), 0) for h in caught_entries)
    missed_entries = [h for h in all_homer_entries if int(h.get("player_id", 0)) not in tracked_player_ids]
    missed_hrs = max(0, total_hrs - caught_hrs)
    capture_pct = round(100 * caught_hrs / total_hrs, 1) if total_hrs else 0.0

    caught_details = []
    for h in caught_entries:
        base = tracked_by_pid.get(int(h.get("player_id", 0)), {})
        caught_details.append({
            **h,
            "hr_score": safe_float(base.get("hr_score"), 0.0),
            "overall_score": safe_float(base.get("overall_score"), 0.0),
        })

    return {
        "total_hrs_on_slate": total_hrs,
        "caught_hrs_on_sheet": caught_hrs,
        "missed_hrs_not_on_sheet": missed_hrs,
        "hr_capture_pct": capture_pct,
        "all_homer_entries": all_homer_entries,
        "caught_homer_entries": caught_details,
        "missed_homer_entries": missed_entries,
    }

def build_unique_player_hr_report(graded_slots: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Count each player once so duplicate slots do not inflate/deflate HR accuracy."""
    by_pid: Dict[int, Dict[str, Any]] = {}
    for r in graded_slots:
        pid = safe_int(r.get("player_id"), 0)
        if not pid:
            continue
        if pid not in by_pid:
            by_pid[pid] = {**r, "got_hr": 0, "slot_tags": []}
        if safe_int(r.get("got_hr"), 0) >= 1:
            by_pid[pid]["got_hr"] = 1
        tag = str(r.get("pick_type", ""))
        if tag == "TOP15":
            tag = f"TOP15#{r.get('rank', '')}"
        if tag and tag not in by_pid[pid]["slot_tags"]:
            by_pid[pid]["slot_tags"].append(tag)

    unique_players = list(by_pid.values())
    hr_players = [r for r in unique_players if safe_int(r.get("got_hr"), 0) >= 1]
    return {
        "unique_players_tracked": len(unique_players),
        "unique_players_with_hr": len(hr_players),
        "unique_hr_accuracy_pct": round(100 * len(hr_players) / len(unique_players), 1) if unique_players else 0.0,
        "unique_hr_players": sorted(hr_players, key=lambda r: (-safe_float(r.get("hr_score")), str(r.get("name", "")))),
    }


def _format_names(names: List[str]) -> str:
    return ", ".join(names) if names else "none"


def get_game_status(game_feed: Dict[str, Any]) -> Dict[str, Any]:
    game_data = game_feed.get("gameData", {}) or {}
    live_data = game_feed.get("liveData", {}) or {}
    status = game_data.get("status", {}) or {}
    linescore = live_data.get("linescore", {}) or {}
    teams = game_data.get("teams", {}) or {}
    # ROLLING-WINDOW DATE FIX (2026-08-21, Sol's audit finding #10; see
    # dash-roadmap-architecture-review.md §C5, "the outcome record carries
    # game_date_actual; every date-based report groups by the outcome's game
    # date, not the prediction's"). officialDate is MLB StatsAPI's own field
    # for which calendar date a game's stats are attributed to -- it is the
    # authoritative answer to "what date did this game actually happen on,"
    # distinct from `prediction_date` (the slate/lock date, i.e. when the
    # PICK was made) which is what build_outcome_candidates() used to be the
    # only date on an outcome row. The two are the same date for the
    # overwhelming majority of games and only diverge in exactly the case
    # this exists for: a game suspended on day D and resumed/completed on
    # D+2 still officially counts toward day D under MLB's own bookkeeping,
    # not the resume day -- and NOT the possibly-much-later day a hourly
    # regrade_stale_dates() recheck happens to notice it finished.
    return {
        "detailed_state": status.get("detailedState", "Unknown"),
        "abstract_state": status.get("abstractGameState", "Unknown"),
        "inning": linescore.get("currentInning"),
        "inning_half": linescore.get("inningHalf"),
        "away": ((teams.get("away") or {}).get("abbreviation") or "AWAY"),
        "home": ((teams.get("home") or {}).get("abbreviation") or "HOME"),
        "official_date": (game_data.get("datetime", {}) or {}).get("officialDate"),
    }


def game_is_final(game_feed: Dict[str, Any]) -> bool:
    st = get_game_status(game_feed)
    detailed = str(st.get("detailed_state", "")).lower()
    abstract = str(st.get("abstract_state", "")).lower()
    return "final" in detailed or abstract == "final"


def minmax_norm(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.5
    value = max(low, min(high, safe_float(value, low)))
    return (value - low) / (high - low)


def pick_top(records: List[Dict[str, Any]], attr: str, n: int, used: Optional[Iterable[int]] = None) -> List[Dict[str, Any]]:
    used_set = set(used or [])
    return sorted(
        [r for r in records if int(r["player_id"]) not in used_set],
        key=lambda x: safe_float(x.get(attr, 0.0)),
        reverse=True,
    )[:n]


def same_date_proxy_score(rec: Dict[str, Any]) -> float:
    return (
        0.45 * minmax_norm(safe_float(rec.get("last5_hr")), 0, 3) +
        0.25 * minmax_norm(safe_float(rec.get("last5_xbh")), 0, 4) +
        0.30 * safe_float(rec.get("numerology_score"), 0.0)
    )


def hot_score(rec: Dict[str, Any]) -> float:
    return (
        0.45 * minmax_norm(safe_float(rec.get("last5_hr")), 0, 3) +
        0.30 * minmax_norm(safe_float(rec.get("last5_xbh")), 0, 4) +
        0.25 * minmax_norm(safe_float(rec.get("last5_hits")), 0, 8)
    )


def due_score(rec: Dict[str, Any]) -> float:
    num = safe_float(rec.get("recent_350_num"))
    den = max(1.0, safe_float(rec.get("recent_350_den"), 1.0))
    return (
        0.40 * minmax_norm(num / den, 0.05, 0.45) +
        0.25 * minmax_norm(safe_float(rec.get("recent_barrel_rate")), 0.02, 0.25) +
        0.20 * minmax_norm(safe_float(rec.get("recent_fb_rate")), 0.20, 0.55) +
        0.15 * (1.0 - minmax_norm(safe_float(rec.get("last5_hr")), 0, 3))
    )


def matchup_score(rec: Dict[str, Any]) -> float:
    pitcher_throws = rec.get("pitcher_throws", "")
    split_avg = safe_float(rec.get("avg_vs_lhp")) if pitcher_throws == "L" else safe_float(rec.get("avg_vs_rhp"))
    split_iso = safe_float(rec.get("iso_vs_lhp")) if pitcher_throws == "L" else safe_float(rec.get("iso_vs_rhp"))
    bats = rec.get("bats", "")
    weak_side = rec.get("pitcher_weak_side", "")
    side_match = 1.0 if ((bats == "L" and weak_side == "LHB") or (bats == "R" and weak_side == "RHB")) else 0.45
    weak_spot = 1.0 if rec.get("weak_spot_flag") else 0.4
    return (
        0.30 * minmax_norm(split_avg, 0.180, 0.360) +
        0.22 * minmax_norm(split_iso, 0.05, 0.35) +
        0.18 * minmax_norm(safe_float(rec.get("pitcher_hr_allowed")), 5, 30) +
        0.15 * minmax_norm(safe_float(rec.get("pitcher_fb_rate")), 0.25, 0.50) +
        0.10 * side_match +
        0.05 * weak_spot
    )


def pair_allowed(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return int(a["player_id"]) != int(b["player_id"]) and int(a["game_pk"]) != int(b["game_pk"])


def best_hr_pair_score(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    a350 = safe_float(a.get("recent_350_num")) / max(1.0, safe_float(a.get("recent_350_den"), 1.0))
    b350 = safe_float(b.get("recent_350_num")) / max(1.0, safe_float(b.get("recent_350_den"), 1.0))
    wea = (minmax_norm(safe_float(a.get("weather_temp_f"), 70), 55, 95) + minmax_norm(safe_float(a.get("weather_wind_mph"), 0), 0, 20)) / 2
    web = (minmax_norm(safe_float(b.get("weather_temp_f"), 70), 55, 95) + minmax_norm(safe_float(b.get("weather_wind_mph"), 0), 0, 20)) / 2
    return (
        0.36 * safe_float(a.get("hr_score")) +
        0.36 * safe_float(b.get("hr_score")) +
        0.08 * matchup_score(a) +
        0.08 * matchup_score(b) +
        0.05 * minmax_norm(a350, 0.05, 0.45) +
        0.05 * minmax_norm(b350, 0.05, 0.45) +
        0.01 * wea + 0.01 * web
    )


def hot_due_pair_score(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    return max(
        0.55 * hot_score(a) + 0.45 * due_score(b),
        0.55 * hot_score(b) + 0.45 * due_score(a),
    )


def numerology_pair_score(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    return (
        0.42 * safe_float(a.get("numerology_score"), 0.0) +
        0.42 * safe_float(b.get("numerology_score"), 0.0) +
        0.08 * minmax_norm(safe_float(a.get("hr_score")), 40, 80) +
        0.08 * minmax_norm(safe_float(b.get("hr_score")), 40, 80)
    )


def select_diverse_pairs(scored_pairs: List[Tuple[Dict[str, Any], Dict[str, Any], float, str]], max_pairs: int = 2, max_player_exposure: int = 1):
    selected = []
    exposure: Dict[int, int] = {}
    for a, b, score, label in scored_pairs:
        ap = int(a["player_id"])
        bp = int(b["player_id"])
        if exposure.get(ap, 0) >= max_player_exposure:
            continue
        if exposure.get(bp, 0) >= max_player_exposure:
            continue
        selected.append((a, b, score, label))
        exposure[ap] = exposure.get(ap, 0) + 1
        exposure[bp] = exposure.get(bp, 0) + 1
        if len(selected) >= max_pairs:
            break
    return selected


def build_pool(rows: List[Dict[str, Any]], size: int, variant: str, used_players=None):
    used_players = set(used_players or [])
    scored = []
    if variant == "4":
        for r in rows:
            score = (
                0.45 * safe_float(r.get("hr_score")) +
                0.20 * hot_score(r) +
                0.20 * due_score(r) +
                0.15 * matchup_score(r)
            )
            scored.append((r, score))
    else:
        for r in rows:
            score = (
                0.38 * safe_float(r.get("hr_score")) +
                0.18 * hot_score(r) +
                0.18 * due_score(r) +
                0.16 * matchup_score(r) +
                0.10 * (safe_float(r.get("overall_score")) / 100.0)
            )
            scored.append((r, score))
    scored.sort(key=lambda x: x[1], reverse=True)

    selected = []
    used_games = set()
    for rec, score in scored:
        pid = int(rec["player_id"])
        game_pk = int(rec["game_pk"])
        if pid in used_players:
            continue
        if game_pk in used_games:
            continue
        selected.append((rec, score))
        used_players.add(pid)
        used_games.add(game_pk)
        if len(selected) >= size:
            break
    return selected, used_players


def build_pair_pool_sections(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ranked = sorted(rows, key=lambda r: safe_float(r.get("hr_score")), reverse=True)
    used_pair_ids: set[int] = set()

    def pair_score(kind: str, a: Dict[str, Any], b: Dict[str, Any]) -> float:
        if kind == "bomb_gap":
            return 0.56 * safe_float(a.get("hr_score")) + 0.20 * safe_float(b.get("hr_score")) + 0.24 * safe_float(b.get("contact_score"))
        if kind == "bomb_matchup":
            return 0.58 * safe_float(a.get("hr_score")) + 0.18 * safe_float(b.get("hr_score")) + 0.24 * matchup_score(b)
        if kind == "bomb_variance":
            return 0.52 * safe_float(a.get("hr_score")) + 0.20 * safe_float(b.get("hr_score")) + 0.28 * due_score(b)
        return best_hr_pair_score(a, b)

    def select_pair(kind: str, right_key: str = "hr_score") -> Tuple[Dict[str, Any], Dict[str, Any], float, str]:
        left_sorted = [r for r in ranked if int(r["player_id"]) not in used_pair_ids]
        right_sorted = sorted([r for r in rows if int(r["player_id"]) not in used_pair_ids], key=lambda r: safe_float(r.get(right_key)), reverse=True)
        best = None
        best_score = -1.0
        for a in left_sorted:
            for b in right_sorted:
                if not pair_allowed(a, b):
                    continue
                if int(a["player_id"]) in used_pair_ids or int(b["player_id"]) in used_pair_ids:
                    continue
                score = pair_score(kind, a, b)
                if score > best_score:
                    best_score = score
                    best = (a, b, round(score, 1))
        if best is None:
            for i in range(len(ranked)):
                if int(ranked[i]["player_id"]) in used_pair_ids:
                    continue
                for j in range(i + 1, len(ranked)):
                    if int(ranked[j]["player_id"]) in used_pair_ids:
                        continue
                    if pair_allowed(ranked[i], ranked[j]):
                        best = (ranked[i], ranked[j], round(best_hr_pair_score(ranked[i], ranked[j]), 1))
                        break
                if best is not None:
                    break
        a, b, score = best
        used_pair_ids.add(int(a["player_id"]))
        used_pair_ids.add(int(b["player_id"]))
        return a, b, score, kind

    pair_groups = [
        {"label": "HR Pair A | Pure Bombs", "pairs": [_trim_pair(select_pair("pure_bombs", "hr_score"))]},
        {"label": "HR Pair B | Bomb + Gap Power", "pairs": [_trim_pair(select_pair("bomb_gap", "contact_score"))]},
        {"label": "HR Pair C | Bomb + Matchup", "pairs": [_trim_pair(select_pair("bomb_matchup", "hr_score"))]},
        {"label": "HR Pair D | Bomb + Variance", "pairs": [_trim_pair(select_pair("bomb_variance", "hr_score"))]},
    ]

    used_for_pools = set(used_pair_ids)
    pool4_buckets = []
    for label in ["4-MAN HR POOL A", "4-MAN HR POOL B", "4-MAN HR POOL C", "4-MAN HR POOL D"]:
        pool, used_for_pools = build_pool(rows, 4, "4", used_for_pools)
        pool4_buckets.append({"label": label, "players": [trim_row(r) for r, _ in pool]})

    # 6-MAN RETIRED (2026-08-09, matches mlb_dashboard.py's build_pair_sections)
    # -- this internal rebuild is the last-resort fallback for when neither
    # the published "latest" nor the dated archive is available (see
    # load_pair_builder_sections below), so it was still quietly building
    # six-man pools nobody ships anymore. Renamed to match: same fallback
    # scoring, three-man pools instead of six.
    pool3_buckets = []
    for label in ["3-MAN HR POOL A", "3-MAN HR POOL B", "3-MAN HR POOL C", "3-MAN HR POOL D"]:
        pool, used_for_pools = build_pool(rows, 3, "3", used_for_pools)
        pool3_buckets.append({"label": label, "players": [trim_row(r) for r, _ in pool]})

    return {"pair_groups": pair_groups, "pools": pool4_buckets + pool3_buckets}

# Fields this script actually reads from a player row, anywhere in the file
# (tracking slots, grading, pair/pool scoring, missed-HR diagnostics). Built
# by grepping every r.get(...)/rec.get(...) call site in this script.
#
# BUGFIX: build_tracking_slots() used `{**r, "pick_type": ...}` to build each
# slot, which spreads the ENTIRE source row -- including heavy fields like
# batter_pitch_type_profile, pitcher_pitch_mix_vs_lhb/_vs_rhb, zone_profile,
# spray_chart/bbe arrays, etc -- into every one of the ~90+ slots generated
# per day. None of those heavy fields are ever read by this script; they were
# just carried along for the ride from today_bot.py's breakdown JSON. That's
# what pushed mlb_results_live_*.json to 104MB+ and broke every git push
# (GitHub's hard limit is 100MB). trim_row() keeps everything this script
# actually uses and drops the rest.
SLOT_FIELDS = {
    "player_id", "name", "team", "game_pk", "bats", "pitcher_throws",
    "hr_score", "overall_score", "hit_score", "hrr_score", "contact_score",
    "season_iso", "season_avg", "season_hr", "season_xbh",
    "last5_hits", "last5_hr", "last5_xbh", "last10_xbh",
    "avg_vs_lhp", "avg_vs_rhp", "iso_vs_lhp", "iso_vs_rhp",
    "recent_350_num", "recent_350_den", "recent_barrel_rate", "recent_fb_rate",
    "recent_ld_rate", "recent_gb_rate", "recent_popup_rate",
    "pitcher_hr_allowed", "pitcher_fb_rate", "pitcher_weak_side",
    "weather_temp_f", "weather_wind_mph", "numerology_score",
    "weak_spot_flag", "weak_spot_reason", "best_bet_type", "true_avoid_hr",
    "best_non_hr_category", "top_board_tags", "game_pick_role",
    "lineup_spot", "lineup_confirmed", "venue_name", "game_time",
    # opp defense validation lane (2026-08-08): opponent rides along so the
    # grade-time defense stamp can join, and the archive can answer whether
    # leaky defenses actually lift HIT/TB picks before it touches a score
    "opponent",
    # Docket #8-10 (2026-08-05): carried through so every score can be
    # backtested against the day it was generated for, instead of joined to
    # tonight's slate — exact for today, wrong for any archived day.
    "hrw_score", "pitch_mix_score", "top_board_score_v2", "recent_375_num",
    "pitcher_name", "pitcher_id", "pitcher_hr9",
    # Walks data (2026-08-12, Donovan): batter BB% was already archived
    # (season_bb_rate, below) -- these two are the pitcher-side half.
    "pitcher_bb_pct", "pitcher_bb9",
    "park_factor", "park_hr_factor", "wind_direction_label",
    "weather_wind_direction_label", "season_k_rate", "season_bb_rate",
    "trap_flag", "alt_look_tag", "final_hr_role",
    # SIGNAL AUDIT (2026-08-08): every flag the site DISPLAYS must be
    # auditable from the archive, or the audit page can only grade half the
    # decorations. These were shown but never kept.
    "pitch_type_match_flag", "pitch_type_match_score", "games_since_last_hr",
    "hidden_hr_value", "high_confidence_hr_flag",
    # ── MULTI-HIT, FINALLY ARCHIVED (2026-08-15) ────────────────────────────
    # Donovan: "multi hit players from the bot, the results need to figure out
    # how that information can help pick."
    #
    # It could not be answered. mlb_dashboard.py computes multi_hit_score,
    # multi_hit_flag and multi_hit_reason on EVERY slate row, and not one of
    # them has ever reached a graded file — 120 fields land in
    # graded_results_*.json and none of them is a multi-hit field. So the
    # bot's own dedicated multi-hit signal has never been backtested and, until
    # this line, could not be.
    #
    # What the archive COULD answer, off scores kept for other reasons, is
    # worth writing down here because it is the reason this matters. On 4,971
    # tracked judgeable hitters the 2+ hit base rate is 25.1%, and sorting by
    # bottom-vs-top quartile: hit_score 19.0% -> 29.8% (+10.8), hrr_score
    # +7.6, contact_score -0.1, hr_score -3.8, and top_board_score_v2 -6.0 —
    # INVERTED, so the TOP pick is the worst place on the board to look for a
    # multi-hit play. The best multi-hit predictor in the archive is currently
    # a score built for something else entirely.
    #
    # These three fields are what let the next sweep ask whether the purpose-
    # built signal beats hit_score at its own job. Scalars and a short string;
    # trim_row's whole point is keeping the payload small and this adds bytes,
    # not kilobytes.
    "multi_hit_score", "multi_hit_flag", "multi_hit_reason",
    # ── THE HR GATE, MADE BACKTESTABLE (2026-08-16) ─────────────────────────
    # Exactly the same shape of gap as multi_hit above, found the same way.
    #
    # A hitter the run designates TOP or HR sometimes also carries an "Avoid
    # for HR" verdict from the decision engine — the two are computed at
    # different levels (best_bet_type per hitter, designation across the whole
    # slate) so neither can see the other. That contradiction is not noise: on
    # the archive those bats homered 18/55 = 32.7% against 124/631 = 19.7% for
    # designated bats without the tag, z = 2.30. The avoid verdict, on a
    # designated hitter, is a BUY signal. Which is why the fix was to change
    # the label and not the selector.
    #
    # mlb_dashboard.py now rewrites best_bet_type to "HR" on those rows and
    # preserves the original in best_bet_type_raw with hr_gate_flagged=True.
    # Neither of those was in this whitelist, so the archive kept only the
    # substituted label with no record a gate had fired -- meaning the 18/55
    # cut above could never have been recomputed from a future extract, and
    # the finding would have been frozen at the day it was measured.
    #
    # Two fields. The flag is a bool and the raw is one of a dozen short
    # strings.
    "best_bet_type_raw", "hr_gate_flagged",
    # DATA-GAP INSTRUMENTATION (2026-08-13): recent_ev/pitcher_ev_allowed/
    # pitcher_hardhit_allowed/last5_runs/last5_rbi/pitcher_whip/pitcher_era
    # all quietly stopped populating for a real stretch (~2026-06-22 onward,
    # some fields still not recovered as of this writing) and nothing caught
    # it for 7+ weeks, because every one of these silently falls back to a
    # plausible-looking default on a failed pull with nothing archived to
    # say so. mlb_dashboard.py already computes (or, for last5/pitcher
    # season-stats, now computes as of this same date) an "ok"/"empty"/
    # "error:<Type>" status right at the pull for every field in that list --
    # it just wasn't being kept past the live slate. These five let a future
    # gap surface from the archive in days, not weeks: watch for any of them
    # drifting away from "ok" across consecutive slates.
    "statcast_pull_status", "pitcher_statcast_status", "pitcher_statcast_bbe",
    "last5_status", "pitcher_season_stats_status",
    # PERSONAL HR SHAPE (2026-08-14, Donovan: categorize each hitter by the
    # KIND of homers he hits -- lasers/moonshots/wall-scrapers -- and whether
    # his recent contact matches HIS OWN shape, "not the overall shape").
    # Archived UNSCORED on purpose: recent_barrel_rate (the generic version
    # of this idea) doesn't predict which night a hitter homers, so the
    # personal version earns its way into scoring from this archive or not
    # at all. After a few weeks, test: do nights with personal_shape_match
    # meaningfully above 0 (recent hard-hit contact landing in his own homer
    # launch-angle window MORE than his season baseline) homer more often,
    # holding hr_score fixed? personal_shape_status gates thin samples
    # ("ok" needs 4+ shaped homers and 5+ recent hard-hit balls).
    "hr_shape_profile", "personal_shape_match", "personal_shape_recent_rate",
    "personal_shape_season_rate", "personal_shape_status",
    # ── YOU CANNOT SWEEP A WEIGHT YOU NEVER RECORDED (2026-08-11) ──────────
    #
    # Found while trying to answer two of Donovan's questions off the archive
    # and discovering neither was answerable, for the same reason: this is a
    # WHITELIST, and anything not named here is dropped by trim_row before the
    # night is written. Three gaps, all silent:
    #
    #   longest_hr_score      0 of 5,766 archived rows. The longest-HR model
    #                         has therefore never been gradeable AT ALL -- and
    #                         now that the backfill records each homer's actual
    #                         distance, it is the one model that could finally
    #                         be scored against ground truth. It just needs to
    #                         start being written down.
    #   hr_due_score          on 1,767 rows from an older writer that bypassed
    #                         trim_row, and absent since. Partial history is
    #                         worse than none, because it looks like a sample.
    #   weather_hr_effect_pct  present on 2,369 rows and ZERO on every single
    #   wind_boost             one of them, while tonight's live slate carries
    #                          -2% to +8%. So no weather question has ever been
    #                          answerable from this archive either.
    #
    # Adding them is additive and costs a few bytes a row. In two or three
    # weeks the questions become answerable; today they are not, and no amount
    # of re-analysis fixes that.
    #
    # NOTE the weather pair may still land as 0 even after this, if the zero
    # originates upstream in the breakdown file rather than here. Check the
    # first graded night after this ships before trusting it -- if it is still
    # 0, the bug is in how the picks file is built, not in this whitelist.
    "longest_hr_score", "hr_due_score",
    "weather_hr_effect_pct", "weather_wind_boost", "wind_boost",
    "park_dist_factor", "weather_label",
    # the lift terms the model selects on -- needed to test whether its
    # moonshot skew (+8.9pp vs the rest of the slate) is weather-fragile
    "l20pa_fb_rate", "l20pa_barrel_rate", "l20pa_ideal_hr_contact",
    "l20pa_hard_hit_rate", "recent_ev", "pitcher_trend_direction",
    # ── MATCHUP AUDIT TRAIL (2026-08-12) ────────────────────────────────
    #
    # Caught live, mid-game: the site showed "WEAK SIDE -- none published"
    # for a pitcher who actually had a real split (1.51 HR/9 vs RHB, 0.60
    # vs LHB). side_weakness() correctly declined to call it -- its own
    # gates (15 BF/side, 12% relative gap, 45 floor) weren't cleared -- but
    # there was no way to check that from the archive, because none of the
    # fields the gate reads were ever kept. Same disease as the block
    # above: a WHITELIST silently drops anything not named here, so "was
    # the weak-side call right" has never been answerable after the fact --
    # only "was there a call at all."
    #
    #   pitcher_hr9_vs_lhb/_vs_rhb   the raw split side_weakness() gates on.
    #   pitcher_weak_side_score     the composite (HR/9 27%, SLG 22%,
    #                                ISO 16%, OPS 13%, BA 10%, BABIP 7%,
    #                                WHIP 5%) that decides which side, if
    #                                any, gets called weak.
    #   pitcher_weak_side_gap       the relative gap between sides -- the
    #                                number the 12%-plus-45-floor gate
    #                                actually tests.
    #
    #   season_pa                   _power_slot()'s eligibility gate is
    #                                PA >= 15. Without this archived, no
    #                                graded slot can be checked against
    #                                that gate after the fact.
    #
    #   beginner_label               the plain-language tier text shown on
    #                                 the card (final_hr_role, the emoji
    #                                 tier, was already archived -- this
    #                                 sibling field wasn't).
    #   damage_conversion_score      the score beginner_label is partly
    #                                 built from.
    #
    # All seven are one-line copies from the slate row, same as every
    # field above. See BOT-DATA-REQUESTS.md, "Live audit, 2026-08-12" for
    # the full case.
    "pitcher_hr9_vs_lhb", "pitcher_hr9_vs_rhb",
    "pitcher_weak_side_score", "pitcher_weak_side_gap",
    "season_pa", "beginner_label", "damage_conversion_score",

    # ── PITCHER BATTED-BALL PROFILE (2026-08-12) ────────────────────────
    #
    # pitcher_gb_rate/pitcher_ld_rate/pitcher_popup_rate now compute for
    # real (mlb_dashboard.py's build_pitcher_statcast_profile reads the
    # same bb_type column pitcher_statcast_fb_rate already used, just adds
    # three more groupby buckets). Archived here so the Pitchers.js caption
    # that's been telling users these publish as 0 can be checked against
    # actual graded values instead of taken on faith.
    "pitcher_gb_rate", "pitcher_ld_rate", "pitcher_popup_rate",
    # ── MODEL FOUNDATION (2026-08-21) ───────────────────────────────────────
    # Which scoring logic (model_version, the HR market's registry version)
    # and which bot execution (run_id) produced this row. Both fields are
    # DEFAULTED ("") on HitterRecord, so a row saved before this change
    # simply carries empty strings here rather than failing to load -- see
    # the no-default-field gotcha documented above load_locked_rows_by_game
    # in mlb_dashboard.py. Without these two names in this whitelist, every
    # future graded row would have its model attribution dropped by
    # trim_row() the same way multi-hit and the HR gate flag were before
    # they were added above -- computed on every slate row, archived on
    # none of them. See bots/model_registry.py, docs/MODELS.md.
    "model_version", "run_id",
    # ── ROADMAP STEP 9B TASK 1 (2026-08-22) ─────────────────────────────────
    # config_hash: provenance already carried on every row (Sol audit #2
    # finding #2's config_hash coverage work) but never whitelisted through
    # to the archive -- same gap model_version/run_id closed above.
    # feature_snapshot / locked_run_id: apply_locked_features()'s own
    # provenance stamp ("locked" | "unavailable") and, when locked, which
    # run_id its overlay came from. Without these two in this whitelist the
    # whole point of task 1 would be silently stripped by trim_row() before
    # it ever reached graded_results_*.json -- exactly the failure mode the
    # comment above model_version/run_id already describes for those two.
    "config_hash", "feature_snapshot", "locked_run_id",
}


def trim_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the fields this script actually reads, dropping heavy
    nested payloads (pitch profiles, zone maps, spray charts, etc.) that
    today_bot.py attaches for the frontend but this script never touches.
    """
    return {k: v for k, v in r.items() if k in SLOT_FIELDS}


def _trim_pair(pair_tuple):
    """Trim both player rows inside a (a, b, score, kind) pair tuple."""
    a, b, score, kind = pair_tuple
    return (trim_row(a), trim_row(b), score, kind)


# What each pick type was actually selected to do. TOP15 is the 15 best
# hr_score hitters on the whole slate; the rest are per-game bests on their
# own board. Grading them all on "did he homer" throws away the doubles and
# multi-hit games the non-HR tiers were chosen for.
DESIGNED_OUTCOME = {
    "TOP15":   "HR",
    "HR":      "HR",
    "HIT":     "1+ hit",
    "HRR":     "2+ hits+runs+RBI",
    "CONTACT": "2+ TB or XBH",
    "TB":      "2+ TB or XBH",
    "TOP":     "most productive of our picks in his game (HRR or TB)",
}


def designed_hit(slot: Dict[str, Any], game_slots: List[Dict[str, Any]]) -> int:
    """1 if this pick did the specific job it was picked for, else 0.

    TOP is relative, not absolute: it's chosen as the single best overall play
    in its game, so the honest test is whether it out-produced the other picks
    from that same game -- on HRR or on total bases. Caveat worth knowing: we
    only see OUR picks, not every hitter in the game, so "most productive"
    means most productive of the ones we tracked.
    """
    pt = str(slot.get("pick_type", "")).upper()
    if pt in ("TOP15", "HR"):
        return 1 if safe_int(slot.get("got_hr")) else 0
    if pt == "HIT":
        return 1 if safe_int(slot.get("got_base_hit")) else 0
    if pt == "HRR":
        return 1 if safe_int(slot.get("hrr_2_plus")) else 0
    if pt in ("CONTACT", "TB"):
        return 1 if (safe_int(slot.get("tb_2_plus"))
                     or safe_int(slot.get("got_xbh"))) else 0
    if pt == "TOP":
        peers = [g for g in game_slots
                 if int(g.get("player_id", -1)) != int(slot.get("player_id", -2))]
        if not peers:
            return 1 if safe_int(slot.get("actual_tb")) else 0
        best_hrr = max(safe_int(g.get("hrr_total")) for g in peers)
        best_tb = max(safe_int(g.get("actual_tb")) for g in peers)
        mine_hrr = safe_int(slot.get("hrr_total"))
        mine_tb = safe_int(slot.get("actual_tb"))
        # Has to actually do something -- leading a game where nobody
        # produced isn't hitting the mark.
        if mine_hrr == 0 and mine_tb == 0:
            return 0
        return 1 if (mine_hrr >= best_hrr or mine_tb >= best_tb) else 0
    return 0


def annotate_designed(graded: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stamp designed_hit / designed_outcome onto every graded slot."""
    by_game: Dict[Any, List[Dict[str, Any]]] = {}
    for g in graded:
        by_game.setdefault(g.get("game_pk"), []).append(g)
    for g in graded:
        g["designed_outcome"] = DESIGNED_OUTCOME.get(
            str(g.get("pick_type", "")).upper(), "")
        g["designed_hit"] = designed_hit(g, by_game.get(g.get("game_pk"), []))
    return graded


def build_tracking_slots(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tracking = []

    top15 = sorted(rows, key=lambda x: safe_float(x.get("hr_score")), reverse=True)[:15]
    for i, r in enumerate(top15, 1):
        tracking.append({**trim_row(r), "pick_type": "TOP15", "rank": i})

    by_game: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        by_game.setdefault(int(r["game_pk"]), []).append(r)

    for _, hitters in by_game.items():
        used = set()

        # MINI-BOT AUDIT FIX (2026-08-08, B9): this loop used to RE-DERIVE
        # the picks (overall_score for TOP, hr_score for HR) — which, after
        # the dashboard's selection changed, meant the tracker was grading a
        # DIFFERENT player than the site published. The published sheet rows
        # carry game_pick_role; the designation on the sheet IS the pick.
        # Re-derivation survives only as the fallback for archive rows that
        # predate the field.
        # TRACKER JOIN FIX (2026-08-22, defect C). The dashboard's TOP/HR
        # double-up (2026-08-12, build_game_pick_role_map) deliberately
        # allows ONE hitter to hold both badges, and CONTACT excludes no
        # one. This loop's `used` set predates that change: it consumed
        # TOP's player and then _designated("HR") skipped him, so in every
        # dual-badge game (94 of 94 measured since 08-12) the board graded a
        # DIFFERENT player than the site's published HR pick -- only 16% of
        # board "HR" rows (14% of "CONTACT") carried that badge on the site,
        # the board understated the real HR designation (19.9% shown vs
        # 20.8% actual) and overstated CONTACT by 16 points (52.9% vs
        # 37.0%). Full evidence:
        # claude/moonshot-hr-pick-correction-and-scoring.md. The 2026-08-08
        # comment below said it plainly the first time: the designation on
        # the sheet IS the pick. `respect_used` now mirrors the dashboard's
        # own exclusion rules exactly: HR and CONTACT may repeat a player
        # who already holds another badge; HIT and HRR still may not.
        def _designated(role: str, respect_used: bool = True):
            for h in hitters:
                if respect_used and int(h.get("player_id") or 0) in used:
                    continue
                roles = str(h.get("game_pick_role") or "").upper().split("/")
                if role in [r.strip() for r in roles]:
                    return h
            return None

        top_pick = _designated("TOP") or pick_top(hitters, "overall_score", 1, used)[0]
        used.add(int(top_pick["player_id"]))
        tracking.append({**trim_row(top_pick), "pick_type": "TOP"})

        hr_pick = _designated("HR", respect_used=False) or (pick_top(hitters, "hr_score", 1, used)[0] if len(hitters) > 1 else top_pick)
        used.add(int(hr_pick["player_id"]))
        tracking.append({**trim_row(hr_pick), "pick_type": "HR"})

        # One per game, matching the game sheet. This was 2, which is why the
        # results board showed 30 HIT and 30 HRR picks against 15 of everything
        # else -- and why the tiers weren't comparable to each other.
        hit_pick = _designated("HIT") or pick_top(hitters, "hit_score", 1, used)[0]
        used.add(int(hit_pick["player_id"]))
        tracking.append({**trim_row(hit_pick), "pick_type": "HIT"})

        hrr_pick = _designated("HRR") or pick_top(hitters, "hrr_score", 1, used)[0]
        used.add(int(hrr_pick["player_id"]))
        tracking.append({**trim_row(hrr_pick), "pick_type": "HRR"})

        contact_pick = _designated("CONTACT", respect_used=False) or (pick_top(hitters, "contact_score", 1, used) or pick_top(hitters, "contact_score", 1))[0]
        tracking.append({**trim_row(contact_pick), "pick_type": "CONTACT"})

    return tracking


# ── MODEL FOUNDATION: append-only outcome log (Task 5) ──────────────────────
#
# Separate from graded_slots/the site's Results payload on purpose: this is
# a model-INDEPENDENT record of what every predicted player-game actually
# did, one line per player-game, keyed so a future prediction log can join
# to it. graded_results_*.json continues completely unchanged -- this is a
# new, additional file, never a replacement.
# ── VERSIONS BUMPED 2026-08-22 ──────────────────────────────────────────────
# schema 1 -> 2: was_substitute, was_replaced, plate_appearances, book_action,
# fair_play_eligible, fair_test_void, fair_test_void_reason.
# grader v1 -> v2: the void RULE changed. A final-game row with fewer than
# MIN_AB_FOR_A_FAIR_TEST at-bats is now void, where v1 graded it as a result.
# That changes what "went_yard: false" means, so a consumer joining old and
# new revisions must be able to tell them apart -- which is the whole reason
# grader_version is stamped on every outcome line.
#
# Old revisions are NOT rewritten. The outcome log is append-only and
# revision-based by design, so history keeps grader v1's answer and every new
# revision carries v2's. Rows graded under v1 simply have no was_substitute /
# was_replaced field, which reads as "unknown", never as False.
OUTCOME_SCHEMA_VERSION = 2
GRADER_VERSION = "mlb_grader_v2"


def outcome_stats_key(row: Dict[str, Any]) -> Tuple:
    """The part of an outcome row that matters for "did anything actually
    change" -- excludes graded_at/outcome_id/revision so a re-grade that
    reproduces the same facts is recognized as a no-op."""
    return (
        row.get("went_yard"), row.get("hits"), row.get("runs"), row.get("rbi"),
        row.get("total_bases"), row.get("at_bats"), row.get("void"), row.get("void_reason"),
        row.get("game_status"),
    )


# Fewer than this many at-bats and the pick never got a fair test -- see the
# void block inside build_outcome_candidates for the reasoning and the
# measured cost of the biased alternative.
MIN_AB_FOR_A_FAIR_TEST = 2


def build_outcome_candidates(
    prediction_date: str,
    rows: List[Dict[str, Any]],
    actual_by_pid: Dict[Tuple[int, int], Dict[str, int]],
    game_status_by_pk: Dict[int, Dict[str, Any]],
    graded_at: str,
) -> List[Dict[str, Any]]:
    """One candidate outcome row per player-game predicted this slate.

    void handling, decided plainly rather than left ambiguous:
      - a game whose status contains "postponed" is void, void_reason
        "postponed" -- it will never go final under this game_pk, so
        without this it would simply never appear in the outcome log.
      - a FINAL game where the player recorded zero of everything
        (AB/H/HR/BB/K) is void, void_reason "did_not_play" -- the
        published prediction row existed, but MLB's boxscore shows no
        plate appearance, i.e. a scratch. This is a proxy (the box score
        alone doesn't say the player was healthy-scratched vs. a defensive
        substitute who never batted); it is stated here rather than left
        implicit.
      - anything else (game in progress / not yet started) is NOT void --
        it is simply not yet a final outcome. It still gets written with
        its current game_status so a partial/live join is possible and
        honest, and a later run with a final box score naturally produces
        a new revision once the game concludes.
    """
    out: List[Dict[str, Any]] = []
    seen_player_games = set()
    for row in rows:
        pid = safe_int(row.get("player_id"), 0)
        game_pk = safe_int(row.get("game_pk"), 0)
        if not pid or not game_pk:
            continue
        key = (game_pk, pid)
        if key in seen_player_games:
            continue
        seen_player_games.add(key)

        status = game_status_by_pk.get(game_pk) or {}
        detailed_state = str(status.get("detailed_state") or "Unknown")
        low = detailed_state.lower()
        is_final = "final" in low
        is_postponed = "postponed" in low or "cancel" in low
        is_suspended = "suspended" in low

        # (game_pk, pid), NOT pid alone -- see Sol audit #2 finding #1. Reuses
        # `key` above, which is already this exact tuple.
        actual = actual_by_pid.get(key) or {}
        hits = safe_int(actual.get("hits"), 0)
        hr = safe_int(actual.get("hr"), 0)
        runs = safe_int(actual.get("runs"), 0)
        rbi = safe_int(actual.get("rbi"), 0)
        tb = safe_int(actual.get("tb"), 0)
        ab = safe_int(actual.get("ab"), 0)
        bb = safe_int(actual.get("bb"), 0)
        k = safe_int(actual.get("k"), 0)
        pa = safe_int(actual.get("pa"), ab + bb)

        # ── SETTLEMENT AND MEASUREMENT ARE DIFFERENT QUESTIONS (2026-08-22) ──
        # An earlier version of this block voided any final-game row with
        # fewer than 2 at-bats, on the stated grounds that "that's how the
        # books do it". Checking the actual rules showed it matched none of
        # them, so this is the correction.
        #
        #   STANDARD BOOK SETTLEMENT. A batter prop has action if the player
        #   is in the starting lineup (DraftKings, BetMGM, Caesars) or gets at
        #   least one plate appearance (FanDuel). A man who starts, bats once
        #   and is pinch-hit for in the 5th HAS ACTION and settles as a LOSS.
        #   Books void for NO plate appearance, not for a short one.
        #
        #   FANATICS FAIR PLAY. A protection layer on top, INJURY-triggered
        #   rather than substitution-triggered: a batter with >=1 PA who
        #   leaves hurt and does not return is refunded -- except that if the
        #   stat line already hit before he left, it pays as a WIN.
        #
        #   MEASURING THE MODEL. Neither. A short appearance must be excluded
        #   in BOTH directions, because keeping the lucky one-AB winners while
        #   dropping the unlucky one-AB losers inflates the measured rate for
        #   free: +1.20pp against +0.83pp on the hit market, measured on
        #   2026-07-27..08-21.
        #
        # So the row carries EVIDENCE and each consumer applies its own rule.
        # Fair Play in particular is per-market -- refund or win depends on
        # whether that market already hit -- so the grader records the
        # precondition and leaves the market call to whoever knows the market.
        #
        # NOT CLAIMED: injury. The boxscore never says why a man left, so a
        # pinch-hitter and a hamstring are indistinguishable.
        # fair_play_eligible is a named PROXY for Fair Play's trigger and it
        # will over-report. No field here ever says "hurt".
        #
        # Worth doing at all because the archive is lopsided: rows with fewer
        # than 2 at-bats run a 3.3% HR rate against 18.6% for everyone else,
        # and 39% for a base hit against 68%. Grading those as misses was
        # scoring the manager's decision, not the model's.
        void = False
        void_reason = None
        if is_postponed:
            void, void_reason = True, "postponed"
        elif is_final and ab == 0 and hits == 0 and hr == 0 and bb == 0 and k == 0:
            # No plate appearance at all -- the one case every book agrees is
            # void, so it stays the canonical `void`.
            void, void_reason = True, "did_not_play"

        book_action = bool(is_final and pa >= 1)
        fair_play_eligible = bool(
            is_final and pa >= 1 and (actual.get("was_replaced") or actual.get("was_substitute")))
        fair_test_void = bool(is_final and ab < MIN_AB_FOR_A_FAIR_TEST and not void)
        if fair_test_void:
            fair_test_reason = ("replaced" if actual.get("was_replaced")
                                else "entered_as_substitute" if actual.get("was_substitute")
                                else "short_appearance")
        else:
            fair_test_reason = None

        # ROLLING-WINDOW DATE FIX (2026-08-21, Sol's audit finding #10).
        # game_date_actual is MLB's own officialDate for this game_pk --
        # authoritative for "which calendar date this game's stats count
        # toward," distinct from prediction_date (when the PICK was made).
        # Falls back to prediction_date when officialDate isn't present on
        # the feed (e.g. a placeholder status dict) -- the same value this
        # row would have carried before this field existed, never a
        # fabricated new one; the two agree for the overwhelming majority
        # of games and only the postponement/suspension case needs the
        # real value at all.
        game_date_actual = status.get("official_date") or prediction_date

        out.append({
            "outcome_schema_version": OUTCOME_SCHEMA_VERSION,
            "prediction_date": prediction_date,
            "game_date_actual": game_date_actual,
            "player_id": pid,
            "game_pk": game_pk,
            "went_yard": bool(hr >= 1),
            "hits": hits,
            "home_runs": hr,
            "runs": runs,
            "rbi": rbi,
            "hrr_total": hits + runs + rbi,
            "total_bases": tb,
            "at_bats": ab,
            "plate_appearances": pa,
            "void": void,
            "void_reason": void_reason,
            "book_action": book_action,
            "fair_play_eligible": fair_play_eligible,
            "fair_test_void": fair_test_void,
            "fair_test_void_reason": fair_test_reason,
            "was_substitute": bool(actual.get("was_substitute")),
            "was_replaced": bool(actual.get("was_replaced")),
            "game_status": detailed_state,
            "is_final": is_final,
            "is_suspended": is_suspended,
            "graded_at": graded_at,
            "grader_version": GRADER_VERSION,
        })
    return out


def load_latest_outcome_revisions(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """{player_game_id: highest-revision row} from a previously-published
    outcome log, so repeated grader runs can tell "nothing changed" from
    "this needs a new revision." Missing/unreadable file -> empty (treated
    as "no prior history," never as an error worth blocking grading)."""
    latest: Dict[str, Dict[str, Any]] = {}
    if not path or not path.exists():
        return latest
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            pgid = obj.get("player_game_id")
            if not pgid:
                continue
            rev = safe_int(obj.get("revision"), 0)
            prior = latest.get(pgid)
            if prior is None or rev >= safe_int(prior.get("revision"), 0):
                latest[pgid] = obj
    except Exception:
        pass
    return latest


def append_outcome_log(
    candidates: List[Dict[str, Any]],
    existing_path: Optional[Path],
) -> Tuple[List[str], int]:
    """Append-safe, auditable revision policy for repeated grader runs:

      - Every existing line in the prior published file is kept verbatim
        (append-only -- a re-grade NEVER rewrites a historical outcome
        record, only adds a new one).
      - A candidate whose stats/void state are IDENTICAL to that
        player-game's latest existing revision (under the same
        grader_version) is skipped -- results.yml runs this grader hourly,
        much of the day is spent re-confirming games that already finished,
        and writing an identical line every hour would make the log grow
        without bound for no informational gain.
      - A candidate that differs (a game just went final, a stat
        correction landed, void status changed) becomes a NEW revision:
        outcome_id = "{game_pk}|{player_id}|r{revision}", with `supersedes`
        pointing at the outcome_id it replaces. The previous revision is
        never edited or removed.

    Returns (all_lines_to_write, appended_count).
    """
    existing_lines: List[str] = []
    if existing_path and existing_path.exists():
        try:
            existing_lines = [
                ln for ln in existing_path.read_text(encoding="utf-8").splitlines() if ln.strip()
            ]
        except Exception:
            existing_lines = []

    latest_by_pgid = load_latest_outcome_revisions(existing_path)

    new_lines: List[str] = []
    appended = 0
    for cand in candidates:
        pgid = f"{cand['game_pk']}|{cand['player_id']}"
        prior = latest_by_pgid.get(pgid)
        if prior is not None and outcome_stats_key(prior) == outcome_stats_key(cand) \
                and prior.get("grader_version") == cand.get("grader_version"):
            continue  # no real difference -- append-safe no-op
        revision = safe_int((prior or {}).get("revision"), 0) + 1
        row = dict(cand)
        row["player_game_id"] = pgid
        row["revision"] = revision
        row["outcome_id"] = f"{pgid}|r{revision}"
        row["supersedes"] = (prior or {}).get("outcome_id")
        new_lines.append(json.dumps(row, default=str))
        appended += 1
        latest_by_pgid[pgid] = row  # so a later candidate this same run compares against it

    return existing_lines + new_lines, appended


def write_outcome_log(
    prediction_date: str,
    rows: List[Dict[str, Any]],
    actual_by_pid: Dict[Tuple[int, int], Dict[str, int]],
    game_status_by_pk: Dict[int, Dict[str, Any]],
    graded_at: str,
    fetch_dir: Path,
    publish_dir: Path,
) -> Optional[Path]:
    """Build this run's outcome candidates, merge them append-safe against
    whatever was already published for this date, and write the result.
    Best-effort: an outcome-log failure must never block or corrupt the
    graded results the rest of main() already produced."""
    try:
        candidates = build_outcome_candidates(prediction_date, rows, actual_by_pid, game_status_by_pk, graded_at)
        if not candidates:
            return None
        existing_path = fetch_dir / f"outcome_log_{prediction_date}.jsonl"
        all_lines, appended = append_outcome_log(candidates, existing_path)
        publish_dir.mkdir(parents=True, exist_ok=True)
        dest = publish_dir / f"outcome_log_{prediction_date}.jsonl"
        dest.write_text("\n".join(all_lines) + ("\n" if all_lines else ""), encoding="utf-8")
        print(f"outcome log: {appended} new revision(s), {len(all_lines)} total line(s) -> {dest}")
        return dest
    except Exception as exc:
        print(f"outcome log write failed: {exc}")
        return None


# SUSPENDED-GAME FIX (2026-08-21, Sol's audit finding #4). resolve_grade_date()
# picks exactly ONE date per run -- today, or (only if today has no breakdown
# published yet) the most recent day that does. Once tomorrow's slate exists,
# that fallback never fires for today again, so a game suspended on day D and
# resumed on D+2 sat in outcome_log_D.jsonl with is_final=False forever --
# nothing in the pipeline ever went back to look at it. This revisits every
# recent date whose latest-logged outcome for some player-game is still
# neither final nor void, and re-fetches its CURRENT live status. A game
# that's still suspended produces no new revision (append_outcome_log's
# existing no-op-if-unchanged check already handles that) -- only a game that
# has genuinely resolved since the last check produces one, so this is safe
# to run every single hourly invocation.
def find_unresolved_outcome_dates(
    fetch_dir: Path, today: dt.date, lookback_days: int = 14,
) -> Dict[str, List[Dict[str, Any]]]:
    """{date_str: [{'player_id':.., 'game_pk':..}, ...]} for every date in the
    last `lookback_days` (today excluded -- that date's own grading run
    already covers it) whose PUBLISHED outcome log's latest revision per
    player-game still shows is_final=False and void=False.

    Deliberately reads only outcome_log_<date>.jsonl, never the original
    slate's breakdown file -- the breakdown file isn't retained anywhere past
    its own day in this pipeline (bots/outputs/ is a fresh, empty directory
    on every CI run), but the outcome log already carries both keys a
    candidate needs (player_id, game_pk), so reconstructing the minimal row
    from it avoids a dependency this fix would otherwise have no way to
    satisfy. A date whose outcome_log isn't present locally is skipped, not
    guessed at -- restoring recent outcome logs before calling this is the
    workflow's job (results.yml), the same way it already restores
    graded_results_*.* for the backtest report.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for back in range(1, lookback_days + 1):
        date_str = (today - dt.timedelta(days=back)).strftime("%Y-%m-%d")
        path = fetch_dir / f"outcome_log_{date_str}.jsonl"
        if not path.exists():
            continue
        latest = load_latest_outcome_revisions(path)
        unresolved = [
            {"player_id": row.get("player_id"), "game_pk": row.get("game_pk")}
            for row in latest.values()
            if not row.get("is_final") and not row.get("void")
        ]
        if unresolved:
            out[date_str] = unresolved
            # OBSERVABILITY FIX (2026-08-21, cleanup pass after Sol audit #2's
            # lower-severity list): a suspended game unresolved past this
            # window used to just silently stop being rechecked, forever, with
            # nothing anywhere saying so -- a documented trade-off, but a
            # trade-off nobody could actually see happen. back == lookback_days
            # is the OLDEST date this call looks at; if it's still unresolved
            # here, this is the LAST run that will ever revisit it (next
            # run's `back` for this same date_str would be lookback_days + 1,
            # outside range() entirely, so find_unresolved_outcome_dates would
            # simply never look at this date again). Loud now, rather than a
            # permanently-stuck is_final=False nobody notices.
            if back == lookback_days:
                stuck_game_pks = sorted({u["game_pk"] for u in unresolved})
                print(f"⚠️ regrade-stale: {date_str} has {len(unresolved)} still-open "
                      f"player-game(s) at the EDGE of the {lookback_days}-day lookback window "
                      f"(game_pks: {stuck_game_pks}) -- if still unresolved after this run, "
                      f"they will never be automatically rechecked again.")
    return out


def regrade_stale_dates(
    fetch_dir: Path, publish_dir: Path, today: dt.date, graded_at: str,
    lookback_days: int = 14,
) -> None:
    """Re-check every date find_unresolved_outcome_dates() flags, using a
    fresh live fetch per still-open game_pk, and append any resulting
    revision through the exact same write_outcome_log() path today's own
    grading uses -- so a suspended game that has since finished shows up as
    final the next time this runs, with no separate code path to trust."""
    stale = find_unresolved_outcome_dates(fetch_dir, today, lookback_days)
    if not stale:
        print("regrade-stale: no unresolved outcomes in the lookback window.")
        return
    for date_str, pending in sorted(stale.items()):
        # PER-ITEM ERROR ISOLATION (2026-08-21, Sol audit #2 finding #6): both
        # loops below are wrapped so one bad game_pk (a stale ID the live API
        # 404s on, a transient 5xx) can't block every other game on this date,
        # or -- since dates are processed oldest-first -- every later stale
        # date on this same run. Before this fix an unhandled fetch error
        # propagated straight out of this function, silently skipping the
        # whole rest of the queue every single hourly invocation, which
        # contradicted this function's own "safe to run forever" design
        # intent (see its docstring above).
        try:
            print(f"regrade-stale: {date_str} has {len(pending)} still-open player-game(s) -- rechecking live status.")
            game_cache: Dict[int, Dict[str, Any]] = {}
            game_status_by_pk: Dict[int, Dict[str, Any]] = {}
            failed_game_pks: set = set()
            # Same (game_pk, player_id) keying as main()'s grading loop -- see
            # the comment there (Sol audit #2 finding #1). A player still
            # unresolved in both legs of a suspended doubleheader on the same
            # recheck pass would otherwise collide here too.
            actual_by_pid: Dict[Tuple[int, int], Dict[str, int]] = {}
            rows: List[Dict[str, Any]] = []
            for item in pending:
                pid = safe_int(item.get("player_id"), 0)
                game_pk = safe_int(item.get("game_pk"), 0)
                if not pid or not game_pk or game_pk in failed_game_pks:
                    continue
                if game_pk not in game_cache:
                    try:
                        game_cache[game_pk] = fetch_game_feed(game_pk)
                        game_status_by_pk[game_pk] = get_game_status(game_cache[game_pk])
                    except Exception as exc:
                        failed_game_pks.add(game_pk)
                        print(f"regrade-stale: {date_str} game_pk {game_pk} fetch failed ({exc}) -- "
                              f"skipping this game, continuing with the rest of the queue.")
                        continue
                # ISOLATION FIX ROUND 2 (2026-08-21, quick review of finding #6's
                # fix): get_player_batting_line() used to sit outside this
                # try/except -- if it ever raised (it's defensive today, all
                # .get()-based, but that's not a guarantee), the exception would
                # propagate to the outer per-date handler and abort every other
                # still-pending game on this date too, not just this one,
                # contradicting the isolation this fix exists to provide.
                try:
                    line = get_player_batting_line(game_cache[game_pk], pid)
                except Exception as exc:
                    failed_game_pks.add(game_pk)
                    print(f"regrade-stale: {date_str} game_pk {game_pk} batting-line lookup failed ({exc}) -- "
                          f"skipping this game, continuing with the rest of the queue.")
                    continue
                rows.append({"player_id": pid, "game_pk": game_pk})
                actual_by_pid[(game_pk, pid)] = line
            if not rows:
                continue
            write_outcome_log(
                prediction_date=date_str,
                rows=rows,
                actual_by_pid=actual_by_pid,
                game_status_by_pk=game_status_by_pk,
                graded_at=graded_at,
                fetch_dir=fetch_dir,
                publish_dir=publish_dir,
            )
        except Exception as exc:
            print(f"regrade-stale: {date_str} failed ({exc}) -- skipping this date, "
                  f"continuing with the rest of the queue.")
            continue


def grade_slot(slot: Dict[str, Any], actual: Dict[str, Any]) -> Dict[str, Any]:
    hrr_total = actual["hits"] + actual["runs"] + actual["rbi"]
    graded = {
        **slot,
        # An extra-base hit is always 2+ total bases, but two singles are 2 TB
        # with no XBH -- so these are genuinely different questions and the
        # report was answering the first one twice.
        "tb_2_plus": 1 if actual["tb"] >= 2 else 0,
        "tb_3_plus": 1 if actual["tb"] >= 3 else 0,
        "actual_hits": actual["hits"],
        "actual_hr": actual["hr"],
        "actual_runs": actual["runs"],
        "actual_rbi": actual["rbi"],
        "actual_tb": actual["tb"],
        "actual_ab": actual["ab"],
        "actual_k": actual.get("k", 0),
        "actual_bb": actual.get("bb", 0),
        "actual_doubles": actual.get("doubles", 0),
        "actual_triples": actual.get("triples", 0),
        "got_base_hit": 1 if actual["hits"] >= 1 else 0,
        "got_hr": 1 if actual["hr"] >= 1 else 0,
        # Singles are worth exactly one base each, so total bases only
        # exceed the hit count when at least one hit went for extra bases.
        # This used to read `tb >= 2`, which made got_xbh an exact duplicate
        # of tb_2_plus and credited anyone with two singles an XBH.
        "got_xbh": 1 if actual["tb"] > actual["hits"] else 0,
        "hrr_total": hrr_total,
        "hrr_2_plus": 1 if hrr_total >= 2 else 0,
        "hrr_3_plus": 1 if hrr_total >= 3 else 0,
    }
    # ── THE SAME THREE FLAGS, ON THE SITE/BACKTEST PATH (2026-08-22) ──────
    # build_outcome_candidates carries book_action / fair_play_eligible /
    # fair_test_void on the outcome log. Mirroring them here keeps
    # graded_results_*.json -- what backtest_report.py, missed_signals.py and
    # the Results tab read -- from disagreeing with it.
    #
    # fair_test_void is the RESEARCH flag and is symmetric: a one-AB home run
    # is excluded too, because keeping the lucky winners while dropping the
    # unlucky losers inflates the measured rate. That is NOT how a book
    # settles, which is exactly why it is a separate field from book_action.
    #
    # Rows are MARKED, never dropped. A deleted row looks like a pick that was
    # never made, and the count is worth seeing: six on a night means the card
    # got dismantled by substitutions.
    _ab = safe_int(actual.get("ab"), 0)
    _pa = safe_int(actual.get("pa"), _ab + safe_int(actual.get("bb"), 0))
    graded["plate_appearances"] = _pa
    graded["was_replaced"] = bool(actual.get("was_replaced"))
    graded["was_substitute"] = bool(actual.get("was_substitute"))
    graded["book_action"] = 1 if _pa >= 1 else 0
    graded["fair_play_eligible"] = 1 if (_pa >= 1 and (actual.get("was_replaced")
                                                       or actual.get("was_substitute"))) else 0
    if _ab < MIN_AB_FOR_A_FAIR_TEST:
        graded["fair_test_void"] = 1
        graded["fair_test_void_reason"] = ("replaced" if actual.get("was_replaced")
                                           else "entered_as_substitute" if actual.get("was_substitute")
                                           else "short_appearance")
    else:
        graded["fair_test_void"] = 0
        graded["fair_test_void_reason"] = None
    # JOB 1: per-homer batted-ball capture (2026-08-11)
    # Include homer events if present in actual
    if actual.get("hr_events"):
        graded["hr_events"] = actual["hr_events"]
    return graded


def merge_homer_entries(graded_slots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[int, Dict[str, Any]] = {}
    emoji_order = {"🏆": 0, "🧨": 1, "🔥": 2, "🏁": 3, "💠": 4, "⚾": 5, "⭐": 6}

    for r in graded_slots:
        if int(r.get("got_hr", 0)) != 1:
            continue
        pid = int(r["player_id"])
        if pid not in merged:
            merged[pid] = {
                "player_id": pid,
                "name": r["name"],
                "team": r["team"],
                "tags": [],
                "base_row": r,
            }
        if r["pick_type"] == "TOP15":
            merged[pid]["tags"].append(f"🏆#{r['rank']}")
        elif r["pick_type"] == "HR":
            merged[pid]["tags"].append("🧨")
        elif r["pick_type"] == "TOP":
            merged[pid]["tags"].append("🔥")
        elif r["pick_type"] == "HRR":
            merged[pid]["tags"].append("🏁")
        elif r["pick_type"] == "HIT":
            merged[pid]["tags"].append("💠")
        elif r["pick_type"] == "CONTACT":
            merged[pid]["tags"].append("⚾")
        if r.get("weak_spot_flag"):
            merged[pid]["tags"].append("⭐")

        existing = merged[pid]["base_row"]
        if safe_float(r.get("hr_score")) > safe_float(existing.get("hr_score")):
            merged[pid]["base_row"] = r

    def unique_tags(tags: List[str]) -> List[str]:
        seen = set()
        out = []
        for tag in tags:
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
        out.sort(key=lambda x: emoji_order.get(x[0], 99))
        return out

    out = []
    for item in merged.values():
        item["tags"] = unique_tags(item["tags"])
        out.append(item)

    def sort_key(item: Dict[str, Any]):
        first = item["tags"][0] if item["tags"] else ""
        return (emoji_order.get(first[0], 99), item["name"])

    return sorted(out, key=sort_key)


def merge_homer_distances(merged_homers: List[Dict[str, Any]], hr_capture_report: Dict[str, Any]) -> None:
    """Fold per-play distance/exit-velo data from the capture report's raw
    homer entries onto merge_homer_entries()'s per-player rows, in place --
    the app reads the merged rows, and "who hit it farthest tonight" is a
    question about the board, not the box score.

    DOUBLEHEADER KEYING FIX (2026-08-21, quick review of finding #1's fix):
    extracted from main() specifically so this is independently testable.
    The lookup dict used to be keyed by player_id alone, the same bug class
    as actual_by_pid -- a player who homered in both legs of a split
    doubleheader would have one leg's distance/exit-velo silently overwrite
    the other's. all_homer_entries already carries game_pk (stamped at
    build_hr_capture_report()'s own game_cache loop), and each merged row's
    base_row (a graded_slot row, itself built from SLOT_FIELDS, which
    includes game_pk) tells us which game that row's numbers came from -- so
    key and look up by (game_pk, player_id) to match the right leg."""
    dist_by_pid = {
        (safe_int(h.get("game_pk"), 0), safe_int(h.get("player_id"), 0)): h
        for h in (hr_capture_report.get("all_homer_entries") or [])
    }
    for m in merged_homers:
        m_gp = safe_int((m.get("base_row") or {}).get("game_pk"), 0)
        src = dist_by_pid.get((m_gp, safe_int(m.get("player_id"), 0))) or {}
        for k in ("longest_ft", "distances_ft", "max_ev_mph", "launch_angle"):
            if src.get(k) is not None:
                m[k] = src[k]


def format_stat_line(r: Dict[str, Any]) -> str:
    pitcher_throws = r.get("pitcher_throws", "")
    split_avg = safe_float(r.get("avg_vs_lhp")) if pitcher_throws == "L" else safe_float(r.get("avg_vs_rhp"))
    split_side = "LHP" if pitcher_throws == "L" else "RHP"
    return (
        f"  BA(S) {safe_float(r.get('season_avg')):.3f} | "
        f"BA vs {split_side} {split_avg:.3f} | "
        f"ISO {safe_float(r.get('season_iso')):.3f} | "
        f"350+ {safe_int(r.get('recent_350_num'))}/{max(1, safe_int(r.get('recent_350_den'), 1))} | "
        f"L5 {safe_int(r.get('last5_hits'))}H/{safe_int(r.get('last5_hr'))}HR/{safe_int(r.get('last5_xbh'))}XBH"
    )


def load_pair_builder_sections(date_str: str):
    """ALIGNMENT (2026-08-07): grade the PUBLISHED pair-builder tickets.

    The site's Pairs and Pools pages render pair_builder_latest.json, but this
    grader used to build its own pair/pool sections from the slate rows — a
    different generator, so "Live pools" and "Tonight's pools" showed
    different tickets on the same page. When the published file is present
    (fetch_picks_for_grading.py now downloads it in CI) and is for the same
    slate date, convert it into the sections shape grade_pairs_pools expects
    and grade THOSE. Missing/stale file falls back to the internal builder,
    so a broken publish never blanks live grading.

    ALIGNMENT PT.2 (2026-08-12): "latest" is a single shared file that the
    NEXT slate's run overwrites, so a grading run any time after a newer
    slate has already generated found "latest" stamped with the wrong date
    and gave up straight to the internal builder — a THIRD, different pair
    generator (see build_pair_pool_sections) — even though
    mlb_dashboard.py has been writing a dated copy right next to "latest"
    (mlb_pair_builder_{date}.json) the whole time. Try that before giving up.
    """
    def _load(p: Path):
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None   # shape guard, see bots/check_shapes.py

    pb = _load(OUT_DIR / "pair_builder_latest.json")
    if not pb or str(pb.get("date") or "") != str(date_str):
        dated = _load(OUT_DIR / f"mlb_pair_builder_{date_str}.json")
        if dated and str(dated.get("date") or "") == str(date_str):
            pb = dated
        else:
            print(f"pair_builder_latest is for {(pb or {}).get('date')}, no dated archive for {date_str} either — using internal pools instead")
            return None
    by_lane: Dict[str, list] = {}
    for rp in (pb.get("recommended_pairs") or []):
        ps = rp.get("players") or []
        if len(ps) != 2 or not ps[0].get("player_id") or not ps[1].get("player_id"):
            continue
        # lane_key first (audit B15): 'type' is derived from an emoji display
        # title — renaming a lane would fork its graded history.
        lane = str(rp.get("lane_key") or rp.get("type") or "Pairs")
        by_lane.setdefault(lane, []).append(
            (ps[0], ps[1], safe_float(rp.get("pair_score")), lane)
        )
    pair_groups = [{"label": lane, "pairs": pairs} for lane, pairs in by_lane.items()]
    pools = []
    # pools_3man added 2026-08-12 -- the retired 6-man's actual replacement
    # key (see mlb_dashboard.py's build_pair_sections). pools_6man kept for
    # any older dated archive file that still carries real 6-man data from
    # before 2026-08-09.
    for key, prefix in (("pools_4man", "4-MAN"), ("pools_3man", "3-MAN"), ("pools_6man", "6-MAN")):
        for pl in (pb.get(key) or []):
            players = [x for x in (pl.get("players") or []) if x.get("player_id")]
            if not players:
                continue
            nm = str(pl.get("name") or pl.get("label") or "POOL")
            pools.append({"label": f"{prefix} {nm}", "players": players})
    if not pair_groups and not pools:
        return None
    print(f"Grading PUBLISHED pair-builder tickets: {len(pair_groups)} pair lanes, {len(pools)} pools — same tickets the site shows")
    return {"pair_groups": pair_groups, "pools": pools}


def grade_pairs_pools(sections: Dict[str, Any], actual_by_pid: Dict[Tuple[int, int], Dict[str, int]]) -> Dict[str, Any]:
    # DOUBLEHEADER GRADING FIX (2026-08-21, Sol audit #2 finding #1): looks up
    # by (game_pk, player_id), matching how actual_by_pid is now keyed
    # everywhere else. Both pair/pool player-dict sources (_s2_player_dict in
    # mlb_dashboard.py, and trim_row()'s SLOT_FIELDS here) carry game_pk, so
    # this is always available; falls back to {} (never a crash or a
    # cross-game collision) if a player dict somehow lacks it.
    def _line_for(p: Dict[str, Any]) -> Dict[str, int]:
        gp = p.get("game_pk")
        if gp is None:
            return {}
        return actual_by_pid.get((int(gp), int(p["player_id"]))) or {}

    graded_pair_groups = []
    all_pairs = []
    cleared_pairs = []

    for group in sections["pair_groups"]:
        graded_pairs = []
        for a, b, score, label in group["pairs"]:
            a_hr = safe_int(_line_for(a).get("hr"), 0)
            b_hr = safe_int(_line_for(b).get("hr"), 0)
            homer_names = []
            if a_hr >= 1:
                homer_names.append(a["name"])
            if b_hr >= 1:
                homer_names.append(b["name"])
            hr_count = len(homer_names)
            hit = 1 if hr_count == 2 else 0
            pair_entry = {
                "label": label,
                "a": a,
                "b": b,
                "score": round(score, 1),
                "cleared": hit,
                "hr_count": hr_count,
                "total_count": 2,
                "homer_names": homer_names,
                "a_hr": a_hr,
                "b_hr": b_hr,
            }
            graded_pairs.append(pair_entry)
            all_pairs.append(pair_entry)
            if hit:
                cleared_pairs.append(pair_entry)
        graded_pair_groups.append({"label": group["label"], "pairs": graded_pairs})

    graded_pools = []
    cleared_pools = []
    pool4 = []
    pool6 = []
    for pool in sections["pools"]:
        players = pool["players"]
        # MINI-BOT AUDIT (2026-08-08, B5+B6): a leg whose player never got an
        # AB is VOIDED (ticket shrinks), matching bar_cleared's rule — a bet
        # that never existed isn't a miss. And all-or-nothing "cleared" fired
        # 0 times in 192 archived pools, so the ladder metrics (≥1, ≥2 HR)
        # ride alongside it — those actually vary night to night.
        void_names = []
        active = []
        for p in players:
            line = _line_for(p)
            if safe_int(line.get("ab"), 0) == 0 and safe_int(line.get("hr"), 0) == 0 and line:
                void_names.append(p["name"])
            else:
                active.append(p)
        homer_names = [p["name"] for p in active if safe_int(_line_for(p).get("hr"), 0) >= 1]
        hr_count = len(homer_names)
        total_count = len(active)
        cleared = 1 if total_count > 0 and hr_count == total_count else 0
        # ─── THE BAR IS NOW REACHABLE (2026-08-09) ─────────────────────────
        # Donovan: "maybe break down two pools of 3-4 and pick the best lines
        # that went or convert... I was using it as another pool of players to
        # use since a decent portion did go every night."
        #
        # Joining every pool leg to its graded row across 40 nights showed he
        # was reading it right and the GRADE was reading it wrong:
        #
        #     4-MAN  0 went 51.2% | 1 went 35.0% | 2 went 13.1% | 3 went 0.6%
        #     6-MAN  0 went 46.2% | 1 went 37.5% | 2 went 11.9% | 3+ went 4.4%
        #
        # A pool leg homers 17.5% against a 14.9% slate baseline, and at least
        # one lands about half of all nights. The selection works. But
        # all-or-nothing "cleared" went 0-for-320, so the only number anyone
        # ever saw said the product had never once worked. That is not a
        # measurement of the pools, it is a measurement of an impossible bar.
        #
        # So `primary` is the headline the site reads: 1+ homered for a 3- or
        # 4-man, 2+ for anything larger. `cleared` keeps its old all-or-nothing
        # meaning so nothing downstream silently changes its mind, and
        # `bar_label` carries the wording so the site never has to guess.
        size = total_count or len(players)
        need = 1 if size <= 4 else 2
        primary = 1 if hr_count >= need else 0
        entry = {
            "label": pool["label"],
            "players": players,
            "cleared": cleared,
            "primary": primary,
            "bar": need,
            "bar_label": f"{need}+ of {size}",
            "hr_count": hr_count,
            "total_count": total_count,
            "homer_names": homer_names,
            "void_names": void_names,
            "hit_any": 1 if hr_count >= 1 else 0,
            "hit_2plus": 1 if hr_count >= 2 else 0,
            "hit_3plus": 1 if hr_count >= 3 else 0,
        }
        graded_pools.append(entry)
        if pool["label"].startswith("4-MAN"):
            pool4.append(entry)
        elif pool["label"].startswith(("3-MAN", "6-MAN")):
            # 6-MAN was retired 2026-08-09 and split into two 3-mans. The key
            # stays `pool6` so an older payload and a newer one land in the
            # same place for anything reading the archive.
            pool6.append(entry)
        if cleared:
            cleared_pools.append(entry)

    return {
        "graded_pair_groups": graded_pair_groups,
        "all_pairs": all_pairs,
        "cleared_pairs": cleared_pairs,
        "graded_pools": graded_pools,
        "cleared_pools": cleared_pools,
        "pool4": pool4,
        "pool6": pool6,
    }


def wrap_text_to_width(text: str, font_name: str, font_size: int, max_width: float) -> List[str]:
    if not text:
        return [""]
    words = text.split(" ")
    lines: List[str] = []
    current = words[0]
    for word in words[1:]:
        trial = current + " " + word
        if stringWidth(trial, font_name, font_size) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def save_text_as_pdf(text: str, pdf_path: Path, title: str) -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    width, height = letter

    margin_left = 42
    margin_right = 42
    margin_top = 42
    margin_bottom = 42
    usable_width = width - margin_left - margin_right

    body_font = "Courier"
    body_size = 8
    body_leading = 11

    def draw_header(page_no: int):
        c.setFont("Helvetica-Bold", 13)
        c.drawString(margin_left, height - 28, title)
        c.setFont("Helvetica", 8)
        c.drawRightString(width - margin_right, height - 28, f"Page {page_no}")
        c.setLineWidth(0.5)
        c.line(margin_left, height - 34, width - margin_right, height - 34)

    page_no = 1
    y = height - margin_top - 10
    draw_header(page_no)
    y -= 10

    for raw_line in text.splitlines():
        wrapped = [""] if raw_line.strip() == "" else wrap_text_to_width(raw_line, body_font, body_size, usable_width)
        for line in wrapped:
            if y < margin_bottom + body_leading:
                c.showPage()
                page_no += 1
                y = height - margin_top - 10
                draw_header(page_no)
                y -= 10
            c.setFont(body_font, body_size)
            c.drawString(margin_left, y, line)
            y -= body_leading
    c.save()



def category_display(pick_type: str) -> str:
    return {
        "TOP15": "🏆 TOP 15 BOARD",
        "TOP": "🔥 TOP PICKS",
        "HR": "🧨 HR PICKS",
        "HRR": "🏁 HRR PICKS",
        "HIT": "💠 HIT PICKS",
        "CONTACT": "⚾ CONTACT PICKS",
    }.get(pick_type, pick_type)


def hit_marker(actual_hits: int) -> str:
    if actual_hits >= 3:
        return " 🔥"
    if actual_hits >= 2:
        return " ⭐"
    return ""


def build_hr_category_counts(graded_slots: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"TOP15": 0, "TOP": 0, "HR": 0, "HRR": 0, "HIT": 0, "CONTACT": 0}
    for r in graded_slots:
        pt = str(r.get("pick_type", ""))
        if pt in counts and safe_int(r.get("got_hr"), 0) >= 1:
            counts[pt] += 1
    return counts


def build_hit_results_by_category(graded_slots: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped = {"TOP": [], "HR": [], "HRR": [], "HIT": [], "CONTACT": []}
    for r in graded_slots:
        pt = str(r.get("pick_type", ""))
        if pt in grouped and safe_int(r.get("actual_hits"), 0) >= 1:
            grouped[pt].append(r)
    for pt in grouped:
        grouped[pt] = sorted(grouped[pt], key=lambda x: (-safe_int(x.get("actual_hits")), str(x.get("name", ""))))
    return grouped


def build_missed_hr_reason(missed: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    pid = safe_int(missed.get("player_id"), 0)
    candidates = [r for r in rows if safe_int(r.get("player_id"), 0) == pid]
    if not candidates:
        return "not in model sheet / lineup pool"
    r = candidates[0]
    hr_score = safe_float(r.get("hr_score"), 0.0)
    recent_350 = safe_int(r.get("recent_350_num"), 0)
    recent_den = max(1, safe_int(r.get("recent_350_den"), 1))
    iso = safe_float(r.get("season_iso"), 0.0)
    reasons = []
    if hr_score < 25:
        reasons.append("low HR score")
    if (recent_350 / recent_den) < 0.10:
        reasons.append("low 350+ signal")
    if iso < 0.140:
        reasons.append("low ISO")
    if not reasons:
        reasons.append("outside top range")
    return ", ".join(reasons)


def build_summary_text(
    date_str: str,
    graded_slots: List[Dict[str, Any]],
    merged_homers: List[Dict[str, Any]],
    pair_pool_results: Dict[str, Any],
    hr_capture_report: Optional[Dict[str, Any]] = None,
    unique_player_report: Optional[Dict[str, Any]] = None,
    live_mode: bool = False,
) -> str:
    top15 = [r for r in graded_slots if r["pick_type"] == "TOP15"]
    hr_picks = [r for r in graded_slots if r["pick_type"] == "HR"]
    top_picks = [r for r in graded_slots if r["pick_type"] == "TOP"]
    hrr_picks = [r for r in graded_slots if r["pick_type"] == "HRR"]
    hit_picks = [r for r in graded_slots if r["pick_type"] == "HIT"]
    contact_picks = [r for r in graded_slots if r["pick_type"] == "CONTACT"]

    final_games = len({int(r["game_pk"]) for r in graded_slots if int(r.get("is_final", 0)) == 1})
    total_games = len({int(r["game_pk"]) for r in graded_slots})
    title = f"LIVE RESULTS TRACKER - {date_str}" if live_mode else f"RESULTS TRACKER - {date_str}"
    lines = [title, "-" * 42]
    if live_mode:
        lines.append(f"Game status: {final_games}/{total_games} graded games final")
        lines.append("Note: live results can change until every game is final.")
        lines.append("")

    lines.append("BETTABLE RESULTS")
    lines.append(f"Top 15 HR: {sum(safe_int(r.get('got_hr')) for r in top15)}/{len(top15)} ({pct(top15, 'got_hr')}%)")
    lines.append(f"HR Picks: {sum(safe_int(r.get('got_hr')) for r in hr_picks)}/{len(hr_picks)} ({pct(hr_picks, 'got_hr')}%)")
    lines.append(f"Top Picks: {sum(safe_int(r.get('got_hr')) for r in top_picks)}/{len(top_picks)} ({pct(top_picks, 'got_hr')}%)")

    lines.append("")
    lines.append("FULL SHEET BASE HIT PERFORMANCE")
    lines.append(f"Base Hit Accuracy: {pct(graded_slots, 'got_base_hit')}%")

    if hr_capture_report:
        total_hrs = safe_int(hr_capture_report.get("total_hrs_on_slate"))
        top15_hr = sum(safe_int(r.get("got_hr")) for r in top15)
        hr_pick_hr = sum(safe_int(r.get("got_hr")) for r in hr_picks)
        lines.append("")
        lines.append("HR CAPTURE (BETTABLE)")
        lines.append(f"Total Slate HRs: {total_hrs}")
        lines.append(f"Caught in Top 15: {top15_hr}")
        lines.append(f"Caught in HR Picks: {hr_pick_hr}")
        if total_hrs:
            lines.append(f"Top 15 Capture Rate: {round(100 * top15_hr / total_hrs, 1)}%")
            lines.append(f"HR Pick Capture Rate: {round(100 * hr_pick_hr / total_hrs, 1)}%")

        all_rows = []
        seen = set()
        for r in graded_slots:
            pid = safe_int(r.get("player_id"))
            if pid and pid not in seen:
                all_rows.append(r)
                seen.add(pid)
        top40_ids = {safe_int(r.get("player_id")) for r in sorted(all_rows, key=lambda x: safe_float(x.get("hr_score")), reverse=True)[:40]}
        all_homers = hr_capture_report.get("all_homer_entries", []) or []
        vision_caught = sum(safe_int(h.get("hr")) for h in all_homers if safe_int(h.get("player_id")) in top40_ids)
        lines.append("")
        lines.append("MODEL VISION (Top 40)")
        lines.append(f"Model Caught HRs: {vision_caught} / {total_hrs}")
        lines.append(f"Vision Rate: {round(100 * vision_caught / total_hrs, 1) if total_hrs else 0.0}%")

    if unique_player_report:
        lines.append("")
        lines.append("UNIQUE PLAYER HR ACCURACY")
        lines.append(f"Unique Players Tracked: {safe_int(unique_player_report.get('unique_players_tracked'))}")
        lines.append(f"Players Who Homered: {safe_int(unique_player_report.get('unique_players_with_hr'))}")
        lines.append(f"Unique HR Accuracy: {safe_float(unique_player_report.get('unique_hr_accuracy_pct')):.1f}%")

    lines.append("")
    lines.append("HR CATEGORY BREAKDOWN")
    lines.append("-" * 42)
    cat_counts = build_hr_category_counts(graded_slots)
    for pt in ["TOP15", "TOP", "HR", "HRR", "HIT", "CONTACT"]:
        lines.append(f"{category_display(pt)} → {cat_counts.get(pt, 0)} HR")
    best_pt = max(cat_counts, key=lambda k: cat_counts.get(k, 0)) if cat_counts else ""
    if best_pt:
        lines.append(f"Best HR-producing category: {category_display(best_pt)}")

    _longest = [m for m in (merged_homers or []) if m.get("longest_ft")]
    if _longest:
        _top = max(_longest, key=lambda m: safe_float(m.get("longest_ft"), 0.0))
        lines.append("")
        lines.append("LONGEST HR (BOARD)")
        lines.append("-" * 42)
        for m in sorted(_longest,
                        key=lambda x: -safe_float(x.get("longest_ft"), 0.0))[:5]:
            ev = f" · {m['max_ev_mph']} mph" if m.get("max_ev_mph") else ""
            la = f" · {m['launch_angle']}°" if m.get("launch_angle") is not None else ""
            lines.append(f"- {m.get('name')} — {int(safe_float(m.get('longest_ft'), 0))} ft{ev}{la}")
        lines.append(f"Longest on the board: {_top.get('name')} "
                     f"({int(safe_float(_top.get('longest_ft'), 0))} ft)")
        lines.append("")

    lines.append("")
    lines.append("HR RESULTS BY PLAYER")
    lines.append("-" * 42)
    if merged_homers:
        for item in merged_homers:
            tags = " + ".join(item.get("tags", []))
            lines.append(f"- {item['name']} — {tags}")
    else:
        lines.append("- none")

    if hr_capture_report:
        missed_entries = hr_capture_report.get("missed_homer_entries", []) or []
        if missed_entries:
            lines.append("")
            lines.append("MISSED HRs (NOT IN MODEL SHEET)")
            lines.append("-" * 42)
            for h in sorted(missed_entries, key=lambda x: (x.get("team", ""), x.get("name", "")))[:30]:
                multi = f" ({safe_int(h.get('hr'))} HR)" if safe_int(h.get("hr")) > 1 else ""
                lines.append(f"- {h.get('name')} ({h.get('team')}){multi} — {build_missed_hr_reason(h, graded_slots)}")

    lines.append("")
    lines.append("POOL PERFORMANCE")
    lines.append("-" * 42)
    if pair_pool_results.get("graded_pools"):
        for pool in pair_pool_results["graded_pools"]:
            names = _format_names(pool.get("homer_names", []))
            lines.append(f"{pool['label']} → {safe_int(pool.get('hr_count'))}/{safe_int(pool.get('total_count'))} HR ({names})")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("PAIR PERFORMANCE")
    lines.append("-" * 42)
    if pair_pool_results.get("all_pairs"):
        for idx, pair in enumerate(pair_pool_results["all_pairs"], 1):
            names = _format_names(pair.get("homer_names", []))
            lines.append(f"Pair {idx} → {safe_int(pair.get('hr_count'))}/{safe_int(pair.get('total_count'), 2)} HR ({names}) | {pair['a']['name']} + {pair['b']['name']}")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("PLAYER TYPE PERFORMANCE")
    lines.append("-" * 42)
    # Every tier now prints the SAME metric set. It used to print only the one
    # or two numbers each tier was "about", which meant backtest_report could
    # never build a comparable table -- TOP15/TOP/HR carried an HR rate and
    # nothing else, so the all-time board had three empty columns.
    _tiers = (
        ("\U0001f3c6 TOP 15 BOARD", top15),
        ("\U0001f525 TOP PICKS", top_picks),
        ("\U0001f9e8 HR PICKS", hr_picks),
        ("\U0001f3c1 HRR PICKS", hrr_picks),
        ("\U0001f4a0 HIT PICKS", hit_picks),
        ("\u26be CONTACT PICKS", contact_picks),
    )
    for _label, _grp in _tiers:
        if not _grp:
            continue
        lines.append(f"{_label} ({len(_grp)})")
        lines.append(
            f"HR: {pct(_grp, 'got_hr')}% | "
            f"1+ Hit: {pct(_grp, 'got_base_hit')}% | "
            f"XBH: {pct(_grp, 'got_xbh')}% | "
            f"2+ TB: {pct(_grp, 'tb_2_plus')}% | "
            f"2+ HRR: {pct(_grp, 'hrr_2_plus')}% | "
            f"3+ HRR: {pct(_grp, 'hrr_3_plus')}%"
        )
        lines.append("")

    lines.append("")
    lines.append("DESIGNED OUTCOME (did the pick do its job)")
    lines.append("-" * 42)
    _settled = [r for r in graded_slots if int(r.get("is_final", 0)) == 1]
    for _pt, _label in (("TOP15", "\U0001f3c6 TOP 15 BOARD"), ("TOP", "\U0001f525 TOP PICKS"),
                        ("HR", "\U0001f9e8 HR PICKS"), ("HRR", "\U0001f3c1 HRR PICKS"),
                        ("HIT", "\U0001f4a0 HIT PICKS"), ("CONTACT", "\u26be CONTACT PICKS")):
        _grp = [r for r in _settled if str(r.get("pick_type", "")).upper() == _pt]
        if not _grp:
            continue
        _n = sum(int(r.get("designed_hit", 0)) for r in _grp)
        lines.append(f"{_label} ({len(_grp)}) -> {_n}/{len(_grp)} "
                     f"({pct(_grp, 'designed_hit')}%)  ·  needs: {DESIGNED_OUTCOME.get(_pt, '')}")
    if _settled:
        _tot = sum(int(r.get("designed_hit", 0)) for r in _settled)
        lines.append(f"ALL PICKS -> {_tot}/{len(_settled)} ({pct(_settled, 'designed_hit')}%)")

    lines.append("")
    lines.append("HIT RESULTS BY CATEGORY")
    lines.append("-" * 42)
    grouped_hits = build_hit_results_by_category(graded_slots)
    hit_headers = [("TOP", "🔥 TOP PICKS (1+ Hit)"), ("HR", "🧨 HR PICKS (1+ Hit)"), ("HRR", "🏁 HRR PICKS (1+ Hit)"), ("HIT", "💠 HIT PICKS (1+ Hit)"), ("CONTACT", "⚾ CONTACT PICKS (1+ Hit)")]
    for pt, header in hit_headers:
        lines.append("")
        lines.append(header)
        entries = grouped_hits.get(pt, [])
        if entries:
            for r in entries:
                hits = safe_int(r.get("actual_hits"))
                lines.append(f"- {r['name']} — {hits}H{hit_marker(hits)}")
        else:
            lines.append("- none")

    if hr_capture_report:
        lines.append("")
        lines.append("MODEL DIAGNOSTIC (LOW PRIORITY)")
        lines.append(f"Full Sheet HR Coverage: {safe_int(hr_capture_report.get('caught_hrs_on_sheet'))} / {safe_int(hr_capture_report.get('total_hrs_on_slate'))} ({safe_float(hr_capture_report.get('hr_capture_pct')):.1f}%)")

    if live_mode:
        live_active = [r for r in graded_slots if int(r.get("is_final", 0)) == 0]
        if live_active:
            lines.append("")
            lines.append("LIVE / IN-PROGRESS PICKS WITH ACTION")
            action = [r for r in live_active if safe_int(r.get("actual_hits")) or safe_int(r.get("actual_hr")) or safe_int(r.get("actual_runs")) or safe_int(r.get("actual_rbi"))]
            if action:
                for r in sorted(action, key=lambda x: (-safe_int(x.get("actual_hr")), -safe_int(x.get("actual_hits")), x.get("name", "")))[:40]:
                    st = r.get("game_status", {}) or {}
                    status_txt = st.get("detailed_state", "Live")
                    lines.append(f"- {r['name']} ({r['team']}) | {r['pick_type']} | {safe_int(r.get('actual_hits'))}H/{safe_int(r.get('actual_hr'))}HR/R{safe_int(r.get('actual_runs'))}/RBI{safe_int(r.get('actual_rbi'))} | {status_txt}")
            else:
                lines.append("- none yet")

    return "\n".join(lines)

def _phoenix_today() -> dt.date:
    """Phoenix is UTC-7 year-round (no DST). Compute the Phoenix calendar date
    regardless of the machine's timezone (GitHub runners are UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/Phoenix")).date()
    except Exception:
        # Manual UTC-7 fallback if zoneinfo unavailable
        return (dt.datetime.utcnow() - dt.timedelta(hours=7)).date()


def _breakdown_exists(date_str: str) -> bool:
    """Check whether ANY breakdown file exists for the given date."""
    candidates = [
        OUT_DIR / f"mlb_breakdown_today_{date_str}.json",
        OUT_DIR / f"mlb_breakdown_tomorrow_{date_str}.json",
        OUT_DIR / f"mlb_daily_breakdown_final_{date_str}.json",
        OUT_DIR / f"mlb_today_breakdown_{date_str}.json",
        OUT_DIR / f"mlb_today_slate_breakdown_{date_str}.json",
        OUT_DIR / f"mlb_tomorrow_early_breakdown_{date_str}.json",
        OUT_DIR / f"mlb_tomorrow_breakdown_{date_str}.json",
        OUT_DIR / f"tomorrow_early_breakdown_{date_str}.json",
    ]
    if any(c.exists() for c in candidates):
        return True
    # Glob fallback (matches the load_rows secondary search)
    try:
        if list(OUT_DIR.glob(f"*{date_str}*.json")):
            return True
        root_out = ROOT_DIR.parent / "outputs"
        if root_out.exists() and list(root_out.glob(f"*{date_str}*.json")):
            return True
    except Exception:
        pass
    return False


def resolve_grade_date(date_arg: str) -> str:
    today = _phoenix_today()
    arg = (date_arg or "auto").strip().lower()
    if arg in {"auto", "today", "live"}:
        target = today
        # Auto-fallback: if today's picks aren't built yet (e.g. an overnight
        # scheduled run before today_bot has produced the new slate), grade
        # the most recent day that DOES have a breakdown file. This prevents
        # the FileNotFoundError crash at midnight-ish runs.
        if not _breakdown_exists(target.strftime("%Y-%m-%d")):
            for back in range(1, 4):
                candidate = today - dt.timedelta(days=back)
                if _breakdown_exists(candidate.strftime("%Y-%m-%d")):
                    print(f"ℹ️  No picks for {target} yet — grading most recent "
                          f"available slate: {candidate}")
                    target = candidate
                    break
    elif arg == "yesterday":
        target = today - dt.timedelta(days=1)
    else:
        target = dt.datetime.strptime(arg, "%Y-%m-%d").date()
    return target.strftime("%Y-%m-%d")


def main() -> int:
    parser = argparse.ArgumentParser(description="Grade MLB breakdown results from the full sheet")
    parser.add_argument("--date", default="auto", help="auto/today/live, yesterday, or YYYY-MM-DD")
    parser.add_argument("--live", action="store_true", help="Run live/in-progress grading for the selected slate")
    parser.add_argument("--final-only", action="store_true", help="Only grade games that are final; skips live games")
    parser.add_argument("--no-regrade-stale", action="store_true",
                         help="Skip the suspended/resumed-game recheck of recent dates (see regrade_stale_dates()). "
                              "On by default; only useful for a --date yesterday/YYYY-MM-DD one-off where "
                              "rechecking other dates isn't wanted.")
    parser.add_argument("--regrade-lookback-days", type=int, default=14,
                         help="How many days back the suspended-game recheck looks for a still-open outcome. Default 14.")
    args = parser.parse_args()
    date_str = resolve_grade_date(args.date)
    live_mode = bool(args.live or args.date.lower() in {"auto", "today", "live"})
    print(f"GRADING DATE: {date_str}" + (" | LIVE MODE" if live_mode else ""))

    rows = load_rows(date_str)

    # ROADMAP STEP 9B TASK 1 (2026-08-22): overlay locked-run features
    # before anything downstream reads hr_score/config_hash/etc off `rows`.
    # See apply_locked_features()'s own docstring above load_rows() for the
    # full mechanism. dropped_locked_rows are players the locked join knows
    # about that the currently-published slate no longer contains -- passed
    # only to build_hr_capture_report() below (slate-membership counting),
    # never merged into `rows`/tracking_slots (pick-selection needs the
    # full slate-row shape these synthetic rows don't have).
    rows, dropped_locked_rows = apply_locked_features(rows, date_str, OUT_DIR)

    tracking_slots = build_tracking_slots(rows)

    game_cache: Dict[int, Dict[str, Any]] = {}
    game_status_by_pk: Dict[int, Dict[str, Any]] = {}
    skipped_live_games: set[int] = set()
    # DOUBLEHEADER GRADING FIX (2026-08-21, Sol audit #2 finding #1). Keyed
    # (game_pk, player_id), NEVER player_id alone: a player who bats in BOTH
    # legs of a doubleheader (the normal case -- same lineup, both games,
    # confirmed real this season, e.g. 2026-08-17 STL@CIN gamePk 824514 +
    # 824478) has two genuinely different batting lines, one per game_pk. A
    # player_id-only dict lets whichever game is processed last silently
    # overwrite the other's line -- corrupting the PERMANENT, append-only
    # outcome_log record for the game that lost the race, not just a
    # transient display value.
    actual_by_pid: Dict[Tuple[int, int], Dict[str, int]] = {}
    graded_slots: List[Dict[str, Any]] = []

    for slot in tracking_slots:
        game_pk = int(slot["game_pk"])
        pid = int(slot["player_id"])
        if game_pk not in game_cache:
            game_cache[game_pk] = fetch_game_feed(game_pk)
            game_status_by_pk[game_pk] = get_game_status(game_cache[game_pk])
        if args.final_only and not game_is_final(game_cache[game_pk]):
            skipped_live_games.add(game_pk)
            continue
        actual = get_player_batting_line(game_cache[game_pk], pid)
        actual_by_pid[(game_pk, pid)] = actual
        graded = grade_slot(slot, actual)
        graded["game_status"] = game_status_by_pk.get(game_pk, {})
        graded["is_final"] = 1 if game_is_final(game_cache[game_pk]) else 0
        graded_slots.append(graded)

    # Make sure every player in rows has an actual line, not just displayed tracking slots.
    # This makes pair/pool partial grading accurate even when a pool player was not a main pick slot.
    for row in rows:
        game_pk = int(row["game_pk"])
        pid = int(row["player_id"])
        if game_pk not in game_cache:
            game_cache[game_pk] = fetch_game_feed(game_pk)
            game_status_by_pk[game_pk] = get_game_status(game_cache[game_pk])
        if (game_pk, pid) not in actual_by_pid:
            actual_by_pid[(game_pk, pid)] = get_player_batting_line(game_cache[game_pk], pid)

    # MODEL FOUNDATION (Task 5): append-only outcome log, one line per
    # player-game, model-independent (built off actual_by_pid/rows the same
    # way the block above already covers every predicted player, not just
    # today's tracking slots). Runs every grading invocation (live and
    # final-only alike); per-row void/game_status reflects whether that
    # PLAYER's game was actually final at this moment, so a live run and a
    # final-only run both produce honest, append-safe output rather than
    # this being gated on --final-only. Best-effort: never blocks grading.
    # Computed outside the try so both names are always bound -- the
    # regrade_stale_dates() call below reuses them regardless of whether
    # today's own write_outcome_log() call succeeds.
    _graded_at = dt.datetime.now(dt.timezone.utc).isoformat()
    _outcome_publish_dir = (DASHBOARD_REPO / "public" / "data" / "current") if _is_dashboard_repo(DASHBOARD_REPO) else (OUT_DIR)
    try:
        write_outcome_log(
            prediction_date=date_str,
            rows=rows,
            actual_by_pid=actual_by_pid,
            game_status_by_pk=game_status_by_pk,
            graded_at=_graded_at,
            fetch_dir=OUT_DIR,
            publish_dir=_outcome_publish_dir,
        )
    except Exception as _ol_exc:
        print(f"outcome log skipped: {_ol_exc}")

    # SUSPENDED-GAME FIX (2026-08-21): see regrade_stale_dates()'s own
    # docstring. Runs after today's own outcome log write, same best-effort
    # guard -- a failure here must never take down the rest of grading.
    if not args.no_regrade_stale:
        try:
            regrade_stale_dates(
                fetch_dir=OUT_DIR,
                publish_dir=_outcome_publish_dir,
                today=_phoenix_today(),
                graded_at=_graded_at,
                lookback_days=args.regrade_lookback_days,
            )
        except Exception as _rs_exc:
            print(f"regrade-stale skipped: {_rs_exc}")

    # 🧤 DEFENSE STAMP (2026-08-08): opp BABIP-against percentile onto every
    # graded slot, so ~2 weeks from now the archive can answer "do picks vs
    # leaky defenses out-hit picks vs elite gloves" with real counts. Same
    # earn-your-weight pipeline k_rate went through before it entered the
    # blend. Never blocks grading.
    try:
        yr = dt.date.today().year
        dj = requests.get(f"https://statsapi.mlb.com/api/v1/teams/stats?season={yr}&group=pitching&stats=season&sportIds=1"
                          "&fields=stats,splits,team,id,stat,hits,homeRuns,strikeOuts,baseOnBalls,battersFaced,hitByPitch",
                          timeout=TIMEOUT).json()
        tj = requests.get("https://statsapi.mlb.com/api/v1/teams?sportId=1&fields=teams,id,abbreviation", timeout=TIMEOUT).json()
        _abbr = {t["id"]: t.get("abbreviation", "") for t in tj.get("teams", [])}
        _rows = []
        for sp in (dj.get("stats") or [{}])[0].get("splits", []):
            s = sp.get("stat") or {}
            bip = (s.get("battersFaced") or 0) - (s.get("strikeOuts") or 0) - (s.get("baseOnBalls") or 0) \
                - (s.get("hitByPitch") or 0) - (s.get("homeRuns") or 0)
            tid = (sp.get("team") or {}).get("id")
            if tid and bip >= 200:
                _rows.append((str(_abbr.get(tid, "")).upper(), ((s.get("hits") or 0) - (s.get("homeRuns") or 0)) / bip))
        _rows.sort(key=lambda x: x[1])
        _def_pct = {ab: round(100 * i / max(1, len(_rows) - 1)) for i, (ab, _) in enumerate(_rows) if ab}
        for g in graded_slots:
            ab = str(g.get("opponent") or "").upper()
            if ab in _def_pct:
                g["opp_def_pctile"] = _def_pct[ab]
    except Exception as exc:
        print(f"defense stamp skipped: {exc}")

    # 🚪 pen-door alerts moved to bots/pen_door_watch.py + pen-door.yml
    # (2026-08-08, same day they landed here): Donovan wanted the ping when
    # the change HAPPENS, not an hourly digest. The 10-minute watcher owns
    # the job alone — calling the hourly version too would double-post
    # every change. send_pitching_change_alerts stays defined as the
    # fallback if the watcher ever has to come out.

    graded_slots = annotate_designed(graded_slots)

    # TOP, GRADED AS ITS OWN CLAIM (2026-08-08, Donovan: "what is another
    # way to score the top pick besides hr"). TOP means "best play in his
    # game" — so grade it relatively: top_beat_game = 1 when the pick's
    # total bases meet or beat every other hitter's in THAT game (ties
    # count — sharing the lead is not losing it). actual_by_pid covers the
    # whole slate, so the comparison is against the game, not just our picks.
    _tb_by_game: Dict[int, list] = {}
    for row in rows:
        _l = actual_by_pid.get((int(row["game_pk"]), int(row["player_id"])))
        if _l is not None:
            _tb_by_game.setdefault(int(row["game_pk"]), []).append(
                (int(row["player_id"]), int(_l.get("tb") or 0)))
    for g in graded_slots:
        _role = str(g.get("game_pick_role") or "").split("/")[0].strip().upper()
        if _role != "TOP":
            continue
        mates = _tb_by_game.get(int(g.get("game_pk") or 0)) or []
        if not mates:
            continue
        own = int(g.get("actual_tb") or 0)
        best_other = max((tb for pid2, tb in mates if pid2 != int(g.get("player_id") or 0)), default=0)
        g["top_beat_game"] = 1 if own >= best_other and own > 0 else 0
        g["top_game_best_tb"] = best_other

    pair_pool_sections = load_pair_builder_sections(date_str) or build_pair_pool_sections(rows)
    pair_pool_results = grade_pairs_pools(pair_pool_sections, actual_by_pid)
    merged_homers = merge_homer_entries(graded_slots)
    # ROADMAP STEP 9B/23 (2026-08-22): hr_capture_pct undercounted for the
    # same root cause task 1 fixes elsewhere -- a player in the LOCKED
    # slate who was dropped by a later rebuild reads as "the model never
    # saw him" even though he was rated at lock time. dropped_locked_rows
    # (from apply_locked_features() above) restores slate MEMBERSHIP for
    # exactly those players without touching tracking_slots/pick selection,
    # which only ever wants the currently-published slate's own rows. See
    # claude/moonshot-full-slate-validation.md §5 for the measured impact
    # (4 of 5 "missed" HRs on 2026-08-21 were this, not a real coverage gap).
    hr_capture_report = build_hr_capture_report(rows + dropped_locked_rows, game_cache, actual_by_pid)
    merge_homer_distances(merged_homers, hr_capture_report)
    unique_player_report = build_unique_player_hr_report(graded_slots)

    summary = build_summary_text(date_str, graded_slots, merged_homers, pair_pool_results, hr_capture_report, unique_player_report, live_mode=live_mode)
    print(summary)

    clean_prefix = "mlb_results_live" if live_mode else "mlb_results_final"
    legacy_prefix = "live_graded_results" if live_mode else "graded_results"
    txt_path = OUT_DIR / f"{clean_prefix}_{date_str}.txt"
    json_path = OUT_DIR / f"{clean_prefix}_{date_str}.json"
    pdf_path = OUT_DIR / f"{clean_prefix}_{date_str}.pdf"
    txt_alias_paths = [OUT_DIR / f"{legacy_prefix}_{date_str}.txt"]
    json_alias_paths = [OUT_DIR / f"{legacy_prefix}_{date_str}.json"]

    # Build a site-friendly results list. The site's Results.js looks for
    # a top-level `results` array with rows that have `grade`, `bet_type`,
    # and `outcome_text` fields. graded_slots has the data but not those
    # exact field names — we map them here.
    def _grade_for_row(r):
        # Final-mode grading uses certainty. Live-mode shows in-progress.
        if not live_mode:
            if int(r.get("got_hr", 0)) == 1:
                return "WIN"
            if int(r.get("got_base_hit", 0)) == 1 and r.get("pick_type") in ("HIT", "HRR", "CONTACT", "TOP", "TOP15"):
                return "WIN"
            if int(r.get("actual_ab", 0)) > 0:
                return "LOSS"
            return "DNP"  # Did not play
        # Live mode
        if int(r.get("got_hr", 0)) == 1:
            return "HIT"
        if int(r.get("got_base_hit", 0)) == 1:
            return "HIT"
        if int(r.get("actual_ab", 0)) > 0:
            return "LIVE"
        return "PENDING"

    def _bet_for_row(r):
        pt = (r.get("pick_type") or "").upper()
        return {
            "HR": "HR", "TOP": "TOP", "TOP15": "TOP15",
            "HIT": "HIT", "HRR": "HRR", "CONTACT": "TB",
        }.get(pt, pt or "PICK")

    def _outcome_text(r):
        ab   = int(r.get("actual_ab", 0))
        hits = int(r.get("actual_hits", 0))
        hr   = int(r.get("actual_hr", 0))
        tb   = int(r.get("actual_tb", 0))
        rbi  = int(r.get("actual_rbi", 0))
        runs = int(r.get("actual_runs", 0))
        if ab == 0 and hits == 0 and hr == 0:
            return "Game not started"
        line = f"{hits}/{ab}"
        extras = []
        if hr:  extras.append(f"{hr} HR")
        if tb:  extras.append(f"{tb} TB")
        if rbi: extras.append(f"{rbi} RBI")
        if runs:extras.append(f"{runs} R")
        if extras:
            line += " · " + ", ".join(extras)
        return line

    site_results = [
        {**slot,
         "grade":        _grade_for_row(slot),
         "bet_type":     _bet_for_row(slot),
         "outcome_text": _outcome_text(slot)}
        for slot in graded_slots
    ]

    payload = {
        "date": date_str,
        "live_mode": live_mode,
        "label": ("Live" if live_mode else "Final") + " · " + date_str,
        # ── Site-friendly aliases (what Results.js looks for) ────────────
        "results": site_results,
        # ── Original (preserved so nothing downstream breaks) ────────────
        "graded_slots": graded_slots,
        "merged_homers": merged_homers,
        "pair_pool_results": pair_pool_results,
        "hr_capture_report": hr_capture_report,
        "game_status_by_pk": game_status_by_pk,
        "skipped_live_games": sorted(skipped_live_games),
    }

    txt_path.write_text(summary + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    save_text_as_pdf(summary, pdf_path, f"{'Live ' if live_mode else ''}Results Tracker - {date_str}")
    sync_results_to_website_repo_v2(date_str, live_mode, json_path, txt_path, pdf_path)

    print(f"\nSaved: {txt_path}")
    print(f"Saved: {json_path}")
    print(f"Saved: {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
