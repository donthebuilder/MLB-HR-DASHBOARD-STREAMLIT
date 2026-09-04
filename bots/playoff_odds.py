"""🏆 PLAYOFF ODDS AND A WORLD SERIES CHAMPION — the season, ten thousand times.

Donovan asked for four new bots: a moneyline bot, most comeback wins, a
playoff predictor, and "who will win the MLB championship." The last two are
not two bots. They are one machine read at two depths: you cannot answer who
wins the World Series without first answering who is in the field, and once
you are simulating the bracket the champion falls out of the same run. Built
as one, published as one file, so the two numbers can never disagree — which
they would within a week if they were computed separately.

WHAT THIS IS AND IS NOT. It is a Monte Carlo of the rest of the season: take
every team's record, take every game still to be played, play them all ten
thousand times, seed the bracket each time and run October. It is not a
projection system. It has no injuries, no rotations, no trade deadline, no
park factors and no schedule strength beyond who is actually on the schedule.
Everything it knows about a team is its record so far.

That is a deliberate ceiling, not an unfinished draft. A one-number team model
is defensible and legible: a reader can check it. The moment it grows a
proprietary strength rating it becomes a thing you have to trust, and this
site's whole position is that you should not have to. If it is ever worth
more, the honest upgrade is run differential — which is public, measurable and
one field away — not a private rating.

── THE THREE PIECES OF ARITHMETIC, EACH DEFENSIBLE ON ITS OWN ────────────────

1. STRENGTH IS A REGRESSED WINNING PERCENTAGE. A 100-62 team is not a .617
   team forever; some of that is luck. So every record is pulled toward .500
   by a prior worth REGRESSION_GAMES games. In September that prior is a
   rounding error against 140 played games, which is correct — by now the
   record IS mostly signal. In April it would dominate, which is also correct.
   One constant, stated, doing the whole job.

2. ONE GAME IS log5 WITH HOME FIELD. log5 (Bill James) is the standard way to
   turn two winning percentages into a head-to-head probability, and it has
   the properties you want for free: two equal teams give .500, and a team
   that never loses beats a team that never wins. Home field is applied as an
   odds multiplier calibrated to the league's actual home win rate, not as a
   flat addition — adding .035 to a .900 favourite would push him past .935
   for no reason, while multiplying his odds moves him a little and moves a
   coin-flip game the full amount, which is how home field actually behaves.

3. TIES ARE COIN FLIPS, AND THAT IS A KNOWN APPROXIMATION. Real MLB tiebreaks
   run head-to-head, then intradivision, then intraleague. Simulating those
   needs the head-to-head grid of games not yet played, which is knowable but
   is a second data source for an effect that changes a team's odds by a
   fraction of a point. It is a coin flip here and it is written down here,
   which is the deal.

── WHY THE MATH DOES NOT TOUCH THE NETWORK ───────────────────────────────────

Everything below `simulate` is pure: standings in, odds out, no I/O, no clock,
no global state. That is not tidiness for its own sake. This module was written
somewhere statsapi.mlb.com is unreachable, so the only way to know the bracket
logic is right was to be able to run it against made-up seasons — a team that
wins every remaining game must make the playoffs every time, exactly twelve
teams must qualify in every single simulation, and the champion probabilities
must sum to one. Those are now tests. The fetching is the small part and it
mirrors what mlb_dashboard.py already does with the same endpoints.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field

# ── constants, all of them ───────────────────────────────────────────────────

# Games of .500 prior mixed into every record. 60 is a season's worth of
# "we do not really know yet" — it halves an April sample and barely dents a
# September one, which is the behaviour you want from a prior.
REGRESSION_GAMES = 60

# The league's long-run home win rate. MLB has sat near .535 for a decade; it
# is the single number home-field advantage is worth, and it is applied as an
# odds ratio (see _home_odds_ratio) rather than a flat probability bump.
HOME_WIN_RATE = 0.535

# 12-team field: three division winners plus three wild cards per league.
DIVISION_WINNERS_PER_LEAGUE = 3
WILD_CARDS_PER_LEAGUE = 3
PLAYOFF_TEAMS_PER_LEAGUE = DIVISION_WINNERS_PER_LEAGUE + WILD_CARDS_PER_LEAGUE

DEFAULT_SIMS = 10000


@dataclass
class Team:
    """A team as this model sees it: a record, a division, and nothing else."""
    team_id: int
    abbr: str
    name: str
    league: str          # 'AL' | 'NL'
    division: str        # 'AL East', 'NL Central', ...
    wins: int = 0
    losses: int = 0

    @property
    def played(self) -> int:
        return self.wins + self.losses

    @property
    def pct(self) -> float:
        return self.wins / self.played if self.played else 0.5

    @property
    def strength(self) -> float:
        """Winning percentage regressed toward .500 — see note 1 up top."""
        return (self.wins + REGRESSION_GAMES * 0.5) / (self.played + REGRESSION_GAMES)


@dataclass
class Game:
    home_id: int
    away_id: int


@dataclass
class Odds:
    """One team's answer. Every field is a share of simulated seasons."""
    team_id: int
    abbr: str
    name: str
    league: str
    division: str
    wins: int
    losses: int
    strength: float
    proj_wins: float = 0.0
    make_playoffs: float = 0.0
    win_division: float = 0.0
    wild_card: float = 0.0
    top_seed: float = 0.0
    win_league: float = 0.0
    win_world_series: float = 0.0
    seed_counts: dict = field(default_factory=dict)


# ── the arithmetic ───────────────────────────────────────────────────────────

def _home_odds_ratio(home_win_rate: float = HOME_WIN_RATE) -> float:
    """Home field as a multiplier on the odds, not an addition to the rate.

    At a neutral .500 the multiplier reproduces the league's home win rate
    exactly. At the extremes it behaves: a .950 favourite at home gains under
    a point, where a flat +.035 would have shoved him to .985.
    """
    return home_win_rate / (1.0 - home_win_rate)


def win_probability(home: float, away: float, neutral: bool = False) -> float:
    """log5, then home field. `home`/`away` are regressed winning percentages."""
    # log5: P(A beats B) = (a - ab) / (a + b - 2ab)
    denom = home + away - 2.0 * home * away
    if denom <= 0:
        p = 0.5
    else:
        p = (home - home * away) / denom
    if neutral:
        return min(0.999, max(0.001, p))
    # convert to odds, apply the home multiplier, convert back
    if p <= 0.0:
        return 0.001
    if p >= 1.0:
        return 0.999
    odds = (p / (1.0 - p)) * _home_odds_ratio()
    return min(0.999, max(0.001, odds / (1.0 + odds)))


def _play_series(rng: random.Random, hi: float, lo: float, length: int,
                 home_pattern: list[bool]) -> bool:
    """True if the higher seed wins. `home_pattern[i]` is True when he is home."""
    need = length // 2 + 1
    hi_wins = lo_wins = 0
    for i in range(length):
        at_home = home_pattern[i] if i < len(home_pattern) else True
        p = win_probability(hi, lo) if at_home else 1.0 - win_probability(lo, hi)
        if rng.random() < p:
            hi_wins += 1
        else:
            lo_wins += 1
        if hi_wins == need or lo_wins == need:
            break
    return hi_wins > lo_wins


# The higher seed hosts every game of the wild-card round, games 1/2 and 5 of
# an LDS, and games 1/2 and 6/7 of an LCS or World Series. These are the real
# formats; they matter because home field is the only edge a seed confers.
WC_HOME = [True, True, True]
LDS_HOME = [True, True, False, False, True]
BEST_OF_7_HOME = [True, True, False, False, False, True, True]


def _seed_league(teams: list[Team], final_wins: dict[int, int],
                 rng: random.Random) -> list[Team]:
    """The six qualifiers for one league, in seed order.

    Division winners take seeds 1-3 by record; the best three remaining teams
    in the whole league take 4-6. Ties are broken by a coin flip — see note 3.
    """
    def key(t: Team):
        return (final_wins[t.team_id], rng.random())

    by_division: dict[str, list[Team]] = defaultdict(list)
    for t in teams:
        by_division[t.division].append(t)

    winners = sorted((max(group, key=key) for group in by_division.values()),
                     key=key, reverse=True)
    winner_ids = {t.team_id for t in winners}
    rest = sorted((t for t in teams if t.team_id not in winner_ids),
                  key=key, reverse=True)
    return winners[:DIVISION_WINNERS_PER_LEAGUE] + rest[:WILD_CARDS_PER_LEAGUE]


def _run_league_bracket(seeds: list[Team], rng: random.Random) -> Team:
    """Seeds 1-2 bye · 3v6 and 4v5 best-of-3 · LDS best-of-5 · LCS best-of-7."""
    s1, s2, s3, s4, s5, s6 = seeds
    wc_a = s3 if _play_series(rng, s3.strength, s6.strength, 3, WC_HOME) else s6
    wc_b = s4 if _play_series(rng, s4.strength, s5.strength, 3, WC_HOME) else s5
    # The 1 seed draws the LOWER surviving seed. seeds.index is the seed order.
    lower, higher = sorted((wc_a, wc_b), key=lambda t: seeds.index(t), reverse=True)
    lds_a = s1 if _play_series(rng, s1.strength, lower.strength, 5, LDS_HOME) else lower
    lds_b = s2 if _play_series(rng, s2.strength, higher.strength, 5, LDS_HOME) else higher
    hi, lo = (lds_a, lds_b) if seeds.index(lds_a) <= seeds.index(lds_b) else (lds_b, lds_a)
    return hi if _play_series(rng, hi.strength, lo.strength, 7, BEST_OF_7_HOME) else lo


def simulate(teams: list[Team], remaining: list[Game], sims: int = DEFAULT_SIMS,
             seed: int | None = None) -> list[Odds]:
    """Play the rest of the season `sims` times and count what happened.

    Deterministic for a given `seed`, which is what makes the tests possible
    and what stops the published file jittering between runs on the same data.
    """
    rng = random.Random(seed)
    by_id = {t.team_id: t for t in teams}
    leagues: dict[str, list[Team]] = defaultdict(list)
    for t in teams:
        leagues[t.league].append(t)

    tally = {t.team_id: Odds(
        team_id=t.team_id, abbr=t.abbr, name=t.name, league=t.league,
        division=t.division, wins=t.wins, losses=t.losses,
        strength=round(t.strength, 4), seed_counts={},
    ) for t in teams}

    # Precompute each remaining game's home win probability once. The teams do
    # not change strength during a simulation -- a season where September form
    # feeds back into October is a different and much less defensible model --
    # so this is the same number every time and belongs outside the loop.
    schedule = [(g.home_id, g.away_id,
                 win_probability(by_id[g.home_id].strength, by_id[g.away_id].strength))
                for g in remaining if g.home_id in by_id and g.away_id in by_id]

    for _ in range(sims):
        final = {t.team_id: t.wins for t in teams}
        for home_id, away_id, p in schedule:
            if rng.random() < p:
                final[home_id] += 1
            else:
                final[away_id] += 1

        champions = {}
        for league, members in leagues.items():
            seeds = _seed_league(members, final, rng)
            winner_ids = {t.team_id for t in seeds[:DIVISION_WINNERS_PER_LEAGUE]}
            for i, t in enumerate(seeds):
                row = tally[t.team_id]
                row.make_playoffs += 1
                row.seed_counts[i + 1] = row.seed_counts.get(i + 1, 0) + 1
                if t.team_id in winner_ids:
                    row.win_division += 1
                else:
                    row.wild_card += 1
                if i == 0:
                    row.top_seed += 1
            champions[league] = _run_league_bracket(seeds, rng)

        for league, pennant in champions.items():
            tally[pennant.team_id].win_league += 1
        if len(champions) == 2:
            a, b = list(champions.values())
            hi, lo = (a, b) if a.strength >= b.strength else (b, a)
            ws = hi if _play_series(rng, hi.strength, lo.strength, 7, BEST_OF_7_HOME) else lo
            tally[ws.team_id].win_world_series += 1
        elif len(champions) == 1:
            # A single-league input is a test fixture, not a season. Its
            # pennant winner is its champion; saying nothing would make the
            # totals silently not sum to 1.
            tally[next(iter(champions.values())).team_id].win_world_series += 1

        for tid, w in final.items():
            tally[tid].proj_wins += w

    for row in tally.values():
        row.proj_wins = round(row.proj_wins / sims, 1)
        for fname in ("make_playoffs", "win_division", "wild_card", "top_seed",
                      "win_league", "win_world_series"):
            setattr(row, fname, round(getattr(row, fname) / sims, 4))
        row.seed_counts = {k: round(v / sims, 4) for k, v in sorted(row.seed_counts.items())}

    return sorted(tally.values(), key=lambda r: (-r.win_world_series, -r.make_playoffs, r.abbr))


def payload(rows: list[Odds], sims: int, source_note: str = "") -> dict:
    """The published shape. Percentages stay as 0-1 shares; the site formats."""
    return {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sims": sims,
        "regression_games": REGRESSION_GAMES,
        "home_win_rate": HOME_WIN_RATE,
        "method": (
            "Monte Carlo of every remaining game. Team strength is winning "
            f"percentage regressed toward .500 by {REGRESSION_GAMES} games; one game is "
            "log5 with home field applied as an odds multiplier; ties are coin "
            "flips. No injuries, rotations, or trades — everything it knows "
            "about a team is its record."
        ),
        "note": source_note,
        "teams": [{
            "team_id": r.team_id, "abbr": r.abbr, "name": r.name,
            "league": r.league, "division": r.division,
            "wins": r.wins, "losses": r.losses, "strength": r.strength,
            "proj_wins": r.proj_wins,
            "make_playoffs": r.make_playoffs, "win_division": r.win_division,
            "wild_card": r.wild_card, "top_seed": r.top_seed,
            "win_league": r.win_league, "win_world_series": r.win_world_series,
            "seeds": r.seed_counts,
        } for r in rows],
    }


# ── the small part: getting the season ───────────────────────────────────────

STATS_API = "https://statsapi.mlb.com/api/v1"


def fetch_season(season: int, on_date: str | None = None, timeout: int = 20):
    """Standings and every unplayed game. Requires the network; kept trivial.

    Split out from everything above so the model can be tested where this
    cannot run. Mirrors the endpoints and the defensive style mlb_dashboard.py
    already uses against the same API.
    """
    import requests  # local import: the pure half must import with no deps

    teams: list[Team] = []
    r = requests.get(f"{STATS_API}/standings",
                     params={"leagueId": "103,104", "season": season,
                             "standingsTypes": "regularSeason"}, timeout=timeout)
    r.raise_for_status()
    for record in (r.json() or {}).get("records", []):
        division = ((record.get("division") or {}).get("id"))
        league_id = ((record.get("league") or {}).get("id"))
        league = "AL" if league_id == 103 else "NL"
        for tr in record.get("teamRecords", []):
            t = tr.get("team") or {}
            teams.append(Team(
                team_id=int(t.get("id") or 0),
                abbr=str(t.get("abbreviation") or t.get("teamName") or "")[:3].upper(),
                name=str(t.get("name") or ""),
                league=league,
                division=str(division or t.get("division", {}).get("id") or "?"),
                wins=int(tr.get("wins") or 0),
                losses=int(tr.get("losses") or 0),
            ))

    # Division names read better than ids on a board, and the divisions
    # endpoint is one cheap call.
    try:
        dr = requests.get(f"{STATS_API}/divisions", params={"sportId": 1}, timeout=timeout)
        if dr.ok:
            names = {str(d.get("id")): d.get("nameShort") or d.get("name")
                     for d in (dr.json() or {}).get("divisions", [])}
            for t in teams:
                t.division = names.get(t.division, t.division)
    except Exception:
        pass

    start = on_date or dt.date.today().isoformat()
    end = f"{season}-11-01"
    sr = requests.get(f"{STATS_API}/schedule",
                      params={"sportId": 1, "startDate": start, "endDate": end,
                              "gameType": "R"}, timeout=timeout)
    sr.raise_for_status()
    remaining: list[Game] = []
    for day in (sr.json() or {}).get("dates", []):
        for g in day.get("games", []):
            state = ((g.get("status") or {}).get("abstractGameState") or "")
            if state == "Final":
                continue
            sides = g.get("teams") or {}
            home = int(((sides.get("home") or {}).get("team") or {}).get("id") or 0)
            away = int(((sides.get("away") or {}).get("team") or {}).get("id") or 0)
            if home and away:
                remaining.append(Game(home_id=home, away_id=away))
    return teams, remaining


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="MLB playoff and World Series odds")
    ap.add_argument("--season", type=int, default=dt.date.today().year)
    ap.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="public/data/current/playoff_odds.json")
    args = ap.parse_args(argv)

    teams, remaining = fetch_season(args.season)
    if not teams:
        raise SystemExit("standings came back empty — refusing to publish an empty board")
    rows = simulate(teams, remaining, sims=args.sims, seed=args.seed)
    note = f"{len(remaining)} regular-season games left when this ran."
    out = payload(rows, args.sims, note)
    with open(args.out, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    top = rows[0]
    print(f"wrote {args.out} — {len(rows)} teams, {args.sims} sims, "
          f"{len(remaining)} games left; favourite {top.abbr} {top.win_world_series:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
