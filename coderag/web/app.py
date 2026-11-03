"""
FastAPI server for the pre-indexed demo repositories.

    GET  /api/health
    GET  /api/demos
    GET  /api/repos/{repo_id}
    GET  /api/repos/{repo_id}/graph?version=v7
    POST /api/repos/{repo_id}/retrieve      JSON: sources (+ context), no LLM
    POST /api/repos/{repo_id}/ask           Server-Sent Events: sources, token*, done | error

Run:  uvicorn coderag.web.app:app --port 7860
      (build the demos first: python scripts/build_demos.py)

Visitors may send their own provider key with a question (llm.api_key). It is
used for that request only and never logged, cached or returned.
"""

import json
import logging
import os
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr

from ..config import get_config
from ..gitfetch import FetchError
from ..llm import DEFAULT_REMOTE_MODELS, FallbackChain, LLMUnavailable, RemoteLLM
from ..visualize import graph_data
from .jobs import Indexer, NotAllowed, QueueFull
from .limits import AnswerCache, RateLimiter, client_ip
from .registry import Repos, load_demos
from .settings import Settings

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
def _sse(event):
    data = {k: v for k, v in event.items() if k != "type"}
    return f"event: {event['type']}\ndata: {json.dumps(data)}\n\n"


def create_app(settings=None, demos=None, llm_factory=None, limiter=None, embedder=None,
               indexer=None):
    """
    settings    : Settings (default: from CODERAG_* env vars)
    demos       : {id: Demo} (default: loaded from settings.data_dir)
    llm_factory : callable(LLMChoice) -> backend or None (default: LLMFactory)
    embedder    : embedding model shared by all indexes (default: loaded, behind a lock)
    indexer     : jobs.Indexer for visitors' repos (default: built from the above)
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
    cache = AnswerCache(settings.answer_cache_size)
    graphs = {}

    app = FastAPI(title="coderag", version="0.1.0",
                  description="Ask questions about a Python codebase, with cited sources.")
    app.state.repos, app.state.indexer = repos, indexer

    def resolve(repo_id, version):
        demo = repos.get(repo_id)
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
                             "per_hour": settings.index_per_ip_per_hour}}

    @app.get("/api/demos")
    def list_demos():
        return [d.info() for d in demos.values()]

    @app.post("/api/repos")
    def index_repo(body: IndexRequest, request: Request):
        """Index a public GitHub repo (202 + job), or 200 if this commit is already indexed."""
        limit(request, "submit", settings.submit_per_ip_per_hour)
        ip = client_ip(request, settings.trusted_proxy_hops)
        try:
            job, queued = indexer.submit(
                body.url, allow_new=lambda: limiter.hit(f"index:{ip}",
                                                        settings.index_per_ip_per_hour, HOUR))
        except FetchError as e:
            raise HTTPException(400, str(e))
        except NotAllowed as e:
            raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.retry_after))})
        except QueueFull as e:
            raise HTTPException(503, str(e), headers={"Retry-After": "60"})
        return JSONResponse(job.public(indexer.position(job)), status_code=202 if queued else 200)

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = indexer.get(job_id)
        if job is None:
            raise HTTPException(404, "Unknown job")
        return job.public(indexer.position(job))

    @app.get("/api/repos/{repo_id}")
    def repo_info(repo_id: str):
        demo, _, _ = resolve(repo_id, None)
        return demo.info()

    @app.get("/api/repos/{repo_id}/graph")
    def graph(repo_id: str, version: str | None = None):
        demo, version, agent = resolve(repo_id, version)
        key = (repo_id, version)
        if key not in graphs:
            graphs[key] = graph_data(agent)
        return graphs[key]

    @app.post("/api/repos/{repo_id}/retrieve")
    def retrieve(repo_id: str, body: RetrieveRequest, request: Request):
        demo, version, agent = resolve(repo_id, body.version)
        limit(request, "retrieve", settings.retrieve_per_ip_per_hour)
        first = next(agent.stream(body.question, llm=None, debug=body.debug, **body.overrides()))
        return {"version": version,
                **with_links(demo, agent, first, body)}

    @app.post("/api/repos/{repo_id}/ask")
    def ask(repo_id: str, body: AskRequest, request: Request):
        demo, version, agent = resolve(repo_id, body.version)
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
        from fastapi.staticfiles import StaticFiles
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
