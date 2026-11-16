"""
GitHub sign-in: the OAuth web flow, users in SQLite, sessions in a signed cookie.

    GET  /api/auth/github            -> GitHub, with a random `state` in a signed cookie
    GET  /api/auth/github/callback   state checked, code exchanged, user saved, -> /app
    GET  /api/auth/me                {name, login, avatar_url} or 401
    POST /api/auth/logout
    GET  /api/github/repos           the user's GitHub repos for the add-repo picker (cached 60 s)

The session cookie is signed (itsdangerous), HttpOnly and SameSite=Lax, and holds
only the user's id. The GitHub token is kept so private repositories can be
indexed; it is stored encrypted (Fernet, key derived from SESSION_SECRET) and is
never logged or returned. Without the GitHub settings nobody can sign in, and
the rest of the site works as before.
"""

import base64
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

log = logging.getLogger("coderag.web.auth")

SESSION_COOKIE = "coderag_session"
STATE_COOKIE = "coderag_oauth_state"
SESSION_MAX_AGE = 30 * 86400
STATE_MAX_AGE = 600
SCOPE = "read:user repo"
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
REPOS_URL = "https://api.github.com/user/repos"
MAX_REPOS = 300
REPOS_TTL = 60
REPO_FIELDS = ("full_name", "private", "language", "pushed_at", "description", "default_branch")


class GitHubError(Exception):
    """GitHub refused or failed a step of the sign-in. The message never holds a token."""


class GitHubReauth(GitHubError):
    """The stored token was rejected (expired or revoked): the user must sign in again."""


def safe_next(raw, fallback="/app"):
    """
    A path on this site only (same rule as safeNext in frontend/auth.js): starts
    with "/" but not "//", no backslash, no control characters (browsers drop
    tabs and newlines, so "/<tab>/evil.example" would become "//evil.example").
    """
    if (not isinstance(raw, str) or not raw.startswith("/") or raw.startswith("//")
            or any(c == "\\" or ord(c) < 0x20 or ord(c) == 0x7f for c in raw)):
        return fallback
    return raw


# =============================================================================
# GITHUB
# =============================================================================
class GitHubOAuth:
    def __init__(self, client_id, client_secret, timeout=10, transport=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.transport = transport

    def authorize_url(self, state, redirect_uri):
        return AUTHORIZE_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": redirect_uri, "scope": SCOPE,
            "state": state, "allow_signup": "true"})

    def _client(self):
        return httpx.Client(timeout=self.timeout, transport=self.transport,
                            headers={"User-Agent": "coderag", "Accept": "application/json"})

    def exchange_code(self, code, redirect_uri):
        """Access token for an authorization code."""
        with self._client() as client:
            r = client.post(TOKEN_URL, data={"client_id": self.client_id,
                                             "client_secret": self.client_secret,
                                             "code": code, "redirect_uri": redirect_uri})
        if r.status_code != 200:
            raise GitHubError(f"token exchange returned HTTP {r.status_code}")
        body = r.json()
        if not body.get("access_token"):
            raise GitHubError(f"token exchange failed: {body.get('error', 'no token')}")
        return body["access_token"]

    def user(self, token):
        """The signed-in GitHub user: {id, login, name, avatar_url}."""
        with self._client() as client:
            r = client.get(USER_URL, headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            raise GitHubError(f"/user returned HTTP {r.status_code}")
        body = r.json()
        if not isinstance(body.get("id"), int) or not body.get("login"):
            raise GitHubError("/user returned no id")
        return body

    def list_repos(self, token, limit=MAX_REPOS):
        """
        Repos the user owns, collaborates on or reaches through an org, most
        recently pushed first, following GitHub's Link pagination up to `limit`.
        """
        repos = []
        url = REPOS_URL
        params = {"sort": "pushed", "per_page": 100,
                  "affiliation": "owner,collaborator,organization_member"}
        with self._client() as client:
            while url and len(repos) < limit:
                r = client.get(url, params=params, headers={"Authorization": f"Bearer {token}",
                                                            "Accept": "application/vnd.github+json"})
                if r.status_code == 401:
                    raise GitHubReauth("GitHub rejected the stored token")
                if r.status_code != 200:
                    raise GitHubError(f"/user/repos returned HTTP {r.status_code}")
                page = r.json()
                if not isinstance(page, list):
                    raise GitHubError("/user/repos returned no list")
                repos.extend({k: item.get(k) for k in REPO_FIELDS} for item in page
                             if isinstance(item, dict) and item.get("full_name"))
                url = r.links.get("next", {}).get("url")
                params = None                       # the next link carries the query
        return repos[:limit]


# =============================================================================
# USERS (SQLite)
# =============================================================================
@dataclass(frozen=True)
class User:
    id: int
    github_id: int
    login: str
    name: str
    avatar_url: str

    def public(self):
        return {"name": self.name or self.login, "login": self.login, "avatar_url": self.avatar_url}


def _fernet(secret):
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"coderag github token").derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    github_id INTEGER NOT NULL UNIQUE,
    login TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    avatar_url TEXT NOT NULL DEFAULT '',
    token_encrypted TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_repos (
    user_id INTEGER NOT NULL REFERENCES users(id),
    repo_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (user_id, repo_id)
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Users:
    """users(id, github_id, login, name, avatar_url, token_encrypted, created_at) + who added which repo."""

    def __init__(self, path, secret):
        self.path = path
        self.fernet = _fernet(secret)
        self._ready = False
        self._lock = threading.Lock()

    def _db(self):
        if not self._ready:                     # created on first use, not at startup
            with self._lock:
                if not self._ready:
                    os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
                    with closing(sqlite3.connect(self.path)) as db:
                        db.executescript(_SCHEMA)
                    self._ready = True
        return closing(sqlite3.connect(self.path, timeout=10))

    @staticmethod
    def _user(row):
        return User(*row) if row else None

    def upsert(self, github_user, token):
        """Save (or refresh) a GitHub user and their token; returns the User."""
        values = (str(github_user["login"]), str(github_user.get("name") or ""),
                  str(github_user.get("avatar_url") or ""),
                  self.fernet.encrypt(token.encode()).decode())
        with self._db() as db, db:
            db.execute(
                "INSERT INTO users (github_id, login, name, avatar_url, token_encrypted, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(github_id) DO UPDATE SET login=excluded.login, "
                "name=excluded.name, avatar_url=excluded.avatar_url, "
                "token_encrypted=excluded.token_encrypted",
                (int(github_user["id"]), *values, _now()))
            row = db.execute("SELECT id, github_id, login, name, avatar_url FROM users "
                             "WHERE github_id = ?", (int(github_user["id"]),)).fetchone()
        return self._user(row)

    def get(self, user_id):
        with self._db() as db:
            return self._user(db.execute("SELECT id, github_id, login, name, avatar_url FROM users "
                                         "WHERE id = ?", (user_id,)).fetchone())

    def token(self, user_id):
        """The user's GitHub token, or None (unknown user, or SESSION_SECRET changed)."""
        with self._db() as db:
            row = db.execute("SELECT token_encrypted FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or not row[0]:
            return None
        try:
            return self.fernet.decrypt(row[0].encode()).decode()
        except InvalidToken:
            return None

    def add_repo(self, user_id, repo_id):
        with self._db() as db, db:
            db.execute("INSERT OR IGNORE INTO user_repos (user_id, repo_id, added_at) VALUES (?, ?, ?)",
                       (user_id, repo_id, _now()))

    def repo_ids(self, user_id):
        """Repos the user added, newest first."""
        with self._db() as db:
            return [r[0] for r in db.execute("SELECT repo_id FROM user_repos WHERE user_id = ? "
                                             "ORDER BY added_at DESC, rowid DESC", (user_id,))]

    def remove_repo(self, user_id, repo_id):
        with self._db() as db, db:
            db.execute("DELETE FROM user_repos WHERE user_id = ? AND repo_id = ?", (user_id, repo_id))

    def has_repo(self, user_id, repo_id):
        with self._db() as db:
            return db.execute("SELECT 1 FROM user_repos WHERE user_id = ? AND repo_id = ?",
                              (user_id, repo_id)).fetchone() is not None


# =============================================================================
# SESSIONS + ROUTES
# =============================================================================
class Auth:
    """
    settings: Settings (github_client_id, github_client_secret, session_secret, public_url, data_dir)
    github  : GitHubOAuth-like object (default: the real one); tests pass a fake
    """

    def __init__(self, settings, github=None):
        self.settings = settings
        self.enabled = bool(settings.github_client_id and settings.github_client_secret
                            and settings.session_secret)
        self.github = github or (GitHubOAuth(settings.github_client_id, settings.github_client_secret)
                                 if self.enabled else None)
        self.users = Users(os.path.join(settings.data_dir, "coderag.db"),
                           settings.session_secret) if self.enabled else None
        secret = settings.session_secret or "unused"
        self._sessions = URLSafeTimedSerializer(secret, salt="coderag.session")
        self._states = URLSafeTimedSerializer(secret, salt="coderag.oauth-state")
        self._repo_cache = {}                   # user id -> (fetched at, [repo dicts])
        self._repo_lock = threading.Lock()
        if not self.enabled:
            log.info("GitHub sign-in is off (set CODERAG_GITHUB_CLIENT_ID, "
                     "CODERAG_GITHUB_CLIENT_SECRET and CODERAG_SESSION_SECRET)")

    # ── sessions ─────────────────────────────────────────────────────────────
    def session_cookie(self, user_id):
        return self._sessions.dumps({"uid": user_id})

    def current_user(self, request):
        """The signed-in User, or None."""
        raw = request.cookies.get(SESSION_COOKIE)
        if not self.enabled or not raw:
            return None
        try:
            data = self._sessions.loads(raw, max_age=SESSION_MAX_AGE)
        except BadSignature:
            return None
        uid = data.get("uid") if isinstance(data, dict) else None
        return self.users.get(uid) if isinstance(uid, int) else None

    def token(self, user):
        return self.users.token(user.id) if user and self.users else None

    def github_repos(self, user, now=None):
        """
        The user's GitHub repos (see GitHubOAuth.list_repos), cached per user for
        REPOS_TTL seconds. GitHubReauth if there is no usable token.
        """
        now = time.monotonic() if now is None else now
        with self._repo_lock:
            hit = self._repo_cache.get(user.id)
            if hit and now - hit[0] < REPOS_TTL:
                return hit[1]
        token = self.token(user)
        if not token:
            raise GitHubReauth("no stored token")
        try:
            repos = self.github.list_repos(token)
        except GitHubReauth:
            with self._repo_lock:
                self._repo_cache.pop(user.id, None)
            raise
        with self._repo_lock:
            for uid in [u for u, (at, _) in self._repo_cache.items() if now - at >= REPOS_TTL]:
                del self._repo_cache[uid]
            self._repo_cache[user.id] = (now, repos)
        return repos

    # ── helpers ──────────────────────────────────────────────────────────────
    def redirect_uri(self, request):
        base = self.settings.public_url.rstrip("/") or str(request.base_url).rstrip("/")
        return f"{base}/api/auth/github/callback"

    def _secure(self, request):
        return self.settings.public_url.startswith("https://") or request.url.scheme == "https"

    def _set(self, response, request, name, value, max_age, path="/"):
        response.set_cookie(name, value, max_age=max_age, path=path, httponly=True,
                            samesite="lax", secure=self._secure(request))

    def router(self):
        r = APIRouter()

        def to_login(error):
            return RedirectResponse(f"/login?error={error}", status_code=303)

        @r.get("/api/auth/github")
        def github_start(request: Request, next: str = "/app"):
            if not self.enabled:
                return to_login("unavailable")
            state = secrets.token_urlsafe(32)
            response = RedirectResponse(self.github.authorize_url(state, self.redirect_uri(request)),
                                        status_code=302)
            self._set(response, request, STATE_COOKIE,
                      self._states.dumps({"state": state, "next": safe_next(next)}),
                      STATE_MAX_AGE, path="/api/auth")
            return response

        @r.get("/api/auth/github/callback")
        def github_callback(request: Request, code: str = "", state: str = "", error: str = ""):
            if not self.enabled:
                return to_login("unavailable")
            try:
                saved = self._states.loads(request.cookies.get(STATE_COOKIE, ""), max_age=STATE_MAX_AGE)
            except BadSignature:
                saved = None
            if error == "access_denied":
                response = to_login("denied")
            elif not (isinstance(saved, dict) and state and code
                      and hmac.compare_digest(state, str(saved.get("state", "")))):
                response = to_login("state")
            else:
                try:
                    token = self.github.exchange_code(code, self.redirect_uri(request))
                    user = self.users.upsert(self.github.user(token), token)
                except (GitHubError, httpx.HTTPError, ValueError) as e:
                    log.warning("GitHub sign-in failed: %s", e if isinstance(e, GitHubError)
                                else type(e).__name__)
                    response = to_login("github")
                else:
                    response = RedirectResponse(safe_next(saved.get("next")), status_code=303)
                    self._set(response, request, SESSION_COOKIE, self.session_cookie(user.id),
                              SESSION_MAX_AGE)
            response.delete_cookie(STATE_COOKIE, path="/api/auth")    # one use only
            return response

        @r.get("/api/auth/me")
        def me(request: Request):
            user = self.current_user(request)
            if user is None:
                return JSONResponse({"error": "Not signed in"}, status_code=401,
                                    headers={"Cache-Control": "no-store"})
            return JSONResponse(user.public(), headers={"Cache-Control": "no-store"})

        @r.post("/api/auth/logout")
        def logout(request: Request):
            response = Response(status_code=204)
            response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax",
                                   secure=self._secure(request))
            return response

        return r
