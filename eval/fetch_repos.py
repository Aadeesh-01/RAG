"""
Fetch the real repositories named in eval/questions/*.yaml at their pinned
commit SHAs into eval/.repos/ (gitignored). Only a shallow fetch of that one
commit; nothing from the repos is executed.

    python eval/fetch_repos.py
"""

import glob
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS_DIR = os.path.join(ROOT, "eval", ".repos")
sys.path.insert(0, ROOT)

from coderag.gitfetch import fetch_commit  # noqa: E402


def main():
    for path in sorted(glob.glob(os.path.join(ROOT, "eval", "questions", "*.yaml"))):
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        if "url" in spec:
            dest = os.path.join(REPOS_DIR, spec["url"].rstrip("/").split("/")[-1])
            fetch_commit(spec["url"], spec["sha"], dest)
            print(f"[fetch] {spec['url']} @ {spec['sha'][:12]} -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
