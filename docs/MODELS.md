# MODELS.md — model version changelog

**This file is the living record of what scoring logic produced which
predictions.** It exists because before 2026-08-21 no prediction row carried
any record of which version of the scoring logic produced it — a weight
change left no mark on the output, so the archive could silently mix two
different models under one label with no way to separate them later (see
`docs/DASH_MODEL_INVENTORY.md` and the 2026-07-14 HR model swap, which is
exactly this problem: known to have happened, un-recoverable at the row
level because nothing stamped it).

Source of truth for the current version strings: `bots/model_registry.py`
(`MODEL_FAMILY`, `MODEL_VERSIONS`, `SCHEMA_VERSION`). This document is the
*narrative* — why each version is what it is, and when it should change.
The registry module is the thing the bot actually imports.

---

## Current model versions (as of 2026-08-21)

| Market | Version | What it represents |
|---|---|---|
| `hr` | `mlb_hr_v3` | The home-run score (`hr_score`) — the blend in `MODEL_WEIGHTS["hr_blend"]` plus the HR gate, caps, and recency multiplier. The flagship score; every other market's version below is versioned independently of it. |
| `hr_shadow` | `mlb_hr_v1_recency` | The recency-first HR variant kept alongside the production HR score for comparison (`hr_score_shadow`) — an older blend, not a candidate replacement. |
| `hit` | `mlb_hit_v2` | The 1+ hit score (`hit_score`). |
| `hrr` | `mlb_hrr_v2` | The hits+runs+RBI production score (`hrr_score`). |
| `contact` | `mlb_contact_v2` | The contact/total-bases score (`contact_score`). |
| `overall` | `mlb_overall_v2` | The blended top-pick score (`overall_score`) — HR + HRR + HIT. |
| `top_board` | `mlb_top_v2` | The Top Board ranking score (`top_board_score_v2`). |
| `hrw` | `mlb_hrw_v1` | Home Run Window — today's HR-timing score (`hrw_score`). |
| `multi_hit` | `mlb_multihit_v1` | The multi-hit score (`multi_hit_score`). |
| `picks` | `mlb_pickmap_v3` | The designation/selection logic (`build_game_pick_role_map()`) that decides who gets called TOP / HR / HIT / HRR / CONTACT on a given slate. This is versioned too, separately from any score, because it is itself a model: it determines who gets evaluated as "the pick," and changing the selection logic (e.g. the 2026-08 double-up redesign) can move results without any score formula changing at all. |

`SCHEMA_VERSION = 1` — the *shape* of a published/archived prediction
record (field names and types). See "Model version vs schema version"
below.

---

## What each version number means

A version string is `{market}_v{N}` (the shadow model additionally carries
a trailing qualifier, `mlb_hr_v1_recency`, because it is a genuinely
different model rather than a newer generation of the same one). `N` is a
plain monotonic integer per market — never reused, never rewritten in
place, no semantic-versioning tiers (no patch/minor/major). Semver exists
to promise API compatibility to downstream consumers; nothing here consumes
a Moonshot model version as an API contract. The only consumer is an
evaluation join, and for that consumer the question is binary.

## When a model version should change

**The rule: bump the integer if and only if the change could alter the
numeric output for at least one historical input.** If it can, it is a
different version — even if the change moves a score by 0.1 points. There
is no smaller category than "different version"; a "patch" that nudges
scores still makes the old and new rows non-exchangeable, and pooling them
under one label is a silent error.

Bumps **(the change can move a number):**

- Any edit to `MODEL_WEIGHTS` (blend weights, gate thresholds, caps,
  recency multiplier).
- Adding, removing, or redefining a blend term or input transform.
- Swapping the underlying model (the 2026-07-14 HR model swap is the
  canonical example of a change that should have bumped a version and
  didn't, because no registry existed yet).
- A bug fix that changes a produced number. A fix is not exempt — history
  written under the buggy logic is still a different population from
  history written after the fix, whether or not the fix was "correct."
- A change to `build_game_pick_role_map()` / the designation logic bumps
  `picks`, not any individual score's version.

Not bumps **(the change cannot move a number):**

- A refactor, rename, or extracted function whose output is bit-identical.
- A comment, a log message, a Discord/report wording change.
- Any frontend/display change (`components/**`, `lib/**`, Streamlit
  layout). The site is not a model.
- A crash fix, retry-logic change, or cache-plumbing edit with identical
  scoring output.
- A dependency upgrade (e.g. `pybaseball`) that happens to move scores with
  no code change on this side. That is real and worth knowing about, but it
  is *input drift*, not a model-version event — it belongs in a run's
  environment metadata (`run_meta.env`), not in `MODEL_VERSIONS`. Folding
  data-source drift into the model version would mean bumping constantly
  for changes nobody made; ignoring it entirely would mean an unexplained
  discontinuity in the archive with no record of why. Recording it in
  `env` is the answer that does neither.

## Model version vs. schema version — why they're separate

`MODEL_VERSIONS` answers "what logic produced this number?" `SCHEMA_VERSION`
answers "can my parser read this row's shape?" They change on different
clocks — a field getting added to the prediction log doesn't mean the HR
model changed, and an HR weight edit doesn't mean the record's field names
moved — and conflating them means every row-shape change would falsely look
like a scoring change (or vice versa) to anything doing version-based
analysis. `SCHEMA_VERSION` bumps when: a field is added, renamed, or
removed from the prediction-log or run-meta record shape. It does not bump
for a weight change, and `MODEL_VERSIONS` does not bump for an added field.

## How current versions relate to history

The version strings in `bots/model_registry.py` are **labels for today's
logic**, declared the day the registry was created (2026-08-21). They are
not a claim about when each market's scoring last actually changed, and
**no historical file is rewritten to carry them.** Two consequences:

1. Rows written before this registry existed carry no `model_version` at
   all — `HitterRecord.model_version` defaults to `""` on any row that
   predates this change, and stays that way forever. At analysis time,
   treat unstamped history as `pre_registry`: known to exist, not
   attributable to `mlb_hr_v3` or any other specific version, because it
   wasn't produced under a regime that recorded one.
2. The 2026-07-14 HR model swap (see `docs/DASH_MODEL_INVENTORY.md`) is a
   real, dated logic change that happened before this registry existed. It
   is not un-happened by `mlb_hr_v3` being "the current version" — it means
   the archive genuinely contains at least two HR-model generations, none
   of them stamped, and nothing in this file should be read as claiming
   otherwise.

## How to document a future version bump

When a change lands that bumps a version per the rule above, append a new
dated block to this section (never edit a previous block — this file is
append-only for the same reason the prediction log is):

```
## mlb_hr_v4 — 2026-09-14
supersedes: mlb_hr_v3
commit: <git sha>
what changed: <one paragraph — which weight/term/threshold, and why>
```

Keep it to what changed and why. The full "should this ship" analysis
(shadow evidence, sample size, pre-registered decision rule) is a Task 6+
concern (candidate model promotion is explicitly not implemented as part of
this change) — this file only needs to say, permanently, "as of this
version bump, here is what changed and when," so a future row's
`model_version` can always be traced back to a real, dated explanation.

---

*This registry (`bots/model_registry.py`, this document, `run_id` stamping,
the prediction/outcome logs) was implemented 2026-08-21 as an additive
reliability change — Tasks 1–5 of the reviewed `DASH_ROADMAP.md`. It changes
no MLB scoring formula, no `MODEL_WEIGHTS` value, and no site behavior.*
