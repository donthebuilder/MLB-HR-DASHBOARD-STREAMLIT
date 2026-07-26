# Streamlit migration — what to run

Everything in this folder is already updated. What's left is applying it to
the GitHub repo and pointing Streamlit Cloud at the right file.

Run these in order.

---

## 0. Why the app was stuck

Streamlit Cloud's log looped:

```
🚀 Starting up repository: 'mlb-hr-dashboard', branch: 'main', main module: 'bots/today_bot.py'
🐙 Cloning repository...        (repeats every ~6 minutes, forever)
```

Two separate problems:

1. **The repo is ~21 GB.** `public/data/` is 12 GB and `bots/outputs/` is
   8.6 GB — individual slate JSONs up to 113 MB, committed to `main` several
   times a day by `tomorrow.yml`, `spray-cache.yml`, `pair-history.yml`, and
   `hr-companion.yml`. The clone never finished, so the app never started.
2. **`bots/today_bot.py` is not a Streamlit app.** It has no `import
   streamlit` anywhere — it's a batch script that writes JSON. Even with a
   fast clone, pointing Streamlit at it produces a blank app.

Both are fixed below.

---

## 1. Back up first

The history rewrite in step 3 is irreversible.

```bash
cd ~/Documents/GitHub
cp -r MLB-HR-DASHBOARD MLB-HR-DASHBOARD-backup-$(date +%Y%m%d)
```

Your daily output also lives in `bots/outputs/` and `public/data/` inside
that backup, so nothing is lost — it just stops being in git.

---

## 2. Copy the updated files in

From your real clone:

```bash
cd ~/Documents/GitHub/MLB-HR-DASHBOARD
SRC="/Volumes/DONX/USERS/Kingdondondon/Downloads/MLB-HR-DASHBOARD-main 2"

# new + updated files
cp "$SRC/streamlit_app.py" .
cp "$SRC/requirements.txt" .
cp "$SRC/.gitignore" .
cp "$SRC/MIGRATION.md" .
cp "$SRC/README.md" .
mkdir -p .streamlit .github/scripts
cp "$SRC/.streamlit/config.toml" .streamlit/
cp "$SRC/.github/scripts/publish_data.sh" .github/scripts/
cp "$SRC/.github/workflows/"*.yml .github/workflows/
cp "$SRC/bots/mlb_dashboard.py" bots/
cp "$SRC/bots/make_slim.py" bots/
cp "$SRC/bots/fetch_picks_for_grading.py" bots/
cp "$SRC/bots/requirements.txt" bots/
cp "$SRC/bots/live_results_tracker.py" bots/
cp "$SRC/bots/run_pipeline.py" bots/
chmod +x .github/scripts/publish_data.sh

# remove the Next.js site and the two dead bot scripts
git rm -r --cached -q app components lib 2>/dev/null || true
rm -rf app components lib
git rm -q --cached next.config.js package.json package-lock.json vercel.json 2>/dev/null || true
rm -f next.config.js package.json package-lock.json vercel.json
git rm -q bots/today_bot.py bots/lineup_status.py 2>/dev/null || true
```

---

## 3. Purge the big data from git history

`.gitignore` stops *future* commits, but the 21 GB is already in history —
a clone still downloads it. This strips it out.

```bash
pip install git-filter-repo --break-system-packages

cd ~/Documents/GitHub/MLB-HR-DASHBOARD
git filter-repo --force \
  --path public/data --path bots/outputs \
  --path app --path components --path lib \
  --path node_modules --path package-lock.json \
  --invert-paths
```

Then check the damage is undone:

```bash
du -sh .git      # expect a few MB, not GB
```

`git filter-repo` drops the remote as a safety measure, so add it back and
force-push:

```bash
git remote add origin https://github.com/donthebuilder/MLB-HR-DASHBOARD.git
git add -A
git commit -m "Migrate to Streamlit; purge generated data from history"
git push --force origin main
```

> If `git filter-repo` isn't available, BFG works too:
> `bfg --delete-folders "{data,outputs}" --no-blob-protection` then
> `git reflog expire --expire=now --all && git gc --prune=now --aggressive`.

Your local `public/data/` and `bots/outputs/` folders stay on disk — they're
just untracked now, which is what the bot expects.

---

## 4. Seed the data branch

The app reads from the `data` branch. Nothing is there yet in the new format,
so run the workflow once by hand:

**GitHub → Actions → "MLB HR Bot — Today (data branch only)" → Run workflow**

When it finishes, confirm these exist on the `data` branch:

```
public/data/current/today_slim.json
public/data/current/today.txt
public/data/current/pair_builder_latest.json
```

---

## 5. Add the secret

**GitHub → Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `OWM_API_KEY` | your OpenWeatherMap key |

The key `8f135bcde3b6e1d859549e8419cc61e8` is hardcoded as a fallback in
`bots/mlb_dashboard.py` and has been in plaintext in the repo and in chat —
**rotate it** at https://home.openweathermap.org/api_keys and put the new one
in this secret.

---

## 6. Repoint Streamlit Cloud

In your Streamlit Cloud app → **Settings → General**:

| Setting | Value |
|---|---|
| Repository | `donthebuilder/MLB-HR-DASHBOARD` |
| Branch | `main` |
| **Main file path** | **`streamlit_app.py`** ← was `bots/today_bot.py` |
| Python version | 3.11 |

Then **Reboot app**. The clone is now a few MB, so it should be live in under
a minute.

**No Streamlit secrets needed.** The repo is public, so the app reads the
`data` branch straight off `raw.githubusercontent.com` with no auth. (The
`GITHUB_TOKEN` hook is still in `streamlit_app.py` in case you ever make the
repo private — it's ignored when unset.)

Verified working: the `data` branch is already serving
`public/data/current/today.txt` at the exact path the app requests. So even
before step 4's first run, the app has something to fall back on — it just
loads the old 76 MB `today.json` slowly until the slim file exists.

---

## 7. Turn off Vercel

The Next.js site is gone, so the Vercel project has nothing to build. Delete
the project (or disconnect the repo) in the Vercel dashboard so it stops
trying and stops billing.

---

## What changed, in one table

| Thing | Before | After |
|---|---|---|
| Front end | Next.js on Vercel | `streamlit_app.py` on Streamlit Cloud |
| Scoring bot | `bots/today_bot.py` | `bots/mlb_dashboard.py` |
| Lineups | `bots/lineup_status.py` step | inline in the scoring bot |
| Grader | `bots/grade_results.py` (didn't exist — the job failed every run) | `bots/live_results_tracker.py` |
| SQLite cache path | `bots/cache.db` (never read or written) | `bots/mlb_hr_cache.sqlite` |
| App payload | 76 MB `today.json` | 2.9 MB `today_slim.json` |
| Data publishing | full `public/data` committed, some jobs to `main` | small files only, `data` branch, single orphan commit |
| Repo size | ~21 GB | a few MB |
