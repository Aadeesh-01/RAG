"""
New commits on a repo's branch, "Update to latest" and "Rebuild index".

  GET  /api/repos/{id}/freshness   {indexed_commit, latest_commit, behind, new_commits}: the newest
                                   commit of the indexed branch (else the default branch), with git
                                   ls-remote, cached CACHE_SECONDS per repo; new_commits is how many
                                   commits it is behind (GitHub's compare API), or null if unknown
  POST /api/repos/{id}/update      index the branch's newest commit as a new index (a new repo id).
                                   The old index stays usable until the new one is ready; then the
                                   user's link moves to it. Their questions keep pointing at the old
                                   one (its own version and snippets) and the history shows them for
                                   both (history.py looks a repo up by owner/name)
  POST /api/repos/{id}/rebuild     rebuild the index at the same commit with the same version:
                                   re-fetch, re-chunk, re-embed. Rate-limited like adding a repo

All three: signed in, and the repo in the user's list (anyone else gets 404, as if it didn't
exist). Private repos use the user's GitHub token.
"""

import logging
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..gitfetch import FetchError
from .jobs import NeedsToken, NotAllowed, QueueFull

log = logging.getLogger("coderag.web")
CACHE_SECONDS = 600


def github_compare(owner, repo, base, head, token=None, timeout=8):
    """How many commits `head` is ahead of `base` (GitHub's compare API), or None."""
    import httpx
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "coderag"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.get(f"https://api.github.com/repos/{owner}/{repo}/compare/{base}...{head}",
                      params={"per_page": 1}, headers=headers, timeout=timeout)
        return int(r.json()["ahead_by"]) if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


class Freshness:
    """latest(owner, repo, ref, token) -> sha; count(owner, repo, base, head, token) -> int | None."""

    def __init__(self, latest, count=github_compare, ttl=CACHE_SECONDS, clock=time.monotonic):
        self.latest, self.count, self.ttl, self.clock = latest, count, ttl, clock
        self.cache = {}
        self.lock = threading.Lock()

    def check(self, repo_id, owner, repo, ref, indexed, token=None):
        with self.lock:
            hit = self.cache.get(repo_id)
            if hit and self.clock() - hit[0] < self.ttl:
                return dict(hit[1])
        latest = self.latest(owner, repo, ref or None, token)          # FetchError when GitHub can't say
        behind = bool(latest) and latest != indexed
        new = self.count(owner, repo, indexed, latest, token) if behind and self.count else None
        result = {"indexed_commit": indexed, "latest_commit": latest, "behind": behind,
                  "new_commits": new if behind else 0}
        with self.lock:
            self.cache[repo_id] = (self.clock(), result)
        return dict(result)

    def forget(self, repo_id):
        with self.lock:
            self.cache.pop(repo_id, None)


def add_routes(app, auth, repos, indexer, limit, allow_index, settings, freshness):
    """Register the freshness, update and rebuild endpoints on `app`."""
    moves = {}                                    # new repo id -> {(user id, old repo id)} once it is ready
    ended = {}                                    # job id -> "ready"/"failed": jobs whose listeners already ran
    moves_lock = threading.Lock()

    def move_links(job, status):                   # runs before the job shows "ready" to anyone polling it
        with moves_lock:
            waiting = moves.pop(job.repo_id, set())
            ended[job.id] = status
            while len(ended) > 1000:
                ended.pop(next(iter(ended)))
        if status != "ready":
            return                                 # failed: everyone keeps the index they had
        for user_id, old in waiting:
            if auth.users.move_repo(user_id, old, job.repo_id):
                log.info("updated %s: a user's link moved to the new index", job.name)
            freshness.forget(old)
    indexer.listeners.append(move_links)

    def mine(request, repo_id):
        user = auth.current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in with GitHub to keep repositories up to date.")
        info = repos.summary(repo_id)
        if info is None or info.get("kind") != "user" or not auth.users.has_repo(user.id, repo_id):
            raise HTTPException(404, f"Unknown repository {repo_id!r}")
        return user, info

    def owner_repo(info):
        owner, _, name = info["name"].partition("/")
        return owner, name

    def queued_response(job, queued, **extra):
        return JSONResponse({**job.public(indexer.position(job)), **extra}, status_code=202 if queued else 200)

    @app.get("/api/repos/{repo_id}/freshness")
    def repo_freshness(repo_id: str, request: Request):
        user, info = mine(request, repo_id)
        limit(request, "freshness", settings.retrieve_per_ip_per_hour)
        owner, name = owner_repo(info)
        try:
            return freshness.check(repo_id, owner, name, info.get("ref") or None, info.get("sha") or "",
                                   auth.token(user))
        except FetchError:
            raise HTTPException(502, "Couldn't reach GitHub to check for new commits.")

    @app.post("/api/repos/{repo_id}/update")
    def update_repo(repo_id: str, request: Request):
        user, info = mine(request, repo_id)
        limit(request, "submit", settings.submit_per_ip_per_hour, user, settings.submit_per_user_per_hour)
        url = info["url"] + (f"/tree/{info['ref']}" if info.get("ref") else "")
        try:
            job, queued = indexer.submit(url, allow_new=allow_index(request, user), token=auth.token(user),
                                         owner_id=user.id)
        except FetchError as e:
            raise HTTPException(502, str(e))
        except NotAllowed as e:
            raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.retry_after))})
        except QueueFull as e:
            raise HTTPException(503, str(e), headers={"Retry-After": "60"})
        freshness.forget(repo_id)
        if job.repo_id == repo_id:                 # the branch hasn't moved
            return {"up_to_date": True, "repo_id": repo_id, "status": "ready"}
        if job.status == "ready":                  # that commit is indexed already (someone else updated)
            auth.users.move_repo(user.id, repo_id, job.repo_id)
        else:
            # the job may have finished since submit(): register the move, or make it now, under one lock
            with moves_lock:
                status = ended.get(job.id)
                if status is None:
                    moves.setdefault(job.repo_id, set()).add((user.id, repo_id))
            if status == "ready":
                auth.users.move_repo(user.id, repo_id, job.repo_id)
                freshness.forget(repo_id)
        return queued_response(job, queued, replaces=repo_id)

    @app.post("/api/repos/{repo_id}/rebuild")
    def rebuild_repo(repo_id: str, request: Request):
        user, _ = mine(request, repo_id)
        limit(request, "submit", settings.submit_per_ip_per_hour, user, settings.submit_per_user_per_hour)
        try:
            job, queued = indexer.rebuild_index(repo_id, allow_new=allow_index(request, user),
                                                token=auth.token(user), owner_id=user.id)
        except KeyError:
            raise HTTPException(404, f"Unknown repository {repo_id!r}")
        except NeedsToken:
            raise HTTPException(409, "Sign in with GitHub again to rebuild this private repository.")
        except NotAllowed as e:
            raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.retry_after))})
        except QueueFull as e:
            raise HTTPException(503, str(e), headers={"Retry-After": "60"})
        return queued_response(job, queued)
