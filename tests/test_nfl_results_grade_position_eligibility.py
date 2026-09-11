"""bots/nfl/nfl_results.py -- grade()'s position-eligibility void (item 9,
2026-09-11).

THE BUG. eligible_lines() already refuses to publish a market for a player
whose position isn't in that market's own MODELS[key]["pos"] list -- its own
docstring: "position eligibility is the only thing" that tells "he really
went scoreless" apart from "this market isn't his". grade() never got that
same check: it only ever voided a rung when the player had no recorded line
at all. outcomes() defaults every one of the 7 markets to 0.0 for every
player who DID record a line, so a rung for a market that doesn't apply to a
player's CURRENT position was graded here as a literal 0.0-vs-bar miss,
while eligible_lines() silently hid that exact same player+market pair from
the published `lines` a user would check that call against -- grade() and
lines disagreeing about the same question, from the same grading run.

Most likely to actually surface for a player whose listed position changed
since a card priced him: preseason carryover pricing (nfl_bot.py's
preseason_rows()) reads a player's position off the END of the PRIOR
season, so a rung built under that stale position can disagree with the
position eligible_lines() (and now grade()) checks him against once the
current season's own recorded position exists.

THE FIX. grade(card, actual, positions) -- same file, `positions` is the
identical {player_id: position} map main() already builds for
eligible_lines(). A rung now voids on either condition: no recorded line at
all (unchanged), OR a recorded line whose position isn't in that market's
eligible list (new). Omitting `positions` keeps the old no-eligibility-check
behavior, since nothing forces every future caller to have a position map.

WHY THIS RUNS FOR REAL, NOT MOCKED LIKE nfl_features.py's tests. grade()'s
own body is plain dict/list code -- it never touches a DataFrame. The only
obstacle to importing nfl_results.py directly is that the module (and
nfl_scoring.py, which it imports MODELS/OUTCOME from) does `import polars as
pl` at module top, and nfl_scoring.py's OUTCOME dict is built with real
`pl.col(...) + pl.col(...)`-shaped expressions at MODULE level, not inside a
function -- so unlike nfl_features.py (whose module-level polars use is just
the plain `import`), a bare stand-in object for `pl` isn't enough; `pl.col()`
has to return something that supports the arithmetic those module-level
expressions apply to it. The tiny _FakeExpr below exists only to survive
that import -- it is never asked to compute a real value, because these
tests call grade() directly with hand-built dicts and never touch
outcomes()/OUTCOME at all.

Neither polars nor nflreadpy is installed anywhere this session has tools
for (confirmed repeatedly this session, both the linked Mac's sandboxed VM
and the cloud container) -- this fake is what lets grade() itself still run
for real rather than falling back to a source-text check like the
regression guards at the bottom of this file do.

Run: python3 tests/test_nfl_results_grade_position_eligibility.py
"""
import os
import sys
import types

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


class _FakeExpr:
    """Just enough of a polars Expr to survive nfl_scoring.py's MODULE-LEVEL
    OUTCOME dict (pl.col(...) + pl.col(...), etc.) without ever being asked
    to actually compute anything -- grade() itself never touches it."""
    def _op(self, *_a, **_k):
        return _FakeExpr()
    __add__ = __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = _op
    __truediv__ = __rtruediv__ = __ge__ = __gt__ = __le__ = __lt__ = _op
    def alias(self, *_a, **_k): return self
    def is_in(self, *_a, **_k): return self
    def fill_null(self, *_a, **_k): return self
    def cast(self, *_a, **_k): return self
    def last(self, *_a, **_k): return self
    def first(self, *_a, **_k): return self
    def sum(self, *_a, **_k): return self


_fake_polars = types.SimpleNamespace(
    DataFrame=object, Utf8=object, Float64=object, Int8=object, Int32=object,
    col=lambda *_a, **_k: _FakeExpr(), lit=lambda *_a, **_k: _FakeExpr(),
)
sys.modules.setdefault("polars", _fake_polars)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots", "nfl"))

from nfl_results import grade  # noqa: E402
from nfl_scoring import MODELS  # noqa: E402

assert MODELS["TD"]["pos"] == ["RB", "WR", "TE"]
assert MODELS["PASS_YDS"]["pos"] == ["QB"]


def _card(market, rungs):
    return {market: {"bar": MODELS[market]["bar"], "rungs": rungs}}


# 1. Matching position, a real hit -- unchanged from before this fix.
card = _card("TD", [{"player_id": "RB1"}])
actual = {"RB1": {"TD": 1.0}}
positions = {"RB1": "RB"}
graded, totals = grade(card, actual, positions)
check("matching position, real hit: value published", graded["TD"]["rungs"][0]["actual"], 1.0)
check("matching position, real hit: hit True", graded["TD"]["rungs"][0]["hit"], True)
check("matching position, real hit: totals n=1 hit=1 void=0",
      (totals["TD"]["n"], totals["TD"]["hit"], totals["TD"]["void"]), (1, 1, 0))

# 2. Matching position, a real miss -- also unchanged: 0 TDs for an
# eligible RB is a genuine, countable miss, not a void.
card = _card("TD", [{"player_id": "RB2"}])
actual = {"RB2": {"TD": 0.0}}
positions = {"RB2": "RB"}
graded, totals = grade(card, actual, positions)
check("matching position, real miss: value published", graded["TD"]["rungs"][0]["actual"], 0.0)
check("matching position, real miss: hit False", graded["TD"]["rungs"][0]["hit"], False)
check("matching position, real miss: counted, not void", totals["TD"]["void"], 0)

# 3. THE BUG ITSELF: a card rung exists for TD (he was RB/WR/TE-eligible
# when priced), but `positions` -- the current, post-change record -- now
# says QB. Must void, not grade his structurally-0.0 TD outcome as a miss.
card = _card("TD", [{"player_id": "P3"}])
actual = {"P3": {"TD": 0.0}}          # a QB's rush+rec TDs default to 0.0
positions = {"P3": "QB"}              # position changed since the card priced him
graded, totals = grade(card, actual, positions)
check("position changed since pricing: voided, not graded",
      graded["TD"]["rungs"][0]["actual"], None)
check("position changed since pricing: hit is None (void), not False (miss)",
      graded["TD"]["rungs"][0]["hit"], None)
check("position changed since pricing: totals show void, not a miss",
      (totals["TD"]["n"], totals["TD"]["hit"], totals["TD"]["void"]), (0, 0, 1))

# 4. Same shape, but the ineligible player's raw stat is NONZERO -- proves
# this isn't just "0.0 happens to look like void by luck". A genuinely
# nonzero, ineligible value must still be suppressed.
card = _card("PASS_YDS", [{"player_id": "P4"}])
actual = {"P4": {"PASS_YDS": 40.0}}   # e.g. a trick-play completion by a non-QB
positions = {"P4": "WR"}              # not QB-eligible for PASS_YDS
graded, totals = grade(card, actual, positions)
check("nonzero but ineligible: still voided, not a hit",
      graded["PASS_YDS"]["rungs"][0]["actual"], None)
check("nonzero but ineligible: totals void=1", totals["PASS_YDS"]["void"], 1)

# 5. No line at all (did not play) -- unchanged, still voids.
card = _card("TD", [{"player_id": "GHOST"}])
actual = {}
positions = {}
graded, totals = grade(card, actual, positions)
check("no line at all: still voided", graded["TD"]["rungs"][0]["hit"], None)
check("no line at all: totals void=1", totals["TD"]["void"], 1)

# 6. positions omitted entirely -- old no-eligibility-check behavior is
# preserved (documented, deliberate: nothing forces a hypothetical other
# caller to supply a position map).
card = _card("PASS_YDS", [{"player_id": "P6"}])
actual = {"P6": {"PASS_YDS": 300.0}}   # >= PASS_YDS's real bar (225), a clear hit
graded, totals = grade(card, actual)   # positions omitted
check("positions omitted: old behavior, graded on raw value regardless of position",
      graded["PASS_YDS"]["rungs"][0]["actual"], 300.0)
check("positions omitted: old behavior, counted a hit",
      graded["PASS_YDS"]["rungs"][0]["hit"], True)

# 7. A mixed slate in one market block: hit + miss + position-void + no-line-void
# all at once, totals must add up correctly across all four.
card = _card("TD", [{"player_id": "RB1"}, {"player_id": "RB2"},
                     {"player_id": "P3"}, {"player_id": "GHOST"}])
actual = {"RB1": {"TD": 1.0}, "RB2": {"TD": 0.0}, "P3": {"TD": 0.0}}
positions = {"RB1": "RB", "RB2": "RB", "P3": "QB"}
graded, totals = grade(card, actual, positions)
check("mixed slate: n counts only the two real (RB) grades", totals["TD"]["n"], 2)
check("mixed slate: hit counts the one real hit", totals["TD"]["hit"], 1)
check("mixed slate: void counts the position-void AND the no-line-void",
      totals["TD"]["void"], 2)

# 8. Unrecognized market key (not in MODELS) fails open -- same defensive
# fallback grade() already had via MODELS.get(key, {}), just confirming the
# new eligible_pos lookup doesn't break it.
card = {"NOT_A_REAL_MARKET": {"bar": 1, "rungs": [{"player_id": "X"}]}}
actual = {"X": {"NOT_A_REAL_MARKET": 5.0}}
positions = {"X": "WHATEVER"}
graded, totals = grade(card, actual, positions)
check("unrecognized market key: fails open, still grades",
      graded["NOT_A_REAL_MARKET"]["rungs"][0]["hit"], True)

# 9. Regression guard: main() actually passes `positions` into grade(), not
# just the two-argument call this fix's whole point replaced.
nfl_results_src = open(
    os.path.join(os.path.dirname(__file__), "..", "bots", "nfl", "nfl_results.py")
).read()
check("nfl_results.py's main() passes positions into grade()",
      "grade(card, actual, positions)" in nfl_results_src, True)


print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
if FAILED:
    print("FAILED:")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ok   grade() now voids a rung whose player's CURRENT position "
          "doesn't match the market's eligible list, the same rule "
          "eligible_lines() already applied to the published `lines` -- so "
          "the two no longer disagree about the same player+market pair, "
          "and a stale pricing-time position (most visible across a "
          "season boundary) can no longer be graded as a false miss.")
    sys.exit(0)
