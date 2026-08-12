# The "HR pathway" prompt, audited against what the bot already does
2026-08-11. Donovan was sent a 10-step prompt (game batches → pitcher weakness
→ flight alignment → verdicts) and asked to act on it. Before acting, each
step was checked against the fields the bot already computes and publishes.

## The headline

**About 80% of this prompt is the existing architecture wearing different
names.** Adopting it wholesale would be a rewrite that mostly renames things.
The remaining 20% is real, and it splits into one cheap addition, one piece of
work that today's backfill just made possible, and one thing we lack data for.

## Step-by-step mapping

| prompt asks for | already exists as | status |
|---|---|---|
| 1. Game environment rating | park_hr_factor, weather_hr_effect_pct, weather_label, PARK_FACTORS_V2, wx_ backfill | data yes; the ELITE/STRONG/NEUTRAL/POOR *label* no |
| 2. Pitcher profile (HR/9, FB%, barrel/EV allowed, arsenal) | pitcher_hr9, pitcher_fb_rate, pitcher_ev_allowed, pitcher_barrel_allowed, pitcher_arsenal_summary, spot/zone damage, putaway/swstr, trend | yes |
| 2. Zone% / Heart% / Meatball% | — | **missing; no data source wired** |
| 3. HR pathway (pitch-level alignment) | pitch_type_match_flag/score/note ("vs FC: batter 67% good-contact \| pitcher 24% usage, 61% HH allowed"), damage_conversion_reasons | yes in kind |
| 3. LA-band flight alignment (pitcher allows 18–25° pulled FB + batter produces them) | — | **missing as stated; now buildable** — hr_events has per-homer LA since today |
| 4. Hand reality (don't auto-punish same-hand) | avg/iso_vs_lhp/rhp, pitcher_weak_side, "SHB attacks pitcher weak side" in matchup_reason | yes |
| 5. Limitations as factors, not eliminations | trap_flag + trap_reason, risk_reason, k_trap | yes |
| 6. Outlier overrides | decision_reasons literally contains "Pitcher mistake-setup overrides Avoid"; hot-bat override in k_trap; hidden_hr_value (rank-based as of today) | yes |
| 7. Form confirms, doesn't drive | form is a weighted TERM in the blend, not a post-hoc confirmation | philosophical difference — see below |
| 8. Weather amplifies, never creates | park_weather is 0.05 of the blend — measured today, it *can't* create anything | already true, by weight |
| 9–10. Game batches → merge | game_pick_role is one pick per game per category; TOP is per-game best; Top-15 is the slate merge | yes — this IS the picks architecture |
| Verdicts ELITE/STRONG/VALUE/OUTLIER/PASS | best_bet_type / beginner_label: "HR Bet", "HR Lean", "Power Watch" | yes, different names |

## What to actually do (and not)

1. **Do NOT reorder the blend to match the prompt's hierarchy by fiat.** Every
   weight change this session went through measurement first, and both times
   the measurement contradicted the intuition (k_rate was not double-charged;
   ISO does not beat raw). The prompt's hierarchy is an opinion; the archive is
   the referee. The Sept 2 validation run is the venue.
2. **Cheap and real: a GAME_ENVIRONMENT tier.** park_hr_factor ×
   (1 + weather_hr_effect_pct/100) bucketed into four labels, shown on Games/
   The Read. Display only, no scoring change. Site-side, one component.
3. **Real work, newly possible: LA-band alignment.** As of today every graded
   homer carries launch angle. Once ~3 more weeks accumulate (and via
   spray_cache for season BBE), "does this batter produce the LA band this
   pitcher allows" becomes computable per pitch type. Queue behind the Sept 2
   validations; do not hand-set a weight for it before it's measured.
4. **Skip Zone%/Heart%/Meatball% for now.** No wired source. Adding a Savant
   zone pull is a new data dependency — decide separately, not as a side
   effect of a prompt.

The prompt's core rule — "rank opportunities, not reputations" — is already
how the picks work (per-game designation). Where the site ranks reputations
(the slate-wide board), that is deliberate and labelled, and as of today its
rank is locked to one definition in lib/scoring.js.
