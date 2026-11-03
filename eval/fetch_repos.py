"""
Fetch the real repositories named in eval/questions/*.yaml at their pinned
commit SHAs into eval/.repos/ (gitignored). Only a shallow fetch of that one
commit; nothing from the repos is executed.

    python eval/fetch_repos.py
"""

import glob
import os
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS_DIR = os.path.join(ROOT, "eval", ".repos")


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def fetch(url, sha):
    dest = os.path.join(REPOS_DIR, url.rstrip("/").split("/")[-1])
    if os.path.isdir(dest):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest,
                              capture_output=True, text=True).stdout.strip()
        if head == sha:
            print(f"[fetch] {dest} already at {sha[:12]}")
            return
    os.makedirs(dest, exist_ok=True)
    if not os.path.isdir(os.path.join(dest, ".git")):
        git("init", "-q", cwd=dest)
    git("fetch", "-q", "--depth", "1", url, sha, cwd=dest)
    git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=dest)
    print(f"[fetch] {url} @ {sha[:12]} -> {dest}")


def main():
    for path in sorted(glob.glob(os.path.join(ROOT, "eval", "questions", "*.yaml"))):
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        if "url" in spec:
            fetch(spec["url"], spec["sha"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
