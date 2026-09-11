"""refresh_locked_lineup_status() must confirm PLAYERS, not just TEAMS.

Run: python tests/test_lineup_confirm_player_level.py

WHY THIS EXISTS (2026-09-11). Donovan: "zac veen did not play today he was in
the lineup as if he was a starter." Real case: Zac Veen (COL) got projected
into a starting slot pregame by build_projected_lineup() (ranks the roster by
season OBP/OPS/SLG when no real order has posted yet). The real posted lineup
for that game did not include him as a starter. refresh_locked_lineup_status()
flipped his row to "confirmed" anyway, because its check was team-level --
"did ANY real lineup post for the Rockies" -- not "is THIS SPECIFIC locked
player actually in it." The site then showed a bench player as a confirmed
starter for a game he was never starting.

The fix checks each row's player_id against the real extracted lineup's own
player-id set. A wrongly-projected pick now stays at Pending (the honest
failure mode -- this function still does not rebuild/replace picks, see its
own docstring) instead of reading as confirmed.

These assertions pin: (1) a real starter gets confirmed, (2) a wrongly-
projected non-starter does NOT get falsely confirmed even though his team's
real lineup posted, (3) the one deliberate fallback -- a FINAL game whose
boxscore never carried a posted order at all -- still confirms everyone
rather than stranding every row at Pending forever.
"""
import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bots.mlb_dashboard import HitterRecord, refresh_locked_lineup_status  # noqa: E402

FAILED: list[str] = []
CHECKS = 0


def check(name, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")


def make_hitter(**over) -> HitterRecord:
    """Every no-default field filled with the empty value for its type."""
    kw = {}
    for f in dataclasses.fields(HitterRecord):
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        t = str(f.type)
        kw[f.name] = "" if "str" in t else (0.0 if "float" in t else (False if "bool" in t else 0))
    kw.update(over)
    return HitterRecord(**kw)


def _boxscore_player(pid, name):
    return {"person": {"id": pid, "fullName": name}}


class FakeClient:
    """Stands in for MLBClient.live_game() with a fixed, hand-built boxscore."""

    def __init__(self, live_blob):
        self._blob = live_blob

    def live_game(self, game_pk: int):
        return self._blob


def live_blob(*, away_abbr="COL", home_abbr="NYY", away_order=None, home_order=None,
              final=False, extra_bench_ids=()):
    """A real posted lineup is `battingOrder` (starters only, in order) plus a
    `players` dict keyed "ID<id>" that also carries the bench (extra_bench_ids)
    -- exactly the shape extract_lineup() reads, and exactly why a bench guy
    being IN `players` must not be enough to count as confirmed; he also has
    to be in `battingOrder`.
    """
    def team_box(order, bench_ids):
        starters = {f"ID{pid}": _boxscore_player(pid, f"P{pid}") for pid in (order or [])}
        bench = {f"ID{pid}": _boxscore_player(pid, f"P{pid}") for pid in bench_ids}
        return {"battingOrder": list(order or []), "players": {**starters, **bench}}

    return {
        "gameData": {
            "teams": {
                "away": {"abbreviation": away_abbr},
                "home": {"abbreviation": home_abbr},
            },
            "status": {
                "abstractGameState": "Final" if final else "Live",
                "detailedState": "Final" if final else "In Progress",
            },
        },
        "liveData": {
            "boxscore": {
                "teams": {
                    "away": team_box(away_order, extra_bench_ids),
                    "home": team_box(home_order, ()),
                }
            }
        },
    }


GAME = {"gamePk": 823499}

# ── 1. A real starter gets confirmed ────────────────────────────────────────
real_starter = make_hitter(player_id=101, team="COL", lineup_confirmed=False)
client = FakeClient(live_blob(away_order=[101, 102, 103, 104, 105, 106, 107, 108, 109]))
rows = refresh_locked_lineup_status(client, GAME, [real_starter])
check("real starter -> confirmed", rows[0].lineup_confirmed, True)

# ── 2. Zac Veen: projected pregame, NOT in the real posted lineup, but IS in
#    the boxscore's bench (players dict) -- must NOT flip to confirmed just
#    because his team's real lineup posted. This is the bug, pinned. ────────
veen = make_hitter(player_id=999, team="COL", lineup_confirmed=False, name="Zac Veen")
client = FakeClient(live_blob(
    away_order=[101, 102, 103, 104, 105, 106, 107, 108, 109],
    extra_bench_ids=[999],
))
rows = refresh_locked_lineup_status(client, GAME, [veen])
check("wrongly-projected bench pick stays Pending (not falsely confirmed)",
      rows[0].lineup_confirmed, False)

# ── 3. Same real lineup, mixed rows: the correct starter confirms, the wrong
#    projection doesn't -- both in one call, since that's the real shape (one
#    team's locked picks are a mix of both). ────────────────────────────────
mix_starter = make_hitter(player_id=101, team="COL", lineup_confirmed=False)
mix_veen = make_hitter(player_id=999, team="COL", lineup_confirmed=False)
client = FakeClient(live_blob(
    away_order=[101, 102, 103, 104, 105, 106, 107, 108, 109],
    extra_bench_ids=[999],
))
rows = refresh_locked_lineup_status(client, GAME, [mix_starter, mix_veen])
check("mixed call: real starter confirms", rows[0].lineup_confirmed, True)
check("mixed call: wrong projection still doesn't", rows[1].lineup_confirmed, False)

# ── 4. Fallback: a FINAL game whose boxscore never carried a posted order at
#    all (team_ids empty) must still confirm -- the old team-level behavior,
#    kept for exactly this one case so a row isn't stranded at Pending
#    forever with nothing to ever check it against. ─────────────────────────
no_order_pick = make_hitter(player_id=555, team="COL", lineup_confirmed=False)
client = FakeClient(live_blob(away_order=[], final=True))
rows = refresh_locked_lineup_status(client, GAME, [no_order_pick])
check("final game, no posted order ever -> falls back to confirmed",
      rows[0].lineup_confirmed, True)

# ── 5. Already-confirmed rows are left alone (never re-checked, never flipped
#    back to False -- the function only ever moves False -> True). ─────────
already = make_hitter(player_id=101, team="COL", lineup_confirmed=True)
client = FakeClient(live_blob(away_order=[102, 103]))  # 101 NOT in this lineup
rows = refresh_locked_lineup_status(client, GAME, [already])
check("already-confirmed row is never revisited", rows[0].lineup_confirmed, True)

if FAILED:
    print(f"\n{len(FAILED)} FAILED\n" + "\n".join(f"  · {f}" for f in FAILED))
    sys.exit(1)
print(f"ok   lineup confirm is player-level: {CHECKS} assertions, Zac Veen stays Pending not Confirmed")
