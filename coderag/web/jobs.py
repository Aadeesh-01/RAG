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
needs a token), and the token is dropped from the job once it finishes. For a
public repo the same token (when the visitor is signed in) is used only for the
GitHub metadata (facts.py): authenticated API calls aren't limited to 60 an hour.

A repo whose row survived but whose index files are gone (a reset disk) is
rebuilt at the same commit: rebuild(), also when it is opened or submitted again.
Each job has a deadline (index_timeout, default 10 minutes); a signed-in user
has at most one job queued or running at a time.
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
from .registry import Demo, repo_id_for, repo_stats
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


class NeedsToken(Exception):
    """A private repo's files must be fetched again, and the requester has no GitHub token for it."""


ACTIVE = ("queued", "fetching", "parsing", "embedding", "saving")


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
    facts_token: str = field(default="", repr=False)   # signed-in user's token, GitHub metadata only; cleared
    reindex: bool = False            # rebuild an indexed repo with the current default version
    rebuild: bool = False            # its index files were missing: same commit, fetched again
    version: str = ""                # build with this preset (an explicit rebuild keeps the repo's own); "" = default
    created_at: str = ""             # ISO, for the job list
    deadline: float = 0.0            # time.monotonic() by which the job must be done (set when it starts)

    @property
    def kind(self):
        return "rebuild" if self.rebuild else "reindex" if self.reindex else "index"

    def public(self, queue_position=0):
        return {"job_id": self.id, "repo_id": self.repo_id, "name": self.name, "sha": self.sha,
                "status": self.status, "progress": round(self.progress, 3),
                "message": self.message, "error": self.error, "queue_position": queue_position,
                "kind": self.kind, "created_at": self.created_at}


# the jobs table keeps the coarse status; the stage (fetching, embedding ...) is its own column
def _db_status(status):
    return {"queued": "queued", "ready": "done", "failed": "failed"}.get(status, "running")


def job_from_row(row):
    """An IndexJob view of a jobs row (a job from before a restart, or another worker's)."""
    status = {"queued": "queued", "done": "ready", "failed": "failed"}.get(row.status, row.stage or "fetching")
    created = row.created_at.replace(tzinfo=timezone.utc).isoformat(timespec="seconds") if row.created_at else ""
    return IndexJob(id=row.id, repo_id=row.repo_id, name=row.repo_name, url=row.url, sha=row.commit,
                    ref=row.ref, status=status, progress=(row.progress or 0) / 100, message=row.message,
                    error=row.error, owner_id=row.user_id, reindex=row.kind in ("reindex", "rebuild"),
                    rebuild=row.kind == "rebuild", created_at=created)


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

    def __init__(self, settings, repos, embedder, fetch=fetch_commit, resolve=resolve_sha, facts=None,
                 db=None):
        self.settings = settings
        self.db = db or repos.db                 # jobs are rows in the `jobs` table
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
        self.failed_rebuilds = {}    # repo_id -> (monotonic time, job): opening it doesn't retry at once
        self.listeners = []          # callables(job) told when a job ends (ready or failed)
        from ..db.models import utcnow
        self.started = utcnow()      # recover() leaves jobs created after this alone
        self.started_wall = time.time()

    REBUILD_RETRY = 600              # seconds before opening a repo retries a failed rebuild

    @property
    def clones(self):
        return os.path.join(os.path.dirname(self.repos.root), ".clones")

    def _busy(self, owner_id):
        """The owner's queued or running job, if any (one at a time per user)."""
        if owner_id is None:
            return None
        return next((j for j in self.jobs.values() if j.owner_id == owner_id and j.status in ACTIVE), None)

    def _check_user(self, owner_id):
        busy = self._busy(owner_id)
        if busy is not None:
            raise NotAllowed(f"{busy.name} is still being indexed; add another repository when it's done.", 30)

    def _start(self, job):
        """Queue `job` (the lock is held)."""
        if self.queue.qsize() >= self.settings.max_index_queue:
            raise QueueFull("The indexing queue is full; please try again in a few minutes.")
        self._remember(job)
        self.inflight[job.repo_id] = job.id
        self.queue.put(job)
        if self.worker is None:
            self.worker = threading.Thread(target=self._work, name="indexer", daemon=True)
            self.worker.start()
        return job

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
            if repo_id in self.inflight:
                return self.jobs[self.inflight[repo_id]], False
            if self.repos.exists(repo_id) and not self.repos.missing(repo_id):
                job = IndexJob(id=uuid.uuid4().hex, repo_id=repo_id, name=name,
                               url=f"https://github.com/{owner}/{repo}", sha=sha, ref=ref or "",
                               status="ready", progress=1.0, message="Already indexed", owner_id=owner_id)
                self._remember(job)
                return job, False
            self._check_user(owner_id)
            missing = self.repos.exists(repo_id)          # the row is there, the files aren't: rebuild it
            if not missing:
                allowed, retry = allow_new()
                if not allowed:
                    raise NotAllowed("You've indexed several repositories recently; try again later.",
                                     retry)
            job = IndexJob(id=uuid.uuid4().hex, repo_id=repo_id, name=name,
                           url=f"https://github.com/{owner}/{repo}", sha=sha, ref=ref or "",
                           owner_id=owner_id, private=private, token=token if private else "",
                           facts_token=token or "", rebuild=missing, reindex=missing,
                           message="Rebuilding index…" if missing else IndexJob.message)
            return self._start(job), True

    def rebuild(self, repo_id, token=None, owner_id=None):
        """
        Fetch a repo whose index files are missing again, at the commit its row
        records. Returns (job, queued_new); the in-flight job if there is one.
        NeedsToken for a private repo without `token`; KeyError if it isn't a visitor repo.
        """
        meta = self.repos.summary(repo_id)
        if not meta or meta.get("kind") != "user":
            raise KeyError(repo_id)
        with self.lock:
            if repo_id in self.inflight:
                return self.jobs[self.inflight[repo_id]], False
            failed = self.failed_rebuilds.get(repo_id)
            if failed and time.monotonic() - failed[0] < self.REBUILD_RETRY:
                return failed[1], False              # the last attempt failed just now: show that, don't loop
            private = bool(meta.get("private"))
            if private and not token:
                raise NeedsToken(repo_id)
            job = IndexJob(id=uuid.uuid4().hex, repo_id=repo_id, name=meta["name"], url=meta["url"],
                           sha=meta["sha"], ref=meta.get("ref") or "", owner_id=owner_id, private=private,
                           token=token if private else "", facts_token=token or "", reindex=True,
                           rebuild=True, message="Rebuilding index…")
            return self._start(job), True

    def rebuild_index(self, repo_id, allow_new=lambda: (True, 0), token=None, owner_id=None):
        """
        Rebuild an indexed visitor repo from scratch at the same commit with the same
        version it was built with (re-fetch, re-chunk, re-embed), asked for by a user.
        Returns (job, queued_new); KeyError if it isn't a visitor repo; NotAllowed past the limits.
        """
        meta = self.repos.summary(repo_id)
        if not meta or meta.get("kind") != "user":
            raise KeyError(repo_id)
        with self.lock:
            if repo_id in self.inflight:
                return self.jobs[self.inflight[repo_id]], False
            self._check_user(owner_id)
            allowed, retry = allow_new()
            if not allowed:
                raise NotAllowed("You've indexed several repositories recently; try again later.", retry)
            private = bool(meta.get("private"))
            if private and not token:
                raise NeedsToken(repo_id)
            job = IndexJob(id=uuid.uuid4().hex, repo_id=repo_id, name=meta["name"], url=meta["url"],
                           sha=meta["sha"], ref=meta.get("ref") or "", owner_id=owner_id, private=private,
                           token=token if private else "", facts_token=token or "", reindex=True, rebuild=True,
                           version=meta.get("indexed_with") or "", message="Rebuilding index…")
            self.failed_rebuilds.pop(repo_id, None)
            return self._start(job), True

    def latest(self, owner, repo, ref=None, token=None):
        """The newest commit of `ref` (else the default branch) with git ls-remote; FetchError if it can't."""
        return self._resolve(owner, repo, ref, token)[0]

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
            self._check_user(owner_id)
            allowed, retry = allow_new()
            if not allowed:
                raise NotAllowed("You've indexed several repositories recently; try again later.", retry)
            private = bool(meta.get("private"))
            job = IndexJob(id=uuid.uuid4().hex, repo_id=repo_id, name=meta["name"], url=meta["url"],
                           sha=meta["sha"], ref=meta.get("ref") or "", owner_id=owner_id,
                           private=private, token=token if private and token else "",
                           facts_token=token or "", reindex=True)
            return self._start(job), True

    def _remember(self, job):
        """Keep a new job in memory (the worker updates it) and insert its row."""
        from ..db.models import Job, utcnow
        now = utcnow()
        job.created_at = now.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
        with self.db.session() as s:
            s.add(Job(id=job.id, repo_id=job.repo_id, repo_name=job.name, user_id=job.owner_id,
                      kind=job.kind, status=_db_status(job.status),
                      stage=job.status, progress=round(job.progress * 100), message=job.message,
                      error=job.error, url=job.url, commit=job.sha, ref=job.ref, created_at=now,
                      finished_at=now if job.status in ("ready", "failed") else None))
        self.jobs[job.id] = job
        if len(self.jobs) > 500:                     # forget the oldest finished jobs (their rows stay)
            for old in sorted(self.jobs.values(), key=lambda j: j.created)[:100]:
                if old.status in ("ready", "failed"):
                    self.jobs.pop(old.id, None)

    def _persist(self, job, started=False, finished=False):
        from ..db.models import Job, utcnow
        with self.db.session() as s:
            row = s.get(Job, job.id)
            if row is None:
                return
            row.status, row.stage = _db_status(job.status), job.status
            row.progress, row.message, row.error = round(job.progress * 100), job.message, job.error
            if started:
                row.started_at = utcnow()
            if finished:
                row.finished_at = utcnow()

    def get(self, job_id):
        """The job: in memory while this process runs it, else its row (None if unknown)."""
        if job_id in self.jobs:
            return self.jobs[job_id]
        from ..db.models import Job
        with self.db.session() as s:
            row = s.get(Job, job_id) if isinstance(job_id, str) and len(job_id) <= 32 else None
            return job_from_row(row) if row is not None else None

    def for_user(self, user_id, limit=50):
        """The user's jobs, newest first (public dicts)."""
        from ..db.models import Job
        with self.db.session() as s:
            rows = (s.query(Job).filter_by(user_id=user_id).order_by(Job.created_at.desc())
                    .limit(limit).all())
            jobs = [self.jobs.get(r.id) or job_from_row(r) for r in rows]
        return [j.public(self.position(j)) for j in jobs]

    def recover(self):
        """
        At start-up: jobs a previous process left queued or running can't finish;
        mark them failed (only jobs created before this process started) and
        delete the clones such jobs left in <data_dir>/.clones.
        """
        from ..db.models import Job, utcnow
        with self.db.session() as s:
            stale = s.query(Job).filter(Job.status.in_(("queued", "running")),
                                        Job.created_at < self.started).all()
            for row in stale:
                row.status, row.stage, row.error, row.finished_at = "failed", "failed", "server restarted", utcnow()
            count = len(stale)
        if os.path.isdir(self.clones):
            for name in os.listdir(self.clones):
                path = os.path.join(self.clones, name)
                try:
                    old = os.path.getmtime(path) < self.started_wall
                except OSError:
                    continue
                if name not in self.jobs and old:
                    rmtree(path) if os.path.isdir(path) else os.remove(path)
        return count

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
                facts = fetcher(owner, repo, token=job.token or job.facts_token or None) or {}
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

    def _check_deadline(self, job):
        if job.deadline and time.monotonic() > job.deadline:
            minutes = self.settings.index_timeout / 60
            raise IndexFailure(f"Indexing took longer than {minutes:g} minutes and was stopped.")

    def _set(self, job, status, progress, message):
        self._check_deadline(job)
        changed = status != job.status or abs(progress - job.progress) >= 0.01
        job.status, job.progress, job.message = status, progress, message
        if changed:                                  # embedding reports often; write at most every 1%
            self._persist(job, started=status == "fetching")

    def run(self, job):
        s = self.settings
        clone = os.path.join(self.clones, job.id)
        timeout = getattr(s, "index_timeout", 600)
        job.deadline = time.monotonic() + timeout
        try:
            self._set(job, "fetching", 0.05, ("Rebuilding index… downloading " if job.rebuild else
                                              "Downloading the source files of ") + f"{job.name}…")
            self.fetch(job.url, job.sha, clone, timeout=max(1, min(s.fetch_timeout, int(timeout))),
                       python_only=True, **({"token": job.token} if job.token else {}))
            self._check_deadline(job)
            found = check_limits(clone, s.max_py_files, s.max_py_mb)

            self._set(job, "parsing", 0.25, ("Rebuilding index… " if job.rebuild else "") +
                      "Parsing code and resolving calls…")
            default = job.version if job.version in VERSIONS else s.default_version if s.default_version in VERSIONS else "v10"
            config = get_config(default, ignore_dirs=tuple(s.index_ignore_dirs))
            from ..index import VectorIndex

            def progress(stage, fraction):              # raises IndexFailure past the deadline
                self._set(job, "embedding", 0.3 + 0.65 * fraction,
                          ("Rebuilding index… " if job.rebuild else "") + f"Embedding code chunks… {fraction:.0%}")

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
            # the repos row (and the loaded copy) match what a reload would give
            self.repos.add(Demo(**meta, agents={config.name: agent}), os.path.join(out, config.name))
            done = ("Rebuilt: " if job.rebuild else f"Re-indexed with {default}: " if job.reindex else "Indexed ") + \
                f"{stats['chunks']:,} chunks from {stats['files']:,} files"
        except (IndexFailure, FetchError) as e:
            job.error = str(e)
        except Exception:
            log.exception("indexing %s failed", job.name)
            job.error = "Indexing failed unexpectedly."
        finally:
            job.token = job.facts_token = ""
            # clean up first, so "ready"/"failed" means the job is completely finished
            if os.path.isdir(clone):
                rmtree(clone)
            if job.error:
                job.status = "failed"
            else:
                job.status, job.progress, job.message = "ready", 1.0, done
            self._persist(job, finished=True)
            with self.lock:
                self.inflight.pop(job.repo_id, None)
                if job.rebuild and job.error:
                    self.failed_rebuilds[job.repo_id] = (time.monotonic(), job)
                else:
                    self.failed_rebuilds.pop(job.repo_id, None)
            self.finished(job)

    def finished(self, job):
        """Tell the listeners a job ended (ready or failed); a listener's error never fails the job."""
        for listener in list(self.listeners):
            try:
                listener(job)
            except Exception:                            # noqa: BLE001
                log.exception("a job listener failed for %s", job.name)
