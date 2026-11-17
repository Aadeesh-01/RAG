"""Web: new repos indexed with CODERAG_DEFAULT_VERSION (v10), older ones offered a re-index."""

import json
import os
import shutil

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tree_sitter_language_pack")

from fastapi.testclient import TestClient  # noqa: E402

from coderag.web.settings import Settings  # noqa: E402
from conftest import ROOT, SAMPLE_REPO, repo_row, sign_in  # noqa: E402
from test_indexing import FakeGitHub, make_app, wait  # noqa: E402

BRIDGES = os.path.join(ROOT, "tests", "fixtures", "bridges")


class WebAppGitHub(FakeGitHub):
    """A repo whose browser code calls its Python API: v10 finds HTTP bridges in it."""

    def fetch(self, url, sha, dest, timeout=120, python_only=False, token=None):
        assert python_only
        self.fetched.append(url)
        shutil.copytree(os.path.join(BRIDGES, "http", "web"), os.path.join(dest, "web"))
        shutil.copytree(os.path.join(BRIDGES, "http", "server"), os.path.join(dest, "server"))
        return dest


def index(client, url="someone/pkg"):
    return wait(client, client.post("/api/repos", json={"url": url}).json()["job_id"])


def test_default_version_setting():
    assert Settings().default_version == "v10"
    assert Settings.from_env({"CODERAG_DEFAULT_VERSION": "v9"}).default_version == "v9"


def test_repos_with_bridges_are_saved_as_v10(tmp_path):
    client, app, _ = make_app(tmp_path, github=WebAppGitHub())
    job = index(client)
    assert job["status"] == "ready", job
    info = client.get(f"/api/repos/{job['repo_id']}").json()
    assert info["versions"] == ["v10"] and info["indexed_with"] == "v10"
    assert info["bridges_checked"] is True and "reindex_to" not in info
    graph = client.get(f"/api/repos/{job['repo_id']}/graph").json()
    assert any(link["type"] == "bridge" for link in graph["links"])
    assert client.get("/api/health").json()["indexing"]["default_version"] == "v10"


def test_an_index_is_saved_under_the_version_used(tmp_path):
    """Python only, no bridges: still saved as v10, the preset it was built with."""
    client, _, _ = make_app(tmp_path)
    info = client.get(f"/api/repos/{index(client)['repo_id']}").json()
    assert info["versions"] == ["v10"] and info["indexed_with"] == "v10"
    assert "reindex_to" not in info


def test_older_repos_are_offered_a_reindex(tmp_path):
    old_client, _, _ = make_app(tmp_path, github=WebAppGitHub(), default_version="v9")
    repo_id = index(old_client)["repo_id"]
    assert (tmp_path / "repos" / repo_id / "v9").is_dir()

    client, app, github = make_app(tmp_path, github=WebAppGitHub())       # the server now defaults to v10
    info = client.get(f"/api/repos/{repo_id}").json()
    assert info["indexed_with"] == "v9" and info["reindex_to"] == "v10" and info["bridges_checked"] is False

    assert TestClient(app).post(f"/api/repos/{repo_id}/reindex").status_code == 401
    assert client.post("/api/repos/sample/reindex").status_code == 404
    r = client.post(f"/api/repos/{repo_id}/reindex")
    assert r.status_code == 202
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "ready" and job["message"].startswith("Re-indexed with v10")
    assert github.fetched == ["https://github.com/someone/pkg"]          # the same commit, fetched again

    info = client.get(f"/api/repos/{repo_id}").json()
    assert info["versions"] == ["v10"] and info["indexed_with"] == "v10" and "reindex_to" not in info
    assert not (tmp_path / "repos" / repo_id / "v9").exists()               # the old index is replaced
    row = repo_row(app, repo_id)
    assert row.indexed_with == "v10" and row.index_path.endswith("v10")
    # the graph is the new index's, not a cached one
    assert any(l["type"] == "bridge" for l in client.get(f"/api/repos/{repo_id}/graph").json()["links"])


def test_indexes_from_before_indexed_with_count_by_their_version(tmp_path):
    client, app, _ = make_app(tmp_path, default_version="v8")
    repo_id = index(client)["repo_id"]
    from coderag.db.models import Repo
    with app.state.db.session() as s:                                    # recorded before indexed_with existed
        s.get(Repo, repo_id).indexed_with = ""
    fresh, _, _ = make_app(tmp_path)
    info = fresh.get(f"/api/repos/{repo_id}").json()
    assert info["indexed_with"] == "v8" and info["reindex_to"] == "v10"
    [mine] = fresh.get("/api/repos/mine").json()
    assert mine["indexed_with"] == "v8" and mine["bridges_checked"] is False


def test_frontend_offers_the_reindex_and_the_no_bridges_note():
    with open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8") as f:
        js = f.read()
    assert "Re-index with ${demo.reindex_to}" in js and "/reindex`" in js
    assert "No cross-language links found (supported: JNI, ctypes, HTTP via fetch/axios/forms)." in js
    assert "bridges_checked" in js
