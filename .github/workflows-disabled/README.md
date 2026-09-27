# Disabled workflows (2026-09-27)

GitHub only runs files in `.github/workflows/`, so a file here is disabled,
not deleted. From .claude-notes/BOT-WORKFLOWS-CLEANUP.txt (site repo):

- Merged into `mlb-nightly-caches.yml` (same steps, one schedule + backup):
  pair-history, hr-companion, spray-archive, spray-cache.
- Folded into `today.yml` as the `run-tomorrow-bot` job (same steps, the
  same 12:05am + 2:05am slots): tomorrow.
- Removed (one-off tools that did their job, or duplicates):
  bat-tracking-probe, stats-probe, odds-probe, nfl-odds-probe (manual-only
  probes; odds now come from the site's SportsGameOdds pipeline),
  bbe-backfill (one-time history backfill), backtest-report (Grade Results
  already runs backtest_report.py nightly), dash-endpoints (Vercel's own
  crons call /api/fantasy/scoring and /api/dash/push/tick).

Checked before moving: nothing on the site or in another workflow reads a
file only these write; none of them was failing. Delete this folder after a
week if nothing broke -- git history keeps every file.
