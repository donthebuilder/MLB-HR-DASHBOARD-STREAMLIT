"""🔄 COMEBACKS — who wins after falling behind, and who gives it away.

The third of the four bots Donovan asked for: "most comeback wins."

WHAT COUNTS AS A COMEBACK, AND WHY THIS DEFINITION. A comeback win is a game a
team won after trailing at the end of some completed half-inning. That is the
whole rule. It is not "won by fewer than N runs," not "won in the late
innings," and not a judgement about how dramatic it felt.

THE ONE THING IT CANNOT SEE, SAID UP FRONT. A line score records runs per
half-inning, so this reads the score at each half-inning boundary and nowhere
else. A team that fell behind in the middle of an inning and retook the lead
before the inning ended never appears to have trailed. That is a real
undercount and there is no fixing it from a line score -- it needs play-by-play,
which is a request per game rather than a request per day, for a difference
this board would round away. So the number here is a floor: every comeback it
reports is real, and it misses the ones that lasted less than half an inning.
That is written into the payload, not just into this comment, because a
reader deserves the same caveat the author had.

WHY THE BLOWN LEADS ARE ON THE SAME BOARD. A comeback needs two teams, and the
same game is a triumph for one and a collapse for the other. Publishing only
the flattering half would be choosing a story over the data. Every comeback win
in this file has a matching blown lead in it.

── shape ────────────────────────────────────────────────────────────────────

Same split as bots/playoff_odds.py, for the same reason: everything above
`fetch_season` is pure and importable with no third-party packages, so the
counting can be tested against invented games somewhere the MLB API is
unreachable. The fetching is the small part.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

# A half-inning boundary deficit of this size or more, erased in a win, is
# worth calling out on its own. Six is not a magic model number -- it is the
# threshold the board uses to pick a "biggest" list, and nothing scores off it.
NOTABLE_DEFICIT = 4


@dataclass
class GameLine:
    """One finished game, as a line score.

    `away_innings[i]` and `home_innings[i]` are runs scored in the top and
    bottom of inning i+1. A home team that did not bat in the ninth has a
    shorter list than the away team, which is normal and handled.
    """
    game_pk: int
    date: str
    home: str
    away: str
    away_innings: list[int] = field(default_factory=list)
    home_innings: list[int] = field(default_factory=list)

    @property
    def away_runs(self) -> int:
        return sum(self.away_innings)

    @property
    def home_runs(self) -> int:
        return sum(self.home_innings)


def deficit_track(game: GameLine) -> tuple[int, int]:
    """Largest deficit each side faced at a half-inning boundary.

    Returns (home_max_deficit, away_max_deficit). Baseball order matters: the
    away team bats first, so after the top of the first the score is already
    lopsided from the home team's point of view even though they have not
    batted. That is a real deficit -- a team down 3-0 before coming to the
    plate is behind by three -- so it is counted.
    """
    h = a = 0
    home_max = away_max = 0
    innings = max(len(game.away_innings), len(game.home_innings))
    for i in range(innings):
        if i < len(game.away_innings):
            a += game.away_innings[i]
            home_max = max(home_max, a - h)
        if i < len(game.home_innings):
            h += game.home_innings[i]
            away_max = max(away_max, h - a)
    return home_max, away_max


@dataclass
class TeamRow:
    abbr: str
    wins: int = 0
    losses: int = 0
    comeback_wins: int = 0
    blown_leads: int = 0
    biggest_comeback: int = 0
    biggest_blown: int = 0
    wire_to_wire_wins: int = 0   # won without ever trailing
    led_and_lost_games: list = field(default_factory=list)
    comeback_games: list = field(default_factory=list)


def tally(games: list[GameLine]) -> list[TeamRow]:
    """Count comebacks and collapses. Pure: games in, rows out."""
    rows: dict[str, TeamRow] = {}

    def row(abbr: str) -> TeamRow:
        if abbr not in rows:
            rows[abbr] = TeamRow(abbr=abbr)
        return rows[abbr]

    for g in games:
        hr, ar = g.home_runs, g.away_runs
        if hr == ar:
            # A tie is a suspended or in-progress game, not a result. Skipping
            # it is the only honest option: counting it as neither a win nor a
            # loss for either side keeps every total consistent.
            continue
        home_def, away_def = deficit_track(g)
        home_won = hr > ar
        winner, loser = (g.home, g.away) if home_won else (g.away, g.home)
        w_def = home_def if home_won else away_def
        l_lead = away_def if home_won else home_def

        wr, lr = row(winner), row(loser)
        wr.wins += 1
        lr.losses += 1

        if w_def > 0:
            wr.comeback_wins += 1
            wr.biggest_comeback = max(wr.biggest_comeback, w_def)
            wr.comeback_games.append({"game_pk": g.game_pk, "date": g.date,
                                      "opp": loser, "deficit": w_def})
            lr.blown_leads += 1
            lr.biggest_blown = max(lr.biggest_blown, w_def)
            lr.led_and_lost_games.append({"game_pk": g.game_pk, "date": g.date,
                                          "opp": winner, "lead": w_def})
        else:
            wr.wire_to_wire_wins += 1
        # l_lead is w_def by construction when the winner trailed; kept as a
        # name so the symmetry is readable rather than implied.
        del l_lead

    for r in rows.values():
        r.comeback_games.sort(key=lambda x: -x["deficit"])
        r.led_and_lost_games.sort(key=lambda x: -x["lead"])
        del r.comeback_games[8:]
        del r.led_and_lost_games[8:]
    return sorted(rows.values(), key=lambda r: (-r.comeback_wins, -r.biggest_comeback, r.abbr))


def payload(rows: list[TeamRow], games_read: int, season: int) -> dict:
    return {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "season": season,
        "games": games_read,
        "notable_deficit": NOTABLE_DEFICIT,
        "method": (
            "A comeback win is a game won after trailing at the end of a completed "
            "half-inning. Read from line scores, so a lead that changed hands "
            "inside a single inning is invisible here — every comeback listed is "
            "real, and the count is a floor rather than an exact total. Every "
            "comeback win is also somebody's blown lead, and both are published."
        ),
        "teams": [{
            "abbr": r.abbr, "wins": r.wins, "losses": r.losses,
            "comeback_wins": r.comeback_wins, "blown_leads": r.blown_leads,
            "biggest_comeback": r.biggest_comeback, "biggest_blown": r.biggest_blown,
            "wire_to_wire_wins": r.wire_to_wire_wins,
            "comeback_rate": round(r.comeback_wins / r.wins, 4) if r.wins else 0.0,
            "top_comebacks": r.comeback_games,
            "top_collapses": r.led_and_lost_games,
        } for r in rows],
    }


# ── the small part: getting the season ───────────────────────────────────────

STATS_API = "https://statsapi.mlb.com/api/v1"


def fetch_season(season: int, timeout: int = 30) -> list[GameLine]:
    """Every finished regular-season game as a line score.

    One request per calendar month rather than one per game: the schedule
    endpoint hydrates line scores, so a whole month of baseball is a single
    response. A season is six or seven requests instead of two and a half
    thousand.
    """
    import requests  # local: the counting half must import with no deps

    out: list[GameLine] = []
    for month in range(3, 11):
        start = dt.date(season, month, 1)
        end = (dt.date(season + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1))
        try:
            r = requests.get(f"{STATS_API}/schedule", params={
                "sportId": 1, "startDate": start.isoformat(), "endDate": end.isoformat(),
                "gameType": "R", "hydrate": "linescore",
            }, timeout=timeout)
            if not r.ok:
                continue
            for day in (r.json() or {}).get("dates", []):
                for g in day.get("games", []):
                    if ((g.get("status") or {}).get("abstractGameState") or "") != "Final":
                        continue
                    ls = g.get("linescore") or {}
                    innings = ls.get("innings") or []
                    if not innings:
                        continue
                    teams = g.get("teams") or {}
                    home = ((teams.get("home") or {}).get("team") or {})
                    away = ((teams.get("away") or {}).get("team") or {})
                    out.append(GameLine(
                        game_pk=int(g.get("gamePk") or 0),
                        date=str(g.get("officialDate") or day.get("date") or ""),
                        home=str(home.get("abbreviation") or home.get("teamName") or "?"),
                        away=str(away.get("abbreviation") or away.get("teamName") or "?"),
                        away_innings=[int((i.get("away") or {}).get("runs") or 0) for i in innings],
                        home_innings=[int((i.get("home") or {}).get("runs") or 0)
                                      for i in innings if (i.get("home") or {}).get("runs") is not None],
                    ))
        except Exception:
            # A month that fails is a month missing from the totals, which the
            # `games` count in the payload makes visible. It is not a reason to
            # publish nothing.
            continue
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="MLB comeback wins and blown leads")
    ap.add_argument("--season", type=int, default=dt.date.today().year)
    ap.add_argument("--out", default="public/data/current/comeback_board.json")
    args = ap.parse_args(argv)

    games = fetch_season(args.season)
    if not games:
        raise SystemExit("no finished games came back — refusing to publish an empty board")
    rows = tally(games)
    with open(args.out, "w") as fh:
        json.dump(payload(rows, len(games), args.season), fh, separators=(",", ":"))
    top = rows[0]
    print(f"wrote {args.out} — {len(games)} games, {len(rows)} teams; "
          f"most comebacks {top.abbr} {top.comeback_wins} (biggest {top.biggest_comeback})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
