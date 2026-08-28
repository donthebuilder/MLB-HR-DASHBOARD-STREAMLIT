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

`SCHEMA_VERSION = 2` — the *shape* of a published/archived prediction
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

## config_hash — the machine-verifiable backstop (2026-08-21)

Everything above this section describes `model_version`: a **declared**
label, written by a human, that depends on that human remembering to bump
it. `bots/model_registry.py`'s own docstring says the honest thing about
that: *"It does not hash config to auto-detect an unbumped drift. Versions
here are declared, not derived."* That is real progress over no trail at
all, but it means a weight (or gate, or threshold) edit that lands without
a version bump is invisible — every row before and after still reads
`model_version: "mlb_hr_v3"`, with nothing to tell them apart.

`config_hash` (`bots/config_fingerprint.py`) is the backstop: a
**derived**, deterministic fingerprint of the exact scoring configuration
in effect when a row was scored, computed the same way every time with no
human step to forget. Three fields now travel together, and they answer
three different questions:

| Field | Answers | Source |
|---|---|---|
| `model_version` | What logic did we **intend** to run? | Declared by a human in `model_registry.py` |
| `git_sha` | What source **commit** executed? | `git rev-parse HEAD` at run time |
| `config_hash` | What scoring configuration was **actually in effect**? | Derived, deterministically, from the configuration itself |

None of the three replaces either of the others. `git_sha` can be identical
across two runs that scored differently if a *config* value came from
somewhere other than the committed source (it doesn't, today, but the
distinction is the point: `git_sha` answers "which commit," not "which
config"). `model_version` can be identical across two runs that scored
differently if a human forgot to bump it — that is exactly the case
`config_hash` exists to catch.

### What determines the HR Score today (the inspection this feature is built on)

Before building this, the working assumption was that `MODEL_WEIGHTS` was
the HR scoring surface. It is not — it's the *centralized, deliberately
extracted* surface, and `MODEL_WEIGHTS`'s own header comment already says
so ("the hit/HRR/contact sub-formulas still have their weights inline for
now"). Tracing `apply_model_v2_layers()` (the ~1,300-line function that
actually computes `hr_score`) end to end found dozens of literal
thresholds and multipliers that are just as capable of moving the score and
live in no dict at all: the trap multipliers (0.72/0.78/0.70/0.80/0.82/
0.86/0.84), the HR-gate penalty/bonus ladder (-18/-8/-6/+3/+5/+8), the
hard-cap trap (45.0), the `strong_confirmed` signal thresholds — and, found
in the course of this work, the single most recently-tuned number in the
whole pipeline: the 2026-07-31 shadow re-anchor, `_hr_form_anchor = 0.30`,
blended against a hard-coded `0.80 * hr_score + 0.20 * season_power`,
neither of which is in `MODEL_WEIGHTS`. A fingerprint over `MODEL_WEIGHTS`
alone would have missed exactly the kind of change this feature exists to
catch.

### Design: two layers, one hash

`config_hash` combines, into one canonical dict before hashing:

1. **`weights`** — the real `MODEL_WEIGHTS["hr_blend"]` /
   `["hr_gate_thresholds"]` / `["recency_multiplier"]`, as literal data.
2. **`formula_structure_sha256`** — a hash of the **AST structure** (not
   the source text) of the functions that turn those weights + the raw
   per-player signals into `hr_score`: `apply_model_v2_layers`,
   `minmax_norm`, `_hr2_clip`, `_spot_damage_for_batter`. Whitespace,
   indentation, and every `#` comment are never part of the AST at all,
   and each function's own leading docstring is stripped before hashing —
   this codebase's dominant style of change is exactly that kind of
   comment/rationale edit (see the giant audit-log comment blocks
   throughout `mlb_dashboard.py`), and those are not scoring changes.
   Everything else — a literal number, an operator, a comparison, a
   branch, a call — changes the hash.

Then: canonical JSON (sorted keys, compact separators, stable float
`repr()`) → UTF-8 → SHA-256 → `"sha256:<64 hex>"`. Full digest always kept;
a caller that wants a short display form slices the string.

**Deliberately conservative in one direction.** A pure rename/refactor with
bit-identical output can still flip the formula-structure hash (AST
structure includes local identifier names, though not the function's own
top-level name — that's supplied externally and stripped from the dumped
structure to avoid double-counting). That is the safe side of the
tradeoff: an occasional false "config changed" flag on a refactor is a
two-minute check; a missed real constant change is a wrong model-evaluation
decision.

**Known, documented scope limit.** `apply_model_v2_layers()` computes HR
alongside sibling markets (hit/hrr/contact soft-multiplier adjustments,
`best_blend_score`, `top_board_raw`, `alt_hr_score`, `longest_hr_score`) in
one pass — structurally interleaved in the current code, not split apart by
this change (that would be a scoring-code refactor, out of scope for a
provenance-only change). Consequence: `hr_config_hash()` can also change
when a *sibling* market's formula changes, even though `hr_score` itself
did not move. That is over-inclusion, not under-inclusion — an extra false
flag to check by eye, never a missed real HR change.

Only the `hr` market has a `config_hash` today. Extending the same pattern
to `hit`/`hrr`/`contact`/etc. is straightforward repetition once someone
wants it costed against those markets' own inline-constant surfaces — not
done here (Task 6+ scope note, not a design limit).

### Never hashed

Timestamps, `run_id`, `git_sha`, environment/package versions, player data,
outcome data — anything that varies per-run or per-input rather than
per-configuration. `config_hash` answers "what configuration," never
"which run" or "which data."

### The rule, restated with a backstop now in place

**Any scoring configuration change should normally receive both a new
semantic model version and a new config hash.** The hash is the backstop
that detects when the version bump was forgotten — not a replacement for
bumping it. A `model_version`'s included rows carrying more than one
distinct `config_hash` is exactly that failure caught in the act; see
`bots/eval_report.py`'s CONFIG PROVENANCE section, which reports it as a
loud warning rather than pooling the rows silently, and never suppresses
that warning just because `--model-version` narrowed the report down to
the contaminated version.

### How it flows through the pipeline

`run_meta["config_hashes"] = {"hr": "sha256:..."}` (alongside
`model_versions`, `git_sha`) → copied onto `HitterRecord.config_hash` for
every freshly-scored row, same defaulted-field safety as `model_version`/
`run_id` (`""` means "written before this field existed, or hashing failed
this run" — never backfilled) → copied onto each prediction-log row (a
hash/reference, not the config blob itself) → preserved on
`prediction_of_record` the instant a game_pk locks, alongside `run_id`/
`model_version`/`generated_at`, immutable from that instant exactly like
they are → read by `eval_report.py`, which reports drift instead of hiding
it and supports `--config-hash` as an explicit filter alongside
`--model-version`.

Legacy `prediction_of_record` entries locked before `config_hash` existed
carry `config_hash: null` forever — never backfilled with today's live
hash, exactly the same historical-honesty rule `generated_at`/
`model_version` already follow (see "How current versions relate to
history" above).

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

*`config_hash` (`bots/config_fingerprint.py`, the "config_hash" section
above, the `HitterRecord.config_hash` field, `prediction_of_record`'s
`config_hash` key, `eval_report.py`'s CONFIG PROVENANCE section and
`--config-hash` flag) was added 2026-08-21, prompted by an adversarial
review of `eval_report.py` that flagged unstamped `model_version` drift as
the highest-priority provenance gap before eval numbers should be trusted
to justify a weight change. Provenance-only: a direct before/after scoring
comparison on an identical synthetic 60-hitter slate (commit `266d458` vs.
this change, 1,320 score values across 22 fields) showed zero mismatches.*
