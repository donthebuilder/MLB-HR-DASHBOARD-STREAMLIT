# Silent-field audit — 2026-08-11

Three bug classes turned up in one session and none was a modelling error:
fields never written, a caption describing code that had stopped doing it, and
a zero standing in for unknown. This is the systematic scan for more of the
same, and it found four more instances of the third class.

## The detector

**A field that never varies is either dead weight or silently broken.** Run
across 5,766 archived rows, then cross-checked against a live slate: if a field
is constant in the archive but VARIES live, the constant is a pipeline failure,
not a dead field. That single check found everything below.

## Class A — never written, or written constant

| field | archive | live slate | verdict |
|---|---|---|---|
| `weather_source` | `'none'` on 2,369 | `'open-meteo'` | fetch never landed on those nights |
| `weather_hr_effect_pct` | `0` on 2,369 | −2% to +8% | **fixed** — now null when unknown |
| `wind_boost`, `weather_wind_boost` | `0.0` on 2,369 | real values | **fixed** — dataclass defaults masked it |
| `weather_wind_deg` | `None` on 3,511 | 10 distinct | same weather gap |
| `roof` | `'open'` on 3,511 | `'open'` 178/178 | **fixed** — see below, broken everywhere |
| `park_fit_summary` | `'Park fit neutral'` on 2,264 | 14 distinct | pipeline gap, era-dependent |
| `longest_hr_score` | absent, 0 of 5,766 | 174/178 populated | **fixed** — grader whitelist |
| `hr_due_score` | 1,767 rows then absent | populated | **fixed** — grader whitelist |
| `data_quality_score` | `0.0` on 3,336 | not published at all | dead field, safe to drop |

## Class B — a default standing in for "we never asked"

### `roof` — every park in baseball reported open-air, forever

The worst one, because it was already diagnosed once. `CacheDB.venue()` fetched
with `hydrate=location` under a comment explaining that without explicit
hydration `get_venue_coords()` "was always going to fail" and that this was
"the actual root cause of weather being empty for the entire slate."

That fix covered coordinates and stopped. `infer_roof()` reads
`v["fieldInfo"]["roofType"]` — never hydrated, never returned — and falls back
to `if not roof: return "open"`.

The league has seven retractable or domed parks. A dome cannot be open 100% of
the time; the constant is the tell. Consequences:

- wind and temperature adjustments applied to games played **indoors**
- `enrich_weather_payload_for_website` gates on
  `has_roof_only_weather = roof in {closed, dome}` to mark a domed game as
  legitimately having weather without a fetch — **a branch never once taken**

Fixed by hydrating `location,fieldInfo`, with a fallback to location-only if
the combined hydration is rejected, so coordinates can never be lost again.

### `k_trap_flag` — defaults below their own thresholds

`putaway = safe_float(..., 0.180)` and `swstr = safe_float(..., 0.110)`, gated
on `putaway >= 0.225 and swstr >= 0.130`. Both defaults sit **below** the
thresholds they are tested against, so an unpopulated pitcher silently
guarantees `False` rather than "unknown". Advanced stats are landing now
(`pitcher_advanced_stats_status` ok on 169/178), so this is currently moot —
but the shape is the bug, and it will bite again the next time the feed lapses.

## Class C — decorations that never fire

`hidden_hr_value` is rendered in 3 site components and is `False` on all 2,534
archived rows AND 178/178 live. `k_trap_flag` is `False` on all 1,669. Neither
is provably broken — both have genuinely strict gates, and swstr >= 0.130 is
well above a league-average ~11% — but a badge that has never once appeared is
either mis-thresholded or should stop being rendered. Worth a decision either
way rather than leaving it ambiguous.

## The pattern worth remembering

Every one of these is the same shape: **a fallback answering a question that
was never actually asked.** `0` for a rate nobody fetched, `'open'` for a roof
nobody hydrated, `0.180` for a stat nobody had. None of them threw, none showed
up in a log, and each looked exactly like a real measurement downstream.

The cheapest defence is the detector at the top of this file. Run it against
the archive whenever a question comes back with a suspiciously clean answer.

---

# Addendum — static reference tables (same day)

Donovan: "the park fit and factor used to come from a csv that may need
updating with all the stuff we have now — go thru and if there are any updates
like that we need."

It is not a CSV any more; it is `PARK_FACTORS_V2`, a hardcoded dict in
mlb_dashboard.py. Three findings.

## 1. The roof answer was already in the file

`PARK_FACTORS_V2` carries a `"roof"` key per park and correctly marks seven as
Retractable: MIA, ARI, HOU, TOR, MIL, TEX, SEA. Meanwhile `infer_roof()` was
returning "open" for all of them, because it only read `fieldInfo.roofType`
from an API response that never hydrated it.

So the fix is better than the API hydration alone: `infer_roof` now falls back
to our own table, which means it returns the right answer even when the API is
slow, rate-limited or reshaped — precisely the failure that caused the bug. A
live `roofType` still wins, because whether a retractable roof is CLOSED
tonight is a fact only the API knows; the table can only say the park has one.

## 2. The table is a season stale, and 4 parks are missing

Header says "sourced from Baseball Savant park-factor table (2025+ all
hitters)". It is the 2026 season. It also still lists the A's at Sutter Health
Park.

More concretely, it holds **26 of 30 parks**. Missing: **KC, SD, SF, TB** —
each silently falls through to `PARK_FACTORS_NEUTRAL` (1.00). Those four are
anything but neutral, and a neutral default is another instance of the pattern
this document is about: a fallback that looks like a measurement.

**Recommended:** refresh from Savant rather than deriving from our archive
(see below for why), and add the four missing parks.

## 3. Our archive CANNOT rebuild the factors yet — and it is worth saying why

Tempting, now that 872 homers carry distances. Measured anyway, matching
graded rows to parks by venue name:

- 3,713 player-nights across 25 parks with >= 60 each, 538 homers, 14.5% base
- correlation between observed per-park HR rate and the 2025 table: **+0.32**

That looks like disagreement, and it is mostly noise. The archive rows are
PICKS, not a random sample of plate appearances, so the per-park rate is
confounded by which hitters got designated there. At ~130 player-nights a park
the standard error on a 14.5% rate is ~3pp, so NYY at 23.4% (n=107) is about
2.6 SD — unremarkable once you have tested 25 parks.

Do not rewrite the table from this. It is a smell test, and what it says is
"no gross contradiction", not "the table is wrong".

## Other static tables reviewed

| table | status |
|---|---|
| `PARK_DIMENSIONS` | fence distances; static but geometry rarely changes |
| `PARK_CF_BEARING` | park orientation for wind; static and correct in kind |
| `MODEL_WEIGHTS` | the blend; changes only through a measured sweep |
| `ISO_BANDS` (site) | measured, and re-tested 2026-08-11 — do not re-enable |
| `CALIB` (site) | measured against the bot's published score bands |
