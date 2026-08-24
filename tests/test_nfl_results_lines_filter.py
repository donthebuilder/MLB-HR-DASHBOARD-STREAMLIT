"""bots/nfl/nfl_results.py -- the published `lines` payload must keep a
market's value by POSITION ELIGIBILITY, not by truthiness.

THE BUG. outcomes() computes all 7 markets for every player via
`float(r[f"_o_{k}"] or 0.0)`, defaulting a missing/inapplicable stat to 0.0 --
the exact same float a player produces when he genuinely recorded zero of
that stat. The old `lines` construction was
`{k: v for k, v in vals.items() if v}`, which drops every 0.0 regardless of
which of those two cases it is. Because TD's bar is 1 (a whole-number count),
0 is the ONLY way to miss a TD line, so that one market could never publish a
miss at all -- confirmed against a live snapshot (151 TD lines, minimum value
1.0, zero misses ever published). The site's Accountability tab
(components/nfl/tabs/Accountability.js) has a hardcoded degenerate-market
flag for exactly this reason.

THE FIX. eligible_lines() keeps {market: value} only where the player's
position is in that market's own MODELS[market]["pos"] list -- the same
eligibility nfl_scoring.score() filters its own scoring pool on. A position
list, not a truthy check, is the only thing that can tell "he really went
scoreless" apart from "this market isn't his."

Run: python3 -m pytest tests/test_nfl_results_lines_filter.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

import polars as pl  # noqa: E402

from nfl_results import STATS, eligible_lines, outcomes  # noqa: E402
from nfl_scoring import MODELS  # noqa: E402


def _lines_df(rows):
    """Build a DataFrame shaped like _reg_lines()/_pre_lines()'s output:
    player_id, name, team, position, + the raw STATS columns (missing stat
    keys default to 0.0, matching how both real loaders zero-fill)."""
    full_rows = []
    for r in rows:
        row = {"player_id": r["player_id"], "name": r["name"], "team": r.get("team", ""),
               "position": r["position"]}
        for c in STATS:
            row[c] = float(r.get(c, 0.0))
        full_rows.append(row)
    schema = {"player_id": pl.Utf8, "name": pl.Utf8, "team": pl.Utf8, "position": pl.Utf8,
              **{c: pl.Float64 for c in STATS}}
    return pl.DataFrame(full_rows, schema=schema)


def _positions_from(lines_df):
    return {str(r["player_id"]): r.get("position") or "" for r in lines_df.iter_rows(named=True)}


def test_rb_with_zero_tds_is_a_real_published_miss():
    """An RB (TD-eligible per MODELS['TD']['pos']) who scored 0 TDs must
    appear in `lines` as TD: 0.0 -- the whole point of the fix. Under the
    old `if v` filter this row vanished silently, identical to a player for
    whom TD never applies."""
    lines_df = _lines_df([
        {"player_id": "RB1", "name": "Ghost Back", "position": "RB",
         "carries": 14.0, "rushing_yards": 61.0, "rushing_tds": 0.0,
         "receptions": 1.0, "receiving_yards": 4.0, "receiving_tds": 0.0},
    ])
    actual = outcomes(lines_df)
    positions = _positions_from(lines_df)
    lines = eligible_lines(actual, positions)

    assert "TD" in lines["RB1"], "an RB's TD line must be published even at 0"
    assert lines["RB1"]["TD"] == 0.0


def test_qb_never_gets_a_td_line_at_all():
    """A QB is not in MODELS['TD']['pos'] -- his (rushing_tds+receiving_tds)
    outcome is structurally always 0 in this dataset shape, and must not
    appear as a TD key at all, published or not."""
    lines_df = _lines_df([
        {"player_id": "QB1", "name": "Pocket Passer", "position": "QB",
         "passing_yards": 245.0},
    ])
    actual = outcomes(lines_df)
    positions = _positions_from(lines_df)
    lines = eligible_lines(actual, positions)

    assert "TD" not in lines["QB1"], "TD must not apply to a QB"
    # PASS_YDS is QB-eligible and genuinely nonzero -- must still publish.
    assert lines["QB1"]["PASS_YDS"] == 245.0


def test_rb_with_zero_rush_yards_is_a_real_published_miss_non_td_market():
    """Not a TD-only fix: an RB who didn't touch the ball this week (0
    rushing yards, a real RUSH_YDS/RUSH_ATT miss) must still publish 0.0 for
    those markets, exactly like the TD case."""
    lines_df = _lines_df([
        {"player_id": "RB2", "name": "Healthy Scratch-ish", "position": "RB",
         "carries": 0.0, "rushing_yards": 0.0,
         "receptions": 3.0, "receiving_yards": 22.0},
    ])
    actual = outcomes(lines_df)
    positions = _positions_from(lines_df)
    lines = eligible_lines(actual, positions)

    assert lines["RB2"]["RUSH_YDS"] == 0.0
    assert lines["RB2"]["RUSH_ATT"] == 0.0
    # REC_YDS is genuinely nonzero and RB-eligible -- confirms the filter
    # isn't accidentally dropping everything for this player.
    assert lines["RB2"]["REC_YDS"] == 22.0


def test_kicker_never_gets_non_kicking_markets():
    """A kicker (position 'K') is eligible only for KICK_PTS. His raw stat
    columns are all 0.0 by construction (he has no carries/targets), so the
    old truthy filter dropped them for the "right" reason by accident here
    -- but the fix must drop them because he's ineligible, not because the
    values happen to be falsy, and it must still publish his real
    KICK_PTS."""
    lines_df = _lines_df([
        {"player_id": "K1", "name": "Automatic Leg", "position": "K",
         "fg_made": 2.0, "pat_made": 3.0},
    ])
    actual = outcomes(lines_df)
    positions = _positions_from(lines_df)
    lines = eligible_lines(actual, positions)

    assert set(lines["K1"].keys()) == {"KICK_PTS"}
    assert lines["K1"]["KICK_PTS"] == 2.0 * 3 + 3.0


def test_unknown_position_publishes_nothing_rather_than_guessing():
    """A player whose position couldn't be joined (empty string, the same
    default main() uses for a positions.get() miss) must be dropped from
    every market -- the same drop-rather-than-guess call _espn_to_gsis()
    already makes for an unjoined ESPN preseason line, not silently
    published under a guessed eligibility."""
    lines_df = _lines_df([
        {"player_id": "UNK1", "name": "No Position On File", "position": "",
         "rushing_tds": 0.0, "receiving_yards": 55.0},
    ])
    actual = outcomes(lines_df)
    positions = _positions_from(lines_df)
    lines = eligible_lines(actual, positions)

    assert lines["UNK1"] == {}


def test_every_market_key_seen_is_actually_eligible_for_its_player():
    """Whole-payload sanity check across a small realistic mixed slate: for
    every (player, market) pair that survives the filter, that player's own
    position is in MODELS[market]['pos'] -- the exact invariant the old
    truthiness filter had no way to enforce."""
    lines_df = _lines_df([
        {"player_id": "RB1", "name": "Ghost Back", "position": "RB",
         "carries": 14.0, "rushing_yards": 0.0, "rushing_tds": 0.0,
         "receptions": 0.0, "receiving_yards": 0.0, "receiving_tds": 0.0},
        {"player_id": "WR1", "name": "Big Play", "position": "WR",
         "receptions": 6.0, "receiving_yards": 88.0, "receiving_tds": 1.0},
        {"player_id": "QB1", "name": "Pocket Passer", "position": "QB",
         "passing_yards": 245.0, "rushing_yards": 12.0},
        {"player_id": "K1", "name": "Automatic Leg", "position": "K",
         "fg_made": 1.0, "pat_made": 1.0},
    ])
    actual = outcomes(lines_df)
    positions = _positions_from(lines_df)
    lines = eligible_lines(actual, positions)

    for pid, vals in lines.items():
        pos = positions[pid]
        for market in vals:
            assert pos in MODELS[market]["pos"], (
                f"{pid} ({pos}) published ineligible market {market}"
            )

    # And the genuine-zero RB TD case is present in this mixed slate too.
    assert lines["RB1"]["TD"] == 0.0
    assert "TD" not in lines["QB1"]
    assert "TD" not in lines["K1"]
