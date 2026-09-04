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


def game_probability(home_strength: float, away_strength: float,
                     home_fip=None, away_fip=None,
                     baseline: float = FALLBACK_LEAGUE_FIP) -> float:
    """The model: a season team base, moved by tonight's starters."""
    base = win_probability(home_strength, away_strength)
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


def make_picks(lines: list[Line], strength: dict[str, float]) -> list[Pick]:
    """One pick per game, or none. Pure: lines and strengths in, picks out.

    The slate's own starters set the baseline (see league_baseline), so this
    takes the whole night at once rather than a game at a time.
    """
    baseline = league_baseline(
        [f for ln in lines for f in (ln.home_fip, ln.away_fip)])
    out: list[Pick] = []
    for ln in lines:
        sh, sa = strength.get(ln.home), strength.get(ln.away)
        if sh is None or sa is None:
            continue
        shift = starter_shift(ln.home_fip, ln.away_fip, baseline)
        model_home = game_probability(sh, sa, ln.home_fip, ln.away_fip, baseline)
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
        ))
    return sorted(out, key=lambda p: -p.edge)


def settle(picks: list[Pick], winners: dict[int, str]) -> list[Pick]:
    """Attach results. `winners` maps game_pk to the winning team's abbr."""
    for p in picks:
        w = winners.get(p.game_pk)
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


def payload(today: list[Pick], history: list[Pick]) -> dict:
    return {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "edge_floor": EDGE_FLOOR,
        "worst_price": WORST_PRICE,
        "method": (
            "A team base — log5 on records regressed toward .500, plus home "
            "field, the same model the playoff odds use — moved by tonight's "
            "probable starters, measured against the average starter on this "
            "slate rather than a fixed league number, so the adjustment is "
            "zero-sum across the night. It still cannot see bullpen usage, "
            "injuries or travel, and the market can, so a large disagreement "
            "may still be the model being blind rather than right. Prices are "
            "de-vigged proportionally (the simple method; it shades favourites "
            "slightly) with the book's raw hold published beside every line. "
            f"Nothing is picked below a {int(EDGE_FLOOR * 100)}-point disagreement."
        ),
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
    args = ap.parse_args(argv)

    teams, _ = fetch_season(args.season)
    if not teams:
        raise SystemExit("standings empty — refusing to publish")
    strength = strengths_from_standings(teams)

    prior: list[Pick] = []
    try:
        with open(args.history) as fh:
            for row in (json.load(fh) or {}).get("history", []):
                prior.append(Pick(**row))
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
    today = make_picks(lines, strength)
    print(f"  {len(lines)} game line(s) → {len(today)} pick(s) over the edge floor")

    out = payload(today, prior)
    with open(args.out, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"wrote {args.out} — record {out['record']['wins']}-{out['record']['losses']}, "
          f"{out['record']['units_profit']:+} units")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
