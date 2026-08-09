# Mini-bot audit — 2026-08-08

Two agents audited the pick/scoring system and the pair/pool systems against
the 38-day graded archive (3,629 player-days, 519 HR, 14.3% base rate).

## Key evidence
- **hr_score adds ~nothing conditioned on ISO.** The 3×3 ISO×hr_score table is
  flat across hr_score in every ISO band. hr70+/ISO<.18 homered 11.4% (below
  base); hr<40/ISO≥.23 homered 21.3%. AUC: hr_score 0.540 vs season_iso 0.620,
  last5_hr 0.599.
- **TOP/HR replay:** ranked on `100·ISO + 10·last5_hr + 0.35·hr_score`,
  TOP 22.9% / HR 18.4% / combined 20.6% vs 19.4/16.1/17.9 shipped.
- **Aligned stack:** weak_spot+pitch_match 22.3% (n=184); + ISO≥.200 → **27.4%**
  (n=106). trap_flag 15.5% vs 15.3% — noise. hidden_hr_value fired 0/2052.
- **Pools:** 0 all-clears in 192 opportunities (mathematically dead metric);
  per-leg 13.8% vs 23.0% for naive top-4-by-hr_score; build-order cascade made
  pool A 18.8%/leg vs pool C 9.4%. Same-game correlation compounds: 1.9× at
  N=3, 2.7× at N=4 all-clear.
- **Pair history:** out-of-sample lift of "top pairs" 1.30× (p≈0.13) ≈ noise;
  ranking is individual HR volume in disguise.

## Implemented (this commit)
1. TOP + HR selection re-ranked on the ISO-led power score, ISO≥.180 floor,
   trap removed from the filter chain (mlb_dashboard build_game_pick_role_map).
2. Tracker grades the PUBLISHED designations (game_pick_role from the sheet)
   instead of re-deriving picks — fixes grading a different player than shown.
3. Pool grading: 0-AB legs void the leg (ticket shrinks), hit_any/hit_2plus
   ladder metrics added alongside the dead all-clear metric.
4. available_pool + recommended_3mans restored to the published payload (the
   site's Build-a-Pair primary source was being dropped).
5. Exposure fixes: build_structured_pairs seeds used_ids from global_exposure
   (was ignored entirely); pool6_exposure seeded from the full map.
6. park_weather weights normalized 0.93 → 1.00 (wind 0.18→0.25).
7. trap_flag −8 board penalty retired; pitch-match +6 double-count in
   pitcher_damage removed; weak_spot_interaction rebuilt as the tiered
   ALIGNED STACK at weight 0.06 (funded from pitcher_damage 0.15→0.10).
8. _hr_alignment_score (pool/pair leg ranker) rebuilt ISO/form-led.
9. _s2_risk thresholds rescaled to System-2 units (pairs no longer all
   "Lower", pools no longer all "High").
10. pair_history: fabricated Happ+Wood manual pair removed; season/career
    double-count in pair_score collapsed. Tracker lanes keyed on lane_key.

## Deferred (bigger surgery, revisit after 2 weeks of new graded data)
- Recompute all derived board fields AFTER the hr re-anchor (audit B4) and
  retire the old_hr_score=50 placeholder + its trap/hidden gates (B1).
- Unify the three overall_score formulas and the two hrr_score formulas.
- Pool architecture: peer pools with distinct objectives (max-EV / all-clear
  same-game stack / max-P(≥1) distinct games) instead of the block cascade;
  publish + grade 3-man tickets.
- Pair history rebuilt as opportunity-adjusted lift with id keys.
- One definition of "TOP did its job" across job_ok / designed_hit /
  top_beat_game.

**Note for the record:** the selection changes are a model regime change as of
2026-08-08 — the trust curves and report card will show the break, by design.
