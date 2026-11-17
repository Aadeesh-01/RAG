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
never logged or returned. It is encrypted with CODERAG_TOKEN_KEY; tokens stored
before that key existed (encrypted with a key derived from SESSION_SECRET) are still
read, and re-encrypted with the token key on their next use. Without the GitHub
settings nobody can sign in, and the rest of the site works as before.
"""

import base64
import hmac
import logging
import os
import secrets
import threading
import time
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
GRANT_URL = "https://api.github.com/applications/{client_id}/grant"
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

    def revoke(self, token):
        """Revoke this app's grant for `token` (the user's authorization of coderag). Raises on failure."""
        with self._client() as client:
            r = client.request("DELETE", GRANT_URL.format(client_id=self.client_id),
                               auth=(self.client_id, self.client_secret), json={"access_token": token},
                               headers={"Accept": "application/vnd.github+json"})
        if r.status_code not in (204, 404, 422):              # 404/422: already revoked or unknown
            raise GitHubError(f"grant revocation returned HTTP {r.status_code}")

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
# USERS (the database: coderag/db)
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


def _fernet(secret, info=b"coderag github token"):
    """Fernet keyed by HKDF(secret). The default `info` is the one used with SESSION_SECRET before CODERAG_TOKEN_KEY."""
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


TOKEN_KEY_INFO = b"coderag github token key"


def _now():
    from ..db.models import utcnow
    return utcnow()


class Users:
    """
    The users table (coderag/db/models.py) and who added which repo. The GitHub
    token is stored Fernet-encrypted with a key derived from `token_key`
    (CODERAG_TOKEN_KEY); it is never logged, returned or kept in clear.
    `secret` (SESSION_SECRET) only decrypts tokens stored before the token key:
    they are re-encrypted with it on first use. Without `token_key`, `secret` is the key.
    """

    SEEN_EVERY = 300                              # seconds between last_seen_at writes

    def __init__(self, db, secret, token_key=None):
        self.db = db
        self.legacy = _fernet(secret) if secret else None
        self.fernet = _fernet(token_key, TOKEN_KEY_INFO) if token_key else self.legacy

    @staticmethod
    def _user(row):
        return User(row.id, row.github_id, row.login, row.name, row.avatar_url) if row else None

    def upsert(self, github_user, token):
        """Save (or refresh) a GitHub user and their token; returns the User."""
        from ..db.models import User as Row
        with self.db.session() as s:
            row = s.query(Row).filter_by(github_id=int(github_user["id"])).one_or_none()
            if row is None:
                row = Row(github_id=int(github_user["id"]), created_at=_now())
                s.add(row)
            row.login = str(github_user["login"])
            row.name = str(github_user.get("name") or "")
            row.avatar_url = str(github_user.get("avatar_url") or "")
            row.token_encrypted = self.fernet.encrypt(token.encode()).decode()
            row.last_seen_at = _now()
            s.flush()
            return self._user(row)

    def get(self, user_id):
        from ..db.models import User as Row
        with self.db.session() as s:
            return self._user(s.get(Row, user_id))

    def touch(self, user_id):
        """Record that the user was seen (at most every SEEN_EVERY seconds)."""
        from ..db.models import User as Row
        now = _now()
        with self.db.session() as s:
            row = s.get(Row, user_id)
            if row is not None and (row.last_seen_at is None
                                    or (now - row.last_seen_at).total_seconds() > self.SEEN_EVERY):
                row.last_seen_at = now

    def token(self, user_id):
        """The user's GitHub token, or None (unknown user, or the keys changed)."""
        from ..db.models import User as Row
        with self.db.session() as s:
            row = s.get(Row, user_id)
            encrypted = row.token_encrypted if row else ""
        if not encrypted:
            return None
        try:
            return self.fernet.decrypt(encrypted.encode()).decode()
        except InvalidToken:
            pass
        if self.legacy is None or self.legacy is self.fernet:
            return None
        try:
            token = self.legacy.decrypt(encrypted.encode()).decode()
        except InvalidToken:
            return None                               # neither key: SESSION_SECRET changed, sign in again
        with self.db.session() as s:                  # stored before CODERAG_TOKEN_KEY: move it to that key
            row = s.get(Row, user_id)
            if row is not None and row.token_encrypted == encrypted:
                row.token_encrypted = self.fernet.encrypt(token.encode()).decode()
        return token

    def add_repo(self, user_id, repo_id):
        from ..db.models import UserRepo
        with self.db.session() as s:
            if s.get(UserRepo, (user_id, repo_id)) is None:
                s.add(UserRepo(user_id=user_id, repo_id=repo_id, added_at=_now()))

    def repo_ids(self, user_id):
        """Repos the user added, newest first."""
        from ..db.models import UserRepo
        with self.db.session() as s:
            rows = s.query(UserRepo).filter_by(user_id=user_id).order_by(UserRepo.added_at.desc()).all()
            return [r.repo_id for r in rows]

    def remove_repo(self, user_id, repo_id):
        from ..db.models import UserRepo
        with self.db.session() as s:
            s.query(UserRepo).filter_by(user_id=user_id, repo_id=repo_id).delete()

    def has_repo(self, user_id, repo_id):
        from ..db.models import UserRepo
        with self.db.session() as s:
            return s.get(UserRepo, (user_id, repo_id)) is not None

    def users_of(self, repo_id):
        """How many users have `repo_id` in their list."""
        from ..db.models import UserRepo
        with self.db.session() as s:
            return s.query(UserRepo).filter_by(repo_id=repo_id).count()

    def delete(self, user_id):
        """
        Delete the account: the user row with its token, repo links, questions
        (with their shares and feedback) and indexing jobs. Repos they indexed
        stay for the other users who have them. Returns the repo ids they had in their list.
        """
        from ..db.models import Job, Question, User as Row, UserRepo
        with self.db.session() as s:
            repo_ids = [r.repo_id for r in s.query(UserRepo).filter_by(user_id=user_id)]
            s.query(UserRepo).filter_by(user_id=user_id).delete()
            s.query(Job).filter_by(user_id=user_id).delete()
            for q in s.query(Question).filter_by(user_id=user_id):
                s.delete(q)                           # shares go with it (cascade)
            row = s.get(Row, user_id)
            if row is not None:
                s.delete(row)
        return repo_ids


# =============================================================================
# SESSIONS + ROUTES
# =============================================================================
class Auth:
    """
    settings: Settings (github_client_id, github_client_secret, session_secret, public_url, data_dir)
    github  : GitHubOAuth-like object (default: the real one); tests pass a fake
    """

    def __init__(self, settings, github=None, db=None):
        self.settings = settings
        self.enabled = bool(settings.github_client_id and settings.github_client_secret
                            and settings.session_secret)
        self.github = github or (GitHubOAuth(settings.github_client_id, settings.github_client_secret)
                                 if self.enabled else None)
        if self.enabled and db is None:
            from ..db import Database, default_url
            db = Database(settings.database_url or default_url(settings.data_dir))
            db.upgrade()
        self.users = (Users(db, settings.session_secret, token_key=getattr(settings, "token_key", "") or None)
                      if self.enabled else None)
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
        user = self.users.get(uid) if isinstance(uid, int) else None
        if user is not None:
            self.users.touch(user.id)
        return user

    def token(self, user):
        return self.users.token(user.id) if user and self.users else None

    def revoke_grant(self, user):
        """Best effort: revoke the user's GitHub authorization of this app (failures are ignored)."""
        token = self.token(user)
        revoke = getattr(self.github, "revoke", None)
        if not token or revoke is None:
            return False
        try:
            revoke(token)
            return True
        except Exception as e:                       # noqa: BLE001 (never block account deletion)
            log.warning("revoking the GitHub grant failed: %s", e if isinstance(e, GitHubError) else type(e).__name__)
            return False

    def forget(self, user_id):
        """Drop what is cached in memory for a user (their GitHub repo list)."""
        with self._repo_lock:
            self._repo_cache.pop(user_id, None)

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
