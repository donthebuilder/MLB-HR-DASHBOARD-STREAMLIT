# Discord copy changes, 2026-10-07 (before -> after)

Public (fan room)
- Live digest, night wrap: "designated picks cleared their bar tonight" -> "picks cleared their bar tonight"
- Live digest title when it needs more than one message: "Moonshot — live digest" -> "Moonshot — live digest (1/2)", "(2/2)"; a section that carries over is titled "<SECTION> (cont.)"
- Pool alert for an old-recipe six-man pool and its halves: three possible "POOL CASHED" lines -> one line naming the six-man pool and which halves cashed

Ops alerts (now sent to DISCORD_OPS_WEBHOOK; falls back to DISCORD_WEBHOOK while that key is unset)
- Slate blocked (make_slim): "is not a slate" -> "is not a full slate"
- Slate blocked: "The data branch keeps its previous slate rather than serving a fragment. The site would have shown every hitter as one the model never picked." -> "The site keeps showing the previous slate instead of a partial one. Publishing it would have shown every hitter as a player we never picked."
- Board alert title (staleness_watch): "Board staleness" -> "Board out of date"
- Board alert: "A GitHub cron slot was likely skipped — the record for any game that locks now is stale." -> "A scheduled refresh was probably skipped, so any game that starts now would be graded against old picks."
- Board alert: "the meta file is missing" -> "the status file is missing"
- Context validation (validate_context): "Which context stats have earned a place in the scoring" -> "Which context stats have earned a place in how picks are scored"
- Context validation: "Nothing here changes a weight on its own." -> "Nothing here changes how picks are scored on its own."
- Pick lock (pick_lock): "N designation change(s) refused after first pitch" -> "N pick change(s) refused after first pitch"
- Pick lock: "This is the rule the receipts card has always claimed; it is enforced now." -> "This is that rule holding."
- Pick lock: a list longer than 8 now ends "+N more not listed" (was cut silently)

Not in this repo (site repo, moonshot-push): "The bot had him for ...", "The bot's TOP pick has gone deep ...", "THE BOT VS THE PEOPLE", "= bot pick". Nothing with that wording exists in the bot repo's Discord code.
