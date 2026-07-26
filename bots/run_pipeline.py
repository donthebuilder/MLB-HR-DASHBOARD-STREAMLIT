"""
run_pipeline.py

This is the fix for "crazy usage": the always-on FastAPI server from before
bills 24/7 whether the bot is doing anything or not. A real cron job only
bills for the minutes it's actually running.

This script does one full pass (lineups + scoring + publish) and then
exits. Railway's Cron Job service type spins up a container, runs this,
and shuts the container down — you're billed for maybe 1-3 minutes per
run, six times a day, instead of 24 hours of an idle server sitting there.

SQLite cache still lives on the persistent volume (/data), so it survives
between these scheduled runs same as before. Output gets pushed to the
`data` git branch (free, and Netlify never rebuilds off it) instead of
served by a live API, since there's no always-on process left to serve it.
"""

import os
import subprocess
import sys
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BOT_DIR = os.environ.get("BOT_DIR", os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.environ.get("GIT_REPO_DIR", BOT_DIR)
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ.get("GITHUB_REPO", "donthebuilder/MLB-HR-DASHBOARD")

os.makedirs(DATA_DIR, exist_ok=True)


def run_step(label, cmd):
    print(f"[{datetime.now().isoformat()}] {label}...")
    result = subprocess.run(cmd, cwd=BOT_DIR)
    if result.returncode != 0:
        print(f"[{datetime.now().isoformat()}] WARNING: {label} exited {result.returncode}", file=sys.stderr)
    return result.returncode == 0


def push_data_to_branch():
    print(f"[{datetime.now().isoformat()}] Publishing to data branch...")
    remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"

    subprocess.run(["git", "-C", REPO_DIR, "config", "user.email", "bot@mlb-hr-dashboard"], check=True)
    subprocess.run(["git", "-C", REPO_DIR, "config", "user.name", "mlb-hr-bot"], check=True)
    subprocess.run(["git", "-C", REPO_DIR, "remote", "set-url", "origin", remote_url], check=True)

    subprocess.run(["git", "-C", REPO_DIR, "fetch", "origin", "data:data"], check=False)
    subprocess.run(["git", "-C", REPO_DIR, "checkout", "data"], check=False)
    subprocess.run(["git", "-C", REPO_DIR, "checkout", "-b", "data"], check=False)

    subprocess.run(["git", "-C", REPO_DIR, "add", "public/data"], check=True)
    commit = subprocess.run(
        ["git", "-C", REPO_DIR, "commit", "-m", f"Auto-update {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    )
    if commit.returncode != 0:
        print(f"[{datetime.now().isoformat()}] Nothing new to publish.")
        return

    subprocess.run(["git", "-C", REPO_DIR, "push", "origin", "data"], check=True)
    print(f"[{datetime.now().isoformat()}] Published.")


def main():
    # STREAMLIT MIGRATION (2026-07-25): lineup_status.py is gone -- confirmed
    # lineups are pulled inline by the scoring bot now (see
    # refresh_locked_lineup_status), and nothing ever read the lineups.json it
    # wrote. today_bot.py was replaced by mlb_dashboard.py. make_slim.py then
    # shrinks the 76 MB slate to the ~3 MB payload the Streamlit app reads.
    run_step("Running scoring bot", ["python", "mlb_dashboard.py", "--today"])
    run_step("Building slim payload", ["python", "make_slim.py"])
    push_data_to_branch()
    print(f"[{datetime.now().isoformat()}] Pipeline run complete.")


if __name__ == "__main__":
    main()
