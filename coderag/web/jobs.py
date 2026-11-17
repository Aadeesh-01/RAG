"""
Background indexing of visitors' GitHub repositories.

One worker thread processes a bounded queue, one repo at a time (free CPU):

    queued -> fetching -> parsing -> embedding -> saving -> ready | failed

Only source files are downloaded (gitfetch python_only: *.py and the languages in
languages.py), nothing from the repo
is executed (the chunker only calls ast.parse), symlinks are not followed,
and the clone is deleted once the index is saved.

A signed-in visitor's GitHub token is used only when the repository can't be
seen without it; such a repo is marked private (only people who added it can
open it), its indexed files are saved beside the index (raw.githubusercontent
needs a token), and the token is dropped from the job once it finishes.
"""

import logging
import os
import queue
import shutil
from collections import Counter
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..agent import RAGCodebaseAgent
from ..config import get_config
from ..gitfetch import SHA, FetchError, fetch_commit, parse_github_url, resolve_sha, rmtree
from ..config import VERSIONS
from ..languages import language_for
from .registry import Demo, repo_id_for, repo_stats, write_repo_meta
from .sources import indexed_files

log = logging.getLogger("coderag.web.jobs")


class IndexFailure(Exception):
    """Indexing failed for a reason the visitor should see."""


class QueueFull(Exception):
    pass


class NotAllowed(Exception):
    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class IndexJob:
    id: str
    repo_id: str
    name: str                        # owner/repo
    url: str
    sha: str
    ref: str = ""
    status: str = "queued"           # queued | fetching | parsing | embedding | saving | ready | failed
    progress: float = 0.0
    message: str = "Waiting for a free indexing slot…"
    error: str = ""
    created: float = field(default_factory=time.time)
    owner_id: int | None = None      # user who asked for it
    private: bool = False            # only visible with that user's token
    token: str = field(default="", repr=False)      # never in public() or logs; cleared when done
    reindex: bool = False            # rebuild an indexed repo with the current default version

    def public(self, queue_position=0):
        return {"job_id": self.id, "repo_id": self.repo_id, "name": self.name, "sha": self.sha,
                "status": self.status, "progress": round(self.progress, 3),
                "message": self.message, "error": self.error, "queue_position": queue_position}


def source_language(name):
    """'python', 'go', ... for an indexable file name, else None."""
    if name.endswith(".py"):
        return "python"
    lang = language_for(name)
    return lang.name if lang else None


def check_limits(root, max_files, max_mb):
    """
    Source files (Python and every registered language) of a checkout: count,
    size and files per language. IndexFailure if out of bounds; the limits
    (max_py_files, max_py_mb: names kept from Python-only days) cover them all.
    """
    languages, size = Counter(), 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = os.path.join(dirpath, name)
            lang = source_language(name)
            if lang and not os.path.islink(path):
                languages[lang] += 1
                size += os.path.getsize(path)
    files = sum(languages.values())
    what = "Python" if set(languages) <= {"python"} else "source"
    if files == 0:
        raise IndexFailure("No Python files or other supported source files found in this repository.")
    if files > max_files:
        raise IndexFailure(f"Too many {what} files ({files:,}; the limit is {max_files:,}).")
    if size > max_mb * 1024 * 1024:
        raise IndexFailure(f"Too much {what} code ({size / 1e6:.0f} MB; the limit is {max_mb} MB).")
    return {"files": files, "bytes": size, "languages": dict(languages)}


def save_sources(agent, clone, cache_dir):
    """Copy the indexed files next to the index (the code viewer's cache, see sources.py)."""
    for rel in indexed_files(agent):
        src, dst = os.path.join(clone, rel), os.path.join(cache_dir, rel)
        if os.path.isfile(src) and not os.path.islink(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)


class Indexer:

    def __init__(self, settings, repos, embedder, fetch=fetch_commit, resolve=resolve_sha, facts=None):
        self.settings = settings
        self.repos = repos
        self.embedder = embedder
        self.fetch = fetch
        self.resolve = resolve
        # facts(owner, repo, token=None) -> GitHub metadata; by default only when the code itself
        # came from GitHub (a test or local fetch has no GitHub repo behind it)
        self.facts = facts
        self.jobs = {}
        self.inflight = {}           # repo_id -> job_id
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.worker = None

    # ── submitting ───────────────────────────────────────────────────────────
    def _resolve(self, owner, repo, ref, token):
        """(sha, private): anonymously if possible, else with `token`. FetchError if neither works."""
        def attempt(**kw):
            sha = self.resolve(owner, repo, ref, **kw)
            if ref and SHA.match(ref):      # a full SHA isn't looked up: check the repo itself is visible
                self.resolve(owner, repo, None, **kw)
            return sha
        try:
            return attempt(), False
        except FetchError:
            if not token:
                raise
        return attempt(token=token), True

    def submit(self, text, allow_new=lambda: (True, 0), token=None, owner_id=None):
        """
        Queue `text` (a GitHub URL) for indexing. Returns (job, queued_new).
        Already-indexed commits and in-flight duplicates don't create jobs.
        allow_new() -> (allowed, retry_after) is consulted only for new work.
        token: the visitor's GitHub token, for private repositories.
        """
        owner, repo, ref = parse_github_url(text)
        sha, private = self._resolve(owner, repo, ref, token)
        repo_id = repo_id_for(owner, repo, sha)
        name = f"{owner}/{repo}"

        with self.lock:
            if self.repos.exists(repo_id):
                job = IndexJob(id=uuid.uuid4().hex, repo_id=repo_id, name=name,
                               url=f"https://github.com/{owner}/{repo}", sha=sha, ref=ref or "",
                               status="ready", progress=1.0, message="Already indexed")
                self._remember(job)
                return job, False
            if repo_id in self.inflight:
                return self.jobs[self.inflight[repo_id]], False
            allowed, retry = allow_new()
            if not allowed:
                raise NotAllowed("You've indexed several repositories recently; try again later.",
                                 retry)
            if self.queue.qsize() >= self.settings.max_index_queue:
                raise QueueFull("The indexing queue is full; please try again in a few minutes.")
            job = IndexJob(id=uuid.uuid4().hex, repo_id=repo_id, name=name,
                           url=f"https://github.com/{owner}/{repo}", sha=sha, ref=ref or "",
                           owner_id=owner_id, private=private, token=token if private else "")
            self._remember(job)
            self.inflight[repo_id] = job.id
            self.queue.put(job)
            if self.worker is None:
                self.worker = threading.Thread(target=self._work, name="indexer", daemon=True)
                self.worker.start()
            return job, True

    def reindex(self, repo_id, allow_new=lambda: (True, 0), token=None, owner_id=None):
        """
        Rebuild an indexed visitor repo (same commit) with the current default
        version. Returns (job, queued_new); KeyError if it isn't a visitor repo.
        """
        meta = self.repos.summary(repo_id)
        if not meta or meta.get("kind") != "user":
            raise KeyError(repo_id)
        with self.lock:
            if repo_id in self.inflight:
                return self.jobs[self.inflight[repo_id]], False
            allowed, retry = allow_new()
            if not allowed:
                raise NotAllowed("You've indexed several repositories recently; try again later.", retry)
            if self.queue.qsize() >= self.settings.max_index_queue:
                raise QueueFull("The indexing queue is full; please try again in a few minutes.")
            private = bool(meta.get("private"))
            job = IndexJob(id=uuid.uuid4().hex, repo_id=repo_id, name=meta["name"], url=meta["url"],
                           sha=meta["sha"], ref=meta.get("ref") or "", owner_id=owner_id,
                           private=private, token=token if private and token else "", reindex=True)
            self._remember(job)
            self.inflight[repo_id] = job.id
            self.queue.put(job)
            if self.worker is None:
                self.worker = threading.Thread(target=self._work, name="indexer", daemon=True)
                self.worker.start()
            return job, True

    def _remember(self, job):
        self.jobs[job.id] = job
        if len(self.jobs) > 500:                     # forget the oldest finished jobs
            for old in sorted(self.jobs.values(), key=lambda j: j.created)[:100]:
                if old.status in ("ready", "failed"):
                    self.jobs.pop(old.id, None)

    def get(self, job_id):
        return self.jobs.get(job_id)

    def position(self, job):
        """1-based place in the queue for a queued job, else 0."""
        if job.status != "queued":
            return 0
        with self.lock:
            return 1 + sum(1 for j in self.jobs.values()
                           if j.status == "queued" and j.created < job.created)

    # ── working ──────────────────────────────────────────────────────────────
    def _facts(self, job, agent):
        """Repo facts: GitHub metadata (the user's token for a private repo) + platforms from config files."""
        from ..facts import fetch_github_facts, platforms
        fetcher = self.facts or (fetch_github_facts if self.fetch is fetch_commit else None)
        facts = {}
        if fetcher is not None:
            owner, repo = job.url.rstrip("/").split("/")[-2:]
            try:
                facts = fetcher(owner, repo, token=job.token or None) or {}
            except Exception:                          # metadata is a bonus: never fail an index for it
                log.warning("repo facts for %s unavailable", job.name)
        facts["platforms"] = platforms(agent.documents)
        return facts

    def _work(self):
        while True:
            job = self.queue.get()
            try:
                self.run(job)
            finally:
                self.queue.task_done()

    def _set(self, job, status, progress, message):
        job.status, job.progress, job.message = status, progress, message

    def run(self, job):
        s = self.settings
        clone = os.path.join(os.path.dirname(self.repos.root), ".clones", job.id)
        try:
            self._set(job, "fetching", 0.05, f"Downloading the source files of {job.name}…")
            self.fetch(job.url, job.sha, clone, timeout=s.fetch_timeout, python_only=True,
                       **({"token": job.token} if job.token else {}))
            found = check_limits(clone, s.max_py_files, s.max_py_mb)

            self._set(job, "parsing", 0.25, "Parsing code and resolving calls…")
            default = s.default_version if s.default_version in VERSIONS else "v10"
            config = get_config(default, ignore_dirs=tuple(s.index_ignore_dirs))
            from ..index import VectorIndex

            def progress(stage, fraction):
                self._set(job, "embedding", 0.3 + 0.65 * fraction,
                          f"Embedding code chunks… {fraction:.0%}")

            try:
                agent = RAGCodebaseAgent(clone, config,
                                         index=VectorIndex(config.embed_model, model=self.embedder),
                                         load_llm=False, verbose=False, progress=progress)
            except ValueError:
                raise IndexFailure("No code to index (tests, docs and examples are skipped).")

            if config.indexes_project_files():
                agent.facts = self._facts(job, agent)
            self._set(job, "saving", 0.97, "Saving the index…")
            out = self.repos.directory(job.repo_id)
            agent.save(os.path.join(out, config.name))
            for old in os.listdir(out):                # a re-index replaces the older version's index
                if old not in (config.name, "sources") and os.path.isfile(os.path.join(out, old, "meta.json")):
                    rmtree(os.path.join(out, old))
            if job.private:
                save_sources(agent, clone, os.path.join(out, "sources"))
            stats = repo_stats(agent)
            meta = {
                "id": job.repo_id, "name": job.name, "kind": "user", "ref": job.ref,
                "description": f"Indexed from GitHub{f' ({job.ref})' if job.ref else ''}.",
                "url": job.url, "sha": job.sha, "root": "", "questions": [],
                "stats": stats, "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "owner_id": job.owner_id, "private": job.private,
                "indexed_with": default, "facts": getattr(agent, "facts", {}) or {},
            }
            write_repo_meta(out, {**meta, "versions": [config.name]})
            # the in-memory record matches what a reload from disk would give
            self.repos.add(Demo(**meta, agents={config.name: agent}))
            done = (f"Re-indexed with {default}: " if job.reindex else "Indexed ") + \
                f"{stats['chunks']:,} chunks from {stats['files']:,} files"
        except (IndexFailure, FetchError) as e:
            job.error = str(e)
        except Exception:
            log.exception("indexing %s failed", job.name)
            job.error = "Indexing failed unexpectedly."
        finally:
            job.token = ""
            # clean up first, so "ready"/"failed" means the job is completely finished
            if os.path.isdir(clone):
                rmtree(clone)
            if job.error:
                job.status = "failed"
            else:
                self._set(job, "ready", 1.0, done)
            with self.lock:
                self.inflight.pop(job.repo_id, None)
