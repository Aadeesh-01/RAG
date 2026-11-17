"""The database layer: migrations (from the old auth DB), jobs, history, shares, privacy, accounts."""

import json
import os
import sqlite3
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
from fastapi.testclient import TestClient  # noqa: E402

from coderag import RAGCodebaseAgent, get_config  # noqa: E402
from coderag.db import Database, default_url, normalize_url  # noqa: E402
from coderag.db.models import Job, Question, Repo, Share, User, UserRepo, utcnow  # noqa: E402
from coderag.web.app import create_app  # noqa: E402
from coderag.web.auth import SESSION_COOKIE, Users  # noqa: E402
from coderag.web.limits import RateLimiter  # noqa: E402
from coderag.web.registry import Demo  # noqa: E402
from coderag.web.settings import Settings  # noqa: E402
from conftest import AUTH_SETTINGS, SAMPLE_REPO, FakeIndex, FakeLLM, HashEncoder, sign_in  # noqa: E402
from test_indexing import FakeGitHub, wait  # noqa: E402


def sample_demo():
    agent = RAGCodebaseAgent(SAMPLE_REPO, get_config("v7"), index=FakeIndex(), load_llm=False, verbose=False)
    return Demo(id="sample", name="Sample", url="https://github.com/o/r", sha="a" * 40, questions=["q?"],
                agents={"v7": agent})


def make(tmp_path, demos=None, db=None, **settings):
    github = FakeGitHub()
    app = create_app(Settings(data_dir=str(tmp_path), **AUTH_SETTINGS, **settings),
                     demos={"sample": sample_demo()} if demos is None else demos,
                     llm_factory=lambda choice: None if choice.provider == "none" else FakeLLM(
                         "normalize_name is called by DataProcessor.transform [S1]."),
                     limiter=RateLimiter(), embedder=HashEncoder(), db=db)
    app.state.indexer.fetch, app.state.indexer.resolve = github.fetch, github.resolve
    return app


def client_for(app, github_id=1, login="ada", token="gho_test"):
    client = TestClient(app)
    user = sign_in(client, app, github_id=github_id, login=login, token=token)
    return client, user


def ask(client, repo_id, question):
    """Ask over SSE; returns the done event's data (with question_id when it was stored)."""
    r = client.post(f"/api/repos/{repo_id}/ask", json={"question": question})
    assert r.status_code == 200, r.text
    events = [b for b in r.text.strip().split("\n\n")]
    done = [json.loads(e.split("data: ", 1)[1]) for e in events if e.startswith("event: done")]
    return done[0] if done else None


# ── migrations ───────────────────────────────────────────────────────────────
def legacy_layout(tmp_path):
    """data/ as coderag left it before the database: auth-only coderag.db + repos/<id>/demo.json."""
    from coderag.web.auth import _fernet
    token = _fernet(AUTH_SETTINGS["session_secret"]).encrypt(b"gho_legacy").decode()
    db = sqlite3.connect(tmp_path / "coderag.db")
    db.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, github_id INTEGER NOT NULL UNIQUE,
            login TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '',
            token_encrypted TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE user_repos (user_id INTEGER NOT NULL REFERENCES users(id), repo_id TEXT NOT NULL,
            added_at TEXT NOT NULL, PRIMARY KEY (user_id, repo_id));
    """)
    db.execute("INSERT INTO users VALUES (7, 4242, 'ada', 'Ada', 'https://a/x.png', ?, '2026-11-16T20:28:20+00:00')",
               (token,))
    db.execute("INSERT INTO user_repos VALUES (7, 'someone__pkg__aaaaaaaaaaaa', '2026-11-16T21:00:00+00:00')")
    db.execute("INSERT INTO user_repos VALUES (7, 'gone__repo__bbbbbbbbbbbb', '2026-11-16T21:00:00+00:00')")
    db.commit()
    db.close()
    folder = tmp_path / "repos" / "someone__pkg__aaaaaaaaaaaa"
    from coderag.index import VectorIndex
    agent = RAGCodebaseAgent(SAMPLE_REPO, get_config("v8"), load_llm=False, verbose=False,
                             index=VectorIndex(get_config("v8").embed_model, model=HashEncoder()))
    agent.save(str(folder / "v8"))
    (folder / "demo.json").write_text(json.dumps({
        "id": "someone__pkg__aaaaaaaaaaaa", "name": "someone/pkg", "kind": "user", "url": "https://github.com/someone/pkg",
        "sha": "a" * 40, "ref": "", "description": "Indexed from GitHub.", "stats": {"files": 3, "languages": {"Python": 3}},
        "built": "2026-11-16T21:00:00+00:00", "owner_id": 7, "private": False, "versions": ["v8"]}))


def test_migrating_the_old_auth_database_keeps_every_row(tmp_path):
    legacy_layout(tmp_path)
    db = Database(default_url(str(tmp_path)))
    db.upgrade()
    assert db.current() == "0002"
    with db.session() as s:
        [user] = s.query(User).all()
        assert (user.id, user.github_id, user.login, user.name) == (7, 4242, "ada", "Ada")
        assert user.created_at.isoformat() == "2026-11-16T20:28:20" and user.last_seen_at is None
        [repo] = s.query(Repo).filter_by(kind="user").all()
        assert repo.id == "someone__pkg__aaaaaaaaaaaa" and repo.indexed_with == "v8" and repo.owner_id == 7
        assert repo.languages == {"Python": 3} and repo.index_path.endswith("v8")
        # the link to a repo whose index is gone is dropped; the other is kept
        assert [(l.user_id, l.repo_id) for l in s.query(UserRepo)] == [(7, "someone__pkg__aaaaaaaaaaaa")]
    # the encrypted token still decrypts with the same session secret
    assert Users(db, AUTH_SETTINGS["session_secret"]).token(7) == "gho_legacy"
    db.upgrade()                                                       # idempotent
    assert db.current() == "0002"


def test_the_server_starts_on_an_old_data_folder(tmp_path):
    legacy_layout(tmp_path)
    app = make(tmp_path)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, app.state.auth.session_cookie(7))
    assert client.get("/api/auth/me").json()["login"] == "ada"
    [mine] = client.get("/api/repos/mine").json()
    assert mine["id"] == "someone__pkg__aaaaaaaaaaaa" and mine["indexed_with"] == "v8"
    assert client.get("/api/repos/someone__pkg__aaaaaaaaaaaa").status_code == 200


def test_postgres_urls_use_psycopg():
    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"
    assert Settings.from_env({"CODERAG_DATABASE_URL": "postgres://u@h/d"}).database_url == "postgres://u@h/d"


# ── jobs ─────────────────────────────────────────────────────────────────────
def test_jobs_left_running_by_a_restart_are_marked_failed(tmp_path):
    db = Database(default_url(str(tmp_path)))
    db.upgrade()
    with db.session() as s:
        for job_id, status in (("a" * 32, "running"), ("b" * 32, "queued"), ("c" * 32, "done")):
            s.add(Job(id=job_id, repo_id="x__y__" + "0" * 12, repo_name="x/y", status=status, user_id=None,
                      stage={"running": "embedding"}.get(status, status), progress=40,
                      created_at=utcnow() - timedelta(minutes=5)))
    app = make(tmp_path, db=db)
    indexer = app.state.indexer                       # /api/jobs/{id} is for a job's owner only (#25)
    running = indexer.get("a" * 32).public()
    assert running["status"] == "failed" and running["error"] == "server restarted"
    assert indexer.get("b" * 32).error == "server restarted"
    assert indexer.get("c" * 32).status == "ready"
    with db.session() as s:
        assert s.get(Job, "a" * 32).finished_at is not None


def test_job_list_is_per_user_and_survives_a_restart(tmp_path):
    app = make(tmp_path)
    ada, _ = client_for(app)
    job = wait(ada, ada.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])
    bob, _ = client_for(app, 2, "bob")
    assert bob.get("/api/jobs").json() == [] and TestClient(app).get("/api/jobs").status_code == 401
    [listed] = ada.get("/api/jobs").json()
    assert listed["job_id"] == job["job_id"] and listed["status"] == "ready" and listed["kind"] == "index"
    fresh = make(tmp_path)                                              # a restart: the row is still there
    again = TestClient(fresh)
    sign_in(again, fresh)
    [after] = again.get("/api/jobs").json()
    assert after["job_id"] == job["job_id"] and after["status"] == "ready" and after["progress"] == 1.0


# ── history ──────────────────────────────────────────────────────────────────
def test_history_is_stored_paginated_and_counted(tmp_path):
    app = make(tmp_path)
    ada, _ = client_for(app)
    ids = [ask(ada, "sample", f"Who calls normalize_name, take {i}?")["question_id"] for i in range(5)]
    assert all(ids) and ids == sorted(ids)
    first = ada.get("/api/repos/sample/history", params={"limit": 2}).json()
    assert [q["id"] for q in first["items"]] == ids[:-3:-1] and first["total"] == 5
    assert first["per_day"][-1] == 5 and len(first["per_day"]) == 7
    second = ada.get("/api/repos/sample/history", params={"limit": 2, "before": first["next"]}).json()
    third = ada.get("/api/repos/sample/history", params={"limit": 2, "before": second["next"]}).json()
    assert [q["id"] for q in second["items"] + third["items"]] == ids[2::-1] and third["next"] is None
    item = first["items"][0]
    assert item["answer"].endswith("[S1].") and item["model"] == "fake" and item["latency_ms"] >= 0
    assert item["sources"][0]["cited"] is True and "code" not in item["sources"][0]
    # the subgraph's node ids are kept, so the thumbnail can be redrawn
    assert set(item["subgraph"]) >= {s["chunk_id"] for s in item["sources"]}
    # a cached answer is recorded as cached
    assert ask(ada, "sample", "Who calls normalize_name, take 0?")["cached"] is True
    assert ada.get("/api/repos/sample/history", params={"limit": 1}).json()["items"][0]["cached"] is True


def test_history_is_private_to_its_user_and_signed_out_questions_are_not_kept(tmp_path):
    app = make(tmp_path)
    ada, _ = client_for(app)
    ask(ada, "sample", "Who calls normalize_name?")
    bob, _ = client_for(app, 2, "bob")
    assert bob.get("/api/repos/sample/history").json()["total"] == 0
    anon = TestClient(app)
    assert "question_id" not in ask(anon, "sample", "What does transform do?")
    assert anon.get("/api/repos/sample/history").status_code == 401
    with app.state.db.session() as s:
        assert s.query(Question).count() == 1


def test_feedback(tmp_path):
    app = make(tmp_path)
    ada, _ = client_for(app)
    qid = ask(ada, "sample", "Who calls normalize_name?")["question_id"]
    assert ada.post(f"/api/questions/{qid}/feedback", json={"rating": "up"}).json()["rating"] == "up"
    assert ada.post(f"/api/questions/{qid}/feedback", json={"rating": "down", "note": "missed a caller"}).status_code == 200
    assert ada.get("/api/repos/sample/history").json()["items"][0]["rating"] == "down"
    assert ada.post(f"/api/questions/{qid}/feedback", json={"rating": "meh"}).status_code == 422
    bob, _ = client_for(app, 2, "bob")
    assert bob.post(f"/api/questions/{qid}/feedback", json={"rating": "up"}).status_code == 404


# ── shares ───────────────────────────────────────────────────────────────────
def test_share_and_revoke(tmp_path):
    app = make(tmp_path)
    ada, _ = client_for(app)
    qid = ask(ada, "sample", "Who calls normalize_name?")["question_id"]
    shared = ada.post(f"/api/questions/{qid}/share").json()
    assert shared["url"] == f"/a/{shared['slug']}" and shared["private"] is False
    assert ada.post(f"/api/questions/{qid}/share").json()["slug"] == shared["slug"]       # one link per answer
    anon = TestClient(app)                                                                 # signed out
    view = anon.get(f"/api/shares/{shared['slug']}").json()
    assert view["question"] == "Who calls normalize_name?" and view["answer"].endswith("[S1].")
    assert view["repo"]["name"] == "Sample" and view["sources"][0]["cited"] and view["sources"][0]["snippet"]
    page = anon.get(f"/a/{shared['slug']}")
    assert page.status_code == 200 and "answer.js" in page.text and page.headers["x-robots-tag"] == "noindex"
    bob, _ = client_for(app, 2, "bob")
    assert bob.post(f"/api/questions/{qid}/share").status_code == 404                   # not bob's
    assert bob.delete(f"/api/questions/{qid}/share").status_code == 404
    assert ada.delete(f"/api/questions/{qid}/share").status_code == 204
    assert anon.get(f"/api/shares/{shared['slug']}").status_code == 404
    assert ada.post(f"/api/questions/{qid}/share").json()["slug"] != shared["slug"]       # a new link


# ── private repos ────────────────────────────────────────────────────────────
def test_private_repo_questions_and_shares(tmp_path):
    app = make(tmp_path, demos={})
    ada, _ = client_for(app)
    repo_id = wait(ada, ada.post("/api/repos", json={"url": "corp/secret"}).json()["job_id"])["repo_id"]
    qid = ask(ada, repo_id, "Who calls normalize_name?")["question_id"]
    bob, _ = client_for(app, 2, "bob", token="gho_bob")
    assert bob.get(f"/api/repos/{repo_id}/history").status_code == 404                  # can't see the repo
    with app.state.db.session() as s:                                                  # even if he could
        assert s.query(Question).filter_by(user_id=2).count() == 0
    r = ada.post(f"/api/questions/{qid}/share")
    assert r.status_code == 409 and "confirm" in r.json()["error"]
    slug = ada.post(f"/api/questions/{qid}/share", json={"confirm_private": True}).json()["slug"]
    view = TestClient(app).get(f"/api/shares/{slug}").json()
    assert view["repo"]["private"] is True and view["repo"]["url"] == ""
    assert view["sources"] and all(s["cited"] for s in view["sources"])                 # only what it cites
    assert all("url" not in s for s in view["sources"])
    assert all(len(s["snippet"].splitlines()) <= 40 for s in view["sources"])           # never a whole file


# ── accounts ─────────────────────────────────────────────────────────────────
def test_deleting_the_account(tmp_path):
    app = make(tmp_path)
    ada, user = client_for(app)
    repo_id = wait(ada, ada.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])["repo_id"]
    qid = ask(ada, "sample", "Who calls normalize_name?")["question_id"]
    slug = ada.post(f"/api/questions/{qid}/share").json()["slug"]
    ada.post(f"/api/questions/{qid}/feedback", json={"rating": "up"})
    assert TestClient(app).delete("/api/me").status_code == 401
    r = ada.delete("/api/me")
    assert r.status_code == 204 and SESSION_COOKIE in r.headers.get("set-cookie", "")
    with app.state.db.session() as s:
        assert s.get(User, user.id) is None
        assert s.query(Question).count() == 0 and s.query(Share).count() == 0
        assert s.query(UserRepo).count() == 0
        assert s.get(Repo, repo_id) is None                       # nobody else had it: index deleted
        assert s.get(Repo, "sample").kind == "demo"               # demos stay
    assert not (tmp_path / "repos" / repo_id).exists()
    assert TestClient(app).get(f"/api/shares/{slug}").status_code == 404
    assert ada.get("/api/auth/me").status_code == 401


# ── Postgres (skipped unless CODERAG_TEST_POSTGRES_URL is set) ───────────────
@pytest.mark.skipif(not os.environ.get("CODERAG_TEST_POSTGRES_URL"), reason="CODERAG_TEST_POSTGRES_URL not set")
def test_postgres(tmp_path):
    from alembic import command
    db = Database(os.environ["CODERAG_TEST_POSTGRES_URL"])
    command.downgrade(db.alembic_config(), "base") if db.current() else None
    db.upgrade()
    try:
        app = make(tmp_path, db=db)
        ada, _ = client_for(app)
        qid = ask(ada, "sample", "Who calls normalize_name?")["question_id"]
        assert ada.get("/api/repos/sample/history").json()["items"][0]["id"] == qid
        slug = ada.post(f"/api/questions/{qid}/share").json()["slug"]
        assert TestClient(app).get(f"/api/shares/{slug}").status_code == 200
        assert ada.delete("/api/me").status_code == 204
    finally:
        command.downgrade(db.alembic_config(), "base")
        db.dispose()
