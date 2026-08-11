# JOB 3 — the two follow-ups, closed (2026-08-11)

The 08-10 handoff left the "leading up to" scan with two named follow-ups and
an explicit instruction not to act on either until a specific question was
answered. Both are answered here, from the code rather than from another
scan, and **both come out differently than the handoff expected.**

---

## The thing that governs both answers

Every field in the results table was tested **within fixed `hr_score` bands**.
That design is right for the question asked, but it has one consequence that
has to be applied before any line of it is read:

**Conditioning on a sum forces that sum's own inputs to trade off against each
other.** If a field is one of the ingredients of `hr_score`, then among hitters
who all scored ~72, the ones high on that field must be *lower* on the other
ingredients — otherwise they would not have landed at 72. So an in-blend field
is pushed toward NEGATIVE within-band lift by the test itself, regardless of
its true effect. (Berkson / collider selection.)

The handoff already noticed the symptom without naming the mechanism: it
labelled `lineup_spot` (−4.5pp) and `weak_spot_flag` (+4.2pp) as "already in
the blend — rediscovery."

So for any in-blend field, the sign has to be read against that pressure:

| field is in the blend | observed sign | reading |
|---|---|---|
| yes | negative | expected artifact — proves nothing |
| yes | positive | signal survived pressure pushing it the other way — real, and under-weighted |

Both follow-ups are in-blend fields. They land on opposite rows.

---

## Follow-up 1 — `season_k_rate` (+7.6pp)

**The handoff said:** "find where `season_k_rate` enters the blend. If it is a
penalty, the model is charging twice for something that partly pays back."

**It is not a penalty.** `mlb_dashboard.py:6635`:

```python
# K-RATE. Normalised over the realistic league span (14%-32%); a hitter
# who never strikes out is usually a slap hitter, not a power threat.
k_rate_term = 100.0 * minmax_norm(getattr(h, "season_k_rate", 0.0), 0.14, 0.32)
```

consumed at `:6699` as `_w["k_rate"] * k_rate_term`, weight **0.04**
(`:267`) — the second-smallest weight in the entire HR blend, above only
`times_through` at 0.02.

The model already believes the three-true-outcomes story on the HR side. It
just barely bets on it.

The K *penalties* are real but they all live in other scores, where charging
for a strikeout is correct because a strikeout is definitionally not a hit:
`:2614` contact, `:2643` hrr, `:2645` hit, `:2646` contact, `:6903` and
`:10622` hit/hrr floors. **None of them touch `hr_score`.** There is no double
charge to undo.

**What the +7.6pp actually means.** `season_k_rate` is an input to `hr_score`,
so the banded test should have pushed it negative. It came back +7.6pp anyway
— the second row of the table above. That is the strongest form of this
evidence, and 0.04 is a weight consistent with a signal the model is
under-using.

**Action:** sweep `_w["k_rate"]` upward (0.04 → 0.06 / 0.08 / 0.10) across the
archive. Do not hand-set it. And per the 08-09 lesson — where a `season_power`
sweep silently ran on 32 of 58 nights and the reported "monotone climb" was an
artifact of the missing 26 — **assert the night count in the sweep output and
fail if it is short.**

---

## Follow-up 2 — `l20pa_fb_rate` (−5.0pp)

**The handoff said:** "Do not act on this until you know whether
`l20pa_fb_rate` is popup-inclusive. A hitter whose air contact is infield
flies is indistinguishable from one who lifts the ball, on that field, and
that alone could produce the whole sign."

**It is not popup-inclusive, so that cannot be the explanation.** Statcast's
`bb_type` is four disjoint values and this codebase uses all four separately:

- `:3230` `ground_ball` · `:3231` `line_drive` · `:3232` `popup`
- `:3408` `l20pa_fb_rate = (bb_type == "fly_ball").mean()`
- `:3437` `l25pa_popup_rate = (bb_type == "popup").mean()` — tracked as its
  own field, because it is its own category
- `:3438` `air_rate = fb_rate + ld_rate + popup_rate` — they are summed, which
  is only valid if they are mutually exclusive

A popup is never counted as a fly ball anywhere in this repo. The contamination
hypothesis is dead.

**What is left is the artifact.** `l20pa_fb_rate` enters `l20_form` positively
at weight 0.07 (`:7654`), which feeds `hr_score` — first row of the table
above. A negative within-band lift is exactly what the test produces for an
in-blend field with no further explanation required. Same mechanism, same sign
as `lineup_spot`, which the handoff had already set aside as rediscovery.

**Action: do NOT flip or drop the fly-ball weight.** The −5.0pp is not evidence
that lifting the ball suppresses home runs; it is evidence that the test bands
on a number this field helps compute. The clean check, if it is worth the time:
re-band on an `hr_score` recomputed **without** the fb term. If the negative
sign collapses, it was the artifact. It is also the weakest line in the table
on sample (17 bands), which is a second reason not to act on it.

---

## Summary

| | handoff expected | actual |
|---|---|---|
| `season_k_rate` | a double penalty to remove | already a positive term at 0.04 — **under-weighted, sweep it up** |
| `l20pa_fb_rate` | popups might explain the sign | popups are excluded — **artifact of banding on the blend, leave it alone** |

Neither conclusion required re-running the scan. Both required reading what the
blend actually does, which is cheaper and was available the whole time.
