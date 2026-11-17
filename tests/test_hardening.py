"""Production hardening (DEPLOY_AUDIT.md): one or more tests per fix, numbered as in the audit. No network."""

import json
import logging
import os
import shutil
import threading
import time
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("faiss")
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import conftest  # noqa: E402
from coderag.db import MIGRATION_LOCK, Database, default_url  # noqa: E402
from coderag.db.models import Job, Question, Repo, User, UserRepo, utcnow  # noqa: E402
from coderag.facts import fetch_github_facts, web_url  # noqa: E402
from coderag.gitfetch import _env, base_env  # noqa: E402
from coderag.web.app import create_app  # noqa: E402
from coderag.web.auth import GitHubOAuth, Users, _fernet  # noqa: E402
from coderag.web.jobs import Indexer, NeedsToken  # noqa: E402
from coderag.web.limits import LRU, RateLimiter  # noqa: E402
from coderag.web.middleware import DropQueryStrings, configure_logging, inline_script_hashes  # noqa: E402
from coderag.web.registry import Repos, build_demos  # noqa: E402
from coderag.web.settings import ConfigError, Settings  # noqa: E402
from conftest import AUTH_SETTINGS, ROOT, FakeLLM, HashEncoder, sign_in  # noqa: E402
from test_db import ask, client_for, make  # noqa: E402
from test_indexing import make_app, wait  # noqa: E402

FRONTEND = os.path.join(ROOT, "frontend")


def wait_open(client, repo_id, timeout=30):
    """GET /api/repos/<id> until it isn't 202 (rebuilding); returns the last response."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/repos/{repo_id}")
        if r.status_code != 202 or r.json()["job"]["status"] == "failed":
            return r
        time.sleep(0.05)
    raise AssertionError("still rebuilding")


def lose_files(app, repo_id):
    """A restart on a reset disk: the row stays, the index files and the loaded copy are gone."""
    app.state.repos.loaded.clear()
    shutil.rmtree(app.state.repos.directory(repo_id))


# ── #1 missing files are rebuilt, never stuck ───────────────────────────────
def test_opening_a_repo_whose_files_are_gone_rebuilds_it(tmp_path):
    client, app, github = make_app(tmp_path)
    job = wait(client, client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])
    repo_id = job["repo_id"]
    lose_files(app, repo_id)
    assert app.state.repos.missing(repo_id)

    r = client.get(f"/api/repos/{repo_id}")
    assert r.status_code == 202 and r.json()["rebuilding"] is True
    assert r.json()["job"]["kind"] == "rebuild" and r.json()["name"] == "someone/pkg"
    # the other repo endpoints say so too (409 with the job) while it runs
    other = client.post(f"/api/repos/{repo_id}/retrieve", json={"question": "x"})
    assert other.status_code in (200, 409)
    r = wait_open(client, repo_id)
    assert r.status_code == 200 and r.json()["sha"] == "a" * 40
    assert github.fetched[-1] == "https://github.com/someone/pkg"            # the same commit, fetched again
    assert not app.state.repos.missing(repo_id)
    assert client.post(f"/api/repos/{repo_id}/retrieve", json={"question": "normalize"}).status_code == 200


def test_resubmitting_a_repo_whose_files_are_gone_requeues_it(tmp_path):
    client, app, _ = make_app(tmp_path)
    repo_id = wait(client, client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])["repo_id"]
    lose_files(app, repo_id)
    r = client.post("/api/repos", json={"url": "someone/pkg"})
    assert r.status_code == 202 and r.json()["message"] != "Already indexed"
    assert r.json()["kind"] == "rebuild"
    assert wait(client, r.json()["job_id"])["status"] == "ready"
    assert client.get(f"/api/repos/{repo_id}").status_code == 200
    again = client.post("/api/repos", json={"url": "someone/pkg"})          # now it really is there
    assert again.status_code == 200 and again.json()["message"] == "Already indexed"


def test_a_failed_rebuild_is_reported_not_retried_in_a_loop(tmp_path):
    client, app, github = make_app(tmp_path)
    repo_id = wait(client, client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])["repo_id"]
    lose_files(app, repo_id)
    real = github.fetch
    app.state.indexer.fetch = lambda *a, **k: (_ for _ in ()).throw(OSError("network down"))
    r = wait_open(client, repo_id)
    assert r.status_code == 202 and r.json()["job"]["status"] == "failed"
    first = r.json()["job"]["job_id"]
    assert client.get(f"/api/repos/{repo_id}").json()["job"]["job_id"] == first      # no new attempt yet
    app.state.indexer.fetch = real                    # an explicit resubmit retries at once
    retry = client.post("/api/repos", json={"url": "someone/pkg"})
    assert retry.status_code == 202 and wait(client, retry.json()["job_id"])["status"] == "ready"


def test_a_private_repo_rebuild_needs_a_token(tmp_path):
    client, app, _ = make_app(tmp_path)
    repo_id = wait(client, client.post("/api/repos", json={"url": "someone/secret"}).json()["job_id"])["repo_id"]
    lose_files(app, repo_id)
    with pytest.raises(NeedsToken):
        app.state.indexer.rebuild(repo_id, token=None)
    r = wait_open(client, repo_id)                    # the user who has it: rebuilt with their token
    assert r.status_code == 200 and r.json()["private"] is True


# ── #4 demos dir ─────────────────────────────────────────────────────────────
def test_demos_come_from_their_own_directory(tmp_path):
    assert Settings(data_dir="d").demos_path == os.path.join("d", "demos")
    assert Settings(data_dir="d", demos_dir="baked").demos_path == "baked"
    spec = tmp_path / "demos.yaml"
    spec.write_text("versions: [v7]\ndemos:\n  - {id: sample, name: Sample, repo: sample_repo}\n")
    baked, data = tmp_path / "image" / "demos", tmp_path / "volume"
    build_demos(str(spec), str(tmp_path / "unused"), embedder=HashEncoder(), log=lambda *_: None,
                demos_dir=str(baked))
    app = create_app(Settings(data_dir=str(data), demos_dir=str(baked)), llm_factory=lambda c: None,
                     embedder=HashEncoder())
    client = TestClient(app)
    assert [d["id"] for d in client.get("/api/demos").json()] == ["sample"]
    assert client.get("/api/ready").json() == {"ready": True, "checks": {"database": "ok", "demos": "ok"},
                                               "demos": ["sample"]}
    assert not (data / "demos").exists() and (data / "coderag.db").exists()


# ── #13 eviction by last use, unlinked only, with a size cap ───────────────
def stored(repos, repo_id, used_minutes_ago, mb, linked_to=None):
    os.makedirs(repos.directory(repo_id), exist_ok=True)
    with open(os.path.join(repos.directory(repo_id), "blob"), "wb") as f:
        f.write(b"x" * int(mb * 1024 * 1024))
    with repos.db.session() as s:
        s.add(Repo(id=repo_id, kind="user", name=repo_id, created_at=utcnow() - timedelta(days=30),
                   last_used_at=utcnow() - timedelta(minutes=used_minutes_ago)))
        if linked_to:
            s.add(UserRepo(user_id=linked_to, repo_id=repo_id, added_at=utcnow()))


def test_eviction_takes_the_least_recently_used_unlinked_repos(tmp_path):
    db = Database(default_url(str(tmp_path)))
    db.upgrade()
    with db.session() as s:
        s.add(User(id=1, github_id=1, login="ada", created_at=utcnow()))
    repos = Repos(str(tmp_path), {}, HashEncoder(), max_stored=2, db=db, max_stored_mb=0)
    ids = [f"o__r{i}__{str(i) * 12}" for i in range(4)]
    stored(repos, ids[0], used_minutes_ago=500, mb=0.1, linked_to=1)      # oldest, but in a list: kept
    stored(repos, ids[1], used_minutes_ago=400, mb=0.1)
    stored(repos, ids[2], used_minutes_ago=1, mb=0.1)                     # used just now
    stored(repos, ids[3], used_minutes_ago=300, mb=0.1)
    assert repos.evict() == [ids[1], ids[3]]
    assert repos.exists(ids[0]) and repos.exists(ids[2]) and not os.path.isdir(repos.directory(ids[1]))


def test_eviction_keeps_the_disk_under_max_stored_mb(tmp_path):
    db = Database(default_url(str(tmp_path)))
    db.upgrade()
    repos = Repos(str(tmp_path), {}, HashEncoder(), max_stored=100, db=db, max_stored_mb=1)
    stored(repos, "o__a__aaaaaaaaaaaa", used_minutes_ago=90, mb=0.6)
    stored(repos, "o__b__bbbbbbbbbbbb", used_minutes_ago=10, mb=0.6)
    assert repos.evict() == ["o__a__aaaaaaaaaaaa"]                        # 1.2 MB > 1 MB: the older goes
    assert repos.evict() == []


def test_opening_a_repo_records_its_last_use(tmp_path):
    client, app, _ = make_app(tmp_path)
    repo_id = wait(client, client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])["repo_id"]
    with app.state.db.session() as s:
        s.get(Repo, repo_id).last_used_at = utcnow() - timedelta(days=3)
        assert s.get(Repo, repo_id).disk_bytes > 0
    app.state.repos._touched.clear()
    client.get(f"/api/repos/{repo_id}")
    with app.state.db.session() as s:
        assert utcnow() - s.get(Repo, repo_id).last_used_at < timedelta(minutes=1)


# ── #14 / #16 recovery ───────────────────────────────────────────────────────
def test_recover_clears_old_clones_and_only_touches_jobs_from_before_the_start(tmp_path):
    db = Database(default_url(str(tmp_path)))
    db.upgrade()
    old_clone = tmp_path / ".clones" / ("d" * 32)
    os.makedirs(old_clone / "pkg")
    (old_clone / "pkg" / "x.py").write_text("x = 1\n")
    os.utime(old_clone, (time.time() - 3600, time.time() - 3600))
    repos = Repos(str(tmp_path), {}, HashEncoder(), db=db)
    indexer = Indexer(Settings(data_dir=str(tmp_path)), repos, HashEncoder(), db=db)
    with db.session() as s:
        s.add(Job(id="o" * 32, repo_id="x__y__" + "0" * 12, status="running", stage="embedding",
                  created_at=indexer.started - timedelta(minutes=1)))
        s.add(Job(id="n" * 32, repo_id="x__z__" + "0" * 12, status="running", stage="embedding",
                  created_at=indexer.started + timedelta(seconds=1)))       # another process's, after our start
    assert indexer.recover() == 1
    with db.session() as s:
        assert s.get(Job, "o" * 32).status == "failed" and s.get(Job, "n" * 32).status == "running"
    assert not old_clone.exists()


def test_migrations_take_an_advisory_lock_on_postgres(tmp_path):
    from sqlalchemy import event
    db = Database(default_url(str(tmp_path)))
    calls = []

    @event.listens_for(db.engine, "connect")
    def fake_pg_lock(connection, _):                  # SQLite stands in: the lock is a recorded function
        connection.create_function("pg_advisory_xact_lock", 1, lambda key: calls.append(key) or 1)
    db.sqlite = False
    db.upgrade()
    assert calls == [MIGRATION_LOCK] and db.current() == "0002"


# ── #5 / #10 fail fast ───────────────────────────────────────────────────────
@pytest.mark.parametrize("settings, message", [
    ({"github_client_id": "x"}, "CODERAG_GITHUB_CLIENT_SECRET is missing"),
    ({**AUTH_SETTINGS, "session_secret": "short"}, "CODERAG_SESSION_SECRET must be at least 32"),
    ({**AUTH_SETTINGS, "token_key": ""}, "CODERAG_TOKEN_KEY must be at least 32"),
    ({**AUTH_SETTINGS, "token_key": AUTH_SETTINGS["session_secret"]}, "must differ"),
    ({"public_url": "http://coderag.example.com"}, "must be https://"),
    ({"public_url": "https://coderag.example.com"}, "CODERAG_TRUSTED_PROXY_HOPS is 0"),
    ({"public_url": "ftp://x"}, "is not a URL"),
])
def test_unsafe_settings_stop_the_start(tmp_path, settings, message):
    with pytest.raises(ConfigError, match=message):
        Settings(**settings).validate()
    with pytest.raises(ConfigError, match=message):
        create_app(Settings(data_dir=str(tmp_path), **settings), demos={}, llm_factory=lambda c: None,
                   embedder=HashEncoder())


def test_safe_settings_start(caplog):
    assert Settings().validate() == []
    assert Settings(public_url="http://127.0.0.1:7860", **AUTH_SETTINGS).validate() == []
    assert Settings(public_url="http://localhost:7860").validate() == []
    assert Settings(public_url="https://c.example.com", trusted_proxy_hops=1, **AUTH_SETTINGS).validate() == []
    [warning] = Settings(public_url="https://c.example.com", allow_no_proxy=True).validate()
    assert "CODERAG_ALLOW_NO_PROXY" in warning
    assert Settings.from_env({"CODERAG_ALLOW_NO_PROXY": "1"}).allow_no_proxy is True


# ── #21 token key ────────────────────────────────────────────────────────────
def test_tokens_from_before_the_token_key_are_read_then_re_encrypted(tmp_path):
    db = Database(default_url(str(tmp_path)))
    db.upgrade()
    legacy = _fernet(AUTH_SETTINGS["session_secret"]).encrypt(b"gho_old").decode()
    with db.session() as s:
        s.add(User(id=5, github_id=5, login="ada", token_encrypted=legacy, created_at=utcnow()))
    users = Users(db, AUTH_SETTINGS["session_secret"], token_key=AUTH_SETTINGS["token_key"])
    assert users.token(5) == "gho_old"
    with db.session() as s:
        stored_now = s.get(User, 5).token_encrypted
    assert stored_now != legacy and "gho_old" not in stored_now
    assert users.fernet.decrypt(stored_now.encode()) == b"gho_old"          # the token key now
    assert users.token(5) == "gho_old"
    # new sign-ins use the token key only: SESSION_SECRET alone can't read them
    user = users.upsert({"id": 6, "login": "bob"}, "gho_new")
    with db.session() as s:
        fresh = s.get(User, user.id).token_encrypted
    assert Users(db, AUTH_SETTINGS["session_secret"]).token(user.id) is None
    assert users.fernet.decrypt(fresh.encode()) == b"gho_new"


# ── #15 logging ──────────────────────────────────────────────────────────────
def test_access_log_drops_query_strings():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", "/api/auth/github/callback?code=SECRET&state=x", "1.1", 303),
                               None)
    assert DropQueryStrings().filter(record)
    assert "SECRET" not in record.getMessage() and "/api/auth/github/callback " in record.getMessage()
    configure_logging()
    configure_logging()
    filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, DropQueryStrings)]
    assert len(filters) == 1 and logging.getLogger("coderag").level == logging.INFO


# ── #24 daily cap from the database ─────────────────────────────────────────
def test_daily_cap_counts_stored_answers_and_survives_a_restart(tmp_path):
    app = make(tmp_path, server_daily_cap=2, answer_cache_size=0)
    client, _ = client_for(app)
    assert ask(client, "sample", "first?")["question_id"]
    assert ask(client, "sample", "second?")["question_id"]
    with app.state.db.session() as s:
        assert s.query(Question).filter_by(server_key=True).count() == 2
    restarted = make(tmp_path, server_daily_cap=2, answer_cache_size=0)      # the same database
    client, _ = client_for(restarted)
    r = client.post("/api/repos/sample/ask", json={"question": "third?"})
    assert "server_quota" in r.text
    # own keys don't count, and aren't limited by it
    assert restarted.state.usage.used() == 2


def test_signed_out_answers_count_in_memory(tmp_path):
    app = make(tmp_path, server_daily_cap=1, answer_cache_size=0)
    client = TestClient(app)
    assert "event: done" in client.post("/api/repos/sample/ask", json={"question": "a?"}).text
    assert app.state.usage.used() == 1
    assert "server_quota" in client.post("/api/repos/sample/ask", json={"question": "b?"}).text
    with app.state.db.session() as s:
        assert s.query(Question).count() == 0                               # still never stored


# ── #28 docs, #29 health and readiness ──────────────────────────────────────
def test_api_docs_are_off_unless_asked_for(tmp_path):
    client = TestClient(make(tmp_path))
    assert [client.get(p).status_code for p in ("/docs", "/redoc", "/openapi.json")] == [404, 404, 404]
    client = TestClient(make(tmp_path / "on", docs=True))
    assert [client.get(p).status_code for p in ("/docs", "/redoc", "/openapi.json")] == [200, 200, 200]


def test_health_is_async_and_ready_checks_the_database(tmp_path):
    import inspect
    app = make(tmp_path)
    [health] = [r for r in app.routes if getattr(r, "path", "") == "/api/health"]
    assert inspect.iscoroutinefunction(health.endpoint)
    client = TestClient(app)
    assert client.get("/api/ready").json()["ready"] is True

    def down():
        raise OSError("connection refused")
    app.state.db.ping = down
    r = client.get("/api/ready")
    assert r.status_code == 503 and r.json()["checks"]["database"].startswith("unavailable")
    assert client.get("/api/health").status_code == 200


# ── #6 security headers ─────────────────────────────────────────────────────
def test_security_headers_with_csp_hashes_from_the_html(tmp_path):
    hashes = inline_script_hashes(FRONTEND)
    assert len(hashes) >= 2 and all(h.startswith("'sha256-") for h in hashes)
    client = TestClient(make(tmp_path))
    for path in ("/app", "/api/health", "/"):
        h = client.get(path).headers
        csp = h["content-security-policy"]
        assert all(x in csp for x in hashes) and "frame-ancestors 'none'" in csp
        assert "script-src 'self' https://cdn.jsdelivr.net" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
        assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "strict-origin-when-cross-origin"
        assert h["x-frame-options"] == "DENY" and "strict-transport-security" not in h
    https = TestClient(make(tmp_path / "https", public_url="https://c.example.com", trusted_proxy_hops=1))
    assert https.get("/app").headers["strict-transport-security"].startswith("max-age=31536000")


def test_csp_hashes_follow_the_html(tmp_path):
    (tmp_path / "a.html").write_text("<script>one()</script><script src=x></script>", encoding="utf-8")
    (tmp_path / "b.html").write_text("<script>two()</script>", encoding="utf-8")
    import base64
    import hashlib
    want = sorted(f"'sha256-{base64.b64encode(hashlib.sha256(s).digest()).decode()}'" for s in (b"one()", b"two()"))
    assert inline_script_hashes(str(tmp_path)) == want


# ── #9 links only http(s) ───────────────────────────────────────────────────
def test_repo_facts_keep_only_http_links():
    assert web_url("https://ok.example.com/x") == "https://ok.example.com/x"
    assert web_url("http://ok.example.com") == "http://ok.example.com"
    for bad in ("javascript:alert(1)", " JaVaScRiPt:alert(1)", "data:text/html,x", "//no-scheme", "", None, 5,
                "https://", "vbscript:x"):
        assert web_url(bad) == "", bad

    def api(request):
        path = request.url.path
        if path == "/repos/o/r":
            return httpx.Response(200, json={"homepage": "javascript:alert(document.cookie)", "has_pages": False})
        if path == "/repos/o/r/deployments":
            return httpx.Response(200, json=[{"environment": "prod", "statuses_url": "https://evil.example/s"}])
        assert request.url.host == "api.github.com", request.url               # the token goes to GitHub only
        return httpx.Response(404)
    facts = fetch_github_facts("o", "r", token="gho_x", transport=httpx.MockTransport(api))
    assert facts["homepage"] == "" and facts["deployments"][0]["environment_url"] == ""


# ── #11 body limit ───────────────────────────────────────────────────────────
def test_request_bodies_over_64_kb_are_refused(tmp_path):
    client = TestClient(make(tmp_path))
    big = json.dumps({"question": "x" * (70 * 1024)})
    r = client.post("/api/repos/sample/retrieve", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and "too large" in r.json()["error"]

    def chunks():                                     # no Content-Length: counted as it arrives
        for _ in range(80):
            yield b"x" * 1024
    r = client.post("/api/repos/sample/retrieve", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert client.post("/api/repos/sample/retrieve", json={"question": "normalize"}).status_code == 200


# ── #25 jobs for their owner only ───────────────────────────────────────────
def test_a_job_is_visible_to_its_owner_only(tmp_path):
    client, app, _ = make_app(tmp_path)
    job_id = client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"]
    assert wait(client, job_id)["status"] == "ready"
    assert TestClient(app).get(f"/api/jobs/{job_id}").status_code == 404                # signed out
    bob = TestClient(app)
    sign_in(bob, app, github_id=2, login="bob", token="gho_bob")
    assert bob.get(f"/api/jobs/{job_id}").status_code == 404


# ── #27 origin check ────────────────────────────────────────────────────────
def test_cross_site_state_changes_are_refused(tmp_path):
    app = make(tmp_path, public_url="https://c.example.com", trusted_proxy_hops=1)
    client, _ = client_for(app)
    evil = client.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403 and evil.json()["error"] == "Cross-site request refused"
    assert client.post("/api/auth/logout", headers={"Origin": "null"}).status_code == 403
    assert client.delete("/api/me", headers={"Referer": "https://evil.example/page"}).status_code == 403
    assert client.post("/api/auth/logout", headers={"Origin": "https://c.example.com"}).status_code == 204
    assert client.post("/api/auth/logout", headers={"Referer": "https://c.example.com/app"}).status_code == 204
    assert client.post("/api/auth/logout").status_code == 204                 # curl: no Origin, no Referer
    assert client.get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 200   # reads pass
    # without PUBLIC_URL there is nothing to compare with
    local, _ = client_for(make(tmp_path / "local"))
    assert local.post("/api/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 204


# ── #31 git's environment ────────────────────────────────────────────────────
def test_git_gets_a_minimal_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_must_not_leak")
    monkeypatch.setenv("CODERAG_SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("CODERAG_DATABASE_URL", "postgresql://u:pw@h/db")
    env = base_env()
    assert "PATH" in env and env["GIT_TERMINAL_PROMPT"] == "0"
    assert not {"GROQ_API_KEY", "CODERAG_SESSION_SECRET", "CODERAG_DATABASE_URL"} & set(env)
    with_token = _env(str(tmp_path / "dest"), token="gho_t")
    assert "gsk_must_not_leak" not in json.dumps(with_token) and with_token["GIT_CONFIG_COUNT"] == "1"
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs["env"])
        raise __import__("subprocess").TimeoutExpired(cmd, 1)
    monkeypatch.setattr("coderag.gitfetch.subprocess.run", fake_run)
    from coderag.gitfetch import FetchError, resolve_sha
    with pytest.raises(FetchError):
        resolve_sha("o", "r")
    assert seen and "GROQ_API_KEY" not in seen and "CODERAG_SESSION_SECRET" not in seen


# ── #34 DOMPurify ────────────────────────────────────────────────────────────
def test_dompurify_is_at_least_3_2_4_with_sri():
    import re
    for page in ("app.html", "answer.html"):
        html = open(os.path.join(FRONTEND, page), encoding="utf-8").read()
        m = re.search(r'dompurify@(\d+)\.(\d+)\.(\d+)/dist/purify\.min\.js"\s+integrity="(sha384-[^"]+)"', html)
        assert m, page
        assert tuple(map(int, m.groups()[:3])) >= (3, 2, 4) and len(m.group(4)) == 71


# ── #7 shares serve the snippets stored with the question ───────────────────
def test_shared_snippets_are_the_ones_stored_when_answered(tmp_path):
    app = make(tmp_path)
    client, _ = client_for(app)
    done = ask(client, "sample", "Who calls normalize_name?")
    with app.state.db.session() as s:
        row = s.get(Question, done["question_id"])
        assert row.version == "v7"
        [cited] = [i for i in row.sources["items"] if i["cited"]]
        assert cited["snippet"] and cited["snippet"].count("\n") < 40
    stored_snippet = cited["snippet"]
    # a re-index renumbers chunks: the share must not read the index again
    agent = app.state.repos.demos["sample"].agents["v7"]
    agent.chunks[:] = ["SOMETHING ELSE ENTIRELY"] * len(agent.chunks)
    slug = client.post(f"/api/questions/{done['question_id']}/share").json()["slug"]
    [shown] = [s for s in TestClient(app).get(f"/api/shares/{slug}").json()["sources"] if s.get("snippet")]
    assert shown["snippet"] == stored_snippet and "SOMETHING ELSE" not in shown["snippet"]


# ── #22 account deletion ─────────────────────────────────────────────────────
def test_deleting_the_account_removes_jobs_cache_and_revokes_the_grant(tmp_path, monkeypatch):
    revoked = []
    monkeypatch.setattr(GitHubOAuth, "revoke", lambda self, token: revoked.append(token))
    client, app, _ = make_app(tmp_path)
    repo_id = wait(client, client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])["repo_id"]
    app.state.cache.put((repo_id, "v10", "q", (), "server", "", False), [{"type": "done"}])
    app.state.cache.put(("sample", "v7", "q", (), "server", "", False), [{"type": "done"}])
    assert client.delete("/api/me").status_code == 204
    assert revoked == ["gho_test"]
    with app.state.db.session() as s:
        assert s.query(Job).count() == 0 and s.query(User).count() == 0
    assert [k[0] for k in app.state.cache.items] == ["sample"]


def test_grant_revocation_calls_github_and_failures_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(GitHubOAuth, "revoke", conftest.REAL_REVOKE)
    seen = []

    def github(request):
        seen.append((request.method, request.url.path, request.headers["authorization"][:6],
                     json.loads(request.content)))
        return httpx.Response(204)
    oauth = GitHubOAuth("cid", "csecret", transport=httpx.MockTransport(github))
    oauth.revoke("gho_x")
    assert seen == [("DELETE", "/applications/cid/grant", "Basic ", {"access_token": "gho_x"})]
    # a GitHub outage never blocks deleting the account
    down = GitHubOAuth("cid", "csecret", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    app = make(tmp_path)
    app.state.auth.github = down
    client, _ = client_for(app)
    assert client.delete("/api/me").status_code == 204


# ── #12 graph cache ──────────────────────────────────────────────────────────
def test_graph_cache_is_a_bounded_lru(tmp_path):
    lru = LRU(2)
    made = []
    for key in ("a", "b", "a", "c", "b"):
        lru.get(key, lambda k=key: made.append(k) or k.upper())
    assert made == ["a", "b", "c", "b"] and list(lru.items) == ["c", "b"]
    app = make(tmp_path, max_loaded_repos=3)
    assert app.state.graphs.size == 3 + 1                    # max_loaded_repos + the demos
    client = TestClient(app)
    assert client.get("/api/repos/sample/graph").json()["nodes"]
    assert len(app.state.graphs) == 1


# ── #17 answer streams: concurrency and total time ──────────────────────────
def test_too_many_concurrent_answers_get_503(tmp_path):
    app = make(tmp_path, max_concurrent_asks=1, answer_cache_size=0)
    client = TestClient(app)
    hold = app.state.asking.try_enter()
    r = client.post("/api/repos/sample/ask", json={"question": "x"})
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    hold()
    for q in ("a", "b", "c"):                                # each finished stream frees its slot
        assert client.post("/api/repos/sample/ask", json={"question": q}).status_code == 200


def test_answer_streams_stop_at_the_time_cap(tmp_path):
    class SlowLLM(FakeLLM):
        def stream(self, context, question, config=None):
            for word in ["slow"] * 50:
                time.sleep(0.02)
                yield word + " "
    app = create_app(Settings(data_dir=str(tmp_path), ask_timeout=0, answer_cache_size=0),
                     demos={"sample": __import__("test_db").sample_demo()},
                     llm_factory=lambda c: SlowLLM(), embedder=HashEncoder())
    text = TestClient(app).post("/api/repos/sample/ask", json={"question": "x"}).text
    kinds = [line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("event: ")]
    assert kinds == ["sources", "error"] and '"code": "timeout"' in text


# ── #18 jobs: deadline, one at a time per user, per-user limits ─────────────
def test_jobs_past_the_deadline_fail(tmp_path):
    client, app, _ = make_app(tmp_path, index_timeout=0)
    job = wait(client, client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])
    assert job["status"] == "failed" and "longer than" in job["error"]


def test_one_active_job_per_user(tmp_path):
    client, app, github = make_app(tmp_path)
    gate = threading.Event()
    fetch = github.fetch

    def slow_fetch(*args, **kwargs):
        gate.wait(10)
        return fetch(*args, **kwargs)
    app.state.indexer.fetch = slow_fetch
    first = client.post("/api/repos", json={"url": "someone/pkg"})
    assert first.status_code == 202
    second = client.post("/api/repos", json={"url": "someone/other"})
    assert second.status_code == 429 and "still being indexed" in second.json()["error"]
    assert client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"] == first.json()["job_id"]
    gate.set()
    assert wait(client, first.json()["job_id"])["status"] == "ready"
    assert client.post("/api/repos", json={"url": "someone/other"}).status_code == 202


def test_per_user_hourly_limit_alongside_the_ip_one(tmp_path):
    client, app, _ = make_app(tmp_path, index_per_ip_per_hour=100, index_per_user_per_hour=1)
    assert wait(client, client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])["status"] == "ready"
    r = client.post("/api/repos", json={"url": "someone/other"})
    assert r.status_code == 429 and "Retry-After" in r.headers
    bob = TestClient(app)                                      # same IP, another user: their own budget
    sign_in(bob, app, github_id=2, login="bob", token="gho_bob")
    assert bob.post("/api/repos", json={"url": "someone/other"}).status_code == 202


# ── #30 rate-limit buckets are swept ────────────────────────────────────────
def test_expired_rate_limit_buckets_are_swept():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    limiter.SWEEP_EVERY = 10
    for i in range(9):
        limiter.hit(f"ask:10.0.0.{i}", 5, 60)
    assert len(limiter.hits) == 9
    now[0] = 61
    limiter.hit("ask:10.0.0.99", 5, 60)                         # the 10th call sweeps
    assert set(limiter.hits) == {"ask:10.0.0.99"}
    assert limiter.hit("ask:10.0.0.1", 1, 60) == (True, 0.0)
