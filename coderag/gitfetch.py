"""
Fetch one commit of a public GitHub repository, safely.

Only shallow-fetches the exact commit: no tags, no history, no credential
prompts. Inputs are validated so they can't be read as git options, and
nothing from the fetched repository is ever executed.

Every git command names its repository explicitly (--git-dir/--work-tree) and
runs with GIT_CEILING_DIRECTORIES set, so git can never fall back to a
repository *above* the destination (e.g. the project's own repo) when the
destination's .git is missing or broken.
"""

import os
import re
import shutil
import stat
import subprocess
import sys

GITHUB_URL = re.compile(r"^https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
SHA = re.compile(r"^[0-9a-f]{40}$")


class FetchError(ValueError):
    pass


def _env(dest):
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_CEILING_DIRECTORIES": os.path.dirname(os.path.abspath(dest))}


def _repo_args(dest):
    dest = os.path.abspath(dest)
    return [f"--git-dir={os.path.join(dest, '.git')}", f"--work-tree={dest}"]


def _git(dest, *args, timeout=120, check=True):
    try:
        return subprocess.run(["git", *_repo_args(dest), *args], cwd=dest, check=check,
                              timeout=timeout, env=_env(dest), capture_output=True)
    except subprocess.CalledProcessError as e:
        raise FetchError(f"git {args[0]} failed: {e.stderr.decode(errors='replace')[:300]}")
    except subprocess.TimeoutExpired:
        raise FetchError(f"git {args[0]} timed out after {timeout}s")


def rmtree(path):
    """shutil.rmtree that also removes read-only files (git objects on Windows)."""
    def make_writable(func, p, _):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


def head_sha(dest):
    """Commit checked out in `dest`'s own repository, or None (never a parent repo's)."""
    if not os.path.isdir(os.path.join(dest, ".git")):
        return None
    result = _git(dest, "rev-parse", "HEAD", check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def prepare_repo(dest):
    """Make `dest` an empty-or-valid repository of its own; wipe broken leftovers."""
    git_dir = os.path.join(dest, ".git")
    if os.path.isdir(git_dir) and _git(dest, "rev-parse", "--git-dir", check=False).returncode:
        rmtree(dest)                    # partial delete or corrupt clone: start over
    os.makedirs(dest, exist_ok=True)
    if not os.path.isdir(git_dir):
        subprocess.run(["git", "init", "-q", os.path.abspath(dest)], check=True,
                       env=_env(dest), capture_output=True)


def fetch_commit(url, sha, dest, timeout=120):
    """Check out commit `sha` of `url` into `dest` (reused if already there)."""
    url = url.rstrip("/").removesuffix(".git")
    if not GITHUB_URL.match(url):
        raise FetchError(f"not a https://github.com/<owner>/<repo> URL: {url!r}")
    if not SHA.match(sha):
        raise FetchError(f"not a full 40-character commit SHA: {sha!r}")

    if head_sha(dest) == sha:
        return dest
    prepare_repo(dest)
    # "--" ends option parsing: url and sha are positional no matter what they contain
    _git(dest, "fetch", "-q", "--depth", "1", "--no-tags", "--", url + ".git", sha,
         timeout=timeout)
    _git(dest, "-c", "core.symlinks=false", "checkout", "-q", "--detach", "FETCH_HEAD",
         timeout=timeout)
    return dest
