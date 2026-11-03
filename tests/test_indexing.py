"""Indexing visitors' GitHub repos: API, background job, limits, storage. No network."""

import os
import shutil
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("faiss")
from fastapi.testclient import TestClient  # noqa: E402

from coderag.gitfetch import FetchError  # noqa: E402
from coderag.web.app import create_app  # noqa: E402
from coderag.web.limits import RateLimiter  # noqa: E402
from coderag.web.settings import Settings  # noqa: E402
from conftest import HashEncoder, SAMPLE_REPO  # noqa: E402

SHA = {"pkg": "a" * 40, "other": "b" * 40, "empty": "c" * 40, "big": "d" * 40}


class FakeGitHub:
    """Stands in for resolve_sha + fetch_commit(python_only=True)."""

    def __init__(self):
        self.fetched = []

    def resolve(self, owner, repo, ref=None):
        if repo not in SHA:
            raise FetchError("Repository not found, or it isn't public")
        return SHA[repo]

    def fetch(self, url, sha, dest, timeout=120, python_only=False):
        assert python_only
        self.fetched.append(url)
        os.makedirs(dest)
        if url.endswith("/empty"):
            return dest
        for root, _, files in os.walk(SAMPLE_REPO):
            for name in files:
                if name.endswith(".py"):
                    rel = os.path.relpath(os.path.join(root, name), SAMPLE_REPO)
                    os.makedirs(os.path.join(dest, os.path.dirname(rel)), exist_ok=True)
                    shutil.copy(os.path.join(root, name), os.path.join(dest, rel))
        os.makedirs(os.path.join(dest, "tests"))
        with open(os.path.join(dest, "tests", "test_skip_me.py"), "w") as f:
            f.write("def test_should_not_be_indexed():\n    assert 1 + 1 == 2 and 'skip' != 'me'\n")
        return dest


def make_app(tmp_path, github=None, **settings):
    github = github or FakeGitHub()
    app = create_app(Settings(data_dir=str(tmp_path), **settings), demos={},
                     llm_factory=lambda choice: None, limiter=RateLimiter(),
                     embedder=HashEncoder())
    app.state.indexer.fetch, app.state.indexer.resolve = github.fetch, github.resolve
    return TestClient(app), app, github


def wait(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("ready", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_index_then_ask(tmp_path):
    client, app, github = make_app(tmp_path)
    r = client.post("/api/repos", json={"url": "https://github.com/someone/pkg"})
    assert r.status_code == 202
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "ready", job
    repo_id = job["repo_id"]
    assert repo_id == "someone__pkg__" + "a" * 12

    info = client.get(f"/api/repos/{repo_id}").json()
    assert info["kind"] == "user" and info["name"] == "someone/pkg" and info["stats"]["chunks"] > 0
    assert info["description"] == "Indexed from GitHub."
    assert info["sha"] == "a" * 40 and info["versions"] == ["v8"]
    sources = client.post(f"/api/repos/{repo_id}/retrieve",
                          json={"question": "normalize names"}).json()["sources"]
    assert sources and sources[0]["url"].startswith(f"https://github.com/someone/pkg/blob/{'a' * 40}/")
    # tests/ is skipped, the clone is gone, the index is on disk
    names = [n["qname"] for n in client.get(f"/api/repos/{repo_id}/graph").json()["nodes"]]
    assert not any(q.startswith("tests/") for q in names)
    assert not os.listdir(tmp_path / ".clones")
    assert (tmp_path / "repos" / repo_id / "demo.json").exists()


def test_same_commit_is_not_indexed_twice(tmp_path):
    client, _, github = make_app(tmp_path)
    wait(client, client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])
    again = client.post("/api/repos", json={"url": "https://github.com/Someone/pkg.git"})
    assert again.status_code == 200 and again.json()["status"] == "ready"
    assert len(github.fetched) == 1


def test_bad_urls_and_missing_repos(tmp_path):
    client, _, _ = make_app(tmp_path)
    for url in ["https://gitlab.com/a/b", "not a url"]:
        r = client.post("/api/repos", json={"url": url})
        assert r.status_code == 400 and "GitHub repository" in r.json()["error"]
    r = client.post("/api/repos", json={"url": "https://github.com/a/b/tree/--x"})
    assert r.status_code == 400 and "Unsupported branch" in r.json()["error"]
    r = client.post("/api/repos", json={"url": "someone/missing"})
    assert r.status_code == 400 and "not found" in r.json()["error"]


def test_failures_are_reported(tmp_path):
    client, _, _ = make_app(tmp_path, max_py_files=3)
    empty = wait(client, client.post("/api/repos", json={"url": "x/empty"}).json()["job_id"])
    assert empty["status"] == "failed" and "No Python files" in empty["error"]
    big = wait(client, client.post("/api/repos", json={"url": "x/big"}).json()["job_id"])
    assert big["status"] == "failed" and "Too many Python files" in big["error"]
    assert not os.listdir(tmp_path / ".clones")


def test_new_repos_are_rate_limited_but_cached_ones_are_free(tmp_path):
    client, _, _ = make_app(tmp_path, index_per_ip_per_hour=1)
    wait(client, client.post("/api/repos", json={"url": "a/pkg"}).json()["job_id"])
    r = client.post("/api/repos", json={"url": "a/other"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert client.post("/api/repos", json={"url": "a/pkg"}).status_code == 200


def test_indexed_repos_survive_restart_and_lru(tmp_path):
    client, _, _ = make_app(tmp_path, max_loaded_repos=1)
    first = wait(client, client.post("/api/repos", json={"url": "a/pkg"}).json()["job_id"])
    second = wait(client, client.post("/api/repos", json={"url": "a/other"}).json()["job_id"])
    # only one stays in memory, both stay reachable
    _, app, _ = make_app(tmp_path, max_loaded_repos=1)
    fresh = TestClient(app)
    for job in (first, second):
        assert fresh.get(f"/api/repos/{job['repo_id']}").status_code == 200
    assert len(app.state.repos.loaded) == 1


def test_repo_ids_cannot_escape_the_data_dir(tmp_path):
    client, _, _ = make_app(tmp_path)
    for repo_id in ["..", "..%2F..%2Fetc", "a__b__" + "z" * 12, "A__B__" + "a" * 12]:
        assert client.get(f"/api/repos/{repo_id}").status_code == 404
