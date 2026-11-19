"""SECURITY_AUDIT.md: authorization across users, sessions, error pages, headers, forged X-Forwarded-For. No network."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("faiss")
from fastapi.testclient import TestClient  # noqa: E402

import coderag.web.auth as auth_module  # noqa: E402
from coderag.db.models import Feedback, Job, Question, Share, User, UserRepo  # noqa: E402
from coderag.web.auth import SESSION_COOKIE  # noqa: E402
from test_db import ask, client_for, make  # noqa: E402
from test_indexing import wait  # noqa: E402


def index(client, url):
    r = client.post("/api/repos", json={"url": url})
    assert r.status_code in (200, 202), r.text
    job = wait(client, r.json()["job_id"]) if r.status_code == 202 else r.json()
    assert job["status"] == "ready", job
    return job


@pytest.fixture
def world(tmp_path):
    """Alice with a private repo, a job, a question (shared) and feedback; Bob signed in; a visitor."""
    app = make(tmp_path)
    app.state.freshness.count = lambda *a, **k: None
    alice, alice_user = client_for(app, github_id=1, login="alice", token="gho_test")      # can see corp/secret
    job = index(alice, "corp/secret")
    private = job["repo_id"]
    path = alice.post(f"/api/repos/{private}/retrieve", json={"question": "x"}).json()["sources"][0]["file"]
    qid = ask(alice, private, "Who calls normalize_name?")["question_id"]
    slug = alice.post(f"/api/questions/{qid}/share", json={"confirm_private": True}).json()["slug"]
    alice.post(f"/api/questions/{qid}/feedback", json={"rating": "up"})
    bob, _ = client_for(app, github_id=2, login="bob", token="gho_bob")
    return {"app": app, "alice": alice, "alice_user": alice_user, "bob": bob, "anon": TestClient(app),
            "private": private, "path": path, "job": job["job_id"], "qid": qid, "slug": slug}


# every route that takes an id, as (method, url template, body); {r} repo, {p} file path, {j} job, {q} question
BY_ID = [
    ("GET", "/api/repos/{r}", None), ("GET", "/api/repos/{r}/graph", None), ("GET", "/api/repos/{r}/file?path={p}", None),
    ("POST", "/api/repos/{r}/retrieve", {"question": "x"}), ("POST", "/api/repos/{r}/ask", {"question": "x"}),
    ("GET", "/api/repos/{r}/history", None), ("POST", "/api/repos/{r}/live", None), ("POST", "/api/repos/{r}/reindex", None),
    ("GET", "/api/repos/{r}/freshness", None), ("POST", "/api/repos/{r}/update", None), ("POST", "/api/repos/{r}/rebuild", None),
    ("PUT", "/api/repos/mine/{r}", None), ("GET", "/api/jobs/{j}", None),
    ("POST", "/api/questions/{q}/share", {"confirm_private": True}), ("DELETE", "/api/questions/{q}/share", None),
    ("POST", "/api/questions/{q}/feedback", {"rating": "down"}),
]


def call(client, w, method, url, body):
    url = url.format(r=w["private"], p=w["path"], j=w["job"], q=w["qid"])
    return client.request(method, url, json=body)


@pytest.mark.parametrize("method, url, body", BY_ID)
def test_another_user_gets_404_for_everything_of_alices(world, method, url, body):
    r = call(world["bob"], world, method, url, body)
    assert r.status_code == 404, (method, url, r.status_code, r.text)
    assert world["private"] not in r.text or "Unknown" in r.text                 # nothing of it echoed back
    assert "normalize_name" not in r.text and world["path"] not in r.text


@pytest.mark.parametrize("method, url, body", BY_ID)
def test_a_signed_out_visitor_gets_401_or_404(world, method, url, body):
    r = call(world["anon"], world, method, url, body)
    assert r.status_code in (401, 404), (method, url, r.status_code, r.text)
    assert "normalize_name" not in r.text and world["path"] not in r.text


def test_nothing_of_alices_shows_in_bobs_lists_and_alices_data_survives(world):
    bob = world["bob"]
    assert bob.get("/api/repos/mine").json() == [] and bob.get("/api/jobs").json() == []
    home = bob.get("/api/home").json()
    assert home["repos"] == [] and home["recent_questions"] == [] and home["last_question"] is None
    # Bob "removing" Alice's repo from his list touches nothing of hers
    assert bob.delete(f"/api/repos/{world['private']}/mine").json()["index_deleted"] is False
    alice = world["alice"]
    assert alice.get(f"/api/repos/{world['private']}").status_code == 200
    assert [i["id"] for i in alice.get(f"/api/repos/{world['private']}/history").json()["items"]] == [world["qid"]]


def test_a_private_share_serves_only_the_cited_snippets(world):
    shared = world["anon"].get(f"/api/shares/{world['slug']}").json()
    assert shared["repo"]["private"] is True and shared["repo"]["url"] == ""
    assert shared["sources"]                                        # the cited one is there
    for src in shared["sources"]:
        assert src["cited"] is True and "url" not in src            # only cited sources, no links into the repo
        assert len((src.get("snippet") or "").splitlines()) <= 40
    assert world["anon"].get(f"/api/repos/{world['private']}/file", params={"path": world["path"]}).status_code == 404


# ── sessions ─────────────────────────────────────────────────────────────────
def test_tampered_and_expired_cookies_are_rejected(world, monkeypatch):
    alice = world["alice"]
    good = alice.cookies.get(SESSION_COOKIE)
    assert alice.get("/api/auth/me").status_code == 200
    forged = TestClient(world["app"])
    forged.cookies.set(SESSION_COOKIE, good[:-2] + ("AA" if not good.endswith("AA") else "BB"))
    assert forged.get("/api/auth/me").status_code == 401
    other = TestClient(world["app"])
    other.cookies.set(SESSION_COOKIE, world["app"].state.auth._states.dumps({"uid": world["alice_user"].id}))   # wrong salt
    assert other.get("/api/auth/me").status_code == 401
    monkeypatch.setattr(auth_module, "SESSION_MAX_AGE", -1)                  # every cookie is now too old
    assert alice.get("/api/auth/me").status_code == 401


def test_signing_out_ends_the_session_on_the_server(world):
    alice = world["alice"]
    copied = alice.cookies.get(SESSION_COOKIE)
    assert alice.post("/api/auth/logout").status_code == 204
    thief = TestClient(world["app"])
    thief.cookies.set(SESSION_COOKIE, copied)                                # a copy taken before signing out
    assert thief.get("/api/auth/me").status_code == 401
    assert thief.get("/api/home").status_code == 401
    # signing in again gives a working session
    fresh = TestClient(world["app"])
    fresh.cookies.set(SESSION_COOKIE, world["app"].state.auth.session_cookie(world["alice_user"].id))
    assert fresh.get("/api/auth/me").status_code == 200


def test_session_cookies_are_httponly_secure_samesite_with_an_expiry(tmp_path):
    app = make(tmp_path, public_url="https://coderag.example.com", trusted_proxy_hops=1)
    r = TestClient(app, base_url="https://coderag.example.com").get("/api/auth/github", follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert r.status_code in (302, 307)
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie and "max-age=600" in cookie
    assert "redirect_uri=https%3a%2f%2fcoderag.example.com%2fapi%2fauth%2fgithub%2fcallback" in r.headers["location"].lower()


# ── errors, headers, forged X-Forwarded-For, account deletion ───────────────
def test_an_unexpected_error_shows_no_details(tmp_path, monkeypatch):
    app = make(tmp_path)
    monkeypatch.setattr(app.state.repos, "summary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db password=hunter2 at /opt/x.py")))
    client, _ = client_for(app)
    r = TestClient(app, raise_server_exceptions=False, cookies=client.cookies).get("/api/repos/sample/history")
    assert r.status_code == 500
    for leak in ("Traceback", "hunter2", "RuntimeError", "/opt/", "File \""):
        assert leak not in r.text


def test_every_response_has_the_security_headers(tmp_path):
    app = make(tmp_path, public_url="https://coderag.example.com", trusted_proxy_hops=1)
    for path in ("/", "/app", "/api/health", "/a/nothing"):
        h = TestClient(app).get(path).headers
        assert "unsafe-eval" not in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
        assert "camera=()" in h["permissions-policy"] and "geolocation=()" in h["permissions-policy"]
        assert h["strict-transport-security"].startswith("max-age=31536000")
        assert "server" not in h and "x-powered-by" not in h
        assert "access-control-allow-origin" not in h


def test_a_forged_x_forwarded_for_cannot_dodge_the_per_ip_limit(tmp_path):
    app = make(tmp_path, retrieve_per_ip_per_hour=2, trusted_proxy_hops=1)
    client = TestClient(app)
    codes = [client.post("/api/repos/sample/retrieve", json={"question": "x"},
                         headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"}).status_code for i in range(4)]
    assert codes == [200, 200, 429, 429]                       # the proxy's entry (the last) is the client


def test_deleting_the_account_leaves_no_row_of_the_user(world):
    app, uid = world["app"], world["alice_user"].id
    assert world["alice"].delete("/api/me").status_code == 204
    with app.state.db.session() as s:
        assert s.get(User, uid) is None
        for model in (UserRepo, Question, Job):
            assert s.query(model).filter_by(user_id=uid).count() == 0, model
        assert s.query(Share).count() == 0 and s.query(Feedback).count() == 0


def test_settings_never_show_secrets_in_repr():
    from coderag.web.settings import Settings
    s = Settings(database_url="postgresql://owner:hunter2-db@ep-x/db", session_secret="s" * 40, token_key="t" * 40,
                 github_client_secret="client-secret-value")
    text = repr(s)
    for secret in ("hunter2-db", "s" * 40, "t" * 40, "client-secret-value"):
        assert secret not in text


def test_the_auth_routes_home_and_the_graph_are_rate_limited(tmp_path):
    app = make(tmp_path, auth_per_ip_per_hour=3, retrieve_per_ip_per_hour=2)
    anon = TestClient(app)
    codes = [anon.get("/api/auth/github", follow_redirects=False).status_code for _ in range(3)]
    codes += [anon.get("/api/auth/github/callback?code=x&state=y", follow_redirects=False).status_code]
    assert codes[:3] != [429] * 3 and codes[-1] == 429                     # sign-in and the OAuth callback share a limit
    assert [anon.get("/api/repos/sample/graph").status_code for _ in range(3)] == [200, 200, 429]
    ada, _ = client_for(make(tmp_path / "home", retrieve_per_ip_per_hour=2))
    assert [ada.get("/api/home").status_code for _ in range(3)] == [200, 200, 429]
