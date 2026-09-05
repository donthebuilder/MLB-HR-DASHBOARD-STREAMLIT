"""⚾ MONEYBALL — a team is its on-base and its slugging, not its record.

The moneyline model's base, replacing "log5 on the win-loss record".

── WHY, IN DONOVAN'S WORDS ────────────────────────────────────────────────────

2026-09-05: "as far as the moneyline thing — linear regression, and use OBP
and SLG over BA, weighted. Think of Moneyball, Oakland A's. And tell me how
that does out of sample."

That is the 2002 A's front-office argument, and it is the right one for a
one-game price. A win-loss record is an OUTCOME: it carries every one-run
game, every blown save and every lucky bounce, and by September it is about
as much luck as skill in the tails. On-base and slugging are INPUTS — what a
lineup actually does at the plate, hundreds of plate appearances a week, and
they stabilise months before a record does. Batting average is the input the
A's threw away, because a walk and a single reach first base the same way and
average only counts one of them.

── THE MODEL, IN THREE PIECES ─────────────────────────────────────────────────

1. RUNS FROM OBP AND SLG, BY LINEAR REGRESSION.
       runs/game = a + b·OBP + c·SLG
   Fit on this season's team rows (every team, at every month-end snapshot),
   by ordinary least squares, solved by hand -- no numpy above the fetch line.
   Thirty teams is a small sample for two correlated regressors, so the fit
   is SHRUNK toward a prior worth PRIOR_WEIGHT team-seasons. The prior is the
   published shape of this regression over many seasons: OBP worth more per
   point than SLG. The fitted ratio b/c is published, so the page can say what
   THIS season's data thinks a point of on-base is worth against a point of
   slugging, instead of quoting the book.

2. A MATCHUP IS OFFENCE × DEFENCE, RELATIVE TO THE LEAGUE.
   The lineup's OBP against the pitching staff's OBP-allowed, scaled by the
   league rate (rate × rate ÷ league, the log5-shaped rule for rates). Same
   for SLG. Feed both through the regression: expected runs for each side.

3. RUNS TO A WIN PROBABILITY, BY PYTHAGENPAT.
   exponent = (R + RA)^0.287, then R^x / (R^x + RA^x). Home field is applied
   as an odds multiplier -- the same one playoff_odds uses -- so a coin flip
   moves the full amount and a lock barely at all.

Tonight's starters are NOT in here. They stay in moneyline_bot.starter_shift,
on top of this base, exactly where they were. This file is the team.

── WHAT IT CANNOT KNOW, SAID ONCE ─────────────────────────────────────────────

Season OBP and SLG are the whole roster's season, including the trade the
team made in July and the shortstop who is now on the IL. The market knows
tonight's lineup card. So a disagreement with the price is still, most often,
the model being behind rather than the model being right, and the board is
still a disagreement log graded in public rather than a tip sheet.

── OUT OF SAMPLE, AND WHAT THAT MEANS HERE ───────────────────────────────────

`backtest()` is walk-forward: for every finished game in month M it uses each
team's rates and the regression fitted THROUGH THE END OF MONTH M-1, never
anything later. April has no prior month and is skipped. It reports log loss,
Brier and favourite accuracy for this model against three yardsticks --
records-only log5 (the model this replaces), "home team always" at the
league home rate, and a coin -- on the same games. Beating the coin is not an
achievement; beating records-only is the claim; beating the market is not
measurable here, because no historical moneyline prices are stored. That last
sentence is the honest limit of the number, and it is printed beside it.

Everything above `fetch_rates` is pure and imports nothing. The tests run
where statsapi is unreachable, which is where this was written.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playoff_odds import HOME_WIN_RATE, REGRESSION_GAMES, win_probability  # noqa: E402

# ── the prior ────────────────────────────────────────────────────────────────
#
# The long-run shape of runs/game ~ OBP + SLG regressions on MLB team seasons
# lands near 16 runs per point of OBP against 9-11 per point of SLG: roughly
# 1.6-1.8x, and "three times" in the DePodesta telling once you count that a
# point of OBP is scarcer than a point of SLG. The prior below is that shape
# with the intercept set so a league-average line (.315 / .400) scores 4.40
# runs. It is a PRIOR, not the answer: the fit moves it, and the fitted ratio
# is what gets published.
PRIOR_INTERCEPT = -4.44
PRIOR_OBP = 16.0
PRIOR_SLG = 9.5
PRIOR_WEIGHT = 30          # pseudo team-seasons of belief in the prior

PYTHAGENPAT_K = 0.287      # Davenport / Smyth exponent
MIN_RUNS = 1.5             # a lineup never projects below this in one game
MIN_GAMES_FOR_RATES = 20   # before this a team's OBP/SLG is mostly noise


@dataclass
class Coef:
    intercept: float
    obp: float
    slg: float
    rows: int = 0            # rows the fit saw
    shrink: float = 1.0      # 1.0 = all prior, 0.0 = all data

    @property
    def obp_per_slg(self) -> float:
        """What a point of on-base is worth in points of slugging."""
        return self.obp / self.slg if self.slg else float("inf")

    def runs(self, obp: float, slg: float) -> float:
        return max(MIN_RUNS, self.intercept + self.obp * obp + self.slg * slg)


PRIOR = Coef(PRIOR_INTERCEPT, PRIOR_OBP, PRIOR_SLG)


@dataclass
class TeamRates:
    """A team as this model sees it: what it does at the plate, and what it
    allows. `games` gates whether the rates are trusted yet."""
    team_id: int
    abbr: str
    obp: float
    slg: float
    obp_allowed: float
    slg_allowed: float
    games: int = 0
    runs: int = 0
    wins: int = 0
    losses: int = 0
    name: str = ""

    @property
    def runs_per_game(self) -> float:
        return self.runs / self.games if self.games else 0.0

    @property
    def usable(self) -> bool:
        return (self.games >= MIN_GAMES_FOR_RATES and 0.2 < self.obp < 0.45
                and 0.25 < self.slg < 0.6 and 0.2 < self.obp_allowed < 0.45
                and 0.25 < self.slg_allowed < 0.6)

    @property
    def record_strength(self) -> float:
        """The old model's number, for the comparison: regressed win%."""
        n = self.wins + self.losses
        return (self.wins + REGRESSION_GAMES * 0.5) / (n + REGRESSION_GAMES)


@dataclass
class League:
    obp: float
    slg: float


def league_of(teams: list[TeamRates]) -> League:
    """Games-weighted league rates from the teams themselves. The rates are
    scaled against this, so it must come from the same snapshot."""
    use = [t for t in teams if t.games > 0]
    g = sum(t.games for t in use) or 1
    return League(
        obp=sum(t.obp * t.games for t in use) / g if use else 0.315,
        slg=sum(t.slg * t.games for t in use) / g if use else 0.400,
    )


# ── 1. the regression ────────────────────────────────────────────────────────

def fit_runs(rows: list[tuple[float, float, float]], prior: Coef = PRIOR,
             prior_weight: int = PRIOR_WEIGHT) -> Coef:
    """OLS of runs/game on (OBP, SLG), by hand, shrunk toward the prior.

    rows: (obp, slg, runs_per_game). Solves the 3x3 normal equations with
    Cramer's rule. Shrinkage is a weighted average of the OLS answer and the
    prior, weighted rows : prior_weight -- with 30 rows the data and the prior
    split it, with 150 rows (five monthly snapshots) the data has 5/6 of it.
    Degenerate input (too few rows, collinear, singular) returns the prior
    with shrink=1.0 so the payload says so.
    """
    clean = [(float(o), float(s), float(r)) for o, s, r in rows or []
             if 0.2 < o < 0.5 and 0.2 < s < 0.7 and 0.5 < r < 10]
    n = len(clean)
    if n < 8:
        return Coef(prior.intercept, prior.obp, prior.slg, rows=n, shrink=1.0)

    # Normal equations X'X β = X'y with X = [1, obp, slg].
    s1 = n
    so = sum(o for o, _, _ in clean); ss = sum(s for _, s, _ in clean)
    soo = sum(o * o for o, _, _ in clean); sss = sum(s * s for _, s, _ in clean)
    sos = sum(o * s for o, s, _ in clean)
    sy = sum(r for _, _, r in clean)
    soy = sum(o * r for o, _, r in clean); ssy = sum(s * r for _, s, r in clean)
    m = [[s1, so, ss], [so, soo, sos], [ss, sos, sss]]
    v = [sy, soy, ssy]

    def det3(a):
        return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
                - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))

    d = det3(m)
    if abs(d) < 1e-12:
        return Coef(prior.intercept, prior.obp, prior.slg, rows=n, shrink=1.0)
    sol = []
    for col in range(3):
        mc = [row[:] for row in m]
        for r in range(3):
            mc[r][col] = v[r]
        sol.append(det3(mc) / d)
    a, b, c = sol
    w = n / (n + prior_weight)          # weight on the data
    return Coef(
        intercept=w * a + (1 - w) * prior.intercept,
        obp=w * b + (1 - w) * prior.obp,
        slg=w * c + (1 - w) * prior.slg,
        rows=n, shrink=round(1 - w, 4),
    )


def fit_rows_from(teams: list[TeamRates]) -> list[tuple[float, float, float]]:
    return [(t.obp, t.slg, t.runs_per_game) for t in teams
            if t.games >= MIN_GAMES_FOR_RATES and t.runs_per_game > 0]


# ── 2. the matchup ───────────────────────────────────────────────────────────

def matchup_rate(offence: float, defence_allowed: float, league: float) -> float:
    """rate × rate ÷ league. A .340 lineup against a .340-allowing staff in a
    .315 league projects .367; against a .290 staff, .313."""
    if league <= 0:
        return offence
    return offence * defence_allowed / league


def expected_runs(off: TeamRates, dfn: TeamRates, lg: League, coef: Coef) -> float:
    """Runs `off` should score against `dfn`, this game."""
    return coef.runs(
        matchup_rate(off.obp, dfn.obp_allowed, lg.obp),
        matchup_rate(off.slg, dfn.slg_allowed, lg.slg),
    )


# ── 3. runs to a win ─────────────────────────────────────────────────────────

def pythagenpat(runs_for: float, runs_against: float) -> float:
    rf, ra = max(MIN_RUNS, runs_for), max(MIN_RUNS, runs_against)
    x = (rf + ra) ** PYTHAGENPAT_K
    return rf ** x / (rf ** x + ra ** x)


def home_field(p_neutral: float, home_win_rate: float = HOME_WIN_RATE) -> float:
    """Odds multiplier, the same shape playoff_odds applies."""
    p = min(0.99, max(0.01, p_neutral))
    mult = home_win_rate / (1 - home_win_rate)
    odds = p / (1 - p) * mult
    return odds / (1 + odds)


def game_probability(home: TeamRates, away: TeamRates, lg: League, coef: Coef,
                     neutral: bool = False) -> float | None:
    """P(home wins). None when either side's rates are not usable yet, so the
    caller falls back to something rather than pricing noise."""
    if not (home.usable and away.usable):
        return None
    rh = expected_runs(home, away, lg, coef)
    ra = expected_runs(away, home, lg, coef)
    p = pythagenpat(rh, ra)
    return p if neutral else home_field(p)


# ── the out-of-sample harness ───────────────────────────────────────────────

@dataclass
class FinishedGame:
    game_pk: int
    date: str            # YYYY-MM-DD
    home_id: int
    away_id: int
    home_runs: int
    away_runs: int

    @property
    def month(self) -> int:
        return int(self.date[5:7])

    @property
    def home_won(self) -> bool:
        return self.home_runs > self.away_runs


def _clip(p: float) -> float:
    return min(0.99, max(0.01, p))


# ── THE MERGE (2026-09-05) ──────────────────────────────────────────────────
#
# Donovan, asked what to do if OBP/SLG does not beat the record out of sample:
# "merge the good findings." So the base is not either/or. It is
#     p = w · p_obp_slg + (1 − w) · p_record
# and w is CHOSEN BY THE WALK-FORWARD, not by hand: the harness scores every
# tenth from 0 (all record) to 1 (all OBP/SLG) on the same games and publishes
# the one with the lowest log loss. A team's record carries things its rates
# do not -- one-run luck, the bullpen, how it actually closes games -- and the
# rates carry what the record has not caught up to yet. If either is worthless
# the sweep says so by landing on 0 or 1; anything in between is the merge.
# The live bot reads the published weight; before the harness has run it uses
# DEFAULT_BLEND, which is the honest prior of "half each".
BLEND_STEPS = [round(i / 10, 1) for i in range(11)]
DEFAULT_BLEND = 0.5


def blend(p_rates: float, p_record: float, w: float = DEFAULT_BLEND) -> float:
    w = min(1.0, max(0.0, float(w)))
    return w * p_rates + (1.0 - w) * p_record


def blend_sweep(pairs_rates: list[tuple[float, bool]], pairs_record: list[tuple[float, bool]]) -> dict:
    """Log loss at every weight, and the best one. Both lists are aligned by
    construction (backtest appends to them in the same loop)."""
    if not pairs_rates or len(pairs_rates) != len(pairs_record):
        return {"weights": [], "best": DEFAULT_BLEND}
    rows = []
    for w in BLEND_STEPS:
        mixed = [(blend(pr, pc, w), won) for (pr, won), (pc, _) in zip(pairs_rates, pairs_record)]
        rows.append({"w": w, **score(mixed)})
    best = min(rows, key=lambda r: (r["log_loss"], abs(r["w"] - DEFAULT_BLEND)))
    return {"weights": rows, "best": best["w"], "best_log_loss": best["log_loss"]}


def score(pairs: list[tuple[float, bool]]) -> dict:
    """Log loss, Brier, and how often the side above .500 won. `pairs` is
    (P(home), home_won)."""
    if not pairs:
        return {"n": 0, "log_loss": None, "brier": None, "fav_accuracy": None}
    ll = br = hit = 0.0
    for p, won in pairs:
        p = _clip(p)
        y = 1.0 if won else 0.0
        ll += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        br += (p - y) ** 2
        hit += 1.0 if (p >= 0.5) == won else 0.0
    n = len(pairs)
    return {"n": n, "log_loss": round(ll / n, 4), "brier": round(br / n, 4),
            "fav_accuracy": round(hit / n, 4)}


def calibration(pairs: list[tuple[float, bool]], width: float = 0.1) -> list[dict]:
    """Predicted vs actual by bucket -- the picture that tells you whether a
    .600 means .600."""
    buckets: dict[int, list] = {}
    for p, won in pairs:
        buckets.setdefault(int(_clip(p) / width), []).append((p, won))
    out = []
    for k in sorted(buckets):
        rows = buckets[k]
        out.append({"from": round(k * width, 2), "to": round((k + 1) * width, 2),
                    "n": len(rows),
                    "predicted": round(sum(p for p, _ in rows) / len(rows), 3),
                    "actual": round(sum(1 for _, w in rows if w) / len(rows), 3)})
    return out


def backtest(games: list[FinishedGame], snapshots: dict[int, list[TeamRates]],
             first_month: int | None = None) -> dict:
    """Walk-forward. snapshots[m] = every team's rates THROUGH THE END OF
    MONTH m. A game in month M is priced with snapshots[M-1] and a regression
    fitted on snapshots[..M-1]. Pure: games and snapshots in, a report out."""
    months = sorted(m for m in {g.month for g in games} if (m - 1) in snapshots)
    if first_month:
        months = [m for m in months if m >= first_month]
    mb, rec, home, coin = [], [], [], []
    per_month, coefs = [], {}
    for m in months:
        hist_rows = [r for k in snapshots if k < m for r in fit_rows_from(snapshots[k])]
        coef = fit_runs(hist_rows)
        coefs[m] = coef
        rates = {t.team_id: t for t in snapshots[m - 1]}
        lg = league_of(snapshots[m - 1])
        month_pairs = {"moneyball": [], "records": [], "home": []}
        for g in (x for x in games if x.month == m):
            h, a = rates.get(g.home_id), rates.get(g.away_id)
            if not h or not a:
                continue
            p = game_probability(h, a, lg, coef)
            if p is None:
                continue
            pr = win_probability(h.record_strength, a.record_strength)
            for lst, val in ((mb, p), (rec, pr), (home, HOME_WIN_RATE), (coin, 0.5)):
                lst.append((val, g.home_won))
            month_pairs["moneyball"].append((p, g.home_won))
            month_pairs["records"].append((pr, g.home_won))
            month_pairs["home"].append((HOME_WIN_RATE, g.home_won))
        per_month.append({
            "month": m, "fit_rows": coef.rows, "obp_per_slg": round(coef.obp_per_slg, 2),
            "moneyball": score(month_pairs["moneyball"]),
            "records": score(month_pairs["records"]),
            "home": score(month_pairs["home"]),
        })
    sweep = blend_sweep(mb, rec)
    merged = [(blend(pr, pc, sweep["best"]), won) for (pr, won), (pc, _) in zip(mb, rec)]
    return {
        "games": len(mb),
        "months": months,
        "moneyball": score(mb),
        "records_only": score(rec),
        "home_always": score(home),
        "coin": score(coin),
        "blend": {"weight": sweep["best"], "score": score(merged), "sweep": sweep.get("weights", [])},
        "calibration": calibration(merged),
        "per_month": per_month,
        "verdict": verdict(score(mb), score(rec), score(home), sweep["best"]),
        "limits": (
            "Walk-forward: every game is priced with rates and a regression "
            "from before its month began. Beating records-only is the claim; "
            "beating the market is not measured, because no historical "
            "moneyline prices are stored. April is skipped (no prior month)."
        ),
    }


def verdict(mb: dict, rec: dict, home: dict, w: float = DEFAULT_BLEND) -> str:
    """One sentence, in the direction the numbers actually point, ending with
    what the live bot will do about it."""
    if not mb.get("n"):
        return "Not enough games to say anything."
    a, b, c = mb["log_loss"], rec["log_loss"], home["log_loss"]
    mix = (f"The live base is {int(round(w * 100))}% OBP/SLG, {int(round((1 - w) * 100))}% record "
           f"-- the mix with the lowest out-of-sample log loss.")
    if a < b and a < c:
        return (f"Out of sample, OBP/SLG beats the record-only model "
                f"(log loss {a:.4f} vs {b:.4f}) and home-always ({c:.4f}) over {mb['n']} games. {mix}")
    if a < c:
        return (f"Out of sample, OBP/SLG beats home-always ({a:.4f} vs {c:.4f}) but "
                f"not the record-only model ({b:.4f}) over {mb['n']} games -- the "
                f"components did not add information the record lacked on their own. {mix}")
    return (f"Out of sample, OBP/SLG ({a:.4f}) did not beat home-always ({c:.4f}) "
            f"over {mb['n']} games. That is a real negative result. {mix}")


def payload(report: dict, coef: Coef, season: int, as_of: str) -> dict:
    return {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "season": season, "as_of": as_of,
        "model": "runs/game = a + b·OBP + c·SLG by OLS shrunk toward a prior; "
                 "offence × defence ÷ league per rate; Pythagenpat to a win; "
                 "home field as an odds multiplier.",
        "coef": {"intercept": round(coef.intercept, 3), "obp": round(coef.obp, 2),
                 "slg": round(coef.slg, 2), "obp_per_slg": round(coef.obp_per_slg, 2),
                 "rows": coef.rows, "prior_share": coef.shrink},
        "prior": {"obp": PRIOR_OBP, "slg": PRIOR_SLG, "weight": PRIOR_WEIGHT},
        **report,
    }


# ── the small part: getting the season ───────────────────────────────────────

STATS_API = "https://statsapi.mlb.com/api/v1"


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_rates(season: int, end_date: str | None = None, timeout: int = 30) -> list[TeamRates]:
    """Every team's OBP/SLG for and against, season-to-date or through
    `end_date`. Two calls for the whole league (hitting, pitching); if the
    league-wide endpoint refuses the date range it falls back to per team."""
    import requests
    from playoff_odds import fetch_team_abbrs
    abbrs = fetch_team_abbrs(timeout)

    def pull(group: str) -> dict[int, dict]:
        params = {"sportId": 1, "season": season, "group": group}
        if end_date:
            params.update({"stats": "byDateRange", "startDate": f"{season}-03-01", "endDate": end_date})
        else:
            params["stats"] = "season"
        out: dict[int, dict] = {}
        try:
            r = requests.get(f"{STATS_API}/teams/stats", params=params, timeout=timeout)
            if r.ok:
                for blk in (r.json() or {}).get("stats", []):
                    for sp in blk.get("splits", []):
                        tid = int(((sp.get("team") or {}).get("id")) or 0)
                        if tid:
                            out[tid] = sp.get("stat") or {}
        except Exception:
            pass
        if len(out) >= 25:
            return out
        # per-team fallback
        for tid in abbrs:
            try:
                r = requests.get(f"{STATS_API}/teams/{tid}/stats", params=params, timeout=timeout)
                if not r.ok:
                    continue
                for blk in (r.json() or {}).get("stats", []):
                    for sp in blk.get("splits", []):
                        out[tid] = sp.get("stat") or {}
            except Exception:
                continue
        return out

    hit, pit = pull("hitting"), pull("pitching")
    teams: list[TeamRates] = []
    for tid, h in hit.items():
        p = pit.get(tid) or {}
        teams.append(TeamRates(
            team_id=tid, abbr=abbrs.get(tid, str(tid)),
            obp=_f(h.get("obp")), slg=_f(h.get("slg")),
            obp_allowed=_f(p.get("obp")), slg_allowed=_f(p.get("slg")),
            games=int(_f(h.get("gamesPlayed"))), runs=int(_f(h.get("runs"))),
            wins=int(_f(p.get("wins"))), losses=int(_f(p.get("losses"))),
        ))
    return teams


def fetch_finished(season: int, timeout: int = 30) -> list[FinishedGame]:
    """Every finished regular-season game with its score. One call per month."""
    import requests
    out: list[FinishedGame] = []
    for month in range(3, 11):
        start = dt.date(season, month, 1)
        end = dt.date(season + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
        try:
            r = requests.get(f"{STATS_API}/schedule", params={
                "sportId": 1, "startDate": start.isoformat(), "endDate": end.isoformat(),
                "gameType": "R",
                "fields": "dates,date,games,gamePk,officialDate,status,abstractGameState,teams,home,away,team,id,score",
            }, timeout=timeout)
            if not r.ok:
                continue
            for day in (r.json() or {}).get("dates", []):
                for g in day.get("games", []):
                    if ((g.get("status") or {}).get("abstractGameState") or "") != "Final":
                        continue
                    t = g.get("teams") or {}
                    h, a = t.get("home") or {}, t.get("away") or {}
                    if h.get("score") is None or a.get("score") is None:
                        continue
                    out.append(FinishedGame(
                        game_pk=int(g.get("gamePk") or 0),
                        date=str(g.get("officialDate") or day.get("date") or ""),
                        home_id=int((h.get("team") or {}).get("id") or 0),
                        away_id=int((a.get("team") or {}).get("id") or 0),
                        home_runs=int(h.get("score") or 0), away_runs=int(a.get("score") or 0),
                    ))
        except Exception:
            continue
    return out


def month_end(season: int, month: int) -> str:
    return (dt.date(season + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)).isoformat()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Moneyball team model: fit, and grade out of sample")
    ap.add_argument("--season", type=int, default=dt.date.today().year)
    ap.add_argument("--out", default="public/data/current/moneyball_backtest.json")
    ap.add_argument("--first-month", type=int, default=5,
                    help="first month to score (needs a prior month of rates)")
    args = ap.parse_args(argv)

    games = fetch_finished(args.season)
    if not games:
        raise SystemExit("no finished games — refusing to publish")
    last_month = max(g.month for g in games)
    snapshots: dict[int, list[TeamRates]] = {}
    for m in range(args.first_month - 1, last_month):
        snap = fetch_rates(args.season, end_date=month_end(args.season, m))
        if len(snap) >= 25:
            snapshots[m] = snap
        print(f"  through {month_end(args.season, m)}: {len(snap)} teams")
    report = backtest(games, snapshots, first_month=args.first_month)

    # The coefficients the LIVE bot should use tonight: everything so far.
    now = fetch_rates(args.season)
    coef = fit_runs([r for s in snapshots.values() for r in fit_rows_from(s)] + fit_rows_from(now))
    out = payload(report, coef, args.season, dt.date.today().isoformat())
    with open(args.out, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"wrote {args.out}\n  {report['verdict']}\n  OBP worth {coef.obp_per_slg:.2f}x SLG per point "
          f"({coef.rows} rows, prior share {coef.shrink})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
