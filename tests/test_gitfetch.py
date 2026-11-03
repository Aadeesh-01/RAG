"""gitfetch must never touch a repository above its destination."""

import os
import shutil
import subprocess

import pytest

from coderag.gitfetch import FetchError, fetch_commit, head_sha, prepare_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def parent_repo(tmp_path):
    """A repository with one commit, standing in for the project's own repo."""
    root = tmp_path / "project"
    root.mkdir()
    git("init", "-q", cwd=root)
    (root / "file.txt").write_text("mine\n")
    git("add", ".", cwd=root)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=root)
    return root


def test_head_sha_ignores_parent_repo(parent_repo):
    dest = parent_repo / "data" / "clones" / "x"
    dest.mkdir(parents=True)
    assert head_sha(str(dest)) is None
    (dest / ".git").mkdir()                     # broken leftover: only a .git folder
    assert head_sha(str(dest)) is None


def test_broken_clone_is_reinitialised_not_resolved_upwards(parent_repo):
    dest = parent_repo / "data" / "clones" / "x"
    (dest / ".git" / "objects").mkdir(parents=True)   # what a failed rmtree left behind
    parent_head = git("rev-parse", "HEAD", cwd=parent_repo).stdout

    prepare_repo(str(dest))

    own = git("rev-parse", "--absolute-git-dir", cwd=dest).stdout.strip()
    assert os.path.samefile(own, dest / ".git")
    assert git("rev-parse", "HEAD", cwd=parent_repo).stdout == parent_head
    assert not (parent_repo / ".git" / "shallow").exists()


@pytest.mark.parametrize("url,sha", [
    ("https://github.com/psf/requests", "--upload-pack=touch /tmp/x"),
    ("https://github.com.evil.com/a/b", "a" * 40),
    ("http://github.com/a/b", "a" * 40),
    ("https://github.com/a/b", "HEAD"),
])
def test_rejects_unsafe_inputs(tmp_path, url, sha):
    with pytest.raises(FetchError):
        fetch_commit(url, sha, str(tmp_path / "x"))
    assert not (tmp_path / "x").exists()
