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


def fetch_commit(url, sha, dest, timeout=120, python_only=False):
    """
    Check out commit `sha` of `url` into `dest` (reused if already there).
    python_only: download only *.py files (blob-less partial fetch + sparse
    checkout), so large data or media files in the repo are never transferred.
    """
    url = url.rstrip("/").removesuffix(".git")
    if not GITHUB_URL.match(url):
        raise FetchError(f"not a https://github.com/<owner>/<repo> URL: {url!r}")
    if not SHA.match(sha):
        raise FetchError(f"not a full 40-character commit SHA: {sha!r}")

    if head_sha(dest) == sha:
        return dest
    prepare_repo(dest)
    if python_only:
        _git(dest, "remote", "add", "origin", url + ".git")
        # a partial fetch through a named remote lets checkout fetch the missing blobs
        _git(dest, "fetch", "-q", "--depth", "1", "--no-tags", "--filter=blob:none",
             "--", "origin", sha, timeout=timeout)
        _git(dest, "sparse-checkout", "set", "--no-cone", "*.py", timeout=timeout)
    else:
        # "--" ends option parsing: url and sha are positional no matter what they contain
        _git(dest, "fetch", "-q", "--depth", "1", "--no-tags", "--", url + ".git", sha,
             timeout=timeout)
    _git(dest, "-c", "core.symlinks=false", "checkout", "-q", "--detach", "FETCH_HEAD",
         timeout=timeout)
    return dest


# =============================================================================
# GitHub URLs from visitors
# =============================================================================
_REPO_INPUT = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?github\.com/)?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?/?(?:(?:tree|commit)/(?P<ref>[A-Za-z0-9._/-]{1,100}))?/?$"
)


def parse_github_url(text):
    """
    'https://github.com/o/r', 'github.com/o/r.git', 'o/r', '.../tree/<branch>'
    -> (owner, repo, ref or None). Raises FetchError for anything else.
    """
    m = _REPO_INPUT.match((text or "").strip())
    if not m or m["repo"] in (".", ".."):
        raise FetchError("Enter a GitHub repository like https://github.com/owner/repo")
    ref = m["ref"]
    if ref and (ref.startswith(("-", "/")) or ".." in ref or ref.endswith("/")):
        raise FetchError(f"Unsupported branch or tag name: {ref!r}")
    return m["owner"], m["repo"], ref


def resolve_sha(owner, repo, ref=None, timeout=30):
    """
    Commit SHA of `ref` (branch, tag or full SHA; default branch if None),
    without cloning. Raises FetchError if the repo or ref doesn't exist or
    isn't public.
    """
    if ref and SHA.match(ref):
        return ref
    url = f"https://github.com/{owner}/{repo}.git"
    try:
        result = subprocess.run(["git", "ls-remote", "--", url, *( [ref] if ref else ["HEAD"] )],
                                capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        raise FetchError("GitHub did not respond in time")
    if result.returncode != 0:
        raise FetchError("Repository not found, or it isn't public")
    refs = dict(reversed(line.split("\t")) for line in result.stdout.splitlines() if "\t" in line)
    for name in (["HEAD"] if not ref else
                 [f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"]):
        if name in refs and SHA.match(refs[name]):
            return refs[name]
    raise FetchError(f"Branch or tag {ref!r} not found" if ref else "Repository has no commits")
