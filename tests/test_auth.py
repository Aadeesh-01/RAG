"""GitHub sign-in (coderag/web/auth.py) with GitHub mocked: no network."""

import logging
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("itsdangerous")
pytest.importorskip("cryptography")
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import URLSafeTimedSerializer  # noqa: E402

from coderag.web.app import create_app  # noqa: E402
from coderag.web.auth import SESSION_COOKIE, STATE_COOKIE, GitHubError, GitHubOAuth  # noqa: E402
from coderag.web.limits import RateLimiter  # noqa: E402
from coderag.web.settings import Settings  # noqa: E402
from conftest import AUTH_SETTINGS, HashEncoder  # noqa: E402

TOKEN = "gho_" + "s3cr3tT0k3n" * 3
ADA = {"id": 42, "login": "ada", "name": "Ada Lovelace",
       "avatar_url": "https://avatars.githubusercontent.com/u/42?v=4"}


class FakeGitHub(GitHubOAuth):
    """The real authorize URL; the code exchange and /user are canned."""

    def __init__(self, user=None):
        super().__init__(AUTH_SETTINGS["github_client_id"], AUTH_SETTINGS["github_client_secret"])
        self.profile = dict(user or ADA)
        self.exchanged = []

    def exchange_code(self, code, redirect_uri):
        self.exchanged.append((code, redirect_uri))
        if code != "good-code":
            raise GitHubError("token exchange failed: bad_verification_code")
        return TOKEN

    def user(self, token):
        assert token == TOKEN
        return dict(self.profile)


def make(tmp_path, github=None, **settings):
    values = {**AUTH_SETTINGS, **settings}
    github = github or FakeGitHub()
    app = create_app(Settings(data_dir=str(tmp_path), **values), demos={},
                     llm_factory=lambda choice: None, limiter=RateLimiter(),
                     embedder=HashEncoder(), github=github)
    return TestClient(app), app, github


def start(client, **params):
    """GET /api/auth/github; returns the GitHub query parameters."""
    r = client.get("/api/auth/github", params=params, follow_redirects=False)
    assert r.status_code == 302
    url = urlparse(r.headers["location"])
    assert (url.scheme, url.netloc, url.path) == ("https", "github.com", "/login/oauth/authorize")
    return {k: v[0] for k, v in parse_qs(url.query).items()}


def callback(client, **params):
    return client.get("/api/auth/github/callback", params=params, follow_redirects=False)


def set_cookies(response):
    return "\n".join(response.headers.get_list("set-cookie"))


def sign_in_via_github(client):
    query = start(client)
    return callback(client, code="good-code", state=query["state"])


def users(tmp_path):
    with sqlite3.connect(tmp_path / "coderag.db") as db:
        return db.execute("SELECT github_id, login, name, avatar_url, token_encrypted FROM users").fetchall()


def test_start_sends_to_github_with_a_signed_state_cookie(tmp_path):
    client, _, _ = make(tmp_path)
    r = client.get("/api/auth/github", follow_redirects=False)
    query = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    assert query["client_id"] == "test-client-id" and query["scope"] == "read:user repo"
    assert query["redirect_uri"] == "http://testserver/api/auth/github/callback"
    assert len(query["state"]) >= 32
    cookie = set_cookies(r)
    assert STATE_COOKIE in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    # each attempt gets its own state
    assert start(client)["state"] != query["state"]


def test_public_url_sets_the_callback_and_secure_cookies(tmp_path):
    client, _, _ = make(tmp_path, public_url="https://coderag.example.com/")
    r = client.get("/api/auth/github", follow_redirects=False)
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["redirect_uri"] == ["https://coderag.example.com/api/auth/github/callback"]
    assert "Secure" in set_cookies(r)


def test_state_mismatch_is_rejected(tmp_path):
    client, _, github = make(tmp_path)
    start(client)
    r = callback(client, code="good-code", state="not-the-state")
    assert r.status_code == 303 and r.headers["location"] == "/login?error=state"
    assert SESSION_COOKIE not in set_cookies(r)
    assert github.exchanged == [] and not (tmp_path / "coderag.db").exists()
    assert client.get("/api/auth/me").status_code == 401


def test_state_without_its_cookie_or_with_a_forged_one_is_rejected(tmp_path):
    client, _, github = make(tmp_path)
    state = start(client)["state"]
    fresh = TestClient(client.app)                          # the right state, but no cookie
    assert callback(fresh, code="good-code", state=state).headers["location"] == "/login?error=state"
    forged = URLSafeTimedSerializer("wrong-secret", salt="coderag.oauth-state").dumps(
        {"state": "s" * 43, "next": "/app"})
    fresh.cookies.set(STATE_COOKIE, forged)
    assert callback(fresh, code="good-code", state="s" * 43).headers["location"] == "/login?error=state"
    # a state is good for one attempt only
    assert sign_in_via_github(client).status_code == 303
    assert callback(client, code="good-code", state=state).headers["location"] == "/login?error=state"
    assert len(github.exchanged) == 1


def test_callback_creates_a_user_and_a_session(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    client, app, github = make(tmp_path)
    r = sign_in_via_github(client)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert github.exchanged == [("good-code", "http://testserver/api/auth/github/callback")]
    cookie = set_cookies(r)
    assert SESSION_COOKIE in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()

    [(github_id, login, name, avatar, token_encrypted)] = users(tmp_path)
    assert (github_id, login, name, avatar) == (42, "ada", "Ada Lovelace", ADA["avatar_url"])
    assert token_encrypted and TOKEN not in token_encrypted           # encrypted at rest
    assert app.state.auth.users.token(1) == TOKEN
    assert TOKEN not in r.text and TOKEN not in cookie and TOKEN not in caplog.text

    # signing in again updates the same user
    github.profile["name"] = "Ada King"
    sign_in_via_github(TestClient(app))
    assert [row[2] for row in users(tmp_path)] == ["Ada King"]


def test_me_returns_the_user(tmp_path):
    client, _, _ = make(tmp_path)
    assert client.get("/api/auth/me").status_code == 401
    sign_in_via_github(client)
    r = client.get("/api/auth/me")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"name": "Ada Lovelace", "login": "ada", "avatar_url": ADA["avatar_url"]}
    assert TOKEN not in r.text


def test_me_falls_back_to_the_login_when_github_has_no_name(tmp_path):
    client, _, _ = make(tmp_path, github=FakeGitHub({**ADA, "name": None}))
    sign_in_via_github(client)
    assert client.get("/api/auth/me").json()["name"] == "ada"


def test_logout_ends_the_session(tmp_path):
    client, _, _ = make(tmp_path)
    sign_in_via_github(client)
    assert client.get("/api/auth/me").status_code == 200
    r = client.post("/api/auth/logout")
    assert r.status_code == 204 and SESSION_COOKIE in set_cookies(r)
    assert client.get("/api/auth/me").status_code == 401


def test_tampered_session_cookies_are_ignored(tmp_path):
    client, _, _ = make(tmp_path)
    sign_in_via_github(client)
    good = client.cookies.get(SESSION_COOKIE)
    for bad in ["x", good[:-2] + "zz",
                URLSafeTimedSerializer("wrong-secret", salt="coderag.session").dumps({"uid": 1})]:
        other = TestClient(client.app)
        other.cookies.set(SESSION_COOKIE, bad)
        assert other.get("/api/auth/me").status_code == 401


def test_next_is_kept_but_only_within_the_site(tmp_path):
    client, _, _ = make(tmp_path)
    state = start(client, next="/app?repo=requests")["state"]
    assert callback(client, code="good-code", state=state).headers["location"] == "/app?repo=requests"
    for evil in ["//evil.example", "https://evil.example", "/\\evil.example"]:
        state = start(client, next=evil)["state"]
        assert callback(client, code="good-code", state=state).headers["location"] == "/app"


def test_github_errors_send_back_to_sign_in(tmp_path):
    client, _, _ = make(tmp_path)
    state = start(client)["state"]
    assert callback(client, code="bad-code", state=state).headers["location"] == "/login?error=github"
    state = start(client)["state"]
    r = callback(client, error="access_denied", state=state)
    assert r.headers["location"] == "/login?error=denied"
    assert client.get("/api/auth/me").status_code == 401


def test_adding_a_repo_needs_a_session(tmp_path):
    client, _, _ = make(tmp_path)
    r = client.post("/api/repos", json={"url": "https://github.com/someone/pkg"})
    assert r.status_code == 401 and "Sign in" in r.json()["error"]
    assert client.get("/api/repos/mine").status_code == 401


def test_without_github_settings_the_server_runs_signed_out(tmp_path):
    app = create_app(Settings(data_dir=str(tmp_path)), demos={}, llm_factory=lambda choice: None,
                     limiter=RateLimiter(), embedder=HashEncoder())
    client = TestClient(app)
    assert client.get("/api/health").json()["auth"] == {"github": False}
    assert client.get("/api/auth/me").status_code == 401
    r = client.get("/api/auth/github", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?error=unavailable"
    assert callback(client, code="c", state="s").headers["location"] == "/login?error=unavailable"
    client.cookies.set(SESSION_COOKIE, URLSafeTimedSerializer("unused", salt="coderag.session").dumps({"uid": 1}))
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/repos", json={"url": "a/b"}).status_code == 401
    assert client.post("/api/auth/logout").status_code == 204


def test_secrets_are_not_in_settings_repr():
    s = Settings(**AUTH_SETTINGS)
    assert "test-client-secret" not in repr(s) and "test-session-secret" not in repr(s)


@pytest.mark.parametrize("evil", [
    "https://evil.example/app", "http://evil.example", "//evil.example", "///evil.example",
    "/\\evil.example", "\\\\evil.example", "/\t/evil.example", "/\n/evil.example",
    "evil.example", "javascript:alert(1)", "",
])
def test_external_next_is_rejected(tmp_path, evil):
    client, _, _ = make(tmp_path)
    state = start(client, next=evil)["state"]
    r = callback(client, code="good-code", state=state)
    assert r.status_code == 303 and r.headers["location"] == "/app"


def test_safe_next_accepts_only_site_paths():
    from coderag.web.auth import safe_next
    assert safe_next("/app?repo=requests#q") == "/app?repo=requests#q"
    assert safe_next("/") == "/"
    for evil in ["https://evil.example", "//evil.example", "/\\evil", "/\x00x", "/\x7fx", None, 5]:
        assert safe_next(evil) == "/app", repr(evil)


# ── the add-repo picker: GET /api/github/repos ───────────────────────────────
import httpx  # noqa: E402

from coderag.web import auth as auth_module  # noqa: E402


def repo(i, **extra):
    return {"full_name": f"Ada/repo-{i}", "private": i % 2 == 0, "language": "Python" if i % 3 else "Rust",
            "pushed_at": "2026-11-01T10:00:00Z", "description": f"repo {i}", "default_branch": "main",
            "id": 1000 + i, "owner": {"login": "Ada"}, "permissions": {"admin": True}, **extra}


class GitHubAPI:
    """httpx.MockTransport stand-in for github.com + api.github.com, with paginated /user/repos."""

    def __init__(self, total=137, status=200):
        self.total, self.status = total, status
        self.repo_requests = []

    def __call__(self, request):
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": TOKEN, "token_type": "bearer"})
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.url.path == "/user":
            return httpx.Response(200, json=ADA)
        assert request.url.path == "/user/repos"
        self.repo_requests.append(request.url)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "Bad credentials"})
        page = int(request.url.params.get("page", 1))
        items = [repo(i) for i in range((page - 1) * 100, min(page * 100, self.total))]
        headers = {}
        if page * 100 < self.total:
            nxt = f"https://api.github.com/user/repos?sort=pushed&per_page=100&page={page + 1}"
            headers["link"] = f'<{nxt}>; rel="next", <https://api.github.com/user/repos?page=99>; rel="last"'
        return httpx.Response(200, json=items, headers=headers)


def picker_client(tmp_path, api):
    github = GitHubOAuth(AUTH_SETTINGS["github_client_id"], AUTH_SETTINGS["github_client_secret"],
                         transport=httpx.MockTransport(api))
    client, app, _ = make(tmp_path, github=github)
    assert sign_in_via_github(client).headers["location"] == "/app"
    return client, app


def test_github_repos_needs_a_session(tmp_path):
    client, _, _ = make(tmp_path)
    r = client.get("/api/github/repos")
    assert r.status_code == 401 and r.json()["error"] != "reauth"


def test_github_repos_follows_pagination(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    api = GitHubAPI(total=137)
    client, _ = picker_client(tmp_path, api)
    r = client.get("/api/github/repos")
    assert r.status_code == 200
    repos = r.json()
    assert [x["full_name"] for x in repos] == [f"Ada/repo-{i}" for i in range(137)]
    assert set(repos[0]) == {"full_name", "private", "language", "pushed_at", "description", "default_branch",
                             "indexed", "repo_id"}
    first = api.repo_requests[0].params
    assert (first["sort"], first["per_page"], first["affiliation"]) == (
        "pushed", "100", "owner,collaborator,organization_member")
    assert [u.params.get("page") for u in api.repo_requests] == [None, "2"]
    assert TOKEN not in r.text and TOKEN not in caplog.text


def test_github_repos_stops_at_300(tmp_path):
    api = GitHubAPI(total=1000)
    client, _ = picker_client(tmp_path, api)
    assert len(client.get("/api/github/repos").json()) == 300
    assert len(api.repo_requests) == 3


def test_github_repos_flags_what_the_user_already_added(tmp_path):
    client, app = picker_client(tmp_path, GitHubAPI(total=5))
    users = app.state.auth.users
    users.add_repo(1, "ada__repo-3__" + "a" * 12)
    repos = {x["full_name"]: x for x in client.get("/api/github/repos").json()}
    assert repos["Ada/repo-3"]["indexed"] is True and repos["Ada/repo-3"]["repo_id"] == "ada__repo-3__" + "a" * 12
    assert not any(x["indexed"] for name, x in repos.items() if name != "Ada/repo-3")
    # the flag is fresh even while GitHub's list is cached
    users.add_repo(1, "ada__repo-1__" + "b" * 12)
    assert client.get("/api/github/repos").json()[1]["indexed"] is True


def test_github_repos_are_cached_per_user_for_a_minute(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: clock[0])
    api = GitHubAPI(total=5)
    client, app = picker_client(tmp_path, api)
    client.get("/api/github/repos")
    clock[0] += 59
    client.get("/api/github/repos")
    assert len(api.repo_requests) == 1
    other = TestClient(app)                     # another user has their own list
    other.cookies.set(SESSION_COOKIE, app.state.auth.session_cookie(
        app.state.auth.users.upsert({"id": 7, "login": "bob"}, TOKEN).id))
    other.get("/api/github/repos")
    assert len(api.repo_requests) == 2
    clock[0] += 2
    client.get("/api/github/repos")
    assert len(api.repo_requests) == 3


def test_revoked_token_asks_to_reconnect(tmp_path):
    api = GitHubAPI(status=401)
    client, _ = picker_client(tmp_path, api)
    r = client.get("/api/github/repos")
    assert r.status_code == 401 and r.json() == {"error": "reauth"}
    assert TOKEN not in r.text
    # still signed in: only GitHub needs a fresh token
    assert client.get("/api/auth/me").status_code == 200
    # a token that can't be decrypted (session secret changed) also means reconnect
    with sqlite3.connect(tmp_path / "coderag.db") as db:
        db.execute("UPDATE users SET token_encrypted = 'garbage'")
    api.status = 200
    assert client.get("/api/github/repos").json() == {"error": "reauth"}


def test_github_outage_is_a_502_not_a_reauth(tmp_path):
    client, _ = picker_client(tmp_path, GitHubAPI(status=500))
    r = client.get("/api/github/repos")
    assert r.status_code == 502 and "paste a URL" in r.json()["error"]
