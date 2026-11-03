"""
Background indexing of visitors' GitHub repositories.

One worker thread processes a bounded queue, one repo at a time (free CPU):

    queued -> fetching -> parsing -> embedding -> saving -> ready | failed

Only *.py files are downloaded (gitfetch python_only), nothing from the repo
is executed (the chunker only calls ast.parse), symlinks are not followed,
and the clone is deleted once the index is saved.
"""

import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..agent import RAGCodebaseAgent
from ..config import get_config
from ..gitfetch import FetchError, fetch_commit, parse_github_url, resolve_sha, rmtree
from .registry import Demo, repo_id_for, repo_stats, write_repo_meta

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

    def public(self, queue_position=0):
        return {"job_id": self.id, "repo_id": self.repo_id, "name": self.name, "sha": self.sha,
                "status": self.status, "progress": round(self.progress, 3),
                "message": self.message, "error": self.error, "queue_position": queue_position}


def check_limits(root, max_files, max_mb):
    """Python file count and size of a checkout; IndexFailure if out of bounds."""
    files, size = 0, 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if name.endswith(".py") and not os.path.islink(path):
                files += 1
                size += os.path.getsize(path)
    if files == 0:
        raise IndexFailure("No Python files found in this repository.")
    if files > max_files:
        raise IndexFailure(f"Too many Python files ({files:,}; the limit is {max_files:,}).")
    if size > max_mb * 1024 * 1024:
        raise IndexFailure(f"Too much Python code ({size / 1e6:.0f} MB; the limit is {max_mb} MB).")
    return {"py_files": files, "py_bytes": size}


class Indexer:

    def __init__(self, settings, repos, embedder, fetch=fetch_commit, resolve=resolve_sha):
        self.settings = settings
        self.repos = repos
        self.embedder = embedder
        self.fetch = fetch
        self.resolve = resolve
        self.jobs = {}
        self.inflight = {}           # repo_id -> job_id
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.worker = None

    # ── submitting ───────────────────────────────────────────────────────────
    def submit(self, text, allow_new=lambda: (True, 0)):
        """
        Queue `text` (a GitHub URL) for indexing. Returns (job, queued_new).
        Already-indexed commits and in-flight duplicates don't create jobs.
        allow_new() -> (allowed, retry_after) is consulted only for new work.
        """
        owner, repo, ref = parse_github_url(text)
        sha = self.resolve(owner, repo, ref)
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
                           url=f"https://github.com/{owner}/{repo}", sha=sha, ref=ref or "")
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
            self._set(job, "fetching", 0.05, f"Downloading the Python files of {job.name}…")
            self.fetch(job.url, job.sha, clone, timeout=s.fetch_timeout, python_only=True)
            check_limits(clone, s.max_py_files, s.max_py_mb)

            self._set(job, "parsing", 0.25, "Parsing code and resolving calls…")
            config = get_config(ignore_dirs=tuple(s.index_ignore_dirs))
            from ..index import VectorIndex

            def progress(stage, fraction):
                self._set(job, "embedding", 0.3 + 0.65 * fraction,
                          f"Embedding code chunks… {fraction:.0%}")

            try:
                agent = RAGCodebaseAgent(clone, config,
                                         index=VectorIndex(config.embed_model, model=self.embedder),
                                         load_llm=False, verbose=False, progress=progress)
            except ValueError:
                raise IndexFailure("No Python code to index (tests, docs and examples are skipped).")

            self._set(job, "saving", 0.97, "Saving the index…")
            out = self.repos.directory(job.repo_id)
            agent.save(os.path.join(out, config.name))
            stats = repo_stats(agent)
            meta = {
                "id": job.repo_id, "name": job.name, "kind": "user", "ref": job.ref,
                "description": f"Indexed from GitHub{f' ({job.ref})' if job.ref else ''}.",
                "url": job.url, "sha": job.sha, "root": "", "questions": [],
                "stats": stats, "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            write_repo_meta(out, {**meta, "versions": [config.name]})
            # the in-memory record matches what a reload from disk would give
            self.repos.add(Demo(**meta, agents={config.name: agent}))
            done = f"Indexed {stats['chunks']:,} chunks from {stats['files']:,} files"
        except (IndexFailure, FetchError) as e:
            job.error = str(e)
        except Exception:
            log.exception("indexing %s failed", job.name)
            job.error = "Indexing failed unexpectedly."
        finally:
            # clean up first, so "ready"/"failed" means the job is completely finished
            if os.path.isdir(clone):
                rmtree(clone)
            if job.error:
                job.status = "failed"
            else:
                self._set(job, "ready", 1.0, done)
            with self.lock:
                self.inflight.pop(job.repo_id, None)
