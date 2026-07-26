# MLB HR Dashboard

A self-running MLB home run prediction dashboard. Python bots generate the
data, GitHub Actions runs them on a Phoenix-time schedule, and Streamlit
Cloud hosts the site.

> **Migrating from the old Next.js/Vercel setup?** Read
> [MIGRATION.md](MIGRATION.md) first — it has the exact commands, including
> the history purge that gets the repo back under a few MB.

## How it works

```
┌──────────────────┐  cron   ┌────────────────────┐  force-push  ┌───────────────┐
│  GitHub Actions  │ ──────> │ bots/              │ ───────────> │ `data` branch │
│  (several/day)   │         │  mlb_dashboard.py  │  1 commit    │ (~4 MB total) │
└──────────────────┘         │  make_slim.py      │              └───────┬───────┘
                             └────────────────────┘                      │ HTTPS
                                                                         v
                                                              ┌─────────────────────┐
                                                              │  streamlit_app.py   │
                                                              │  on Streamlit Cloud │
                                                              └─────────────────────┘
```

No computer of yours has to be on.

Two rules keep it that way:

1. **Nothing generated is ever committed to `main`.** `main` is code only.
   Data goes to the `data` branch as a single force-pushed orphan commit, so
   history never accumulates.
2. **The app reads a slim payload.** `make_slim.py` drops per-player raw logs
   (spray charts, contact logs, pitch-type tables) that the dashboard never
   displays, taking the slate from 76 MB to ~3 MB.

Ignoring rule 1 is what put 21 GB in this repo and made Streamlit Cloud time
out cloning it.

## Layout

| Path | What it is |
|---|---|
| `streamlit_app.py` | The dashboard. Streamlit Cloud's main module. |
| `bots/mlb_dashboard.py` | Scoring bot. Builds one slate per run (`--today` / `--tomorrow`). |
| `bots/make_slim.py` | Shrinks the slate JSON for the app. |
| `bots/live_results_tracker.py` | Grades picks against actual game results. |
| `bots/fetch_picks_for_grading.py` | Pulls the published slate into CI so the grader has input. |
| `bots/spray_cache.py`, `pair_history_cache.py`, `hr_companion_cache.py` | Supporting caches (persisted via `actions/cache`, not git). |
| `.github/scripts/publish_data.sh` | The single publish path to the `data` branch. |
| `requirements.txt` | Streamlit app deps only — keeps Cloud boots fast. |
| `bots/requirements.txt` | Bot deps (pybaseball etc.), installed by Actions only. |

## Workflows

| Workflow | Schedule (Phoenix) | Does |
|---|---|---|
| `today.yml` | 5am, 8am, 11am, 1pm, 3–6pm | Builds today's slate, publishes to `data` |
| `tomorrow.yml` | 12:05am | Builds tomorrow's slate |
| `results.yml` | 7pm, 9pm, 11pm, 1am, 3am | Grades picks, publishes results |
| `spray-cache.yml` | 6am | Warms the Statcast zone cache |
| `pair-history.yml` | 12:15am | Rebuilds pair-history cache |
| `hr-companion.yml` | 12:45am | Rebuilds companion cache |
| `backtest-report.yml` | manual | Tier-segmented backtest artifact |

## Dashboard tabs

- **🏆 Board** — ranked hitters, sortable by HR / board / overall / hit / HRR
  / contact / due score, with the top 12 as detail cards. CSV export.
- **🗓️ Games** — per-game blocks with weather, park factor, and that game's picks.
- **🎯 Pairs** — Pair Builder output (System 2) with tags and reasons.
- **🧩 Pools** — 4-man and 6-man pools.
- **✅ Results** — live and final grading with hit rates.
- **🤖 Bot Report** — the full text report, searchable and downloadable.

## Running locally

```bash
pip install -r requirements.txt      # app only
streamlit run streamlit_app.py
```

The app prefers local files in `public/data/current/` and falls back to the
`data` branch over HTTPS, so a local bot run shows up immediately.

To run the bot itself:

```bash
pip install -r bots/requirements.txt
python bots/mlb_dashboard.py --today
python bots/make_slim.py
```

## Configuration

| Where | Key | Purpose |
|---|---|---|
| GitHub → Secrets → Actions | `OWM_API_KEY` | OpenWeatherMap. **Rotate the old hardcoded key.** |
| Streamlit → Settings → Secrets | `GITHUB_TOKEN` | Only needed if the repo is private. |
| Env | `MLB_DASHBOARD_DIR` | Override the repo path the bot syncs into. |
