"""Gate-power label shadow (2026-10-06). TEST DATA: synthetic hitters, no real players.
Run: PYTHONPATH=. python3 tests/test_hr_label_shadow.py

The bug: _hr2_best_bet_and_label reads batted_ball_power_score >= 80 as a gate signal,
but apply_model_v2_layers assigns that field AFTER the call, so it is always 0.0 there.
The fix runs as a SHADOW (row.hr_label_shadow); the live label must not move."""
import dataclasses, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import mlb_dashboard as m  # noqa: E402
import smoke_test as S  # noqa: E402

FAILED, N = [], 0
def check(name, got, want):
    global N
    N += 1
    if got != want: FAILED.append(f"{name}: got {got!r}, want {want!r}")

# ── 1. LIVE IS UNCHANGED ────────────────────────────────────────────────────
# (a) the live entry point is the old function, result for result: bpp is 0.0 at call time.
def gate_rec(bpp=0.0):
    return S.make_record(player_id=1, name="Gate Test", season_iso=0.200, hrw_score=75,
                         last5_hr=0, last10_hr=0, recent_barrel_rate=0.0, recent_ideal_hr_contact=0.0,
                         recent_ev=0.0, batted_ball_power_score=bpp)
live = m._hr2_best_bet_and_label(gate_rec(), 65.0, 65.0, False, False, True)
core = m._hr2_best_bet_and_label_core(gate_rec(), 65.0, 65.0, False, False, True)
check("wrapper returns exactly what the old function returned", live, core)
check("old ordering: 2 signals + bpp 0 is NOT Strong HR Look", live, ("HR or HRR", "Safer Production Play"))
m._HR_LABEL_SHADOW_PENDING.clear()

# (b) full scoring of a fixed synthetic slate: every live field matches the pre-change run.
rows = S.a_slate(120)
for r in rows: m.apply_model_v2_layers(r)
# fingerprint of the whole scored slate (all fields except the new one), captured on f5009a5c
import hashlib, json
dump = []
for r in rows:
    d = dataclasses.asdict(r); d.pop("hr_label_shadow", None); dump.append(d)
digest = hashlib.sha256(json.dumps(dump, sort_keys=True, default=str).encode()).hexdigest()
check("120-row scored slate is byte-identical to the base commit", digest,
      "58060d5151c56b55cdc68b082854da1020e8a3cd8f3a91c3b33c2958f9b49fd7")
check("an unstamped row carries no shadow", rows[0].hr_label_shadow, {})
check("config_hash is unmoved", m.hr_config_hash(),
      "sha256:c795e06c4352ce7b22d4a3320a07642ededc60143ab0a7f29f9a9d97d8caf5cb")

# ── 2. THE SHADOW FIXES THE ORDERING ────────────────────────────────────────
# Mimic the pipeline: call the live label at bpp==0.0, THEN assign bpp (as the pipeline does at :8688), stamp.
h = gate_rec()
m._hr2_best_bet_and_label(h, 65.0, 65.0, False, False, True)
h.batted_ball_power_score = 85.0
m.stamp_hr_label_shadow([h], game_started=False)
sh = h.hr_label_shadow
check("shadow gives Strong HR Look with bpp>=80 + 2 other signals", sh.get("label"), "Strong HR Look")
check("shadow best bet", sh.get("best_bet_type"), "HR")
check("shadow high_confidence flag", sh.get("high_confidence_hr_flag"), True)
check("live label on the same inputs does not", sh.get("live_label"), "Safer Production Play")
check("live flag on the same inputs is off", sh.get("live_high_confidence_hr_flag"), False)
check("differs", sh.get("differs"), True)
check("shadow carries its own model_version", sh.get("model_version"), m.HR_LABEL_SHADOW_VERSION)
check("shadow version is not the live one", m.HR_LABEL_SHADOW_VERSION != m.MODEL_REGISTRY.MODEL_VERSIONS["hr"], True)
check("the live fields were not touched by stamping", (h.beginner_label, h.high_confidence_hr_flag), ("Safer Production Play", False))
# below 80 the two agree
h2 = gate_rec(); m._hr2_best_bet_and_label(h2, 65.0, 65.0, False, False, True)
h2.batted_ball_power_score = 79.9; m.stamp_hr_label_shadow([h2], False)
check("bpp 79.9: shadow == live", h2.hr_label_shadow["differs"], False)
# end to end: on a whole scored slate the shadow only ever differs where bpp >= 80
rows2 = S.a_slate(120)
for r in rows2: m.apply_model_v2_layers(r)
m.stamp_hr_label_shadow(rows2, False)
check("every scored row got a shadow", all(r.hr_label_shadow for r in rows2), True)
check("shadow differs only where the gate_power signal fires",
      all((not r.hr_label_shadow["differs"]) or r.hr_label_shadow["gate_power_signal"] for r in rows2), True)
check("shadow's live_label equals the real stage label wherever no later layer overrode it",
      all(r.hr_label_shadow["live_label"] == r.beginner_label for r in rows2 if r.beginner_label in ("Be Careful",)), True)

# ── 3. NEVER WRITTEN AT/AFTER FIRST PITCH ───────────────────────────────────
fields = m.HitterRecord.__dataclass_fields__
def rec(pid, spot, run):
    base = {k: (f.default if f.default is not dataclasses.MISSING else (f.default_factory() if f.default_factory is not dataclasses.MISSING else None)) for k, f in fields.items()}
    base.update(game_pk=1, player_id=pid, name=f"P{pid}", lineup_spot=spot, run_id=run)
    return m.HitterRecord(**base)
# a started game: the rebuild scores a hitter, but nothing is stamped and nothing is left pending
late = gate_rec(); m._hr2_best_bet_and_label(late, 65.0, 65.0, False, False, True)
late.batted_ball_power_score = 90.0
m.stamp_hr_label_shadow([late], game_started=True)
check("started game: no shadow written", late.hr_label_shadow, {})
check("started game: pending inputs dropped", id(late) in m._HR_LABEL_SHADOW_PENDING, False)
# the first-pitch freeze: a pregame-stamped hitter keeps his pregame shadow; a guess-miss rebuilt after first pitch has none
pre = rec(1, 3, "pre"); pre.hr_label_shadow = {"label": "HR Look", "model_version": m.HR_LABEL_SHADOW_VERSION}
rebuilt = [rec(1, 2, "live"), rec(3, 9, "live")]
rebuilt[0].hr_label_shadow = {}; rebuilt[1].hr_label_shadow = {}
out, carried = m.freeze_pregame_rows(rebuilt, [pre, rec(2, 9, "pre")])
by = {r.player_id: r for r in out}
check("carried hitter keeps the PREGAME shadow", by[1].hr_label_shadow.get("label"), "HR Look")
check("a hitter first scored after first pitch has no shadow", by[3].hr_label_shadow, {})
# it survives the saved-row round trip the locked path uses (JSON -> HitterRecord)
rt = json.loads(json.dumps(dataclasses.asdict(pre), default=str))
check("shadow field is a loadable HitterRecord field", "hr_label_shadow" in fields and rt["hr_label_shadow"]["label"], "HR Look")
# prediction log carries it beside the other candidates
row = {"player_id": 1, "hr_label_shadow": sh}
lines = m.build_prediction_log_lines({"run_id": "t", "slate_date": "2026-10-06"}, [row])
check("prediction log candidate block carries the shadow", lines[1]["candidate"]["hr_label_shadow"]["label"], "Strong HR Look")

print(f"{N - len(FAILED)}/{N} checks passed")
for f in FAILED: print("  FAIL", f)
sys.exit(1 if FAILED else 0)
