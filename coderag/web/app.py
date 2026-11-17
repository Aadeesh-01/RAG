"""
FastAPI server for the pre-indexed demo repositories.

    GET  /  /app  /login                     home page, the app, sign-in (frontend/)
    GET  /api/health
    GET  /api/demos
    GET  /api/auth/github, /api/auth/github/callback, /api/auth/me; POST /api/auth/logout  (auth.py)
    POST /api/repos                         index a GitHub repo (signed in) with CODERAG_DEFAULT_VERSION
    POST /api/repos/{repo_id}/reindex       rebuild it with the default version (signed in)
    GET  /api/repos/mine                    repos the signed-in user added (their repo tabs)
    PUT  /api/repos/mine/{repo_id}          add an indexed repo they can open to that list
    DELETE /api/repos/mine/{repo_id}        take it off the list (the index stays)
    GET  /api/github/repos                  the user's GitHub repos, for the add-repo picker
    GET  /api/repos/{repo_id}
    GET  /api/repos/{repo_id}/graph?version=v7
    GET  /api/repos/{repo_id}/file?path=pkg/mod.py    whole indexed file, for the code viewer
    POST /api/repos/{repo_id}/retrieve      JSON: sources (+ context), no LLM
    POST /api/repos/{repo_id}/ask           Server-Sent Events: sources, token*, done | error

Run:  uvicorn coderag.web.app:app --port 7860
      (build the demos first: python scripts/build_demos.py)

Visitors may send their own provider key with a question (llm.api_key). It is
used for that request only and never logged, cached or returned.

Demos are public. Adding a repo needs a GitHub sign-in; a private repo can be
opened only by the users who added it (anyone else gets 404).
"""

import json
import logging
import os
from typing import Literal

import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from starlette.datastructures import MutableHeaders

from ..config import get_config
from ..gitfetch import FetchError
from ..llm import DEFAULT_REMOTE_MODELS, FallbackChain, LLMUnavailable, RemoteLLM
from ..visualize import graph_data
from .auth import Auth, GitHubError, GitHubReauth
from .jobs import Indexer, NotAllowed, QueueFull
from .limits import AnswerCache, RateLimiter, client_ip
from .live import LiveChecker, candidate_urls
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


def _sse(event):
    data = {k: v for k, v in event.items() if k != "type"}
    return f"event: {event['type']}\ndata: {json.dumps(data)}\n\n"


def create_app(settings=None, demos=None, llm_factory=None, limiter=None, embedder=None,
               indexer=None, fetch_source=fetch_url, github=None, live=None):
    """
    settings    : Settings (default: from CODERAG_* env vars)
    demos       : {id: Demo} (default: loaded from settings.data_dir)
    llm_factory : callable(LLMChoice) -> backend or None (default: LLMFactory)
    embedder    : embedding model shared by all indexes (default: loaded, behind a lock)
    indexer     : jobs.Indexer for visitors' repos (default: built from the above)
    fetch_source: callable(url) -> text, for files not saved locally (default: GitHub raw)
    github      : GitHub OAuth client (default: auth.GitHubOAuth from the settings)
    live        : web.live.LiveChecker for "Check if live" (default: real HEAD requests)
    """
    settings = settings or Settings.from_env()
    if embedder is None:
        from ..index import SharedEncoder, _load_model
        embedder = SharedEncoder(_load_model(get_config().embed_model))
    if demos is None:
        demos = load_demos(settings.data_dir, embedder=embedder)
    repos = Repos(settings.data_dir, demos, embedder, settings.max_loaded_repos,
                  settings.max_stored_repos)
    indexer = indexer or Indexer(settings, repos, embedder)
    llm_factory = llm_factory or LLMFactory(settings)
    limiter = limiter or RateLimiter()
    auth = Auth(settings, github)
    live = live or LiveChecker()
    cache = AnswerCache(settings.answer_cache_size)
    graphs = {}

    app = FastAPI(title="coderag", version="0.1.0",
                  description="Ask questions about a codebase, with cited sources.")
    app.state.repos, app.state.indexer, app.state.auth = repos, indexer, auth
    app.add_middleware(RevalidateFrontend)
    app.include_router(auth.router())

    def can_open(repo_id, request):
        """Private repos only for users who added them; everything else for everyone."""
        if not repos.private(repo_id):
            return True
        user = auth.current_user(request)
        return user is not None and auth.users.has_repo(user.id, repo_id)

    def resolve(repo_id, version, request):
        demo = repos.get(repo_id) if can_open(repo_id, request) else None
        if demo is None:
            raise HTTPException(404, f"Unknown repository {repo_id!r}")
        version = version or demo.versions[-1]
        if version not in demo.agents:
            raise HTTPException(400, f"Version {version!r} is not built for {repo_id}; "
                                     f"available: {', '.join(demo.versions)}")
        return demo, version, demo.agents[version]

    def limit(request, bucket, per_hour):
        allowed, retry = limiter.hit(f"{bucket}:{client_ip(request, settings.trusted_proxy_hops)}",
                                     per_hour, HOUR)
        if not allowed:
            raise HTTPException(429, f"Too many requests; try again in {retry:.0f}s",
                                headers={"Retry-After": str(int(retry))})

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
    def health():
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

    @app.get("/api/demos")
    def list_demos():
        return [d.info() for d in demos.values()]

    @app.post("/api/repos")
    def index_repo(body: IndexRequest, request: Request):
        """Index a GitHub repo (202 + job), or 200 if this commit is already indexed. Signed in only."""
        user = auth.current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in with GitHub to add a repository.")
        limit(request, "submit", settings.submit_per_ip_per_hour)
        ip = client_ip(request, settings.trusted_proxy_hops)
        try:
            job, queued = indexer.submit(
                body.url, allow_new=lambda: limiter.hit(f"index:{ip}",
                                                        settings.index_per_ip_per_hour, HOUR),
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

    @app.delete("/api/repos/mine/{repo_id}", status_code=204)
    def forget_repo(repo_id: str, request: Request):
        """Take a repo off the user's list. A private one can then be opened again only by re-adding it."""
        auth.users.remove_repo(signed_in(request).id, repo_id)
        return Response(status_code=204)

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = indexer.get(job_id)
        if job is None:
            raise HTTPException(404, "Unknown job")
        return job.public(indexer.position(job))

    @app.get("/api/repos/{repo_id}")
    def repo_info(repo_id: str, request: Request):
        demo, _, _ = resolve(repo_id, None, request)
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
        demo, _, _ = resolve(repo_id, None, request)
        if demo.kind != "user":
            raise HTTPException(404, f"Unknown repository {repo_id!r}")
        limit(request, "submit", settings.submit_per_ip_per_hour)
        ip = client_ip(request, settings.trusted_proxy_hops)
        try:
            job, queued = indexer.reindex(
                repo_id, allow_new=lambda: limiter.hit(f"index:{ip}", settings.index_per_ip_per_hour, HOUR),
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
        key = (repo_id, version, id(agent))               # a re-index brings a new agent
        if key not in graphs:
            graphs[key] = graph_data(agent)
        return graphs[key]

    @app.get("/api/repos/{repo_id}/file")
    def source_file(repo_id: str, path: str, request: Request, version: str | None = None):
        demo, version, agent = resolve(repo_id, version, request)
        limit(request, "file", settings.retrieve_per_ip_per_hour)
        if len(path) > 500:
            raise HTTPException(404, "Not an indexed file")
        base = (repos.directory(repo_id) if demo.kind == "user"
                else os.path.join(settings.data_dir, "demos", repo_id))
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
        cache_key = (repo_id, version, " ".join(body.question.lower().split()),
                     tuple(sorted(overrides.items())), body.llm.provider,
                     body.llm.model or "", body.debug)          # never the key itself
        cached = cache.get(cache_key) if body.llm.provider != "none" else None

        def replay():
            for event in cached:
                yield _sse(with_links(demo, agent, event, body))

        if cached is not None:
            return StreamingResponse(replay(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache"})

        llm = llm_factory(body.llm)
        quota_hit = False
        if llm is not None and not own_key:
            quota_hit = not limiter.hit("server-daily", settings.server_daily_cap, DAY)[0]
            if quota_hit:
                llm = None

        def events():
            collected, text = [], []
            for event in agent.stream(body.question, llm=llm, debug=body.debug, **overrides):
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
                yield _sse(with_links(demo, agent, event, body))
            if collected and collected[-1]["type"] == "done":
                # store one token event with the whole answer; replays are instant
                cache.put(cache_key, [collected[0], {"type": "token", "text": "".join(text)},
                                      {**collected[-1], "cached": True}])

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.exception_handler(HTTPException)
    def http_error(request, exc):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

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
