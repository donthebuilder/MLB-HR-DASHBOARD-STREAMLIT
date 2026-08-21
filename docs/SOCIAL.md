# DASH Network social pipeline — Phase 1

Built 2026-08-21. Turns a fully-graded MLB slate into a Moonshot Night Recap
draft — captions, three graphics, deduped by fingerprint — that lands in an
approval queue for a human to approve, edit, or reject before anything ever
reaches X or Instagram. Nothing here changes model logic, grading logic, or
the site. Nothing auto-publishes.

## 1. Files that changed

New:
- `bots/social/` — the reusable package (schema, brand config, fingerprint/
  dedupe, queue+history storage, Claude captions, Pillow asset rendering,
  X/Instagram/Discord publisher adapters).
- `bots/social_night_recap.py` — the one wired-up content type: builds a
  Moonshot Night Recap from already-published grading data and queues it.
- `.github/workflows/social-night-recap.yml` — runs the above nightly,
  5 minutes after `accountability.yml`'s memory job.
- `pages/1_Social_Queue.py` — a second Streamlit page (multipage
  convention — ships in the sidebar of the app you already have deployed,
  zero changes to `streamlit_app.py`). Approve / Save edits / Reject /
  Copy caption / Publish Now.
- `tests/test_social.py` — schema, fingerprint/dedupe, brand reuse
  (Moonshot + Tuddy), "never invent a fact", asset caching, and
  credential-absent publisher behavior. `python tests/test_social.py`.
- `.streamlit/secrets.toml.example`, `docs/SOCIAL.md` (this file).

Modified:
- `bots/requirements.txt` — added `anthropic` (captions.py only).
- `.github/scripts/publish_data.sh` — added `social/` to the directory
  carry-forward list (same treatment as `detail/`, `splits/`, `zones/`) and
  a trim block for `social/history/*.jsonl`, same shape as the existing
  `GRADED_KEEP`/`ODDS_KEEP` logs.

Nothing in `moonshot-mlb` (the site repo) changed. It stays read-only by
design (see `lib/dataSource.js`'s own header comment) — the approval queue
lives in the Streamlit app instead, which already existed and already has
write-capable tooling patterns (`st.secrets`) to build on.

## 2. Architecture

```
graded_results_<date>.json  ──┐
odds_history.json  (optional) ─┼─► bots/social/night_recap.py ─► `data` dict
hr_capture_report (same file) ─┘        (Moonshot/MLB only, for now)
                                                │
                                                ▼
                                    bots/social/schema.py
                                    (the one post shape every
                                     DASH product will share)
                                                │
                              ┌─────────────────┼─────────────────┐
                              ▼                 ▼                 ▼
                  bots/social/captions.py  bots/social/assets.py  bots/social/
                  (Claude, structured        (Pillow, headless    fingerprint.py
                   output, POST_DATA only)    shareCard.js         (dedupe key
                                               equivalent)          per platform)
                              │                 │                 │
                              └─────────────────┼─────────────────┘
                                                ▼
                                  bots/social/store.py — writes
                                  public/data/current/social/{queue,
                                  fingerprints}.json + history/*.jsonl,
                                  picked up by publish_data.sh like every
                                  other bot output.
                                                │
                                                ▼
                              pages/1_Social_Queue.py (Streamlit)
                              reads the queue, writes Approve/Reject/Edit
                              back via the GitHub Contents API
                                                │
                                                ▼
                              bots/social/publishers.py — publish_x() /
                              publish_instagram(), called ONLY from the
                              queue page's Publish Now button right now.
```

Why this shape: every automation trigger, secret, and the one existing
multi-destination notifier (`DISCORD_WEBHOOK`) already live in the bot repo
(`MLB-HR-DASHBOARD-STREAMLIT`), not the site. This reuses that — a new
package under `bots/`, one new workflow, and a second page on the Streamlit
app that was already deployed and already had a GitHub token pattern
(`st.secrets`, `_headers()`) to build the write path on.

**Generic by construction, not by promise**: `bots/social/schema.py`,
`fingerprint.py`, `store.py`, `captions.py`, `assets.py`, and
`publishers.py` contain zero MLB-specific field names. The only
sport-specific code is `night_recap.py`, which reads MLB's
`graded_results_<date>.json` shape and produces the same generic `data`
dict any content-type builder would. A Tuddy week-recap builder is a new,
similarly-sized file, not a rewrite — see §11.

## 3. Social post schema

One object per post (`bots/social/schema.py`):

```json
{
  "id": "moonshot-mlb-2026-08-21-night-recap",
  "created_at": "2026-08-21T09:26:00Z",
  "brand": "dash",
  "product": "moonshot",
  "sport": "MLB",
  "content_type": "night_recap",
  "status": "pending_review",
  "priority": "normal",
  "data": { "board_record": "3/15", "hr_scorers": 7, "slate_hr_coverage": "13/14", "...": "..." },
  "assets": { "square": "social/assets/2026-08-21/...png", "story": "...", "landscape": "..." },
  "captions": { "x": "...", "instagram": "...", "story": "..." },
  "headline": "...",
  "recommended_platforms": ["x", "instagram"],
  "publish": { "x": false, "instagram": false },
  "fingerprints": { "x": "MLB|moonshot|2026-08-21|night_recap|RECAP|x", "instagram": "..." },
  "approved_at": null,
  "published_at": null,
  "error": null,
  "status_history": [{ "status": "pending_review", "at": "..." }]
}
```

Statuses: `draft, pending_review, approved, scheduled, published, rejected,
failed` — every generated post defaults to `pending_review`; nothing skips
that state. `data` is intentionally free-form per content type, but every
value in it must trace back to an already-published DASH file — see §5.

## 4. Social queue workflow

1. `social-night-recap.yml` runs at 09:25 UTC (5 min after
   `accountability.yml`'s nightly memory job) or on demand
   (`workflow_dispatch`, optional `date` input for a backfill).
2. `bots/social_night_recap.py` checks the slate is actually fully graded
   (`night_recap.is_night_complete()` — every `game_pk` has `is_final=1`);
   if not, it exits quietly and tries again next run.
3. It checks the queue for a post with this exact id
   (`moonshot-mlb-<date>-night-recap`) — one recap per product per date,
   the spam guard spec section 16 asks for.
4. Builds `data`, generates captions (Claude), renders three PNGs
   (Pillow), writes the post to `queue.json` as `pending_review`, appends a
   `queued`/`pending_review` line to today's history file, and pings
   Discord (reusing the existing `DISCORD_WEBHOOK` convention) that a
   draft is waiting.
5. Open the Streamlit app → **Social Queue** page (new sidebar entry).
   Each card shows the rendered image, editable X/Instagram caption boxes,
   and buttons: **Approve**, **Save edits**, **Reject**, **Copy X**,
   **Copy IG**, **Publish Now**.
6. Approve/Reject/Save write straight back to `queue.json` on the `data`
   branch via the GitHub Contents API (needs `GITHUB_TOKEN` in the app's
   Streamlit secrets — see §9).
7. **Publish Now** only works once platform credentials are also present
   in Streamlit's secrets (separate from the GitHub Actions secrets of the
   same name — two different runtimes). Until then, **Copy X / Copy IG**
   is the real publish path, matching what you asked for as the default.

## 5. Hallucination protection — how Claude is boxed in

`bots/social/captions.py` sends Claude exactly one thing: the post's
`data` dict, already computed by `night_recap.py` from a published grading
file. The system prompt states the hard rule (every number/name/price must
come from `POST_DATA`, omit rather than invent), and — more importantly —
Claude is structurally unable to reach for anything else, because nothing
else is in its context: no odds feed, no box score, no player database.
Output is forced through a single tool call (`tool_choice`), not parsed
from prose, so a malformed response fails loudly instead of shipping.
`night_recap.py` itself only ever adds a key to `data` when it actually
computed a value — `{k: v for k, v in data.items() if v not in (None, [],
"")}` — so a missing odds price is *absent*, never `null` or `0`.

Verified in `tests/test_social.py::test_night_recap_omits_fields_it_cannot_compute`.

## 6. Duplicate protection

`bots/social/fingerprint.py`:
`{sport}|{product}|{date}|{content_type}|{subject}|{platform}` — e.g.
`MLB|moonshot|2026-08-21|night_recap|RECAP|x`. **One platform's fingerprint
never blocks another** (X and Instagram are independent), matching the
spec exactly. Two tiers:

- **Drafting**: gated by the post's own `id` (one recap per product/date —
  a second run the same day is a no-op).
- **Publishing**: gated by `fingerprints.json`, a compact set of every
  fingerprint that has reached a terminal state (published OR rejected).
  `night_recap.py` checks this before even queueing a redundant draft;
  the queue page's Reject and Publish Now buttons both add to it via
  `mark_fingerprints_decided()`. A rejected fingerprint stays blocked too
  — regenerating the same event later does not silently re-offer it. The
  queue UI doesn't yet expose an explicit "repost anyway" override
  (§13 limitation) — for now, deleting the fingerprint entry by hand is
  the escape hatch.

Verified in `tests/test_social.py` (fingerprint shape, platform
independence, idempotence across repeated builds).

## 7. Claude caption-generation flow

`generate_captions(post, brand_cfg=...)` → one Claude call, tool-forced
JSON: `headline`, `x_caption`, `instagram_caption`, `story_text`,
`recommended_platforms`. Model is `SOCIAL_CAPTION_MODEL` (env var, default
`claude-sonnet-4-5` — **check this is still current in Claude's docs before
relying on it**, model names change). Missing `ANTHROPIC_API_KEY`, an
import error, or any API failure returns `None` — the post still queues,
just with empty caption fields for you to fill in by hand, rather than
failing the whole run. Publishing/content generation is explicitly
downstream of grading (§18 of the spec) — this is that rule in code.

## 8. Screenshot / asset integration

There's no browser in a GitHub Actions runner, so `bots/social/assets.py`
is a headless equivalent of `components/shareCard.js` — same "dark field,
one accent glow, big numbers" visual language, drawn with Pillow (already
a bot dependency) instead of `<canvas>`. Square (1080×1080), story
(1080×1920), landscape (1600×900). `cached_or_render()` skips re-rendering
if the file already exists for that post id + variant (spec §9's "do not
generate the same image repeatedly"). Verified by rendering all three
variants plus a second brand (Tuddy) — see the images sent alongside this
doc, and `tests/test_social.py::test_cached_or_render_does_not_recompute_an_existing_asset`.

One thing worth knowing: brand icons are drawn as a letter monogram, not
the 🌙/🏈 emoji in `brands.py` — the DejaVu font on a bare Actions runner
has no pictographic glyphs, so an emoji there rendered as a tofu box until
this was caught and fixed. `components/shareCard.js` made the same call
for its own header chip ("HR" text, not an emoji) for what looks like the
same reason.

## 9. Publishing provider — direct APIs, both wired, neither turned on

You said manual by default but wire the APIs if it's not much extra work,
and asked what I'd recommend given you don't know Buffer: **direct APIs**
for both X and Instagram, not Buffer. You already made both accounts, and
Buffer's free tier caps at 3 channels/10 scheduled posts — a subscription
for functionality X's and Instagram's own free write tiers already give
you for a single account. `bots/social/publishers.py` implements both:

- **`publish_x()`** — X API v2 (`POST /2/tweets`) + v1.1 media upload,
  OAuth 1.0a user-context signing (hand-rolled HMAC-SHA1, no new
  dependency). Needs a free X developer app.
- **`publish_instagram()`** — Instagram Graph API, two calls (create media
  container from a public image URL, then publish it). Needs an Instagram
  **professional** (Business or Creator) account linked to a Facebook
  Page, and a Meta developer app. No App Review is required to publish to
  your own account under your own app in Development Mode — App Review is
  only needed to publish on *other* people's accounts.

**Neither is turned on.** Both return `{"ok": false, "error": "...
not configured"}` until their env vars exist, and nothing calls them
except the queue page's Publish Now button and (in the future, once you
want it) an auto-publish path gated by `SOCIAL_AUTO_PUBLISH=true`, which
`social_night_recap.py` never sets and never reads.

## 10. Required environment variables / secrets

No `.env` file exists in this repo — it uses **GitHub Actions repo
secrets** (Settings → Secrets and variables → Actions) for the bot side,
and **Streamlit's own secrets** (Streamlit Cloud → App settings → Secrets,
or a local `.streamlit/secrets.toml`) for the queue page. See
`.streamlit/secrets.toml.example` for the second store.

| Name | Where | Needed for |
|---|---|---|
| `ANTHROPIC_API_KEY` | GitHub Actions | Caption generation |
| `DISCORD_WEBHOOK` | GitHub Actions | "New draft" ping (already existed) |
| `GITHUB_TOKEN` | Streamlit secrets | Queue page writes (Approve/Reject/Edit) — needs `contents: write` on this repo |
| `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` | GitHub Actions and/or Streamlit secrets | Direct X publishing |
| `META_ACCESS_TOKEN`, `INSTAGRAM_ACCOUNT_ID` | GitHub Actions and/or Streamlit secrets | Direct Instagram publishing |
| `SOCIAL_AUTO_PUBLISH` | GitHub Actions | Gate for a future auto-publish path — leave unset/`false` |
| `SOCIAL_CAPTION_MODEL` | GitHub Actions | Optional override of the Claude model used for captions |

Never commit any of these. Add the ones you're ready to use; every code
path degrades safely without a given secret (empty captions, no asset if
Pillow/fonts are missing, publisher returns a clear "not configured"
error).

## 11. How Tuddy (NFL) will reuse this

`bots/social/brands.py` already has a `tuddy` entry (green accent,
🏈 icon — rendered as a "T" monogram, same reasoning as §8). Nothing in
`schema.py`, `fingerprint.py`, `store.py`, `captions.py`,
`assets.py`, or `publishers.py` mentions MLB. Standing up a Tuddy content
type is:

1. A new `bots/social/week_recap_nfl.py` (or whatever content type comes
   first) that reads `nfl_results.json`/`nfl_report_card.json` (already
   published — see `nfl.yml`) and returns the same shape of `data` dict
   `night_recap.py` does.
2. A thin `bots/social_week_recap_nfl.py` entrypoint, copy-shaped from
   `bots/social_night_recap.py`, swapping `product="tuddy"`,
   `sport="NFL"`, `brand("tuddy")`.
3. A new workflow, or an extra step in `nfl.yml`.

The queue page, fingerprinting, captions, asset rendering, and publishers
need zero changes — verified directly in `tests/test_social.py` by
rendering a Tuddy card with the same `render_recap_card()` used for
Moonshot and confirming the two use different colors/text and neither
leaks the other's data.

## 12. Tests performed

`python tests/test_social.py` — 36 checks, all passing:
- schema defaults (`pending_review`, no auto-publish flags set), unknown
  content-type rejection, status-history stamping.
- fingerprint shape matches the spec's own example exactly, platform
  independence, idempotence across repeated builds of the same event.
- brand config present and distinct for `moonshot` and `tuddy`, unknown
  product degrades instead of crashing.
- `is_night_complete()` correctly requires every game final.
- the omit-don't-invent contract for fields `night_recap.py` couldn't
  compute.
- `cached_or_render()` renders once and reuses the file on a second call.
- `publish_x()`/`publish_instagram()`/`auto_publish_enabled()` all fail
  safe with no credentials present.

Also, manually, against **real published data** (2026-08-20, a fully
graded MLB night, fetched live from the `data` branch):
- `night_recap.build()` produced correct numbers (board record 3/15,
  7 unique HR scorers, 13/14 slate coverage, longest HR 428 ft) —
  confirmed against `hr_capture_report` and `graded_slots` by hand.
- All three asset variants rendered and were visually inspected — this
  is how the landscape-variant text/footer overlap bug and the
  emoji-tofu-box bug below were actually caught, not guessed at.
- The full `bots/social_night_recap.py --dry-run` orchestration ran
  end-to-end against that real data and produced a valid, complete post
  object.

Bugs found and fixed during this testing pass (not just written and
assumed correct): a landscape card's stat rows overlapped its footer URL
because sizing was based on canvas width alone, which breaks on a
wide-short aspect ratio — fixed by sizing off `min(width, height)` and
adding an explicit footer-collision guard; brand emoji icons rendered as
empty boxes on a bare Linux font set — replaced with a text monogram; a
fingerprint's date was getting its hyphens stripped, which would have
silently broken the spec's own dedupe-key format — fixed the slug regex.

**Not tested**: an actual publish call to X or Instagram (no live
credentials available in this environment), the Streamlit queue page's
GitHub Contents API write path (needs a real `GITHUB_TOKEN` and a live
Streamlit session), and the GitHub Actions workflow itself (needs to run
in that environment against real repo secrets).

## 13. Current limitations — read before trusting this in production

- **This is not yet proven end-to-end in the real environment.** The
  orchestration logic, data sourcing, and rendering are tested against
  real published data; the workflow's actual run in GitHub Actions and
  the Streamlit page's actual write-back to the `data` branch are not.
  Run `social-night-recap.yml` once via `workflow_dispatch` on a past
  graded date, then exercise Approve/Reject on the queue page, before
  trusting the nightly schedule.
- **No live publish has been tested.** `publish_x()`/`publish_instagram()`
  are implemented against each platform's documented API shape but have
  never been called with real credentials. Test with one low-stakes post
  before relying on them.
- **The odds-price join for "top cashes" is best-effort.** `night_recap.py`
  guesses at `odds_history.json`'s per-player shape from a couple of
  common patterns; on the one real date tested it found no match and
  correctly omitted prices rather than invent one — but that also means
  the shape hasn't been confirmed. If you want "Ben Rice +310" style
  lines, `_odds_price_for()` in `bots/social/night_recap.py` needs its
  key path checked against `odds_history.json`'s actual layout.
- **Fingerprint dedupe has no "repost anyway" UI yet** — see §6.
- **No Daily Board / Player Spotlight / Watchlist / Live Result / Pick
  Cashed content types yet** — only Night Recap, matching the spec's own
  instruction to get this one workflow stable first (§21–22). The schema,
  queue, dedupe, and asset renderer are all generic enough to take them,
  but none of that code exists yet.
- **No `scheduled` status handling** — a post can reach `approved` but
  nothing currently turns a scheduled time into an automatic publish.
- **Existing workflows were not modified and were not re-run here** —
  `today.yml`, `results.yml`, `accountability.yml`, the site, and the
  Streamlit app's existing pages are untouched by this change; only
  `publish_data.sh` and `bots/requirements.txt` were edited, both
  additively (see §1). Worth a real CI run to confirm before trusting it.
