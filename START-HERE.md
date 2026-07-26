# Do this — new repo, all from the browser

Everything in this folder is the complete new repo: 24 files, 800 KB. Your old
repo isn't touched and stays as an archive.

---

## 1. Create the repo

Go to **https://github.com/new**

| Field | Value |
|---|---|
| Repository name | `MLB-HR-DASHBOARD-STREAMLIT` |
| Visibility | **Public** |
| Add a README | **leave unchecked** |
| .gitignore / license | **None** |

> Naming it exactly `MLB-HR-DASHBOARD-STREAMLIT` means zero edits. If you pick a
> different name, see step 5 — it's one extra line, not a code change.

Click **Create repository**.

---

## 2. Upload the files

On the new empty repo page, click **uploading an existing file**.

In Finder, open this `UPLOAD-TO-NEW-REPO` folder and press **⌘ + Shift + .**
(period) — this reveals hidden files. You must see `.github`, `.streamlit`,
and `.gitignore`. If you don't, GitHub won't get the workflows and nothing
will run automatically.

Select **everything** (⌘A) and drag it onto the GitHub upload box.

Confirm the file list shows all four folders' worth of files, then
**Commit changes**.

> If the drag drops only loose files and skips the folders, drag the folders
> `.github`, `.streamlit`, and `bots` in as a second, separate upload — GitHub
> keeps folder structure on folder drags.

---

## 3. Add the weather key

**Settings → Secrets and variables → Actions → New repository secret**

- Name: `OWM_API_KEY`
- Value: your OpenWeatherMap key

⚠️ The old key `8f135bcde3b6e1d859549e8419cc61e8` has been sitting in plaintext
in the code and in chat. Rotate it at
https://home.openweathermap.org/api_keys and paste the **new** one here.

---

## 4. Run the bot once

**Actions** tab → you'll see a banner asking to enable workflows → click
**I understand my workflows, go ahead and enable them**.

Then: **MLB HR Bot — Today (data branch only)** → **Run workflow** →
**Run workflow**.

Give it 10–30 minutes. When it's green, check the repo's branch dropdown —
a `data` branch should now exist containing
`public/data/current/today_slim.json`.

---

## 5. Point Streamlit at it

In your Streamlit Cloud app → **Settings → General**:

| Setting | Value |
|---|---|
| Repository | `donthebuilder/MLB-HR-DASHBOARD-STREAMLIT` |
| Branch | `main` |
| Main file path | `streamlit_app.py` |
| Python version | 3.11 |

**Reboot app.** The repo is 800 KB now, so it clones in seconds instead of
timing out.

> **Only if you named the repo something else:** Streamlit → Settings →
> Secrets, add one line —
> `GITHUB_REPO = "donthebuilder/whatever-you-named-it"`

---

## 6. Retire the old stuff

- **Vercel** — delete the project. The Next.js site is gone; it has nothing
  left to build.
- **Old GitHub repo** — leave it alone as your archive. Its scheduled
  workflows will keep firing though, so go to its **Actions** tab and disable
  each workflow (⋯ menu → Disable workflow) so it stops churning.

---

## What you end up with

| | Old | New |
|---|---|---|
| Repo size | ~21 GB | 800 KB |
| Front end | Next.js on Vercel | Streamlit |
| Clone time | timed out forever | seconds |
| Data | committed to `main` all day | `data` branch, 1 orphan commit, force-pushed |
| App payload | 76 MB | 2.9 MB |
