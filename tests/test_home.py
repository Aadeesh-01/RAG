"""GET /api/home and the last-opened repo (user_repos.last_opened_at). No network."""

import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("faiss")
from fastapi.testclient import TestClient  # noqa: E402

from coderag.db.models import UserRepo  # noqa: E402
from test_db import ask, client_for, make  # noqa: E402
from test_indexing import wait  # noqa: E402


def index(client, url):
    r = client.post("/api/repos", json={"url": url})
    assert r.status_code in (200, 202), r.text
    job = wait(client, r.json()["job_id"]) if r.status_code == 202 else r.json()
    assert job["status"] == "ready", job
    return job["repo_id"]


def test_signed_out_gets_401(tmp_path):
    assert TestClient(make(tmp_path)).get("/api/home").status_code == 401


def test_a_new_user_has_an_empty_home(tmp_path):
    client, _ = client_for(make(tmp_path))
    body = client.get("/api/home").json()
    assert body == {"last_repo": None, "last_question": None, "recent_questions": [], "repos": [],
                    "stats": {"questions_week": 0, "per_day": [0] * 7, "repos": 0, "bridges": 0}}


def test_home_shows_the_last_opened_repo_and_its_last_question(tmp_path):
    app = make(tmp_path)
    client, user = client_for(app)
    first = index(client, "someone/pkg")
    second = index(client, "someone/other")
    # never opened: the one added last comes first
    assert [r["id"] for r in client.get("/api/home").json()["repos"]] == [second, first]

    assert client.get(f"/api/repos/{first}").status_code == 200          # opening it is remembered
    with app.state.db.session() as s:
        assert s.get(UserRepo, (user.id, first)).last_opened_at is not None
        assert s.get(UserRepo, (user.id, second)).last_opened_at is None
    done = ask(client, first, "Who calls normalize_name?")
    ask(client, "sample", "What does the pipeline do?")                  # a demo question counts too

    body = client.get("/api/home").json()
    assert [r["id"] for r in body["repos"]] == [first, second]
    last = body["last_repo"]
    assert last["id"] == first and last["name"] == "someone/pkg" and last["last_opened_at"]
    assert last["job"]["status"] == "ready" and "bridges" in last and "facts" not in last
    q = body["last_question"]
    assert q["id"] == done["question_id"] and q["question"] == "Who calls normalize_name?"
    assert q["answer"].startswith("normalize_name is called by") and q["cited"] == 1
    assert [x["question"] for x in body["recent_questions"]] == ["What does the pipeline do?", "Who calls normalize_name?"]
    assert body["recent_questions"][1]["repo_name"] == "someone/pkg"
    assert "answer" not in body["recent_questions"][0]                   # only the last question's answer is sent
    assert body["stats"]["questions_week"] == 2 and body["stats"]["per_day"][-1] == 2
    assert body["stats"]["repos"] == 2

    # opening the other one makes it the last
    client.get(f"/api/repos/{second}")
    body = client.get("/api/home").json()
    assert body["last_repo"]["id"] == second and body["last_question"] is None


def test_user_b_never_sees_user_a(tmp_path):
    app = make(tmp_path)
    alice, _ = client_for(app, github_id=1, login="alice")
    repo_id = index(alice, "someone/pkg")
    alice.get(f"/api/repos/{repo_id}")
    ask(alice, repo_id, "Who calls normalize_name?")
    ask(alice, "sample", "Alice's demo question")
    cached = len(app.state.cache.items)

    bob, _ = client_for(app, github_id=2, login="bob")
    bob.get(f"/api/repos/{repo_id}")                       # opening a repo that isn't in bob's list: not recorded
    body = bob.get("/api/home").json()
    assert body["repos"] == [] and body["last_repo"] is None and body["last_question"] is None
    assert body["recent_questions"] == [] and body["stats"]["questions_week"] == 0
    text = bob.get("/api/home").text
    assert "normalize_name" not in text and "Alice" not in text and repo_id not in text
    with app.state.db.session() as s:
        assert s.query(UserRepo).filter_by(repo_id=repo_id).count() == 1             # bob got no link

    # alice still sees hers; Home put nothing in the shared answer cache
    assert alice.get("/api/home").json()["last_repo"]["id"] == repo_id
    assert len(app.state.cache.items) == cached


def test_bridges_are_counted_from_the_saved_graph(tmp_path):
    app = make(tmp_path)
    client, _ = client_for(app)
    repo_id = index(client, "someone/pkg")
    row = app.state.repos._row(repo_id)
    path = os.path.join(row.index_path, "graph.json")
    import json
    with open(path, encoding="utf-8") as f:
        graph = json.load(f)
    graph["bridges"] = [{"src": 0, "dst": 1}] * 3
    with open(path, "w", encoding="utf-8") as f:
        json.dump(graph, f)
    os.utime(path, (os.path.getatime(path), os.path.getmtime(path) + 5))   # a new mtime: the cache reads it again
    app.state.repos.loaded.clear()
    body = client.get("/api/home").json()
    assert body["repos"][0]["bridges"] == 3 and body["stats"]["bridges"] == 3
