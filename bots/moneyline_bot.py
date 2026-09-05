"""💰 THE MONEYLINE BOT — where a naive model disagrees with the market.

The fourth of the four bots. It picks sides on games, prices them honestly,
and grades itself in public from the first day.

── THE KEY YOU ALREADY PAY FOR CAN DO THIS ──────────────────────────────────

odds_fetch.py concluded, in writing and after a live run on 2026-08-15, that
theoddsapi "is unusable for props until the plan changes": /props/ is their
Business tier and the key 403s on it. Two lines further up, the same file
records the other half of that finding — "/odds/ only carries h2h/spreads/
totals" — which is exactly and only what a moneyline bot needs.

So the provider written off as unusable is unusable FOR PROPS and perfectly
usable for game lines. This bot calls /odds/ with markets=h2h through
odds_fetch's own `api()` helper, on the same key, with no plan change and no
new secret.

── WHAT IT ACTUALLY CLAIMS, WHICH IS LESS THAN IT SOUNDS ────────────────────

The model is bots/playoff_odds.win_probability: log5 on records regressed
toward .500, plus home field. That is imported rather than reimplemented, on
purpose — two team models in one repo drift apart and then the playoff odds
and the moneyline picks start disagreeing about who is better, which is
indefensible from a site that grades itself.

That model knows a team's record and nothing else. No rotations, no bullpen,
no injuries, no travel. The market knows all of it. So when this bot finds a
big edge, THE MOST LIKELY EXPLANATION IS THAT THE MARKET IS RIGHT AND THE
MODEL IS BLIND — an ace on the mound against a bullpen game looks like free
money to a machine that cannot see pitchers.

That is not a reason to hide the number. It is a reason to publish it with the
grade attached from day one and let the record settle it. The site's own True
Price page already says flat betting its player props has not made money over
1,621 settled bets; a moneyline board that arrived promising better without
evidence would be the one dishonest page on the site.

EDGE_FLOOR exists for that reason. Below it the disagreement is smaller than
the model's own error and nothing is picked.

── AND THEN THE SIMULATION SAID THE FLOOR DOES NOT SAVE IT ──────────────────

Before wiring this to anything, the null hypothesis was played out: build a
world where the market is exactly right and the model is blind by the amount
the market can see and the record cannot, price it with an ordinary hold, and
run it. At every edge floor tried (5, 8, 12 points) and every level of market
knowledge (3, 6, 9 points) the bot loses roughly 4-8% flat, and RAISING THE
FLOOR DOES NOT HELP — it takes fewer bets and loses at the same rate, because
selectivity concentrates the vig rather than finding an edge.

That is not a reason to delete the bot; it is the reason this file exists in
the shape it does. It publishes a DISAGREEMENT LOG, graded from the first
day, not a bet list — and the simulated expectation goes on the page beside
the real record, so a reader can see what the null hypothesis predicted before
the first game settled.

The upgrade that would make it a real bet board is not a better edge floor. It
is giving the model eyes: the probable starting pitchers.

── AND THAT UPGRADE IS NOW HERE, SO: WHY WAS IT NOT THERE FIRST ─────────────

Donovan, reasonably: "why didn't the moneyline shit have the pitchers."

Not because the data was missing. Every row of today_slim.json carries 114
pitcher fields -- era, fip, whip, k9, bb9, hr9, the arsenal, the mistake pitch
-- and because each row is a batter facing the OPPOSING starter, grouping one
slate by game_pk hands you both probable starters and their full season lines.
It has been published every day for months.

The reason was that the first version imported playoff_odds.win_probability
and stopped, with "one team model, so the two bots cannot disagree" written
above it as though that settled the question. It is a real principle in the
wrong place. A season-long team model describes an AVERAGE game for that team.
A moneyline is one specific game, and the largest single input to one specific
game is who is pitching it. Reusing the season model was the tidy choice and
the wrong one -- and what caught it was the simulation, not the reasoning,
which is the part worth remembering.

The team model is still shared, and it is now the BASE rather than the whole
answer: starter_shift moves it by how much better or worse tonight's starters
are than the rest of the slate's.

── DE-VIGGING, AND WHY THE SIMPLE METHOD IS NAMED AS SIMPLE ─────────────────

Two American prices imply two probabilities that sum to more than one; the
excess is the book's margin. This divides it out proportionally, which is the
standard first method and is known to shade favourites slightly. Shin and
power methods handle that better and need assumptions this file does not want
to make silently. Proportional, named, and the raw hold is published beside
every line so a reader can see how much was removed.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playoff_odds import Team, win_probability  # noqa: E402
import moneyball  # noqa: E402

# ── THE BASE IS OBP AND SLG NOW, NOT THE RECORD (2026-09-05) ─────────────────
#
# Donovan: "linear regression, OBP and SLG over BA, weighted — think Moneyball,
# Oakland A's. And tell me how that does out of sample."
#
# bots/moneyball.py is that model and its own walk-forward harness. Here it
# REPLACES win_probability as the team base whenever both sides have usable
# rates (20+ games, sane numbers); the record-only log5 stays as the fallback
# for the first weeks of a season and for a team the rates feed missed, and
# every pick says which base priced it. The starters sit on top of either,
# unchanged. The out-of-sample grade is published by `python bots/moneyball.py`
# as moneyball_backtest.json and rendered beside this board -- the number is
# the bot's to earn, not this comment's to claim.

# A model that sees only records has no business acting on small disagreements.
# Five points is wide: it is roughly the gap between a coin flip and a -120
# favourite, which is more than a record-only model can claim to resolve.
EDGE_FLOOR = 0.05

# A book quoting worse than this on the side we like is not worth the trip,
# whatever the model thinks. Stated so "no pick" is never a silent outcome.
WORST_PRICE = -250


# ── GIVING THE MODEL EYES ───────────────────────────────────────────────────
#
# Four constants, and only one is a judgement call. They are separated so that
# is obvious rather than buried.

# How long a starter is assumed to go. The rest of the game is bullpen, which
# is already inside the team's season record and needs no separate term. 5.5 is
# roughly the modern average start and is an ASSUMPTION, not a measurement --
# named so it can be replaced with a real per-pitcher number the day the slate
# publishes innings per start.
STARTER_INNINGS = 5.5

# Runs to wins, in one game. Pythagenpat at a 4.5-runs-per-game environment
# gives dW/dRA = -x / (4 * RA) with x ~ 1.83, which is -0.102. Derived, not
# chosen: at a neutral game, one extra expected run allowed costs about ten
# points of win probability.
RUNS_TO_WINS = 0.102

# THE JUDGEMENT CALL, AND THE ONLY ONE. A team's season record already contains
# the average quality of its own rotation, so adjusting by (slate average minus
# tonight's starter) double-counts a little -- for a team whose rotation is
# unusually good, the "average" baked into its record is not the slate average.
# Correcting that properly needs each team's own rotation baseline, which is a
# second pass over the season the slate does not publish. Damping is the crude
# alternative. 0.75 is CHOSEN, not derived, and is written down as chosen. The
# cost of getting it wrong is that starter quality is weighted a quarter too
# little or too much -- a smaller error than ignoring pitchers (version one) or
# trusting them completely (this version at 1.0).
STARTER_WEIGHT = 0.75

# Used only when a slate is too thin to compute its own baseline.
FALLBACK_LEAGUE_FIP = 4.20


def league_baseline(fips) -> float:
    """The average starter on tonight's slate, computed from tonight's slate.

    Self-calibrating rather than a hardcoded constant, for two reasons and the
    second is the better one: a fixed number goes stale as the run environment
    moves, and a baseline taken from the same pitchers being compared means the
    STARTERS' DEVIATIONS SUM TO ZERO. The bot cannot decide that everyone
    pitching tonight is above average; it can only rank tonight's arms against
    each other.

    CORRECTED, because the first version of this comment claimed the adjustment
    was "zero-sum across the slate" and a test caught it: that is false. The
    deviations are zero-sum per STARTER, not per GAME. If the better arm happens
    to be at home in every game on a slate, every shift points at the home side
    and their sum is large — which is correct, not a bug, and worth knowing when
    reading a night where the picks all lean one way.
    """
    clean = []
    for f in fips or []:
        try:
            v = float(f)
        except (TypeError, ValueError):
            continue
        if 0.5 < v < 12.0:
            clean.append(v)
    if len(clean) < 4:
        return FALLBACK_LEAGUE_FIP
    return sum(clean) / len(clean)


def starter_shift(home_fip, away_fip, baseline: float) -> float:
    """Win-probability shift for the HOME side from the two starters.

    Positive means the home starter is the better of the two relative to the
    slate. A missing starter on either side returns 0.0 -- "no pitcher known"
    is not the same as "an average pitcher", but it is the only honest default,
    because guessing puts a fabricated number inside a price.
    """
    try:
        h, a = float(home_fip), float(away_fip)
    except (TypeError, ValueError):
        return 0.0
    if not (0.5 < h < 12.0 and 0.5 < a < 12.0):
        return 0.0
    home_saved = (baseline - h) * STARTER_INNINGS / 9.0
    away_saved = (baseline - a) * STARTER_INNINGS / 9.0
    return (home_saved - away_saved) * RUNS_TO_WINS * STARTER_WEIGHT


def team_base(home_strength: float, away_strength: float,
              home_rates=None, away_rates=None, league=None,
              coef: moneyball.Coef = moneyball.PRIOR,
              blend_w: float = moneyball.DEFAULT_BLEND) -> tuple[float, str]:
    """P(home) before the starters, and the name of what produced it.

    "Merge the good findings" (Donovan, 2026-09-05): when both sides have
    usable rates the base is the BLEND of OBP/SLG and the record at the
    weight the walk-forward chose (moneyball.blend_sweep). Only when a side
    has no usable rates does the record stand alone, and the pick says so.
    """
    p_rates, p_rec = base_parts(home_strength, away_strength, home_rates, away_rates, league, coef)
    if p_rates is not None:
        w = min(1.0, max(0.0, float(blend_w)))
        return moneyball.blend(p_rates, p_rec, w), f"blend {int(round(w * 100))}/{int(round((1 - w) * 100))}"
    return p_rec, "record"


def base_parts(home_strength: float, away_strength: float,
               home_rates=None, away_rates=None, league=None,
               coef: moneyball.Coef = moneyball.PRIOR) -> tuple[float | None, float]:
    """The two ingredients of the base, unmixed -- kept in the price file so
    the blend can be replayed at any weight later (moneyball.roi_sweep)."""
    p_rec = win_probability(home_strength, away_strength)
    p_rates = None
    if home_rates is not None and away_rates is not None and league is not None:
        p_rates = moneyball.game_probability(home_rates, away_rates, league, coef)
    return p_rates, p_rec


def game_probability(home_strength: float, away_strength: float,
                     home_fip=None, away_fip=None,
                     baseline: float = FALLBACK_LEAGUE_FIP,
                     home_rates=None, away_rates=None, league=None,
                     coef: moneyball.Coef = moneyball.PRIOR,
                     blend_w: float = moneyball.DEFAULT_BLEND) -> float:
    """The model: a team base (OBP/SLG blended with the record when known,
    else the record), moved by tonight's starters."""
    base, _ = team_base(home_strength, away_strength, home_rates, away_rates, league, coef, blend_w)
    return min(0.95, max(0.05, base + starter_shift(home_fip, away_fip, baseline)))


def implied(american_price: int | float | None) -> float | None:
    """American odds → implied probability, vig included."""
    if american_price is None:
        return None
    p = float(american_price)
    if p == 0:
        return None
    return (-p) / ((-p) + 100.0) if p < 0 else 100.0 / (p + 100.0)


def devig(home_price, away_price) -> tuple[float | None, float | None, float | None]:
    """Proportional de-vig. Returns (home_fair, away_fair, hold)."""
    h, a = implied(home_price), implied(away_price)
    if h is None or a is None:
        return None, None, None
    total = h + a
    if total <= 0:
        return None, None, None
    return h / total, a / total, total - 1.0


def payout(american_price: int | float, stake: float = 1.0) -> float:
    """Profit on a winning bet of `stake`. A loss is simply -stake."""
    p = float(american_price)
    return stake * (p / 100.0) if p > 0 else stake * (100.0 / -p)


@dataclass
class Pick:
    game_pk: int
    date: str
    home: str
    away: str
    side: str            # team abbreviation
    price: int
    model_p: float
    market_p: float
    edge: float
    hold: float
    book: str = ""
    result: str = ""     # 'win' | 'loss' | '' while unsettled
    profit: float = 0.0
    # Published so a reader can see WHERE the model's opinion came from --
    # how much of it was the records and how much was tonight's arms.
    starter_shift: float = 0.0
    home_sp: str = ""
    away_sp: str = ""
    # Which team model priced the base: "blend 60/40" (OBP/SLG merged with
    # the record at the walk-forward's weight) or "record" (log5 on the
    # standings, a side had no usable rates). Published per pick so the grade
    # can be split by base once there are enough of each to split.
    base: str = "record"


@dataclass
class Line:
    game_pk: int
    date: str
    home: str
    away: str
    home_price: int
    away_price: int
    book: str = ""
    # Tonight's probable starters, as FIP. None means not published yet, which
    # is a real and common state a couple of hours before first pitch.
    home_fip: float | None = None
    away_fip: float | None = None
    home_sp: str = ""
    away_sp: str = ""


def make_picks(lines: list[Line], strength: dict[str, float],
               rates: dict[str, "moneyball.TeamRates"] | None = None,
               coef: moneyball.Coef = moneyball.PRIOR,
               blend_w: float = moneyball.DEFAULT_BLEND) -> list[Pick]:
    """One pick per game, or none. Pure: lines and strengths in, picks out.

    The slate's own starters set the baseline (see league_baseline), so this
    takes the whole night at once rather than a game at a time. `rates` maps
    the same keys as `strength` to moneyball.TeamRates; when absent for a
    side, that game is priced from the record, and the pick says so.
    """
    baseline = league_baseline(
        [f for ln in lines for f in (ln.home_fip, ln.away_fip)])
    rates = rates or {}
    league = moneyball.league_of(list({id(t): t for t in rates.values()}.values())) if rates else None
    out: list[Pick] = []
    for ln in lines:
        sh, sa = strength.get(ln.home), strength.get(ln.away)
        if sh is None or sa is None:
            continue
        shift = starter_shift(ln.home_fip, ln.away_fip, baseline)
        _, base_name = team_base(sh, sa, rates.get(ln.home), rates.get(ln.away), league, coef, blend_w)
        model_home = game_probability(sh, sa, ln.home_fip, ln.away_fip, baseline,
                                      rates.get(ln.home), rates.get(ln.away), league, coef, blend_w)
        home_fair, away_fair, hold = devig(ln.home_price, ln.away_price)
        if home_fair is None:
            continue
        home_edge = model_home - home_fair
        away_edge = (1.0 - model_home) - away_fair
        if home_edge >= away_edge:
            side, price, edge = ln.home, ln.home_price, home_edge
            mp, fp = model_home, home_fair
        else:
            side, price, edge = ln.away, ln.away_price, away_edge
            mp, fp = 1.0 - model_home, away_fair
        if edge < EDGE_FLOOR or price < WORST_PRICE:
            continue
        out.append(Pick(
            game_pk=ln.game_pk, date=ln.date, home=ln.home, away=ln.away,
            side=side, price=int(price), model_p=round(mp, 4),
            market_p=round(fp, 4), edge=round(edge, 4),
            hold=round(hold, 4), book=ln.book,
            starter_shift=round(shift, 4),
            home_sp=ln.home_sp, away_sp=ln.away_sp,
            base=base_name,
        ))
    return sorted(out, key=lambda p: -p.edge)


def settle(picks: list[Pick], winners: dict) -> list[Pick]:
    """Attach results. `winners` maps game_pk -- or (date, home, away), which
    is what the live bot uses, because the odds feed's "game_pk" is a hash of
    the provider's event id and not MLB's -- to the winning team's name."""
    for p in picks:
        if p.result:
            continue
        w = winners.get(p.game_pk) or winners.get((p.date, p.home, p.away))
        if not w:
            # A late West Coast game commences the next UTC day.
            try:
                prev = (dt.date.fromisoformat(p.date) - dt.timedelta(days=1)).isoformat()
                w = winners.get((prev, p.home, p.away))
            except ValueError:
                w = None
        if not w:
            continue
        if w == p.side:
            p.result, p.profit = "win", round(payout(p.price), 4)
        else:
            p.result, p.profit = "loss", -1.0
    return picks


def record(picks: list[Pick]) -> dict:
    """The grade. Flat one unit a side, at the price that was actually offered."""
    graded = [p for p in picks if p.result]
    wins = sum(1 for p in graded if p.result == "win")
    staked = float(len(graded))
    profit = round(sum(p.profit for p in graded), 3)
    return {
        "graded": len(graded),
        "wins": wins,
        "losses": len(graded) - wins,
        "win_rate": round(wins / len(graded), 4) if graded else 0.0,
        "units_staked": staked,
        "units_profit": profit,
        "roi": round(profit / staked, 4) if staked else 0.0,
        # What the model needed to hit to break even at the prices it took.
        "breakeven_rate": round(
            sum(p.market_p for p in graded) / len(graded), 4) if graded else 0.0,
    }


def payload(today: list[Pick], history: list[Pick],
            coef: moneyball.Coef | None = None, backtest: dict | None = None,
            blend_w: float = moneyball.DEFAULT_BLEND) -> dict:
    by_base = {
        "blend": record([p for p in history if getattr(p, "base", "record").startswith("blend")
                         or getattr(p, "base", "") == "obp/slg"]),
        "record": record([p for p in history if getattr(p, "base", "record") == "record"]),
    }
    return {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "edge_floor": EDGE_FLOOR,
        "worst_price": WORST_PRICE,
        "method": (
            "A team base from on-base and slugging, not the record: runs per "
            "game by linear regression on OBP and SLG (OBP weighted heavier, "
            "the Moneyball argument), each lineup against the staff it faces, "
            "Pythagenpat to a win probability, home field as an odds "
            "multiplier. Batting average is not an input. That is merged with "
            "the record-only log5 at the weight the walk-forward backtest found "
            "best; before a team has 20 games the record stands alone, and each "
            "pick says which priced it. Then tonight's probable starters move it, measured "
            "against the average starter on this slate. It still cannot see "
            "tonight's lineup card, the bullpen, injuries or travel, and the "
            "market can, so a large disagreement may still be the model being "
            "behind rather than right. Prices are de-vigged proportionally "
            "(the simple method; it shades favourites slightly) with the book's "
            "raw hold published beside every line. "
            f"Nothing is picked below a {int(EDGE_FLOOR * 100)}-point disagreement."
        ),
        "team_model": {
            "coef": ({"intercept": round(coef.intercept, 3), "obp": round(coef.obp, 2),
                      "slg": round(coef.slg, 2), "obp_per_slg": round(coef.obp_per_slg, 2),
                      "rows": coef.rows, "prior_share": coef.shrink} if coef else None),
            # The walk-forward grade from bots/moneyball.py, if it has run.
            "out_of_sample": backtest,
            "blend_weight": blend_w,
        },
        "record_by_base": by_base,
        "starter_weight": STARTER_WEIGHT,
        "starter_innings": STARTER_INNINGS,
        "record": record(history),
        "today": [p.__dict__ for p in today],
        "history": [p.__dict__ for p in history[-400:]],
    }


# ── the small part ───────────────────────────────────────────────────────────

def fetch_lines(regions: str = "us") -> list[Line]:
    """Game moneylines from theoddsapi /odds/ with markets=h2h.

    Uses odds_fetch's own api() helper so the key handling, the forensics
    record and the error shapes stay in one place.
    """
    from odds_fetch import api, SPORT, american  # noqa: E402

    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        print("moneyline: no ODDS_API_KEY — nothing to fetch", file=sys.stderr)
        return []
    raw = api("/odds/", key, sport_key=SPORT, markets="h2h",
              regions=regions, oddsFormat="american")
    events = raw if isinstance(raw, list) else (raw or {}).get("data") or []
    out: list[Line] = []
    for ev in events:
        home = str(ev.get("home_team") or ev.get("homeTeam") or "")
        away = str(ev.get("away_team") or ev.get("awayTeam") or "")
        best: dict[str, tuple[int, str]] = {}
        for bk in ev.get("bookmakers") or []:
            for mk in bk.get("markets") or []:
                if (mk.get("key") or "") != "h2h":
                    continue
                for oc in mk.get("outcomes") or []:
                    nm = str(oc.get("name") or "")
                    pr = american(oc.get("price"))
                    if pr is None:
                        continue
                    # Best available price on each side, which is what a
                    # bettor would actually get.
                    if nm not in best or pr > best[nm][0]:
                        best[nm] = (pr, str(bk.get("title") or bk.get("key") or ""))
        if home in best and away in best:
            out.append(Line(
                game_pk=int(str(ev.get("id") or "0").replace("-", "")[:9] or 0),
                date=str(ev.get("commence_time") or "")[:10],
                home=home, away=away,
                home_price=best[home][0], away_price=best[away][0],
                book=best[home][1],
            ))
    return out


def fetch_starters(slate_url: str = "") -> dict:
    """Tonight's probable starters, from the slate the site already publishes.

    HOW THE SLATE ENCODES THIS, because it is not obvious and it is the whole
    trick: every row is a HITTER, and the pitcher_* fields on that row describe
    the pitcher he is facing — the OPPOSING starter. So grouping one slate by
    game_pk and reading each team's rows gives you the other team's starter,
    and doing it for both teams in a game gives you both. No extra request, no
    statsapi call, no new field: it has been sitting in today_slim.json every
    day for months.

    Returns {(game_pk, team_abbr): {"fip": float|None, "name": str}} keyed by
    the team the pitcher THROWS FOR, having inverted the facing relationship.
    """
    import requests

    url = slate_url or (
        "https://raw.githubusercontent.com/donthebuilder/"
        "MLB-HR-DASHBOARD-STREAMLIT/data/public/data/current/today_slim.json")
    try:
        rows = requests.get(url, timeout=45).json()
    except Exception as e:
        print(f"moneyline: could not read the slate ({type(e).__name__}) — "
              "picks will fall back to records only", file=sys.stderr)
        return {}
    if not isinstance(rows, list):
        return {}

    out: dict = {}
    for r in rows:
        pk, team, opp = r.get("game_pk"), r.get("team"), r.get("opponent")
        if not pk or not team or not opp:
            continue
        fip = r.get("pitcher_fip")
        if fip is None:
            # ERA is worse (it carries the defence behind him) but a starter
            # with an ERA and no FIP is better than no starter at all, and the
            # payload records which was used.
            fip = r.get("pitcher_era")
        # The pitcher on a hitter's row throws for the OPPONENT.
        key = (pk, opp)
        if key not in out and fip is not None:
            out[key] = {"fip": fip, "name": r.get("pitcher_name") or ""}
    return out


def price_rows(lines: list[Line], strength: dict[str, float], rates=None,
               coef: moneyball.Coef = moneyball.PRIOR,
               blend_w: float = moneyball.DEFAULT_BLEND) -> list[dict]:
    """Every game line the book offered tonight, with the model beside it --
    picked or not. Pure. This is the file that makes a real ROI backtest
    possible later: a closing price is not re-fetchable, so the night it is
    not kept is a night that can never be in the history (2026-09-05)."""
    rates = rates or {}
    league = moneyball.league_of(list({id(t): t for t in rates.values()}.values())) if rates else None
    baseline = league_baseline([f for ln in lines for f in (ln.home_fip, ln.away_fip)])
    out = []
    for ln in lines:
        hf, af, hold = devig(ln.home_price, ln.away_price)
        sh, sa = strength.get(ln.home), strength.get(ln.away)
        model = base_name = p_rates = p_rec = None
        shift = starter_shift(ln.home_fip, ln.away_fip, baseline)
        if sh is not None and sa is not None:
            _, base_name = team_base(sh, sa, rates.get(ln.home), rates.get(ln.away), league, coef, blend_w)
            p_rates, p_rec = base_parts(sh, sa, rates.get(ln.home), rates.get(ln.away), league, coef)
            p_rates = None if p_rates is None else round(p_rates, 4)
            p_rec = round(p_rec, 4)
            model = round(game_probability(sh, sa, ln.home_fip, ln.away_fip, baseline,
                                           rates.get(ln.home), rates.get(ln.away), league, coef, blend_w), 4)
        out.append({
            "game_pk": ln.game_pk, "date": ln.date, "home": ln.home, "away": ln.away,
            "home_price": ln.home_price, "away_price": ln.away_price, "book": ln.book,
            "home_fair": None if hf is None else round(hf, 4),
            "away_fair": None if af is None else round(af, 4),
            "hold": None if hold is None else round(hold, 4),
            "model_home": model, "base": base_name,
            # The unmixed parts, so the blend can be replayed at any weight.
            "p_rates": p_rates, "p_record": p_rec, "starter_shift": round(shift, 4),
            "home_sp": ln.home_sp, "away_sp": ln.away_sp,
            "home_fip": ln.home_fip, "away_fip": ln.away_fip,
        })
    return out


def write_prices(lines, strength, rates, coef, blend_w, out_dir: str) -> str:
    """One file per slate date, moneyline_prices_YYYY-MM-DD.json; re-running
    the same night overwrites with the later (closer to close) prices."""
    if not lines:
        return ""
    date = max((ln.date for ln in lines if ln.date), default=dt.date.today().isoformat())
    path = os.path.join(out_dir, f"moneyline_prices_{date}.json")
    body = {"built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "date": date, "blend_weight": blend_w, "lines": price_rows(lines, strength, rates, coef, blend_w)}
    with open(path, "w") as fh:
        json.dump(body, fh, separators=(",", ":"))
    print(f"  kept {len(lines)} game line(s) in {path}")
    return path


def strengths_from_standings(teams: list[Team]) -> dict[str, float]:
    """Team name → regressed winning percentage, keyed both ways.

    The odds feed names teams in full ("New York Yankees") and the standings
    feed carries both, so both keys are stored rather than guessing a mapping.
    """
    out: dict[str, float] = {}
    for t in teams:
        out[t.abbr] = t.strength
        if t.name:
            out[t.name] = t.strength
    return out


def main(argv=None):
    import argparse
    from playoff_odds import fetch_season

    ap = argparse.ArgumentParser(description="MLB moneyline picks, graded")
    ap.add_argument("--season", type=int, default=dt.date.today().year)
    ap.add_argument("--out", default="public/data/current/moneyline_board.json")
    ap.add_argument("--history", default="public/data/current/moneyline_board.json")
    ap.add_argument("--backtest", default="public/data/current/moneyball_backtest.json")
    args = ap.parse_args(argv)

    teams, _ = fetch_season(args.season)
    if not teams:
        raise SystemExit("standings empty — refusing to publish")
    strength = strengths_from_standings(teams)

    # THE RECORD, AND WHY IT WAS 0-0 FOREVER (2026-09-05). This read the last
    # board from a local path, on a runner that starts from a clean checkout:
    # nothing was ever there, so the history was empty every run -- and even
    # when it wasn't, nothing here ever called settle(). The last board comes
    # off the data branch now, yesterday's "today" picks are folded into the
    # history, and every unsettled pick is graded against the finished games.
    prior: list[Pick] = []
    seen = set()

    def add(row):
        try:
            p = Pick(**{k: v for k, v in row.items() if k in Pick.__dataclass_fields__})
        except Exception:
            return
        key = (p.date, p.home, p.away, p.side)
        if key in seen:
            return
        seen.add(key)
        prior.append(p)

    last = moneyball.fetch_published("moneyline_board.json")
    if not last:
        try:
            with open(args.history) as fh:
                last = json.load(fh) or {}
        except Exception:
            last = {}
    for row in (last or {}).get("history", []) or []:
        add(row)
    for row in (last or {}).get("today", []) or []:
        add(row)
    try:
        finished = moneyball.fetch_finished(args.season)
        by_id = {t.team_id: t.name for t in teams}
        winners = {}
        for g in finished:
            h, a = by_id.get(g.home_id), by_id.get(g.away_id)
            if h and a:
                winners[(g.date, h, a)] = h if g.home_won else a
        before = sum(1 for p in prior if p.result)
        settle(prior, winners)
        print(f"  history {len(prior)} pick(s); settled {sum(1 for p in prior if p.result) - before} new")
    except Exception as e:
        print(f"  could not settle ({e}); record carried forward as is")

    # The team rates, keyed the same two ways as `strength`; and the fitted
    # regression plus the out-of-sample grade from moneyball.py's last run,
    # so the base the bot prices with is the one the page reports on.
    rates: dict[str, moneyball.TeamRates] = {}
    coef = moneyball.PRIOR
    backtest = None
    blend_w = moneyball.DEFAULT_BLEND
    try:
        for tr in moneyball.fetch_rates(args.season):
            rates[tr.abbr] = tr
            for t in teams:
                if t.team_id == tr.team_id and t.name:
                    rates[t.name] = tr
        coef = moneyball.fit_runs(moneyball.fit_rows_from(list({id(t): t for t in rates.values()}.values())))
        print(f"  team rates for {len({id(t) for t in rates.values()})} team(s); "
              f"OBP worth {coef.obp_per_slg:.2f}x SLG per point")
    except Exception as e:  # the record-only base still prices the night
        print(f"  team rates unavailable ({e}); pricing from records")
    try:
        with open(args.backtest) as fh:
            bt = json.load(fh) or {}
            backtest = {k: bt.get(k) for k in ("as_of", "games", "months", "moneyball", "records_only",
                                                "home_always", "coin", "blend", "verdict", "limits", "calibration")}
            if isinstance(bt.get("blend"), dict) and bt["blend"].get("weight") is not None:
                blend_w = float(bt["blend"]["weight"])
            if bt.get("coef"):
                # Prefer the coefficients fitted on every monthly snapshot.
                c = bt["coef"]
                coef = moneyball.Coef(c["intercept"], c["obp"], c["slg"], c.get("rows", 0), c.get("prior_share", 0))
    except Exception:
        pass

    lines = fetch_lines()
    starters = fetch_starters()
    # Attach tonight's arms. The odds feed names teams in full and the slate in
    # abbreviations, so the join is on whatever the standings knew both by --
    # see strengths_from_standings, which stores both keys for the same reason.
    abbr = {}
    for t in teams:
        abbr[t.name] = t.abbr
        abbr[t.abbr] = t.abbr
    for ln in lines:
        for side in ("home", "away"):
            team = abbr.get(getattr(ln, side))
            if not team:
                continue
            for (pk, tm), sp in starters.items():
                if tm == team:
                    setattr(ln, f"{side}_fip", sp.get("fip"))
                    setattr(ln, f"{side}_sp", sp.get("name", ""))
                    break
    known = sum(1 for ln in lines if ln.home_fip and ln.away_fip)
    print(f"  starters known for {known}/{len(lines)} game(s)")
    today = make_picks(lines, strength, rates, coef, blend_w)
    write_prices(lines, strength, rates, coef, blend_w, os.path.dirname(args.out) or ".")
    print(f"  {len(lines)} game line(s) → {len(today)} pick(s) over the edge floor")

    out = payload(today, prior, coef, backtest, blend_w)
    with open(args.out, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"wrote {args.out} — record {out['record']['wins']}-{out['record']['losses']}, "
          f"{out['record']['units_profit']:+} units")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
