import os
import shutil
import zlib

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_REPO = os.path.join(ROOT, "sample_repo")


class HashEncoder:
    """Deterministic bag-of-words embedding: no model download, real FAISS search."""

    def __init__(self):
        self.encoded = 0

    def encode(self, texts, show_progress_bar=False):
        self.encoded += len(texts)
        out = np.zeros((len(texts), 64), dtype="float32")
        for row, text in enumerate(texts):
            for word in text.lower().split():
                out[row, zlib.crc32(word.encode()) % 64] += 1
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)


class FakeIndex:
    """Stands in for the FAISS index: returns a fixed ranking."""

    def __init__(self, ranking=None):
        self.ranking = ranking

    def build(self, texts):
        self.texts = texts

    def search(self, query, k):
        ranking = self.ranking if self.ranking is not None else range(len(self.texts))
        return list(ranking)[:k]


class FakeLLM:
    name = "fake"

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply

    def stream(self, context, question, config=None):
        self.calls.append((context, question))
        text = self.reply or f"answer to {question}"
        yield from (text[i:i + 4] for i in range(0, len(text), 4))    # several tokens

    def generate(self, context, question, config=None):
        return "".join(self.stream(context, question, config))


@pytest.fixture
def sample_repo():
    return SAMPLE_REPO


@pytest.fixture
def zoo_repo(tmp_path):
    """Just the original toy inheritance file."""
    shutil.copy(os.path.join(SAMPLE_REPO, "test_inheritance.py"), tmp_path)
    return str(tmp_path)


# ── GitHub sign-in (coderag/web/auth.py) ────────────────────────────────────
# secrets of at least 32 characters (Settings.validate refuses shorter ones with GitHub sign-in)
AUTH_SETTINGS = {"github_client_id": "test-client-id", "github_client_secret": "test-client-secret",
                 "session_secret": "test-session-secret-0123456789abcdef",
                 "token_key": "test-token-key-0123456789abcdef-xyz"}


def sign_in(client, app, github_id=1, login="ada", token="gho_test"):
    """Save a user and give `client` their session cookie, without the OAuth round trip."""
    auth = app.state.auth
    user = auth.users.upsert({"id": github_id, "login": login, "name": login.title(), "avatar_url": ""}, token)
    client.cookies.set("coderag_session", auth.session_cookie(user.id))
    return user


def repo_row(app, repo_id):
    """A visitor repo's row in the database (the registry; demo.json is no longer written)."""
    from coderag.db.models import Repo
    with app.state.db.session() as s:
        return s.get(Repo, repo_id)


# ── no network: DELETE /api/me revokes the GitHub grant; tests never reach GitHub ──
try:
    from coderag.web.auth import GitHubOAuth
    REAL_REVOKE = GitHubOAuth.revoke
except ImportError:                                  # the web extra isn't installed
    GitHubOAuth = REAL_REVOKE = None


@pytest.fixture(autouse=True)
def _no_github_grant_revocation(monkeypatch):
    if GitHubOAuth is not None:
        monkeypatch.setattr(GitHubOAuth, "revoke", lambda self, token: None)
