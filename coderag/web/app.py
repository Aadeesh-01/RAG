"""
FastAPI server for the pre-indexed demo repositories.

    GET  /  /app  /login                     home page, the app, sign-in (frontend/)
    GET  /api/health                         liveness (no I/O)
    GET  /api/ready                          readiness: the database answers, the demos loaded
    GET  /api/demos
    GET  /api/auth/github, /api/auth/github/callback, /api/auth/me; POST /api/auth/logout  (auth.py)
    POST /api/repos                         index a GitHub repo (signed in) with CODERAG_DEFAULT_VERSION
    POST /api/repos/{repo_id}/reindex       rebuild it with the default version (signed in)
    DELETE /api/repos/{repo_id}/mine        take it off the user's list (its index goes when nobody has it)
    GET  /api/jobs                          the signed-in user's indexing jobs, newest first
    GET  /api/jobs/{job_id}                 one of them (its owner only)
    GET  /api/repos/{id}/history, POST/DELETE /api/questions/{id}/share, GET /api/shares/{slug},
    POST /api/questions/{id}/feedback, GET /a/{slug}           (history.py)
    DELETE /api/me                          delete the account: tokens, history, shares

State lives in the database (coderag/db; CODERAG_DATABASE_URL, default SQLite in
data_dir), migrated on start. Index files stay on disk; answer cache and rate
limits stay in memory, behind interfaces (limits.py).
    GET  /api/repos/mine                    repos the signed-in user added (their repo tabs)
    PUT  /api/repos/mine/{repo_id}          add an indexed repo they can open to that list
    DELETE /api/repos/mine/{repo_id}        take it off the list (the index stays)
    GET  /api/github/repos                  the user's GitHub repos, for the add-repo picker
    GET  /api/repos/{repo_id}
    GET  /api/repos/{repo_id}/graph?version=v7
    GET  /api/repos/{repo_id}/file?path=pkg/mod.py    whole indexed file, for the code viewer
    POST /api/repos/{repo_id}/retrieve      JSON: sources (+ context), no LLM
    POST /api/repos/{repo_id}/ask           Server-Sent Events: sources, token*, done | error

Run:  uvicorn coderag.web.app:app --port 7860 --workers 1
      (build the demos first: python scripts/build_demos.py)

One process only (--workers 1): the indexing queue, rate limits, the answer cache
and the concurrent-answer limit live in memory. Settings are checked at start
(Settings.validate): unsafe or half-done ones stop the server with a message.

A visitor repo whose index files are gone (a reset disk; the database kept its
row) is rebuilt at the same commit when it is opened or submitted again:
GET /api/repos/{id} answers 202 {"rebuilding": true, "job": ...} meanwhile, and the
other repo endpoints 409 with the same job.

Visitors may send their own provider key with a question (llm.api_key). It is
used for that request only and never logged, cached or returned.

Demos are public. Adding a repo needs a GitHub sign-in; a private repo can be
opened only by the users who added it (anyone else gets 404).
"""

import json
import logging
import os
import time
from typing import Literal

import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from starlette.background import BackgroundTask
from starlette.datastructures import MutableHeaders

from ..config import get_config
from ..gitfetch import FetchError
from ..llm import DEFAULT_REMOTE_MODELS, FallbackChain, LLMUnavailable, RemoteLLM
from ..visualize import graph_data
from ..db import Database, default_url, safe_url
from . import history
from .auth import SESSION_COOKIE, Auth, GitHubError, GitHubReauth
from .jobs import Indexer, NeedsToken, NotAllowed, QueueFull
from .limits import LRU, AnswerCache, DailyUsage, Gate, RateLimiter, client_ip
from .live import LiveChecker, candidate_urls
from .middleware import BodyLimit, OriginCheck, SecurityHeaders, configure_logging
from .registry import Repos, is_older, load_demos, repo_name_of
from .settings import Settings
from .sources import SourceUnavailable, fetch_url, read_source

log = logging.getLogger("coderag.web")
HOUR, DAY = 3600, 86400
CODE_CHARS = 4000            # cap on per-source code sent to the browser
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "frontend")


# =============================================================================
# REQUEST MODELS
# =============================================================================
class LLMChoice(BaseModel):
    provider: Literal["server", "groq", "openrouter", "gemini", "none"] = "server"
    model: str | None = Field(None, max_length=100)
    api_key: SecretStr | None = Field(None, max_length=300)    # hidden in repr/logs


class RetrieveRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    version: str | None = None
    top_k: int | None = Field(None, ge=1, le=20)
    max_hops: int | None = Field(None, ge=0, le=4)
    max_expansion: int | None = Field(None, ge=0, le=16)
    include_callers: bool | None = None
    include_context: bool = False
    include_code: bool = False          # add each source's code (capped) for display
    debug: bool = False                 # add how each source was retrieved (rank, graph path)

    def overrides(self):
        return {"top_k": self.top_k, "max_hops": self.max_hops,
                "max_expansion": self.max_expansion, "include_callers": self.include_callers}


class IndexRequest(BaseModel):
    url: str = Field(min_length=3, max_length=300)


class AskRequest(RetrieveRequest):
    prompt_style: Literal["cited", "legacy"] = "cited"
    llm: LLMChoice = LLMChoice()


# =============================================================================
# LLM SELECTION
# =============================================================================
def remote_config(provider, model):
    model = model or DEFAULT_REMOTE_MODELS[provider]
    return get_config(llm_backend="remote", llm_provider=provider, llm_model=model,
                      max_new_tokens=1024,
                      reasoning_effort="low" if "gpt-oss" in model else None)


class LLMFactory:
    """
    Picks the backend for one request:
      visitor's key  -> that provider only (their quota, no fallback to ours)
      no key         -> the server models in order, falling back when one is busy
    """

    def __init__(self, settings):
        self.server = []
        for model in settings.server_llm_models:
            try:
                self.server.append(RemoteLLM(remote_config(settings.server_llm_provider, model)))
            except LLMUnavailable:
                pass        # no server key configured: visitors need their own or get sources only

    @property
    def server_available(self):
        return bool(self.server)

    def __call__(self, choice):
        if choice.provider == "none":
            return None
        if choice.api_key and choice.provider in DEFAULT_REMOTE_MODELS:
            return RemoteLLM(remote_config(choice.provider, choice.model),
                             api_key=choice.api_key.get_secret_value())
        return FallbackChain(self.server) if self.server else None


# =============================================================================
# APP
# =============================================================================
class RevalidateFrontend:
    """
    Cache-Control: no-cache on the site's pages, scripts and stylesheets, so
    browsers check for a new version every time (a cheap 304 when unchanged).
    Without it, browsers guess a freshness lifetime from Last-Modified and can
    keep running an old auth.js next to a new login.js.
    Plain ASGI, so the answer stream (SSE) passes through untouched.
    """

    PAGES = ("/", "/app", "/login")
    SUFFIXES = (".html", ".js", ".mjs", ".css")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path or path.startswith("/api/") or not (path in self.PAGES or path.endswith(self.SUFFIXES)):
            return await self.app(scope, receive, send)

        async def send_revalidating(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_revalidating)


class Rebuilding(Exception):
    """The repo's index files are missing and are being rebuilt (job: IndexJob)."""

    def __init__(self, job, position=0):
        super().__init__(job.repo_id)
        self.job, self.position = job, position


def _sse(event):
    data = {k: v for k, v in event.items() if k != "type"}
    return f"event: {event['type']}\ndata: {json.dumps(data)}\n\n"


def create_app(settings=None, demos=None, llm_factory=None, limiter=None, embedder=None,
               indexer=None, fetch_source=fetch_url, github=None, live=None, db=None):
    """
    settings    : Settings (default: from CODERAG_* env vars)
    demos       : {id: Demo} (default: loaded from settings.data_dir)
    llm_factory : callable(LLMChoice) -> backend or None (default: LLMFactory)
    embedder    : embedding model shared by all indexes (default: loaded, behind a lock)
    indexer     : jobs.Indexer for visitors' repos (default: built from the above)
    fetch_source: callable(url) -> text, for files not saved locally (default: GitHub raw)
    github      : GitHub OAuth client (default: auth.GitHubOAuth from the settings)
    live        : web.live.LiveChecker for "Check if live" (default: real HEAD requests)
    db          : coderag.db.Database (default: CODERAG_DATABASE_URL, else SQLite in data_dir)
    """
    configure_logging()
    settings = settings or Settings.from_env()
    for warning in settings.validate():             # ConfigError: unsafe settings stop the start here
        log.warning("%s", warning)
    if embedder is None:
        from ..index import SharedEncoder, _load_model
        embedder = SharedEncoder(_load_model(get_config().embed_model))
    if demos is None:
        demos = load_demos(settings.demos_path, embedder=embedder)
        log.info("demos: %s from %s", ", ".join(demos) or "none", settings.demos_path)
    if db is None:
        db = Database(settings.database_url or default_url(settings.data_dir))
    db.upgrade()                                   # migrations on start (the old auth DB included)
    log.info("database: %s", safe_url(db.url))
    repos = Repos(settings.data_dir, demos, embedder, settings.max_loaded_repos,
                  settings.max_stored_repos, db=db, max_stored_mb=settings.max_stored_mb)
    repos.register_demos()
    repos.import_legacy()
    indexer = indexer or Indexer(settings, repos, embedder, db=db)
    restarted = indexer.recover()
    if restarted:
        log.warning("%d indexing job(s) interrupted by a restart were marked failed", restarted)
    llm_factory = llm_factory or LLMFactory(settings)
    limiter = limiter or RateLimiter()
    auth = Auth(settings, github, db=db)
    live = live or LiveChecker()
    cache = AnswerCache(settings.answer_cache_size)
    graphs = LRU(settings.max_loaded_repos + len(demos))      # graph JSON per (repo, version, index)
    usage = DailyUsage(db)                                    # the server key's answers today
    asking = Gate(settings.max_concurrent_asks)

    docs = settings.docs
    app = FastAPI(title="coderag", version="0.1.0",
                  description="Ask questions about a codebase, with cited sources.",
                  docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
                  openapi_url="/openapi.json" if docs else None)
    app.state.repos, app.state.indexer, app.state.auth, app.state.db = repos, indexer, auth, db
    app.state.cache, app.state.usage, app.state.graphs, app.state.asking = cache, usage, graphs, asking
    app.add_middleware(RevalidateFrontend)
    app.add_middleware(SecurityHeaders, frontend_dir=FRONTEND_DIR if os.path.isdir(FRONTEND_DIR) else None,
                       public_url=settings.public_url)
    app.add_middleware(BodyLimit)
    app.add_middleware(OriginCheck, public_url=settings.public_url)      # outermost: refused before anything
    app.include_router(auth.router())

    def can_open(repo_id, request):
        """Private repos only for users who added them; everything else for everyone."""
        if not repos.private(repo_id):
            return True
        user = auth.current_user(request)
        return user is not None and auth.users.has_repo(user.id, repo_id)

    def rebuild(repo_id, request):
        """
        Queue (or find) the rebuild of a repo whose index files are missing. Public
        repos need nobody's token; a private one uses the requester's (they can open it).
        """
        user = auth.current_user(request)
        token = auth.token(user) if user is not None else None
        try:
            job, _ = indexer.rebuild(repo_id, token=token)
        except NeedsToken:
            raise HTTPException(409, "This private repository's index has to be rebuilt; sign in with "
                                     "GitHub again so it can be fetched with your access.")
        except QueueFull as e:
            raise HTTPException(503, str(e), headers={"Retry-After": "60"})
        except KeyError:
            raise HTTPException(404, f"Unknown repository {repo_id!r}")
        return job

    def resolve(repo_id, version, request):
        allowed = can_open(repo_id, request)
        demo = repos.get(repo_id) if allowed else None
        if demo is None and allowed and repos.missing(repo_id):
            job = rebuild(repo_id, request)
            raise Rebuilding(job, indexer.position(job))
        if demo is None:
            raise HTTPException(404, f"Unknown repository {repo_id!r}")
        version = version or demo.versions[-1]
        if version not in demo.agents:
            raise HTTPException(400, f"Version {version!r} is not built for {repo_id}; "
                                     f"available: {', '.join(demo.versions)}")
        return demo, version, demo.agents[version]

    def limit(request, bucket, per_hour, user=None, user_per_hour=None):
        """Per-IP limit on `bucket`; with `user`, also a per-user one (user_per_hour, default the same)."""
        keys = [(f"{bucket}:{client_ip(request, settings.trusted_proxy_hops)}", per_hour)]
        if user is not None:
            keys.append((f"{bucket}:user:{user.id}", user_per_hour or per_hour))
        for key, n in keys:
            allowed, retry = limiter.hit(key, n, HOUR)
            if not allowed:
                raise HTTPException(429, f"Too many requests; try again in {retry:.0f}s",
                                    headers={"Retry-After": str(int(retry))})

    def allow_index(request, user):
        """allow_new for the indexer: the per-IP and per-user hourly limits on new repos."""
        ip = client_ip(request, settings.trusted_proxy_hops)

        def allow():
            by_ip = limiter.hit(f"index:{ip}", settings.index_per_ip_per_hour, HOUR)
            if not by_ip[0]:
                return by_ip
            return limiter.hit(f"index:user:{user.id}", settings.index_per_user_per_hour, HOUR)
        return allow

    def with_links(demo, agent, event, body):
        """Add GitHub links (and code if asked) to sources; drop the raw context unless asked."""
        if event["type"] != "sources":
            return event
        event = dict(event)
        sources = []
        for s in event["sources"]:
            s = {**s, "url": demo.source_url(s["file"], s["start_line"], s["end_line"])}
            if body.include_code:
                s["code"] = agent.chunks[s["chunk_id"]][:CODE_CHARS]
            sources.append(s)
        event["sources"] = sources
        if not body.include_context:
            event.pop("context", None)
        return event

    # ── endpoints ────────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def health():
        defaults = get_config()
        return {"ok": True, "demos": sorted(demos),
                "llm": {"server": getattr(llm_factory, "server_available", True),
                        "provider": settings.server_llm_provider,
                        "models": list(settings.server_llm_models)},
                "defaults": {"version": defaults.name, "top_k": defaults.top_k,
                             "max_hops": defaults.max_hops},
                "indexing": {"max_py_files": settings.max_py_files,
                             "max_py_mb": settings.max_py_mb,
                             "skipped_dirs": list(settings.index_ignore_dirs),
                             "per_hour": settings.index_per_ip_per_hour,
                             "default_version": settings.default_version},
                "auth": {"github": auth.enabled}, "live_check": settings.live_check}

    expected_demos = sorted(d for d in (os.listdir(settings.demos_path) if os.path.isdir(settings.demos_path) else [])
                            if os.path.isfile(os.path.join(settings.demos_path, d, "demo.json")))

    @app.get("/api/ready")
    def ready():
        """200 when the database answers and every demo in CODERAG_DEMOS_DIR is loaded, else 503."""
        checks = {}
        try:
            db.ping()
            checks["database"] = "ok"
        except Exception as e:                        # noqa: BLE001
            checks["database"] = f"unavailable ({type(e).__name__})"
        not_loaded = [d for d in expected_demos if d not in demos]
        checks["demos"] = "ok" if not not_loaded else f"not loaded: {', '.join(not_loaded)}"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": ok, "checks": checks, "demos": sorted(demos)}, status_code=200 if ok else 503)

    @app.get("/api/demos")
    def list_demos():
        return [d.info() for d in demos.values()]

    @app.post("/api/repos")
    def index_repo(body: IndexRequest, request: Request):
        """Index a GitHub repo (202 + job), or 200 if this commit is already indexed. Signed in only."""
        user = auth.current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in with GitHub to add a repository.")
        limit(request, "submit", settings.submit_per_ip_per_hour, user, settings.submit_per_user_per_hour)
        try:
            job, queued = indexer.submit(body.url, allow_new=allow_index(request, user),
                                         token=auth.token(user), owner_id=user.id)
        except FetchError as e:
            raise HTTPException(400, str(e))
        except NotAllowed as e:
            raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.retry_after))})
        except QueueFull as e:
            raise HTTPException(503, str(e), headers={"Retry-After": "60"})
        auth.users.add_repo(user.id, job.repo_id)
        return JSONResponse(job.public(indexer.position(job)), status_code=202 if queued else 200)

    @app.get("/api/repos/mine")
    def my_repos(request: Request):
        """Repos the signed-in user added (newest first) that are indexed."""
        user = auth.current_user(request)
        if user is None:
            raise HTTPException(401, "Not signed in")
        found = (repos.summary(repo_id) for repo_id in auth.users.repo_ids(user.id))
        return [info for info in found if info is not None]

    def signed_in(request):
        user = auth.current_user(request)
        if user is None:
            raise HTTPException(401, "Not signed in")
        return user

    @app.get("/api/github/repos")
    def github_repos(request: Request):
        """
        The signed-in user's GitHub repos, newest push first (up to 300):
        [{full_name, private, language, pushed_at, description, default_branch, indexed, repo_id}].
        `indexed`: the user already added it (repo_id: the newest such index, to open it).
        401 {"error": "reauth"} when GitHub no longer accepts their token.
        """
        user = signed_in(request)
        limit(request, "github-repos", settings.retrieve_per_ip_per_hour)
        try:
            listed = auth.github_repos(user)
        except GitHubReauth:
            raise HTTPException(401, "reauth")
        except (GitHubError, httpx.HTTPError, ValueError) as e:
            log.warning("listing GitHub repos failed: %s", e if isinstance(e, GitHubError) else type(e).__name__)
            raise HTTPException(502, "Couldn't load your repositories from GitHub. You can still paste a URL.")
        added = {}
        for repo_id in auth.users.repo_ids(user.id):           # newest first: keep the first
            added.setdefault(repo_name_of(repo_id), repo_id)
        out = []
        for r in listed:
            repo_id = added.get(str(r["full_name"]).lower())
            out.append({**r, "indexed": repo_id is not None, "repo_id": repo_id})
        return out

    @app.put("/api/repos/mine/{repo_id}")
    def keep_repo(repo_id: str, request: Request):
        """Add a visitor repo the user can open (e.g. from a shared link) to their list."""
        user = signed_in(request)
        info = repos.summary(repo_id) if can_open(repo_id, request) else None
        if info is None or info.get("kind") != "user":
            raise HTTPException(404, f"Unknown repository {repo_id!r}")
        auth.users.add_repo(user.id, repo_id)
        return info

    def unlink(user_id, repo_id):
        """Remove a user's link; delete the repo's index when no user has it (never a demo)."""
        auth.users.remove_repo(user_id, repo_id)
        if repo_id not in repos.demos and auth.users.users_of(repo_id) == 0 and repos.exists(repo_id):
            return repos.remove(repo_id)
        return False

    @app.delete("/api/repos/{repo_id}/mine")
    def remove_mine(repo_id: str, request: Request):
        """Take a repo off the user's list; when nobody has it any more its index files are deleted."""
        user = signed_in(request)
        if repo_id in repos.demos:
            raise HTTPException(400, "Demos aren't in anyone's list.")
        return {"repo_id": repo_id, "index_deleted": unlink(user.id, repo_id)}

    @app.delete("/api/repos/mine/{repo_id}", status_code=204)
    def forget_repo(repo_id: str, request: Request):
        """The same as DELETE /api/repos/{repo_id}/mine (older path, kept for links in the wild)."""
        unlink(signed_in(request).id, repo_id)
        return Response(status_code=204)

    @app.get("/api/jobs")
    def my_jobs(request: Request):
        """The signed-in user's indexing jobs, newest first (status, progress 0-1, message)."""
        return indexer.for_user(signed_in(request).id)

    @app.delete("/api/me")
    def delete_me(request: Request):
        """Delete the account: the user, their encrypted token, links, history, feedback and shares."""
        user = signed_in(request)
        auth.revoke_grant(user)                       # best effort, before the token is deleted
        had = auth.users.delete(user.id)              # user, token, links, questions, shares, feedback, jobs
        for repo_id in had:
            if repo_id not in repos.demos and auth.users.users_of(repo_id) == 0 and repos.exists(repo_id):
                repos.remove(repo_id)
        gone = set(had)
        cache.drop(lambda key: key[0] in gone)        # cached answers about their repos
        auth.forget(user.id)
        for job in list(indexer.jobs.values()):       # in-memory copies of their jobs
            if job.owner_id == user.id and job.status in ("ready", "failed"):
                indexer.jobs.pop(job.id, None)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, request: Request):
        """One indexing job, for the user who started it (anyone else: 404)."""
        user = auth.current_user(request)
        job = indexer.get(job_id)
        if job is None or user is None or job.owner_id != user.id:
            raise HTTPException(404, "Unknown job")
        return job.public(indexer.position(job))

    @app.get("/api/repos/{repo_id}")
    def repo_info(repo_id: str, request: Request):
        try:
            demo, _, _ = resolve(repo_id, None, request)
        except Rebuilding as e:                       # files missing: 202 while the rebuild runs
            info = repos.summary(repo_id) or {"id": repo_id}
            return JSONResponse({**info, "rebuilding": True, "job": e.job.public(e.position)}, status_code=202)
        info = demo.info()
        if demo.kind == "user" and is_older(demo.built_with(), settings.default_version):
            info["reindex_to"] = settings.default_version         # the UI offers "Re-index with v10"
        return info

    @app.post("/api/repos/{repo_id}/live")
    def live_check(repo_id: str, request: Request):
        """
        HEAD the repo's homepage (or latest deployment / Pages URL) from the server:
        status and response time, cached 10 min. https and public addresses only.
        """
        if not settings.live_check:                       # CODERAG_LIVE_CHECK off: no such endpoint
            raise HTTPException(404, "Not found")
        demo, _, _ = resolve(repo_id, None, request)
        urls = candidate_urls(demo.repo_facts())
        if not urls:
            raise HTTPException(404, "No homepage or deployment URL is recorded for this repository.")
        limit(request, "live", 30)
        return live.check(urls[0])

    @app.post("/api/repos/{repo_id}/reindex")
    def reindex_repo(repo_id: str, request: Request):
        """Rebuild a visitor repo with the default version (202 + job)."""
        user = signed_in(request)
        try:
            demo, _, _ = resolve(repo_id, None, request)
        except Rebuilding as e:                       # files missing: the rebuild is the re-index
            auth.users.add_repo(user.id, repo_id)
            return JSONResponse(e.job.public(e.position), status_code=202)
        if demo.kind != "user":
            raise HTTPException(404, f"Unknown repository {repo_id!r}")
        limit(request, "submit", settings.submit_per_ip_per_hour, user, settings.submit_per_user_per_hour)
        try:
            job, queued = indexer.reindex(repo_id, allow_new=allow_index(request, user),
                                          token=auth.token(user), owner_id=user.id)
        except KeyError:
            raise HTTPException(404, f"Unknown repository {repo_id!r}")
        except NotAllowed as e:
            raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.retry_after))})
        except QueueFull as e:
            raise HTTPException(503, str(e), headers={"Retry-After": "60"})
        auth.users.add_repo(user.id, repo_id)
        return JSONResponse(job.public(indexer.position(job)), status_code=202 if queued else 200)

    @app.get("/api/repos/{repo_id}/graph")
    def graph(repo_id: str, request: Request, version: str | None = None):
        demo, version, agent = resolve(repo_id, version, request)
        return graphs.get((repo_id, version, id(agent)), lambda: graph_data(agent))   # LRU; a re-index: new key

    @app.get("/api/repos/{repo_id}/file")
    def source_file(repo_id: str, path: str, request: Request, version: str | None = None):
        demo, version, agent = resolve(repo_id, version, request)
        limit(request, "file", settings.retrieve_per_ip_per_hour)
        if len(path) > 500:
            raise HTTPException(404, "Not an indexed file")
        base = (repos.directory(repo_id) if demo.kind == "user"
                else os.path.join(settings.demos_path, repo_id))
        try:
            text, origin = read_source(demo, agent, path, os.path.join(base, "sources"),
                                       fetch=fetch_source)
        except KeyError:
            raise HTTPException(404, "Not an indexed file")
        except SourceUnavailable as e:
            raise HTTPException(502, str(e))
        url = demo.source_url(path, 1, 1)
        return {"path": path, "text": text, "origin": origin,
                "url": url.split("#")[0] if url else None}

    @app.post("/api/repos/{repo_id}/retrieve")
    def retrieve(repo_id: str, body: RetrieveRequest, request: Request):
        demo, version, agent = resolve(repo_id, body.version, request)
        limit(request, "retrieve", settings.retrieve_per_ip_per_hour)
        first = next(agent.stream(body.question, llm=None, debug=body.debug, **body.overrides()))
        return {"version": version,
                **with_links(demo, agent, first, body)}

    @app.post("/api/repos/{repo_id}/ask")
    def ask(repo_id: str, body: AskRequest, request: Request):
        demo, version, agent = resolve(repo_id, body.version, request)
        own_key = body.llm.api_key is not None and body.llm.provider != "server"
        if body.llm.provider == "none":
            limit(request, "retrieve", settings.retrieve_per_ip_per_hour)
        elif own_key:
            limit(request, "ask-own", settings.own_key_ask_per_ip_per_hour)
        else:
            limit(request, "ask", settings.ask_per_ip_per_hour)

        overrides = {**body.overrides(), "prompt_style": body.prompt_style}
        user = auth.current_user(request)                 # signed in: the question is kept (history)
        release = asking.try_enter()
        if release is None:
            raise HTTPException(503, "Many answers are being written right now; try again in a few seconds.",
                                headers={"Retry-After": "5"})

        def keep(sources_event, answer, done, server_key=False):
            """Store the answered question for its (signed-in) user; returns its id."""
            if done is None:
                return None
            if user is None:
                if server_key:
                    usage.add_anonymous()              # not stored, but it counts against the daily cap
                return None
            linked = with_links(demo, agent, sources_event, body)
            latency = (sources_event.get("retrieve_ms") or 0) + (done.get("llm_ms") or 0)
            return history.record(db, repo_id, user.id, body.question, answer, linked["sources"],
                                  done.get("backend", ""), latency, done.get("cached", False),
                                  version=version, chunks=agent.chunks, server_key=server_key)

        def guarded(events):
            """The stream, releasing the concurrent-answer slot however it ends."""
            try:
                yield from events
            finally:
                release()
        cache_key = (repo_id, version, " ".join(body.question.lower().split()),
                     tuple(sorted(overrides.items())), body.llm.provider,
                     body.llm.model or "", body.debug)          # never the key itself
        cached = cache.get(cache_key) if body.llm.provider != "none" else None

        def replay():
            answer = "".join(e["text"] for e in cached if e["type"] == "token")
            for event in cached:
                if event["type"] == "done":
                    qid = keep(cached[0], answer, event)
                    event = {**event, "question_id": qid} if qid else event
                yield _sse(with_links(demo, agent, event, body))

        if cached is not None:
            return StreamingResponse(guarded(replay()), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache"}, background=BackgroundTask(release))

        try:
            llm = llm_factory(body.llm)
            quota_hit = False
            if llm is not None and not own_key:
                quota_hit = not usage.allowed(settings.server_daily_cap)
                if quota_hit:
                    llm = None
        except BaseException:
            release()
            raise
        server_key = llm is not None and not own_key
        deadline = time.monotonic() + settings.ask_timeout

        def events():
            collected, text = [], []
            stream = agent.stream(body.question, llm=llm, debug=body.debug, **overrides)
            for event in stream:
                if event["type"] not in ("sources", "done") and time.monotonic() > deadline:
                    stream.close()                    # stops the provider's stream too
                    yield _sse({"type": "error", "code": "timeout",
                                "message": f"The answer took longer than {settings.ask_timeout} s and was "
                                           "stopped. Try a narrower question, or read the sources."})
                    return
                if event["type"] == "error" and event["code"] == "no_llm" and quota_hit:
                    event = {"type": "error", "code": "server_quota",
                             "message": "Today's free answers are used up. Add your own API "
                                        "key, or read the retrieved sources."}
                elif event["type"] == "error" and event["code"] == "no_llm":
                    event = {**event, "message": "No LLM available. Add your own API key, "
                                                 "or read the retrieved sources."}
                collected.append(event)
                if event["type"] == "token":
                    text.append(event["text"])
                if event["type"] == "done":
                    qid = keep(collected[0], "".join(text), event, server_key=server_key)
                    event = {**event, "question_id": qid} if qid else event
                yield _sse(with_links(demo, agent, event, body))
            if collected and collected[-1]["type"] == "done":
                # store one token event with the whole answer; replays are instant
                cache.put(cache_key, [collected[0], {"type": "token", "text": "".join(text)},
                                      {**collected[-1], "cached": True}])

        return StreamingResponse(guarded(events()), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                                 background=BackgroundTask(release))

    history.add_routes(app, db, auth, repos, can_open, limit,
                       FRONTEND_DIR if os.path.isdir(FRONTEND_DIR) else None)

    @app.exception_handler(HTTPException)
    def http_error(request, exc):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(Rebuilding)
    def rebuilding(request, exc):
        return JSONResponse({"error": "Rebuilding index…", "rebuilding": True,
                             "job": exc.job.public(exc.position)}, status_code=409,
                            headers={"Retry-After": "5"})

    if os.path.isdir(FRONTEND_DIR):
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        def page(name, request):
            """The page, or 304 when the browser's copy (If-None-Match) is current."""
            path = os.path.join(FRONTEND_DIR, name)
            response = FileResponse(path, stat_result=os.stat(path))
            etag = response.headers.get("etag")
            if etag and etag in request.headers.get("if-none-match", ""):
                return Response(status_code=304, headers={"ETag": etag})
            return response

        # pages: / is the home page (index.html, served by the mount), the tool lives at /app
        @app.get("/app", include_in_schema=False)
        def app_page(request: Request):
            return page("app.html", request)

        @app.get("/login", include_in_schema=False)
        def login_page(request: Request):
            return page("login.html", request)

        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    else:
        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/docs")

    return app


def __getattr__(name):
    # `uvicorn coderag.web.app:app` builds the app on first access, not on import
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
