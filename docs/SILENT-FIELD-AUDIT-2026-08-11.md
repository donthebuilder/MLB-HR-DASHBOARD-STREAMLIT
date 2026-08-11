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
