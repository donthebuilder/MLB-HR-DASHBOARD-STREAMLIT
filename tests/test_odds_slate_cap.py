"""The per-slate odds cap must reset when the card changes.

THE BUG THIS PINS (found 2026-08-23, on live data)
==================================================
`bots/odds_fetch.py` allows at most ODDS_MAX_PER_SLATE (5) paid fetches per
slate. The counter was incremented correctly — with a slate-date check — but
the GATE that reads it never looked at the date:

    prev_count = int(prev.get("fetches_this_slate") or 0)
    ...
    if prev_count >= max_slate:
        write_status(state="capped", reason="... Resets when the slate date
                                             changes.")
        return 0

The reset lives in the write path that the gate blocks, so once ANY card
reached five fetches the pipeline was capped forever: every later run, on every
later card, hit `5 >= 5` and returned before it could write the reset. The
status file promised a reset it was structurally incapable of performing.

Observed: odds_latest.json sat at slate_date 2026-08-17 with
fetches_this_slate 5 for six days. Every price on the site — the props sheet,
the game cards, True Price — was six days stale while the pipeline reported
"capped" and spent nothing.

These tests exercise the DECISION, not the network: the gate's arithmetic is
`prev_slate != tonight -> count resets to 0`, and that is what is asserted.
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bots.odds_fetch import MARKETS, resolve_slate_date  # noqa: E402


MAX_PER_SLATE = 5


def _gate(prev, tonight, max_slate=MAX_PER_SLATE):
    """The cap gate's decision, transcribed from main().

    Returns (allowed, effective_count). Keep this in step with the gate; if the
    two ever drift the tests below stop protecting anything.
    """
    count = int(prev.get("fetches_this_slate") or 0)
    prev_slate = str(prev.get("slate_date") or "")[:10]
    if prev_slate and prev_slate != tonight:
        count = 0
    return (count < max_slate), count


def test_same_slate_still_caps():
    """The cap has to keep doing its job — this is a spend limit."""
    prev = {"slate_date": "2026-08-23", "fetches_this_slate": 5}
    allowed, count = _gate(prev, "2026-08-23")
    assert not allowed
    assert count == 5


def test_new_slate_resets_the_count():
    """The bug. A maxed-out yesterday must not charge against today."""
    prev = {"slate_date": "2026-08-17", "fetches_this_slate": 5}
    allowed, count = _gate(prev, "2026-08-23")
    assert allowed, "a card that has never been fetched must not start capped"
    assert count == 0


def test_under_the_cap_on_the_same_slate_is_allowed():
    prev = {"slate_date": "2026-08-23", "fetches_this_slate": 4}
    allowed, count = _gate(prev, "2026-08-23")
    assert allowed
    assert count == 4


def test_missing_slate_date_is_treated_as_the_same_card():
    """An old file with no slate_date keeps the conservative behaviour.

    Absent a date we cannot prove the card changed, and a wrong guess here
    spends money. The cap stands.
    """
    prev = {"fetches_this_slate": 5}
    allowed, _ = _gate(prev, "2026-08-23")
    assert not allowed


def test_stolen_bases_is_actually_requested():
    """SB v1 armed the matcher; the fetch has to ASK for the market too.

    The alias table has mapped "stolen base" -> batter_stolen_bases since
    2026-08-23, but a matcher only ever sees markets the request named, and
    this one was in neither CATEGORY_MARKET nor GRID_MARKETS. The probe could
    not have answered whether SB props exist.
    """
    assert "batter_stolen_bases" in MARKETS
    # and the markets that were already working stay working
    for m in ("batter_home_runs", "batter_hits", "batter_hits_runs_rbis",
              "batter_total_bases", "batter_runs_scored", "batter_rbis",
              "batter_doubles", "batter_triples"):
        assert m in MARKETS


def test_resolve_slate_date_reads_a_bare_list(tmp_path):
    """public/data/today.json is a BARE LIST — the shape that broke the first
    attempt at this resolution. Rows carry the date."""
    p = tmp_path / "slate.json"
    p.write_text(json.dumps([{"player_id": 1, "slate_date": "2026-08-23"}]))
    assert resolve_slate_date(str(p), quiet=True) == "2026-08-23"


def test_resolve_slate_date_falls_back_to_game_time_in_us_eastern(tmp_path):
    """A 01:00 UTC first pitch belongs to the PREVIOUS day's card."""
    p = tmp_path / "slate.json"
    p.write_text(json.dumps([{"player_id": 1, "game_time": "2026-08-24T01:05:00Z"}]))
    assert resolve_slate_date(str(p), quiet=True) == "2026-08-23"


def test_resolve_slate_date_last_resort_is_us_eastern_not_utc(tmp_path):
    p = tmp_path / "missing.json"
    now = dt.datetime(2026, 8, 24, 2, 0, tzinfo=dt.timezone.utc)
    assert resolve_slate_date(str(p), now, quiet=True) == "2026-08-23"


def test_the_gate_in_the_source_still_compares_slates():
    """A transcription test protects arithmetic, not the code that runs.

    `_gate` above is a copy of the gate's decision; if someone deletes the
    slate comparison from odds_fetch.py, every test above still passes and the
    six-day deadlock comes back silently. So this one reads the source — the
    same technique the site's check-rank-lock.mjs uses on its own invariant.
    """
    src = (Path(__file__).resolve().parents[1] / "bots" / "odds_fetch.py").read_text()
    gate = src[src.index("prev_count = int(prev.get(\"fetches_this_slate\")"):]
    gate = gate[:gate.index("if prev_count >= max_slate")]
    assert "prev.get(\"slate_date\")" in gate, \
        "the cap gate no longer reads the previous snapshot's slate date"
    assert "prev_count = 0" in gate, \
        "the cap gate no longer resets the count on a new card"
    assert "resolve_slate_date(" in src[:src.index("prev_count = int(")], \
        "tonight's slate date is no longer resolved before the gate"


# ── RUNNABLE AS A SCRIPT TOO ─────────────────────────────────────────────────
# Every other file in tests/ is a script that prints its assertions and exits
# non-zero on failure (see test_snapshot_and_sb.py). pytest is not installed
# everywhere this repo runs, so a pytest-only file would be a test that quietly
# never executes — the exact failure mode this whole file exists to document.
if __name__ == "__main__":
    import inspect

    failed, checks = [], 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        kwargs = {}
        if "tmp_path" in inspect.signature(fn).parameters:
            import tempfile
            kwargs["tmp_path"] = Path(tempfile.mkdtemp())
        try:
            fn(**kwargs)
            checks += 1
        except AssertionError as e:
            failed.append(f"{name}: {e}")
        except Exception as e:                      # noqa: BLE001
            failed.append(f"{name}: {type(e).__name__}: {e}")

    if failed:
        print(f"\n{len(failed)} FAILED\n" + "\n".join(f"  · {f}" for f in failed))
        sys.exit(1)
    print(f"ok   odds per-slate cap: {checks} assertions — a maxed-out card no "
          f"longer caps the next one (the gate reads the slate date, not just "
          f"the count), the cap still holds within a card, and the fetch now "
          f"ASKS for batter_stolen_bases instead of only matching it")
