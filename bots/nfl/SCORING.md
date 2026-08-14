# MOONSHOT · NFL — how each model scores

Source of truth for the Guide tab. Every number below is measured, not asserted.

---

## The shape (same as `hr_score`)

Each market gets a **0–100 score**. Every component is **percentile-ranked inside that week's eligible pool** before it's weighted, which means:

- a weight of 0.30 really is 30% of the score, always
- no component dominates just because its raw scale is bigger
- the score **ranks**, it does not predict probability. It answers *who's most likely*, never *how likely*

A component written `-name` is inverted — lower raw value scores higher.

### The rule that governs every weight

> **Context modulates. Volume selects.**

Ranking a 200-man WR/RB pool by implied team total floats backups on good offenses straight into the top 15. Measured solo, team context is a *terrible* player-selection signal:

| Solo ranker, receiving yards | Top-15 hit% |
|---|---|
| `f_wopr` | 74.6% |
| `f_receiving_yards` | 74.6% |
| `f_target_share` | 73.8% |
| `implied_total` | **27.1%** |
| `opp_pass_soft` | **25.4%** |
| `pass_script` | **19.2%** |

Context isn't noise — it's *player-agnostic*. It says something true about the game and nothing about which guy in that game to pick. So it's capped hard (≤10% combined) in every market where the pool is full of non-starters.

**The exception is QB and K.** Those pools are 32 starters who all have guaranteed volume, so context becomes selective rather than diluting. For passing yards, `total_line` solo hits 55.8% — *better* than trailing passing yards. That's why the QB and kicker models are context-led and everything else is volume-led.

---

## 1. Anytime TD — bar: 1+ rush or rec TD · RB/WR/TE

The headline market. The only one where the bar is genuinely hard.

| Component | Weight | What it is |
|---|---|---|
| `f_gl_opp` | **30%** | Goal-line opportunity — inside-10 targets + inside-5 carries |
| `f_rz_opp` | **22%** | All red-zone touches |
| `implied_total` | **18%** | Points his team is expected to score |
| `f_xtd` | **15%** | Expected TDs from field position |
| `opp_td_soft` | 8% | TDs the defense has been giving up |
| `td_regression` | 7% | xTD minus actual — buy the cold guy |

**Why this shape.** TDs are won at the goal line, so proximity-weighted opportunity carries over half the score. `implied_total` gets an unusually high 18% here because unlike a yardage prop, a TD *requires* the team to score — context is causally upstream, not just correlated.

`td_regression` is the BABIP port. It's deliberately small at 7% — it's a tiebreaker between two players with similar opportunity, not a thesis. In 2025 it would have flagged Justin Jefferson (2 actual TDs on 6.8 expected) and faded Dallas Goedert (11 on 5.6).

**The xTD curve** — league TD rate per target, by distance from the end zone:

```
 0– 4 yd out: 41.8%      15–19 yd out: 13.4%
 5– 9 yd out: 31.7%      20–24 yd out:  9.2%
10–14 yd out: 19.5%      30–34 yd out:  3.3%
```

**How it separates** (hit rate by score decile, D10 = highest):

| Decile | 2025 | 2024 |
|---|---|---|
| **D10** | **46.4%** | **43.9%** |
| D9 | 31.2% | 33.2% |
| D7 | 22.9% | 24.2% |
| D5 | 18.7% | 18.4% |
| D3 | 11.8% | 11.8% |
| **D1** | **4.9%** | **8.2%** |

Monotonic in both seasons, top decile 5–9× the bottom. This is the strongest evidence in the whole build that the scoring works — and it's a *ranking* result, which is exactly what a ranked board needs.

---

## 2. Receiving yards — bar: 40 · WR/TE/RB

| Component | Weight | What it is |
|---|---|---|
| `f_wopr` | **42%** | Weighted opportunity — target share + air yards share |
| `f_receiving_yards` | **36%** | Trailing production |
| `f_receiving_air_yards` | 12% | Depth of target — deeper targets carry more yards |
| `implied_total` | 6% | Context, capped |
| `opp_pass_soft` | 4% | Context, capped |

WOPR leads because opportunity precedes production. Air yards is the aDOT proxy — the launch-angle analog.

---

## 3. Receptions — bar: 4 · WR/TE/RB

| Component | Weight | What it is |
|---|---|---|
| `f_target_share` | **50%** | Share of his team's targets |
| `f_receptions` | **35%** | Trailing catches |
| `f_targets` | 15% | Raw volume |

**Zero context.** Every context variant tested made this market worse. Receptions are close to pure target share, and anything else is dilution.

Note this model is the *opposite* of receiving yards on depth: short targets get caught more, deep targets gain more yards. A 5% inverted air-yards term was tested and dropped — real in direction, too small to justify.

---

## 4. Rushing yards — bar: 50 · RB/QB

| Component | Weight | What it is |
|---|---|---|
| `f_carries` | **65%** | Trailing carries |
| `f_rushing_yards` | 20% | Trailing production |
| `f_rz_car` | 15% | Red-zone carries — role security |

Rushing yards are volume with noise on top. Efficiency barely survives week to week; carries do. `f_rz_car` is here as a role signal, not an efficiency one — a back who gets goal-line work is a back the staff trusts to stay on the field.

**This model does not beat trailing carries out of sample** (see below). Read it as a convenience ranking, not an edge.

---

## 5. Rushing attempts — bar: 12 · RB

| Component | Weight | What it is |
|---|---|---|
| `f_carries` | **75%** | Trailing carries |
| `f_rz_car` | 25% | Goal-line role |

The most nearly-tautological model here: predicting carries from carries. Game script was tested at 7–20% and made it worse in both seasons. **This is the weakest of the seven** and the strongest candidate to be presented as a plain sorted table rather than a "score."

---

## 6. Passing yards — bar: 225 · QB

| Component | Weight | What it is |
|---|---|---|
| `total_line` | **26%** | Game total — shootout environment |
| `opp_pass_soft` | **22%** | Pass yards the defense allows |
| `f_passing_yards` | 22% | Trailing production |
| `f_attempts` | 18% | Volume |
| `f_passing_cpoe` | 12% | Completion % over expected — the one true skill term |

**The context-led model, and the reason for it.** With 32 starters who all throw, "who plays" is already settled, so environment does the selecting. `total_line` alone (55.8%) outperforms trailing passing yards alone (55.4%).

Worth naming: the base rate here is ~48%. This bar is close to a coin flip by construction, and no amount of modeling changes that.

---

## 7. Kicking points — bar: 6 (FG×3 + PAT) · K

| Component | Weight | What it is |
|---|---|---|
| `implied_total` | **35%** | The offense has to move the ball |
| `f_tm_fg_drive_rate` | **25%** | …and then stall |
| `f_tm_rz_td_rate` *(inverted)* | 15% | Teams that DON'T punch it in kick more |
| `f_fg_att` | 10% | Trailing attempts |
| `kick_env` | 8% | Indoors bonus, wind penalty |
| `f_tm_drives` | 7% | Pace |

**The surprise of the build.** Trailing form for kickers was worth +0.3 points — statistically nothing. This model is worth **+4.6 out of sample**, the largest edge of any of the seven.

The mechanism is the inverted red-zone term. A kicker's best week is an offense that moves the ball *and stalls*; his worst is an offense that scores touchdowns. Nobody's trailing stat line contains that, because it's a property of the offense, not the kicker. Field-goal points are almost entirely a team-context stat wearing a player's name — which is exactly why context-led scoring works here and form-based scoring can't.

Don't cut this market. It's the best one.

---

## The Report Card

Weights were tuned on 2025, then run untouched on 2024. **The 2024 column is the honest one.**

| Market | Bar | 2024 MODEL | vs FORM | vs BASE | 2025 (tuned) | vs FORM |
|---|---|---|---|---|---|---|
| Anytime TD | 1 | 50.0% | **+1.7** | +28.2 | 49.2% | +7.9 |
| Receiving yards | 40 | 74.6% | +0.4 | +48.6 | 76.7% | +2.1 |
| Receptions | 4 | 79.6% | **+1.7** | +52.7 | 73.8% | +2.5 |
| Rushing yards | 50 | 67.9% | **−1.7** | +44.2 | 69.2% | +2.9 |
| Rushing attempts | 12 | 74.2% | **−2.9** | +43.6 | 80.0% | +0.4 |
| Passing yards | 225 | 56.7% | **+1.7** | +7.6 | 58.3% | +2.9 |
| Kicking points | 6 | 72.1% | **+4.6** | +5.2 | 69.2% | +1.2 |

`FORM` = rank by trailing average in the market's own stat. `BASE` = every eligible player. Top 15 per week, 240 picks per market per season.

### What this actually says

**The TD model's 2025 edge was mostly overfit.** +7.9 in-sample became +1.7 out of sample. That shrinkage is the single most important number in this document and it should be stated on the site, not buried. What *did* survive is the decile separation — 44% top decile vs 8% bottom in 2024 — so the score ranks well even where its top-15 edge over naive form is thin.

**Kicking points is the real find.** It's the only market where the edge grew out of sample (+1.2 → +4.6), which is what a genuine mechanism looks like as opposed to a fitted one.

**Both rushing markets fail out of sample.** Trailing carries beat everything built on top of them. Ship them as sorted tables, or ship them with the negative number printed next to them. Do not dress them up.

**Every market crushes BASE by 40–50 points** — but read that honestly. Most of that gap is just "this player is a starter," which the sportsbook already knows. `vs FORM` is the column that means anything.

---

## Known gaps

1. **No injury or inactive filter yet.** Every backtest above scores players who were later ruled out. Wiring `load_injuries` + the Sunday inactives run will move these numbers, probably upward.
2. **No NGS layer.** `avg_separation`, `avg_cushion`, `rush_yards_over_expected_per_att`, `percent_attempts_gte_eight_defenders` are all available and none are in the models yet. RYOE is the most promising untested addition for the two failing rushing markets.
3. **Weeks 1–2 unscoreable.** The trailing window needs 4 weeks and a 2-game minimum, so the table starts at week 3. Week 1 needs prior-season carryover or the boards don't exist for the opener.
4. **Two seasons is a small validation set.** 2023 and 2022 are one flag away and should be run before Week 1.
5. **No odds.** Every bar here is a fixed threshold, not a real line. Edge-vs-line is a different and better question that needs a paid feed.
