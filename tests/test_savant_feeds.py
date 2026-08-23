"""Two league-wide tables, and the two join traps that would have made them lie.

WHAT THIS PINS (2026-08-23)
===========================
Donovan: "try to pull static data from savant and espn if needed for model and
site use wild pitches, pickoffs, pitcher SB-against, catcher CS%, team defense,
ABS challenge record."

Four of those six were never missing — wildPitches, pickoffs, stolenBases and
caughtStealing are keys on StatsAPI's season pitching blob the bot already
fetches (see test_running_game_fields below). Two genuinely live on Baseball
Savant, and this file pins the loaders for them.

EVERY BYTE IN THE FIXTURES BELOW IS REAL. The header rows and data rows are
copied verbatim out of the live 2026 endpoints, accents and quoting intact.
A fixture invented by the same person who wrote the parser tests nothing at
all — it tests that they were consistent with themselves.

THE TWO TRAPS
-------------
1. The catcher feed writes "León, Sandy" — LAST, FIRST, accented. The slate
   writes "Sandy Leon". A raw string compare matches NOBODY, and a feed that
   matches nobody is indistinguishable from a feed reporting that every
   catcher in baseball is exactly average. That is the failure mode that ships
   quietly and looks fine.
2. The team-defence feed writes "Angels", a nickname, where the slate writes
   "LAA". It also carries team_id — MLB's own, already on every bot row — so
   the join is on the id and the nickname is decoration.

AND THE REFUSALS
----------------
A rate over a handful of attempts is not a rate. 1-of-2 is 50% and would sit
at the top of a caught-stealing board; the loader returns None under the floor
and the caller renders an em-dash. Likewise every loader returns a STATUS, so
"Savant is down" can never be read as "there are no catchers in MLB".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))

from bots import savant_feeds as SF  # noqa: E402

# ── verbatim from https://baseballsavant.mlb.com/leaderboard/catcher-throwing
#    ?season=2026&min=q&type=Cat&sortColumn=cs_above_avg&sortDirection=desc&csv=true
CATCHER_CSV = (
    '"player_id","player_name","team_name","start_year","end_year","sb_attempts",'
    '"catcher_stealing_runs","caught_stealing_above_average","n_cs","rate_cs",'
    '"est_cs_pct","cs_aa_per_throw","seasonal_runner_speed","runner_distance_from_second",'
    '"pop_time","exchange_time","arm_strength","n_xcs_with_flight_over_xcs",'
    '"n_xcs_with_exchange_over_xcs","n_xcs_with_accuracy_over_xcs",'
    '"n_xcs_with_ground_other_over_xcs","n_xcs_with_onfly_other_over_xcs",'
    '"n_xcs_with_untracked_other_over_xcs"\n'
    '506702,"León, Sandy","ATL",2026,2026,16,0.27688967301885425,0.4259841123366991,5,'
    '0.31250000000000006,0.28587599297895633,0.026624007021043675,"28.2",55.70957966463042,'
    '"1.930133333","0.624","74.306272",-1.5385223277756908,0.7441927850433844,'
    '0.7989940826515255,-0.34166577898386075,0.7629853514013403,0\n'
    '543510,"McCann, James","AZ",2026,2026,29,-0.5830663427976319,-0.8970251427655875,4,'
    '0.13793103448275865,0.16886293595743404,-0.030931901474675436,"28.537931034",'
    '52.97898204437321,"1.9302","0.6277","77.824771",-0.5402068219442226,'
    '-0.038181738076120686,0.1454555948447524,-1.1070656824585217,0.642973504868525,0\n'
    # invented ONLY to exercise the sample floor — a real 1-of-2 backup
    '999999,"Backup, Barry","SF",2026,2026,2,0,0,1,0.5,0.28,0,"28.0",55.0,'
    '"2.05","0.70","72.0",0,0,0,0,0,0\n'
)

# ── verbatim from .../leaderboard/outs_above_average?type=Fielding_Team
#    &startYear=2026&endYear=2026&...&csv=true
TEAMDEF_CSV = (
    '"team_name","team_id","year","primary_pos_formatted","outs_above_average",'
    '"outs_above_average_infront","outs_above_average_lateral_toward3bline",'
    '"outs_above_average_lateral_toward1bline","outs_above_average_behind",'
    '"outs_above_average_rhh","outs_above_average_lhh",'
    '"actual_success_rate_formatted","adj_estimated_success_rate_formatted",'
    '"diff_success_rate_formatted"\n'
    '"Angels","108","","",-27,-6,-11,-3,-5,-10,-17,"78%","79%","-1%"\n'
    '"Astros","117","","",-1,-5,5,-1,-1,-12,11,"79%","79%","0%"\n'
)


def _prime(key, text):
    """Put a real CSV body into the module cache so the loaders parse it
    without a network call. The sandbox cannot reach Savant; the parser is
    what is under test, not the socket."""
    import csv as _csv, io as _io, time as _time
    rows = [r for r in _csv.DictReader(_io.StringIO(text)) if r]
    SF._CACHE[key] = (_time.time(), rows, "ok")


def test_catcher_feed_parses_the_real_bytes():
    _prime("catcher:2026", CATCHER_CSV)
    cat, status = SF.catcher_throwing(2026)
    assert status == "ok", status
    leon = cat[506702]
    assert leon["cs"] == 5 and leon["sb_attempts"] == 16
    assert leon["cs_rate"] == 0.313, leon["cs_rate"]
    assert round(leon["cs_rate_expected"], 3) == 0.286
    assert leon["pop_time"] == 1.930133333
    assert leon["arm_strength"] == 74.306272
    assert leon["attempts_below_floor"] is False


def test_the_accented_last_comma_first_name_joins():
    """"León, Sandy" has to match a slate that says "Sandy Leon". If it does
    not, the feed matches nobody — and a feed that matches nobody looks exactly
    like a feed saying every catcher is average."""
    _prime("catcher:2026", CATCHER_CSV)
    cat, _ = SF.catcher_throwing(2026)
    assert cat[506702]["name_key"] == "sandy leon", cat[506702]["name_key"]
    assert SF.norm_name("Sandy León") == "sandy leon"
    assert SF.norm_name("León, Sandy") == "sandy leon"
    assert SF.norm_name("McCann, James") == "james mccann"
    assert SF.norm_name("Jr., Ronald Acuña") == "ronald acuna jr"
    assert SF.norm_name("") == "" and SF.norm_name(None) == ""


def test_a_two_attempt_backup_has_no_rate():
    """1-of-2 is 50% and would top a caught-stealing board. None, not 0.0 and
    not 0.5 — the caller renders an em-dash and the reader learns nothing
    false."""
    _prime("catcher:2026", CATCHER_CSV)
    cat, _ = SF.catcher_throwing(2026)
    backup = cat[999999]
    assert backup["cs_rate"] is None, backup["cs_rate"]
    assert backup["attempts_below_floor"] is True
    # the raw counts still ride along, so the reader can see WHY it is blank
    assert backup["cs"] == 1 and backup["sb_attempts"] == 2


def test_team_defence_joins_on_id_not_nickname():
    """The feed says "Angels"; the slate says "LAA". team_id is the join."""
    _prime("teamdef:2026", TEAMDEF_CSV)
    tm, status = SF.team_defense(2026)
    assert status == "ok"
    assert set(tm) == {108, 117}, set(tm)
    laa = tm[108]
    assert laa["team_name"] == "Angels"
    assert laa["oaa"] == -27
    # the split by batter hand is the half this site actually needs
    assert laa["oaa_vs_rhb"] == -10 and laa["oaa_vs_lhb"] == -17
    # "78%" is a string in the feed
    assert laa["success_rate"] == 78.0
    assert laa["success_rate_diff"] == -1.0


def test_an_html_error_page_is_not_read_as_an_empty_league():
    """Savant answers bad parameters with an HTML page and a 200. Parsing that
    as CSV yields zero rows, and zero rows with an 'ok' status would be read
    downstream as 'nobody in baseball caught anybody'."""
    src = (Path(__file__).resolve().parents[1] / "bots" / "savant_feeds.py").read_text(encoding="utf-8")
    # The guard has to be on the BODY, not the status code, because the bad
    # response is a 200.
    assert 'head.startswith("<!doctype")' in src
    assert 'return [], "error:HTMLNotCSV"' in src
    # and a body that fails the check must never reach the cache
    body = src[src.index("def _fetch_csv"):src.index("def _f(")]
    guard_at = body.index("error:HTMLNotCSV")
    cache_at = body.index("_CACHE[key] = ")
    assert guard_at < cache_at, "the HTML guard runs after the cache write"


def test_every_loader_returns_a_status():
    """A caller that gets {} with no status cannot tell 'Savant is down' from
    'there are no catchers in MLB', and the second reading ships a board of
    zeroes."""
    SF._CACHE.clear()
    src = (Path(__file__).resolve().parents[1] / "bots" / "savant_feeds.py").read_text(encoding="utf-8")
    assert "return out, status" in src
    assert src.count("Tuple[Dict[int, dict], str]") >= 2


def test_abs_is_deliberately_not_wired():
    """The ABS leaderboard exists and its columns are known, but the parameter
    that selects Batters vs Catchers vs Pitchers is JS-rendered and silently
    ignores every name tried — it answers 200 with the batter table. Wiring it
    would give every pitcher a batter's challenge record with nothing saying
    so, which is worse than no term at all."""
    assert hasattr(SF, "ABS_PARAM_CANDIDATES")
    assert len(SF.ABS_PARAM_CANDIDATES) >= 5
    # no parsed ABS shape exists yet, on purpose
    assert not hasattr(SF, "abs_challenges")
    assert hasattr(SF, "abs_challenges_raw")


def test_running_game_fields_come_off_the_blob_the_bot_already_has():
    """The four 'missing' pitcher stats were never missing. This asserts the
    scorer reads them off StatsAPI's season pitching blob rather than any new
    host — key names verified verbatim against statsapi.mlb.com."""
    src = (Path(__file__).resolve().parents[1] / "bots" / "mlb_dashboard.py").read_text(encoding="utf-8")
    for key in ('stat.get("wildPitches")', 'stat.get("pickoffs")',
                'stat.get("stolenBases")', 'stat.get("caughtStealing")',
                'stat.get("balks")'):
        assert key in src, f"{key} is not being read"
    # and the rates refuse under their sample floors
    assert "if sb_attempts_against >= 5 else None" in src
    assert "if baserunners >= 20 else None" in src


if __name__ == "__main__":
    failed, checks = [], 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn(); checks += 1
        except AssertionError as e:
            failed.append(f"{name}: {e}")
        except Exception as e:                      # noqa: BLE001
            failed.append(f"{name}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{len(failed)} FAILED\n" + "\n".join(f"  · {f}" for f in failed))
        sys.exit(1)
    print(f"ok   savant feeds: {checks} assertions against VERBATIM live 2026 bytes — "
          f'"León, Sandy" joins a slate that says "Sandy Leon", team defence joins on '
          f"team_id and not the nickname, a 1-of-2 backup has no caught-stealing rate, "
          f"an HTML error page is not read as an empty league, and ABS stays unwired "
          f"until the probe names its parameter")
