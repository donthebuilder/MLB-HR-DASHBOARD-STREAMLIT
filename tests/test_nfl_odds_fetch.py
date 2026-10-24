"""bots/nfl/nfl_odds_fetch.py -- the market-key mapping and the output shape.

This is the NFL sibling of tests/test_odds_slate_cap.py, aimed at the two
things a first pass of this script can get wrong silently: mapping the wrong
odds-api market key to an nfl_scoring model (a wrong number wearing a real
market's name, same failure class odds_fetch.py's PROP_HINTS guards against),
and publishing a payload shape the site's lib/nfl/oddsMatch.js doesn't
actually read.

These exercise the pure functions -- name normalisation, American-odds math,
consensus(), the key-resolution order, and the empty/no-key file writers --
never the network, which this sandbox cannot reach anyway (see
nfl_odds_fetch.py's own module docstring on that limitation).

Run: python3 -m pytest tests/test_nfl_odds_fetch.py -v

Same flat-import convention as tests/test_nfl_config_fingerprint.py and
tests/test_nfl_registry.py (bots/nfl on sys.path, bare module names) rather
than test_odds_slate_cap.py's `bots.odds_fetch` dotted form -- there is no
bots/nfl/__init__.py, and the other three NFL test files already settled on
this shape.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))

import nfl_odds_fetch as odds  # noqa: E402
import nfl_scoring as ns  # noqa: E402

CATEGORY_MARKET = odds.CATEGORY_MARKET
MARKETS = odds.MARKETS
SPORT = odds.SPORT
american = odds.american
consensus = odds.consensus
implied = odds.implied
norm_name = odds.norm_name
resolve_key = odds.resolve_key
unwrap = odds.unwrap
write_empty_board = odds.write_empty_board
write_status = odds.write_status
MODELS = ns.MODELS


# ── THE MARKET MAP: every nfl_scoring key must have a real odds-api market ───

def test_every_scoring_model_has_a_mapped_market():
    """The whole point of this file. A market with no line silently degrades
    the site to a bare rank, which is the exact gap this bot closes -- so a
    MODELS key with no entry here is a regression, not a feature."""
    for key in MODELS:
        assert key in CATEGORY_MARKET, (
            f"{key} is a scoring model with no odds-api market mapped -- "
            f"the site would show a rank with no price for it")


def test_market_keys_use_the_documented_player_prop_convention():
    """Every one of the seven is a `player_*` key (the-odds-api's NFL prop
    convention) or the anytime-TD special case -- not a batter_* key
    accidentally left over from copying odds_fetch.py."""
    for model, market in CATEGORY_MARKET.items():
        assert market == "player_anytime_td" or market.startswith("player_"), (
            f"{model} -> {market} doesn't look like an NFL player-prop key")
        assert not market.startswith("batter_"), (
            f"{model} -> {market} is an MLB batter_* key, not NFL")


# B1 (2026-08-28 master plan): two of Donovan's named markets with no
# nfl_scoring.MODELS entry (no graded bar exists for either yet) -- these
# publish PRICE DATA ONLY via nfl_odds_latest.json, same as the seven scored
# markets, but deliberately aren't required to have a MODELS counterpart.
# Named explicitly here (not just "len() != 7") so a truly accidental third
# addition still fails loudly instead of silently passing as "one more
# price-only market, sure."
PRICE_ONLY_MARKETS = {"FTD", "LONG_REC"}


def test_markets_is_exactly_the_mapped_set():
    assert MARKETS == sorted(set(CATEGORY_MARKET.values()))
    # every one of the seven MODELS keys is represented, none forced/duplicated
    scored = {k: v for k, v in CATEGORY_MARKET.items() if k not in PRICE_ONLY_MARKETS}
    assert scored.keys() == MODELS.keys()
    assert len(scored) == len(MODELS) == 7
    # the only markets with no MODELS entry are the explicitly-allowed
    # price-only ones -- an unexplained extra key here is a stray edit
    assert set(CATEGORY_MARKET) - set(MODELS) == PRICE_ONLY_MARKETS


def test_the_documented_mapping_matches_the_module_docstring_table():
    """Transcription check, same technique as
    test_odds_slate_cap.py's test_the_gate_in_the_source_still_compares_slates:
    if someone edits CATEGORY_MARKET without updating the confidence table in
    the docstring, this catches the drift instead of the two silently
    disagreeing forever."""
    expected = {
        "TD": "player_anytime_td",
        "REC_YDS": "player_reception_yds",
        "REC": "player_receptions",
        "RUSH_YDS": "player_rush_yds",
        "RUSH_ATT": "player_rush_attempts",
        "PASS_YDS": "player_pass_yds",
        "KICK_PTS": "player_kicking_points",
        "FTD": "player_1st_td",
        "LONG_REC": "player_longest_reception",
    }
    assert CATEGORY_MARKET == expected


def test_sport_key_is_the_nfl_convention():
    assert SPORT == "americanfootball_nfl"


# ── NAME NORMALISATION -- must agree with the site's own copy ───────────────

def test_norm_name_strips_suffix_and_punctuation():
    assert norm_name("Ja'Marr Chase Jr.") == "ja marr chase"
    assert norm_name("A.J. Brown") == "a j brown"


def test_norm_name_strips_accents():
    assert norm_name("Amon-Ra St. Brown") == "amon ra st brown"


# ── JOIN KEY, REVIEWED ALIASES, ST FILTER (TEST data, no network) ───────────

def _q(name):
    return {"player_anytime_td": {"name": name, "line": 0.5, "over": 100, "books": 1}}


def _slate(*rows):  # TEST slate rows: (player_id, name, team)
    return [{"player_id": i, "name": n, "team": t} for i, n, t in rows]


def test_norm_name_itself_is_unchanged_by_the_join_layer():
    assert odds.norm_name("A.J. Brown") == "a j brown"  # the published by_name key and the site's normName


def test_join_key_fuses_adjacent_initials_only():
    assert odds.join_key("a j brown") == "aj brown"
    assert odds.join_key("c j williams") == odds.join_key("cj williams")
    assert odds.join_key("j michael sturdivant") == "j michael sturdivant"
    assert odds.join_key("amon ra st brown") == "amon ra st brown"


def test_initials_meet_without_an_alias():
    board = {"d j moore": _q("D.J. Moore")}
    m, u = odds.match_slate(board, _slate(("p1", "DJ Moore", "BUF")), aliases=[])
    assert list(m) == ["p1"] and u == []


def test_alias_needs_the_same_team_and_is_exact():
    al = [{"odds": "Cameron Ward", "ours": "Cam Ward", "team": "TEN"}]
    board = {"cameron ward": _q("Cameron Ward")}
    m, _ = odds.match_slate(board, _slate(("p1", "Cam Ward", "TEN")), aliases=al)
    assert list(m) == ["p1"]
    m, u = odds.match_slate(board, _slate(("p1", "Cam Ward", "DAL")), aliases=al)
    assert m == {} and u == ["Cameron Ward"]
    m, u = odds.match_slate(board, _slate(("p1", "Cam Wardle", "TEN")), aliases=al)  # no nearest name
    assert m == {} and u == ["Cameron Ward"]


def test_second_spelling_only_adds_markets_never_replaces():
    al = [{"odds": "Kenneth Gainwell", "ours": "Kenny Gainwell", "team": "TB"}]
    a = {"player_anytime_td": {"name": "Kenny Gainwell", "over": 100}}
    b = {"player_anytime_td": {"name": "Kenneth Gainwell", "over": 999},
         "player_rush_yds": {"name": "Kenneth Gainwell", "over": 110}}
    m, _ = odds.match_slate({"kenny gainwell": a, "kenneth gainwell": b},
                            _slate(("p1", "Kenny Gainwell", "TB")), aliases=al)
    assert m["p1"]["player_anytime_td"]["over"] == 100 and "player_rush_yds" in m["p1"]


def test_team_special_teams_lines_are_not_reported_unmatched():
    board = {"buf bills st": _q("BUF Bills ST"), "sf st": _q("SF ST"), "saints st": _q("Saints ST"),
             "nobody real": _q("Nobody Real")}
    m, u = odds.match_slate(board, _slate(("p1", "Someone Else", "BUF")), aliases=[])
    assert m == {} and u == ["Nobody Real"]


def test_shipped_alias_table_is_well_formed():
    rows = odds.load_aliases()
    assert rows and all(set(r) == {"odds", "ours", "team"} for r in rows)
    assert len({odds.norm_name(r["odds"]) for r in rows}) == len(rows)
    assert all(odds.norm_name(r["odds"]) != odds.norm_name(r["ours"]) for r in rows)


# ── AMERICAN ODDS MATH ───────────────────────────────────────────────────────

def test_implied_probability_matches_known_values():
    assert implied(-180) == 64.3
    assert implied(150) == 40.0
    assert implied(None) is None


def test_american_coerces_and_rejects_zero():
    assert american("-150") == -150
    assert american(150.0) == 150
    assert american(0) is None
    assert american("garbage") is None


# ── KEY RESOLUTION -- the documented order, ODDS_API_KEY first ──────────────

def test_resolve_key_prefers_odds_api_key(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "primary")
    monkeypatch.setenv("ODDSAPI_IO_KEY", "backup1")
    monkeypatch.setenv("ODDSPAPI_KEY", "backup2")
    assert resolve_key() == ("ODDS_API_KEY", "primary")


def test_resolve_key_falls_back_in_order(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.setenv("ODDSAPI_IO_KEY", "backup1")
    monkeypatch.setenv("ODDSPAPI_KEY", "backup2")
    assert resolve_key() == ("ODDSAPI_IO_KEY", "backup1")

    monkeypatch.delenv("ODDSAPI_IO_KEY", raising=False)
    assert resolve_key() == ("ODDSPAPI_KEY", "backup2")


def test_resolve_key_empty_when_nothing_set(monkeypatch):
    for k in ("ODDS_API_KEY", "ODDSAPI_IO_KEY", "ODDSPAPI_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert resolve_key() == ("", "")


# ── unwrap() -- tolerant of the shapes documented as possible ───────────────

def test_unwrap_bare_list():
    assert unwrap([{"a": 1}]) == [{"a": 1}]


def test_unwrap_common_wrapper_keys():
    assert unwrap({"data": [{"a": 1}]}) == [{"a": 1}]
    assert unwrap({"events": [{"a": 1}]}) == [{"a": 1}]


def test_unwrap_single_event_object():
    ev = {"home_team": "SF", "away_team": "SEA", "bookmakers": []}
    assert unwrap(ev) == [ev]


def test_unwrap_unrecognised_shape_is_empty_not_a_crash():
    assert unwrap({"nonsense": 1}) == []
    assert unwrap("not even a dict") == []


# ── consensus() -- the pricing algorithm itself ──────────────────────────────

def _row(name, market, side, book, point, price, home="SF", away="SEA"):
    return {
        "name": name, "norm": norm_name(name), "market": market, "side": side,
        "book": book, "point": point, "price": price, "home": home, "away": away,
        "commence": "2026-09-10T20:15:00Z",
    }


def test_consensus_picks_the_most_common_line():
    """Three books at 39.5, one at 49.5 -- the line the room is at wins, not
    the first row seen (this is the whole reason a median exists)."""
    rows = [
        _row("Justin Jefferson", "player_reception_yds", "over", "dk", 39.5, -115),
        _row("Justin Jefferson", "player_reception_yds", "under", "dk", 39.5, -105),
        _row("Justin Jefferson", "player_reception_yds", "over", "fanatics", 39.5, -120),
        _row("Justin Jefferson", "player_reception_yds", "over", "caesars", 49.5, +105),
    ]
    board = consensus(rows)
    q = board["justin jefferson"]["player_reception_yds"]
    assert q["line"] == 39.5
    assert q["lines_seen"] == 2
    assert q["books"] == 2          # only the two books actually AT 39.5


def test_consensus_best_over_is_the_largest_american_number():
    """+105 beats -110 beats -120 for a bettor taking the over -- highest raw
    American value, not lowest magnitude."""
    rows = [
        _row("CeeDee Lamb", "player_receptions", "over", "dk", 5.5, -120),
        _row("CeeDee Lamb", "player_receptions", "over", "fanatics", 5.5, -110),
    ]
    board = consensus(rows)
    q = board["ceedee lamb"]["player_receptions"]
    assert q["best_over"] == -110
    assert q["best_book"] == "fanatics"


def test_consensus_implied_tracks_the_median_over_price():
    rows = [_row("Josh Jacobs", "player_rush_yds", "over", "dk", 79.5, -180)]
    board = consensus(rows)
    q = board["josh jacobs"]["player_rush_yds"]
    assert q["over"] == -180
    assert q["implied"] == implied(-180)


def test_consensus_carries_the_game_string_and_commence_for_kickoff_freeze():
    """No freeze-at-kickoff yet (module docstring #4) -- but `commence` still
    has to ride the payload, since that's the field any future freeze logic
    would need. Losing it here would silently block that feature later."""
    rows = [_row("Josh Jacobs", "player_rush_yds", "over", "dk", 79.5, -180)]
    board = consensus(rows)
    q = board["josh jacobs"]["player_rush_yds"]
    assert q["commence"] == "2026-09-10T20:15:00Z"
    assert q["game"] == "SEA @ SF"


# ── the "say what happened, always" files ────────────────────────────────────

def test_write_empty_board_is_valid_and_explicit(tmp_path):
    write_empty_board(tmp_path, "no key configured", state="no_key")
    payload = json.loads((tmp_path / "nfl_odds_latest.json").read_text())
    assert payload["empty"] is True
    assert payload["sport"] == SPORT
    assert payload["by_player_id"] == {}
    assert payload["by_name"] == {}
    assert payload["category_market"] == CATEGORY_MARKET
    assert "no key configured" in payload["reason"]


def test_write_status_is_valid_json_with_state(tmp_path):
    write_status(tmp_path, state="ok", reason="fetched fine", players=12)
    payload = json.loads((tmp_path / "nfl_odds_status.json").read_text())
    assert payload["state"] == "ok"
    assert payload["players"] == 12
    assert "checked_at" in payload


# ── ALSO RUNNABLE AS A PLAIN SCRIPT ──────────────────────────────────────────
# The other three NFL test files (test_nfl_registry.py etc.) are pytest-only,
# but most of this repo's tests (test_odds_slate_cap.py etc.) are dual-mode --
# `python <file>.py` works with no pytest install at all. Cheap to keep both
# here: `pytest -v` is the primary contract this file was asked to satisfy,
# and the fallback costs one small shim for the monkeypatch fixture.
if __name__ == "__main__":
    import inspect
    from pathlib import Path

    class _FakeMonkeypatch:
        """Minimal stand-in for pytest's monkeypatch fixture, env-vars only."""
        def __init__(self):
            self._saved = {}

        def setenv(self, k, v):
            self._saved.setdefault(k, os.environ.get(k))
            os.environ[k] = v

        def delenv(self, k, raising=False):
            self._saved.setdefault(k, os.environ.get(k))
            os.environ.pop(k, None)

        def undo(self):
            for k, v in self._saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    failed, checks = [], 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        params = inspect.signature(fn).parameters
        kwargs = {}
        mp = None
        if "tmp_path" in params:
            import tempfile
            kwargs["tmp_path"] = Path(tempfile.mkdtemp())
        if "monkeypatch" in params:
            mp = _FakeMonkeypatch()
            kwargs["monkeypatch"] = mp
        try:
            fn(**kwargs)
            checks += 1
        except AssertionError as e:
            failed.append(f"{name}: {e}")
        except Exception as e:                      # noqa: BLE001
            failed.append(f"{name}: {type(e).__name__}: {e}")
        finally:
            if mp:
                mp.undo()

    if failed:
        print(f"\n{len(failed)} FAILED\n" + "\n".join(f"  · {f}" for f in failed))
        sys.exit(1)
    print(f"ok   nfl_odds_fetch: {checks} assertions -- all seven nfl_scoring "
          f"markets map to a player_* odds-api key, consensus() picks the "
          f"room's line and the best price correctly, key resolution follows "
          f"ODDS_API_KEY -> ODDSAPI_IO_KEY -> ODDSPAPI_KEY, and the "
          f"empty/status files publish valid, explicit JSON.")


# ── odds-api.io football wiring (2026-10-16). TEST DATA, not real quotes. ────
import datetime as _dt  # noqa: E402
import odds_fetch as _mlb  # noqa: E402


def _kind(label):
    saved = _mlb.PROP_HINTS
    _mlb.PROP_HINTS = odds.FOOTBALL_HINTS
    try:
        return _mlb._prop_key(label)
    finally:
        _mlb.PROP_HINTS = saved


def test_football_hints_file_each_market_under_its_own_key():
    cases = {
        "Anytime Touchdown Scorer": "player_anytime_td",
        "First Touchdown Scorer": "player_1st_td",
        "Receiving Yards": "player_reception_yds",
        "Receptions": "player_receptions",
        "Longest Reception": "player_longest_reception",
        "Rushing Yards": "player_rush_yds",
        "Rushing Attempts": "player_rush_attempts",
        "Passing Yards": "player_pass_yds",
        "Kicking Points": "player_kicking_points",
    }
    for label, want in cases.items():
        assert _kind(label) == want, label
        assert want in MARKETS


def test_football_compounds_are_never_filed_as_a_single_market():
    for label in ("Rushing + Receiving Yards", "Passing Touchdowns", "Passing + Rushing Yards"):
        assert _kind(label) == "_other", label
    assert "_other" not in MARKETS


def test_fetch_oaio_swaps_hints_and_restores_them(monkeypatch):
    before = _mlb.PROP_HINTS
    seen = {}

    def fake(key, books, **kw):
        seen.update(kw)
        seen["hints_live"] = _mlb.PROP_HINTS
        return [{"name": "TEST Player", "norm": "test player", "market": "player_rush_yds",
                 "side": "over", "book": "DraftKings", "point": 60.5, "price": -115},
                {"name": "TEST Player", "norm": "test player", "market": "_other",
                 "side": "over", "book": "DraftKings", "point": 1.5, "price": 100}]
    monkeypatch.setattr(_mlb, "fetch_oddsapiio", lambda k, b, **kw: fake(k, b, **kw))
    rows = odds.fetch_oaio("TESTKEY")
    assert [r["market"] for r in rows] == ["player_rush_yds"]       # _other dropped
    assert seen["slugs"][0] == "american-football"
    assert _mlb.PROP_HINTS is before                                 # MLB hints untouched


def test_mlb_defaults_unchanged_by_the_generalisation():
    import inspect
    sig = inspect.signature(_mlb.fetch_oddsapiio)
    assert sig.parameters["slugs"].default == ("mlb", "baseball")
    assert sig.parameters["days"].default == 2


def test_snapshot_freeze_keeps_pregame_row_once_game_started(tmp_path):
    now = _dt.datetime(2026, 10, 18, 18, 0, tzinfo=_dt.timezone.utc)
    snap = tmp_path / "nfl_odds_2026-10-18.json"
    snap.write_text(json.dumps({"rows": {"1": {"player_rush_yds": [60.5, -115, 53.5]},
                                         "2": {"player_rush_yds": [40.5, -110, 52.4]}},
                                "commence": {"1": "2026-10-18T17:00:00Z", "2": "2026-10-18T20:25:00Z"}}))
    fresh = {"1": {"player_rush_yds": [70.5, -300, 75.0]},   # live price, game started
             "2": {"player_rush_yds": [41.5, -112, 52.8]}}   # not started: takes the new price
    rows, com = odds.freeze_started(snap, fresh, {"1": "2026-10-18T17:00:00Z", "2": "2026-10-18T20:25:00Z"}, now)
    assert rows["1"]["player_rush_yds"][0] == 60.5
    assert rows["2"]["player_rush_yds"][0] == 41.5


def test_oaio_reads_a_gzip_body(monkeypatch):
    """The 2026-10-16 probes got gzip bytes from /events. TEST DATA."""
    import gzip
    import io

    class R(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    body = gzip.compress(json.dumps({"events": [{"id": 1}]}).encode())
    monkeypatch.setattr(_mlb.urllib.request, "urlopen", lambda req, **kw: R(body))
    assert _mlb.oaio("/events", "TESTKEY") == {"events": [{"id": 1}]}
