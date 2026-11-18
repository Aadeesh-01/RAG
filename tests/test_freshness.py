"""New commits, "Update to latest" and "Rebuild index" (web/freshness.py). No network."""

import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("faiss")
from fastapi.testclient import TestClient  # noqa: E402

import test_indexing  # noqa: E402
from coderag.db.models import Repo  # noqa: E402
from test_db import ask, client_for, make  # noqa: E402
from test_indexing import wait  # noqa: E402

OLD, NEW = "a" * 40, "f" * 40


def index(client, url="someone/pkg"):
    r = client.post("/api/repos", json={"url": url})
    assert r.status_code in (200, 202), r.text
    job = wait(client, r.json()["job_id"]) if r.status_code == 202 else r.json()
    assert job["status"] == "ready", job
    return job["repo_id"]


@pytest.fixture
def app(tmp_path):
    app = make(tmp_path)
    app.state.freshness.count = lambda owner, repo, base, head, token=None: 3     # no GitHub API in tests
    return app


@pytest.mark.parametrize("method, path", [("get", "freshness"), ("post", "update"), ("post", "rebuild")])
def test_signed_in_only_and_only_your_own_repos(app, method, path):
    alice, _ = client_for(app, github_id=1, login="alice")
    repo_id = index(alice)
    assert getattr(TestClient(app), method)(f"/api/repos/{repo_id}/{path}").status_code == 401
    bob, _ = client_for(app, github_id=2, login="bob")
    assert getattr(bob, method)(f"/api/repos/{repo_id}/{path}").status_code == 404          # not in bob's list
    assert getattr(bob, method)("/api/repos/sample/" + path).status_code == 404            # demos are pinned
    assert getattr(alice, method)(f"/api/repos/{repo_id}/{path}").status_code in (200, 202)


def test_freshness_reports_new_commits_and_caches_for_ten_minutes(app, monkeypatch):
    client, _ = client_for(app)
    repo_id = index(client)
    assert client.get(f"/api/repos/{repo_id}/freshness").json() == {
        "indexed_commit": OLD, "latest_commit": OLD, "behind": False, "new_commits": 0}
    monkeypatch.setitem(test_indexing.SHA, "pkg", NEW)                    # someone pushed
    assert client.get(f"/api/repos/{repo_id}/freshness").json()["behind"] is False   # cached
    now = app.state.freshness.clock()
    app.state.freshness.clock = lambda: now + 601
    assert client.get(f"/api/repos/{repo_id}/freshness").json() == {
        "indexed_commit": OLD, "latest_commit": NEW, "behind": True, "new_commits": 3}


def test_update_indexes_the_new_commit_then_moves_the_link_and_keeps_history(app, monkeypatch):
    client, user = client_for(app)
    old = index(client)
    asked = ask(client, old, "Who calls normalize_name?")
    monkeypatch.setitem(test_indexing.SHA, "pkg", NEW)

    r = client.post(f"/api/repos/{old}/update")
    assert r.status_code == 202 and r.json()["replaces"] == old
    new = r.json()["repo_id"]
    assert new != old and new.endswith("f" * 12)
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "ready"
    assert app.state.auth.users.repo_ids(user.id) == [new]                # the link moved
    assert client.get(f"/api/repos/{old}").status_code == 200              # the old index still opens
    history = client.get(f"/api/repos/{new}/history").json()               # earlier answers are still there
    assert [i["id"] for i in history["items"]] == [asked["question_id"]] and history["items"][0]["repo_id"] == old
    home = client.get("/api/home").json()
    assert home["last_repo"]["id"] == new and home["recent_questions"][0]["open_repo_id"] == new
    assert client.get(f"/api/repos/{new}/freshness").json()["behind"] is False

    # already up to date: nothing to do
    assert client.post(f"/api/repos/{new}/update").json() == {"up_to_date": True, "repo_id": new, "status": "ready"}


def test_an_update_that_fails_leaves_the_link_alone(app, monkeypatch):
    client, user = client_for(app)
    old = index(client)
    monkeypatch.setitem(test_indexing.SHA, "pkg", NEW)
    github = test_indexing.FakeGitHub()
    app.state.indexer.fetch = lambda *a, **k: (_ for _ in ()).throw(test_indexing.FetchError("network down"))
    r = client.post(f"/api/repos/{old}/update")
    assert wait(client, r.json()["job_id"])["status"] == "failed"
    assert app.state.auth.users.repo_ids(user.id) == [old]
    app.state.indexer.fetch = github.fetch


def test_rebuild_keeps_the_commit_and_version_and_is_rate_limited(tmp_path):
    app = make(tmp_path, index_per_user_per_hour=2, index_per_ip_per_hour=2)
    client, _ = client_for(app)
    repo_id = index(client)                                                # 1 of 2
    before = app.state.repos.summary(repo_id)
    r = client.post(f"/api/repos/{repo_id}/rebuild")                       # 2 of 2
    assert r.status_code == 202 and r.json()["kind"] == "rebuild" and r.json()["repo_id"] == repo_id
    assert wait(client, r.json()["job_id"])["status"] == "ready"
    after = app.state.repos.summary(repo_id)
    assert (after["sha"], after["indexed_with"]) == (before["sha"], before["indexed_with"])
    assert client.post(f"/api/repos/{repo_id}/rebuild").status_code == 429


def test_eviction_keeps_the_rows_of_repos_with_questions(tmp_path):
    app = make(tmp_path)
    client, user = client_for(app)
    asked_about = index(client)
    ask(client, asked_about, "Who calls normalize_name?")
    plain = index(client, "someone/other")
    for repo_id in (asked_about, plain):
        app.state.auth.users.remove_repo(user.id, repo_id)
    app.state.repos.max_stored = 0
    app.state.repos.evict()
    with app.state.db.session() as s:
        assert s.get(Repo, asked_about) is not None and s.get(Repo, plain) is None
    assert not os.path.isdir(app.state.repos.directory(asked_about))       # its files went, its history stays
