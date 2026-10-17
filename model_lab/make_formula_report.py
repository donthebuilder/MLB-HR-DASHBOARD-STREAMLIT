#!/usr/bin/env python3
"""Generate the NFL tables of docs/model-lab/PICK_FORMULA_AUDIT.md (NFL_TABLES.md fragment) from results/*.json.
No number in the fragment is typed by hand."""
import json, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = lambda n: json.load(open(os.path.join(ROOT, "model_lab", "results", n)))
sel, hold, cand = R("nfl_formula_audit_selection.json"), R("nfl_formula_audit_holdout.json"), R("nfl_weight_candidates.json")
o = []
o.append("### NFL feature ablation and weight sensitivity (selection folds 2022-24)\n")
o.append("Each row removes one component (weights renormalised) or scales its weight by 0.5 or 1.5, on identical player-weeks. dLL = log loss minus the published formula's (probabilities from a train-season logistic map of the score); **negative = the change IMPROVES the formula**. 95% paired cluster bootstrap over (season, week).\n")
for key, m in sel["markets"].items():
    o.append(f"**{key}** ({m['label']}, bar {m['bar']}, n = {m['n_total']}, base rate {m['base_rate']:.3f}); published AUC {m['results']['published']['auc']:.3f}\n")
    o.append("| component | published weight | drop dLL [CI] | x0.5 dLL [CI] | x1.5 dLL [CI] | fitted weight by fold 2022 / 2023 / 2024 |")
    o.append("|---|---:|---|---|---|---|")
    fw = m["fitted_weights_by_fold"]
    for c, w in m["weights_used_after_availability"].items():
        cell = lambda nm: (lambda v: f"{v['d_logloss_vs_published']:+.4f} [{v['lo']:+.4f}, {v['hi']:+.4f}]")(m["results"][f"{nm}:{c}"]) if f"{nm}:{c}" in m["results"] else "n/a"
        o.append(f"| `{c}` | {w:.3f} | {cell('drop')} | {cell('x0.5')} | {cell('x1.5')} | " + " / ".join(f"{fw[f][c]:.2f}" for f in fw) + " |")
    o.append("")
o.append("### NFL candidate formulas, selection folds then the untouched 2025 holdout\n")
o.append("Probabilities from a logistic map fitted on earlier seasons. `fitted` = non-negative regularised logistic weights on the SAME components; `LR same` = unrestricted logistic on the same components; `LR wide` = logistic on every trailing feature. dLL against the published formula.\n")
o.append("| market | n sel / hold | published LL sel / hold | best candidate (selection) | its dLL sel [CI] | its dLL hold [CI] | published AUC -> candidate AUC (hold) |")
o.append("|---|---|---|---|---|---|---|")
for key, m in sel["markets"].items():
    h = hold["markets"][key]
    cs = {k: v for k, v in m["results"].items() if k in ("fitted_weights", "lr_same_components", "lr_wide_features", "equal_weights")}
    best = min(cs, key=lambda k: cs[k]["logloss"])
    hv = h["results"][best]
    o.append(f"| {key} | {m['n_total']} / {h['n_total']} | {m['results']['published']['logloss']:.4f} / {h['results']['published']['logloss']:.4f} | {best} | {cs[best]['d_logloss_vs_published']:+.4f} [{cs[best]['lo']:+.4f}, {cs[best]['hi']:+.4f}] | {hv['d_logloss_vs_published']:+.4f} [{hv['lo']:+.4f}, {hv['hi']:+.4f}] | {h['results']['published']['auc']:.3f} -> {hv['auc']:.3f} |")
o.append("")
o.append("### Frozen candidate weight vectors, scored once on 2025 (Holm over 7 markets)\n")
o.append("Freezing rule fixed beforehand: mean of the three selection-fold fitted weights, 2 decimals, drop < 0.02, renormalise.\n")
o.append("| market | stable across folds | published -> candidate weights | holdout n | LL published -> candidate | dLL [95% CI] | p | Holm threshold | result |")
o.append("|---|---|---|---:|---|---|---:|---:|---|")
for key, v in cand.items():
    h = v["holdout"]
    pw = ", ".join(f"{c.replace('f_', '')} {w:.2f}" for c, w in v["published_weights"].items()); cw = ", ".join(f"{c.replace('f_', '')} {w:.2f}" for c, w in v["frozen_candidate"].items())
    res = "VALIDATED (significant after Holm)" if v["significant_after_holm"] else ("worse on holdout" if h["d_logloss"] > 0.002 else "no measurable improvement")
    o.append(f"| {key} | {'yes' if v['stable'] else 'no'} | {pw} -> **{cw}** | {h['n']} | {h['published']['logloss']:.4f} -> {h['candidate']['logloss']:.4f} | {h['d_logloss']:+.4f} [{h['lo']:+.4f}, {h['hi']:+.4f}] | {h['p']:.3f} | {v['holm_threshold']:.4f} | {res} |")
o.append("")
o.append("### Selection-policy diagnostics for the top-5 card (selection folds, 54 weeks; holdout 18 weeks)\n")
o.append("Hit rate of the player at each within-week rank, then concentration. A cell is one player per week, so each rank cell has only n = 54 (selection) and its standard error is about 6.7 points.\n")
o.append("| market | rank 1 | 2 | 3 | 4 | 5 | #1-#2 gap (median score pts) | weeks with 2+ picks in one game | weeks with 2+ on one team | outcome correlation same game / different game (pairs) |")
o.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
for key, v in sel["selection_policy"].items():
    h = v["hit_by_rank"]
    f = lambda x: "n/a" if x is None else f"{x:+.2f}"
    o.append(f"| {key} | " + " | ".join(f"{h[str(i)]*100:.0f}%" for i in range(1, 6)) + f" | {v.get('gap_median_points', float('nan')):.1f} | {v['weeks_with_two_or_more_same_game']*100:.0f}% | {v['weeks_with_two_or_more_same_team']*100:.0f}% | {f(v['outcome_phi_same_game'])} / {f(v['outcome_phi_different_game'])} ({v['pair_counts']['same_game']} / {v['pair_counts']['different_game']}) |")
o.append("")
open(os.path.join(ROOT, "model_lab", "results", "NFL_TABLES.md"), "w").write("\n".join(o))
print("wrote NFL_TABLES.md", len("\n".join(o)))

# ── FORMULA_COMPARISON.md: one row per sport and market ──────────────────────────
c = ["# FORMULA COMPARISON (companion to PICK_FORMULA_AUDIT.md)", "",
     "NFL rows are generated from `model_lab/results/*.json` (selection folds 2022-24, untouched 2025 holdout). dLL = log loss of the candidate minus the published formula, negative = better, 95% paired cluster bootstrap. Everything else is not testable with current data and says so.", "",
     "| sport / market | current formula | strongest candidate | key differences | out-of-sample result | verdict |", "|---|---|---|---|---|---|"]
for key, v in cand.items():
    h = v["holdout"]; m = sel["markets"][key]
    cur = ", ".join(f"{k.replace('f_', '')} {w:.2f}" for k, w in v["published_weights"].items())
    new = ", ".join(f"{k.replace('f_', '')} {w:.2f}" for k, w in v["frozen_candidate"].items())
    dropped = [k.replace("f_", "") for k in v["published_weights"] if k not in v["frozen_candidate"]]
    diff = ("drops " + ", ".join(dropped) + "; ") if dropped else ""
    diff += "weights re-fitted on earlier seasons"
    verdict = "VALIDATED: ready for shadow" if v["significant_after_holm"] else ("keep current (candidate worse)" if h["d_logloss"] > 0.002 else "keep current (no measurable gain)")
    c.append(f"| NFL {key} | {cur} | {new} | {diff} | holdout n = {h['n']}: dLL {h['d_logloss']:+.4f} [{h['lo']:+.4f}, {h['hi']:+.4f}]; selection best candidate dLL {min(x['d_logloss_vs_published'] for k, x in m['results'].items() if k in ('fitted_weights','lr_same_components','lr_wide_features','equal_weights')):+.4f} | {verdict} |")
c += ["| NHL goal | equal percentiles: shots/g, goals/g, TOI/g (`lamp-goal-v3`) | `lamp-goalw-v1` ridge logistic (shadow; repo's own replay) | adds a defence indicator and fitted weights; goalie, power play, shot quality in neither | not re-run here; repo-stated fold results in section 2.2 | not testable here |",
      "| NHL shots 3+ / 2+ goals / points / assists | equal percentiles (2+ goals: 35/30/20/15) | none | | not testable | not testable here |",
      "| NBA (hidden) all markets | equal percentiles of volume and opponent legs | none | | not testable | not testable here; stays hidden |",
      "| MLB home run | `hr_raw` blend then re-anchor (`mlb_hr_v4`) | none tested; defects 1-5 identified by reading | | ledger only: HR rate by score quartile 11.6% to 20.0% among picked players (n = 3,247) | not testable here |",
      "| MLB hit / HRR / contact / overall | weighted blends, published values overwritten after scoring | none | | not testable | not testable here |", ""]
open(os.path.join(ROOT, "docs", "model-lab", "FORMULA_COMPARISON.md"), "w").write("\n".join(c))
print("wrote FORMULA_COMPARISON.md")
