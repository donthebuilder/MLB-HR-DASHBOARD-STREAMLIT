"""Two things the 2026-09-01 bot pass pins.

1. game_pick_type_map (pairs/pools + text report) and build_game_pick_role_map
   (the badges the site reads) name the SAME TOP and the SAME HR for a game.
   Before this pass the builder ranked TOP/HR by overall_score behind a
   trap_flag filter, so a pair "built around the HR pick" could be built
   around a man not wearing the HR badge. Both call _top_and_hr_slots now.

2. Savant's leaderboard CSVs open with a UTF-8 byte-order mark. Decoded as
   plain utf-8 the BOM landed inside the first header cell, csv could not
   unquote it, and the key came through as '\\ufeff"player_id"' -- none of the
   five spellings _row_id tries. Every catcher parsed, none joined, and the
   steal board printed NoJoinableRows(70parsed) for weeks while the tests
   passed, because the tests primed the cache and never went through
   _fetch_csv. This one goes through _fetch_csv with the BOM in the bytes.

Run: python tests/test_pick_map_alignment_and_bom.py
"""
import dataclasses
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import bots.mlb_dashboard as md  # noqa: E402
from bots import savant_feeds as SF  # noqa: E402

FAILED: list[str] = []
CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILED.append(f"{name}: {detail}")


def mk(**over) -> md.HitterRecord:
    kw = {}
    for f in dataclasses.fields(md.HitterRecord):
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        t = str(f.type)
        kw[f.name] = "" if "str" in t else (0.0 if "float" in t else (False if "bool" in t else 0))
    kw.update(over)
    return md.HitterRecord(**kw)


# ── 1. the two maps agree on TOP and HR, including the cases that used to split them ──
GP = 900600
# The trap-flagged bat with the top hr_score: the OLD type_map skipped him for
# HR (trap filter); the role map never did. The high overall_score bat with
# thin ISO: the OLD type_map made him TOP; the ISO-led rank does not.
rows = [
    mk(game_pk=GP, player_id=1, name="Trap Slugger", season_iso=0.290, season_pa=400, last5_hr=2,
       hr_score=78.0, overall_score=50.0, trap_flag=True, hit_score=10, hrr_score=10, contact_score=10),
    mk(game_pk=GP, player_id=2, name="Overall Guy", season_iso=0.140, season_pa=420, last5_hr=0,
       hr_score=52.0, overall_score=80.0, hit_score=20, hrr_score=20, contact_score=20),
    mk(game_pk=GP, player_id=3, name="Iso Bat", season_iso=0.260, season_pa=380, last5_hr=1,
       hr_score=60.0, overall_score=58.0, hit_score=30, hrr_score=30, contact_score=30),
] + [mk(game_pk=GP, player_id=10 + i, name=f"Bat {i}", season_iso=0.150, season_pa=300,
        hr_score=30.0 - i, overall_score=30.0 - i, hit_score=50.0 - i, hrr_score=40.0 - i,
        contact_score=45.0 - i) for i in range(6)]

role = md.build_game_pick_role_map(rows)
typ = md.game_pick_type_map(rows)
role_top = [pid for (gp, pid), v in role.items() if gp == GP and "TOP" in v.split("/")]
role_hr = [pid for (gp, pid), v in role.items() if gp == GP and "HR" in v.split("/")]
typ_top = [pid for pid, v in typ.items() if v == "🥇TOP"]
typ_hr = [pid for pid, v in typ.items() if v == "🎆HR"]
check("role map has one TOP", len(role_top) == 1, role_top)
check("type map has one TOP", len(typ_top) == 1, typ_top)
check("TOP agrees across the two maps", role_top == typ_top, f"{role_top} vs {typ_top}")
check("HR agrees across the two maps", role_hr == typ_hr, f"{role_hr} vs {typ_hr}")
# The OLD type_map would have said TOP=2 (overall_score) and HR=3 (trap filter
# skipping #1). The ISO-led rank makes the .290-ISO trap-flagged bat TOP --
# the trap flag was measured as noise for selection -- and HR is the best
# remaining hr_score, distinct from him.
check("TOP is the ISO-led power bat, trap flag ignored", role_top == [1], role_top)
check("HR is the best remaining hr_score bat, distinct from TOP", role_hr == [3], role_hr)
check("neither map still picks the overall_score bat for TOP", 2 not in role_top + typ_top, (role_top, typ_top))
check("type map still hands out 2 HIT", sum(1 for v in typ.values() if v == "➕HIT") == 2, typ)
check("type map still hands out 2 HRR", sum(1 for v in typ.values() if v == "🔺HRR") == 2, typ)
check("type map still anchors CONTACT", any(v == "🟢CON" for v in typ.values()), typ)

# a second game: the same-man case (only one eligible bat) must not crash or double-tag
GP2 = 900601
solo = [mk(game_pk=GP2, player_id=50, name="Only Man", season_iso=0.2, season_pa=200, hr_score=40, overall_score=40)]
typ2 = md.game_pick_type_map(solo)
check("a one-man game tags him TOP once, never HR too", typ2 == {50: "🥇TOP"}, typ2)


# ── 2. the BOM goes through _fetch_csv, and the id column survives it ──
CSV = (
    '"player_id","player_name","team_name","start_year","end_year","sb_attempts",'
    '"catcher_stealing_runs","caught_stealing_above_average","n_cs","rate_cs","est_cs_pct",'
    '"cs_aa_per_throw","seasonal_runner_speed","runner_distance_from_second","pop_time",'
    '"exchange_time","arm_strength"\n'
    '\n'
    '506702,"León, Sandy","ATL",2026,2026,16,0.27,0.42,5,0.3125,0.2858,0.026,"28.2",55.7,'
    '"1.930133333","0.624","74.306272"\n'
)
BOM_BYTES = b"\xef\xbb\xbf" + CSV.encode("utf-8")


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


_real_urlopen = SF.urllib.request.urlopen
SF.urllib.request.urlopen = lambda req, timeout=0: _Resp(BOM_BYTES)
SF._CACHE.clear()
try:
    rows_, status_ = SF._fetch_csv("https://example.invalid/catcher.csv", "catcher:bomtest")
    check("BOM CSV parses", status_ == "ok" and len(rows_) == 1, (status_, len(rows_)))
    check("first header key is bare player_id, no BOM, no quotes", rows_ and "player_id" in rows_[0], list(rows_[0].keys())[:2] if rows_ else rows_)
    check("_row_id reads the id", rows_ and SF._row_id(rows_[0]) == 506702, rows_ and SF._row_id(rows_[0]))
    SF._CACHE.clear()
    cat, cstatus = SF.catcher_throwing(2026)
    check("catcher_throwing joins through the BOM", cstatus == "ok" and 506702 in cat, (cstatus, sorted(k for k in cat if isinstance(k, int))))
    check("the by-name index rides along", "sandy leon" in cat or any(isinstance(k, str) for k in cat), [k for k in cat if isinstance(k, str)][:2])
finally:
    SF.urllib.request.urlopen = _real_urlopen
    SF._CACHE.clear()

print(f"{CHECKS - len(FAILED)}/{CHECKS} checks passed")
for f in FAILED:
    print("  FAIL", f)
sys.exit(1 if FAILED else 0)
