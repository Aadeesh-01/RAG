"""
Repositories the server answers about.

Curated demos are built offline and loaded from disk:
    <data_dir>/demos/<demo_id>/demo.json      name, url, sha, questions, stats
    <data_dir>/demos/<demo_id>/<version>/     saved index (see coderag/persist.py)
Visitor repos are rows in the database (`repos`), their index on disk at
    <data_dir>/repos/<repo_id>/<version>/
"""

import json
import os
import re
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone

import yaml

from ..agent import RAGCodebaseAgent
from ..config import VERSIONS, get_config
from ..gitfetch import fetch_commit, rmtree
from ..languages import label_of, language_of

log = logging.getLogger("coderag.web.registry")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class Demo:
    id: str
    name: str
    description: str = ""
    url: str = ""                   # GitHub URL, empty for local demos
    sha: str = ""
    root: str = ""                  # indexed subdirectory of the repo
    questions: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    built: str = ""
    order: int = 0                  # position in demos.yaml
    kind: str = "demo"              # "demo" (curated) | "user" (indexed by a visitor)
    ref: str = ""                   # branch or tag the visitor asked for, if any
    owner_id: int | None = None     # user who indexed it (visitor repos)
    facts: dict = field(default_factory=dict)      # repo facts: GitHub metadata + platforms (v10)
    indexed_with: str = ""          # preset the server indexed it with (its index may be saved under
                                    # an older, identical one: see jobs.settled_config)
    private: bool = False           # private on GitHub: only users who added it may open it
    agents: dict = field(default_factory=dict, repr=False)     # version -> agent

    @property
    def versions(self):
        return sorted(self.agents)

    def source_url(self, file, start_line, end_line):
        """Link to the exact lines at the pinned commit, or None for local demos."""
        if not (self.url and self.sha and file):
            return None
        path = f"{self.root}/{file}" if self.root else file
        return f"{self.url}/blob/{self.sha}/{path}#L{start_line}-L{end_line}"

    def counts(self):
        """Files, functions (with methods) and classes in the newest index: people's words, not chunks."""
        agent = self.agents.get(self.versions[-1]) if self.agents else None
        docs = [d for d in getattr(agent, "documents", []) if "qname" in d]
        return {"files": len({d["qname"].split("::")[0] for d in docs}),
                "functions": sum(d["type"] in ("function", "method") for d in docs),
                "classes": sum(d["type"] == "class" for d in docs)}

    def languages(self):
        """{"Python": 12, "Go": 3}: indexed files per language, most files first."""
        agent = self.agents.get(self.versions[-1]) if self.agents else None
        if agent is None:
            return self.stats.get("languages", {})
        return language_files(agent.documents)

    def info(self):
        return {"id": self.id, "name": self.name, "description": self.description, "counts": self.counts(),
                "languages": self.languages(),
                "url": self.url, "sha": self.sha, "root": self.root, "questions": self.questions,
                "stats": self.stats, "built": self.built, "versions": self.versions,
                "kind": self.kind, "ref": self.ref, "private": self.private,
                "indexed_with": self.built_with(), "bridges_checked": bridges_checked(self.built_with()),
                "facts": self.repo_facts()}

    def repo_facts(self):
        """GitHub metadata and platforms recorded when it was indexed ({} if none)."""
        if self.facts:
            return self.facts
        agent = self.agents.get(self.versions[-1]) if self.agents else None
        return getattr(agent, "facts", None) or {}

    def built_with(self):
        """The preset this repo was indexed with (older indexes: the newest version saved)."""
        return self.indexed_with or (self.versions[-1] if self.versions else "")


def bridges_checked(version):
    """Whether indexing with `version` looked for cross-language bridges."""
    return bool(version in VERSIONS and VERSIONS[version].bridges)


def is_older(version, than):
    """True when preset `version` comes before `than` (unknown versions count as older)."""
    order = list(VERSIONS)
    return (order.index(version) if version in order else -1) < (order.index(than) if than in order else -1)


def language_files(documents):
    """{"Python": 12, "Go": 3}: files per language label, most files first."""
    files = {}
    for d in documents:
        if "qname" in d and d.get("type") != "config":     # config files aren't a language
            files.setdefault(d["qname"].split("::")[0], language_of(d))
    counts = {}
    for lang in files.values():
        counts[label_of(lang)] = counts.get(label_of(lang), 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def repo_stats(agent):
    edges = agent.code_graph.edge_counts() if agent.code_graph else {}
    return {"files": len({d["qname"].split("::")[0] for d in agent.documents if "qname" in d}),
            "chunks": len(agent.documents), "call_edges": sum(edges.values()),
            "languages": language_files(agent.documents)}


# =============================================================================
# BUILD (offline, scripts/build_demos.py)
# =============================================================================
def build_demos(spec_path, data_dir, only=None, keep_clones=False, embedder=None, log=print, demos_dir=None):
    """Index the demos in `spec_path` into `demos_dir` (default <data_dir>/demos)."""
    demos_dir = demos_dir or os.path.join(data_dir, "demos")
    with open(spec_path, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    versions = spec.get("versions", ["v7"])
    clones = os.path.join(data_dir, ".clones")

    for order, entry in enumerate(spec["demos"]):
        if only and entry["id"] not in only:
            continue
        if entry.get("url"):
            checkout = fetch_commit(entry["url"], entry["sha"], os.path.join(clones, entry["id"]))
        else:
            checkout = os.path.join(PROJECT_ROOT, entry["repo"])
        repo = os.path.join(checkout, entry.get("root", ""))

        out = os.path.join(demos_dir, entry["id"])
        stats = {}
        for version in versions:
            index = None
            if embedder is not None:
                from ..index import VectorIndex
                index = VectorIndex(get_config(version).embed_model, model=embedder)
            agent = RAGCodebaseAgent(repo, get_config(version), index=index,
                                     load_llm=False, verbose=False)
            agent.save(os.path.join(out, version))
            stats = repo_stats(agent)
            log(f"[demos] {entry['id']} {version}: {stats}")

        meta = {k: entry.get(k, "") for k in ("id", "name", "description", "url", "sha", "root")}
        meta.update(questions=entry.get("questions", []), stats=stats, versions=versions, order=order,
                    built=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        write_repo_meta(out, meta)

    if not keep_clones and os.path.isdir(clones):
        rmtree(clones)


# =============================================================================
# LOAD
# =============================================================================
def write_repo_meta(directory, meta):
    with open(os.path.join(directory, "demo.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def load_repo_dir(directory, embedder=None):
    """Demo with its indexes loaded from a directory holding demo.json + <version>/."""
    with open(os.path.join(directory, "demo.json"), encoding="utf-8") as f:
        meta = json.load(f)
    versions = meta.pop("versions", [])
    known = {f.name for f in fields(Demo)} - {"agents"}
    demo = Demo(**{k: v for k, v in meta.items() if k in known})
    for version in versions:
        demo.agents[version] = RAGCodebaseAgent.load(
            os.path.join(directory, version), load_llm=False, verbose=False, embedder=embedder)
    return demo


def load_demos(demos_dir, embedder=None):
    """{demo_id: Demo} for every built demo in `demos_dir` (CODERAG_DEMOS_DIR), indexes loaded."""
    demos = {}
    root = demos_dir
    if not os.path.isdir(root):
        return demos
    for demo_id in sorted(os.listdir(root)):
        if os.path.exists(os.path.join(root, demo_id, "demo.json")):
            demos[demo_id] = load_repo_dir(os.path.join(root, demo_id), embedder)
    return dict(sorted(demos.items(), key=lambda item: item[1].order))


class Repos:
    """
    Curated demos (always loaded, from CODERAG_DEMOS_DIR, built offline) plus
    repos indexed by visitors: a row each in the `repos` table (coderag/db),
    with the FAISS index on disk at its index_path (<data_dir>/repos/<id>/<preset>).
    Visitor repos are loaded on demand; at most `max_loaded` stay in memory (least
    recently used are unloaded). On disk at most `max_stored` repos and `max_stored_mb`
    MB are kept: past either, the least recently used repos that nobody has in their
    list are deleted (demos never are).
    A row whose index files are gone (a disk that was reset while the database
    survived) is "missing files": missing() says so and the indexer rebuilds it.
    Demos get a row too (kind "demo") so questions about them can reference it.
    """

    TOUCH_EVERY = 300                 # seconds between last_used_at writes per repo

    def __init__(self, data_dir, demos, embedder=None, max_loaded=6, max_stored=100, db=None, max_stored_mb=0):
        self.data_dir = data_dir
        self.root = os.path.join(data_dir, "repos")
        self.demos = demos
        self.embedder = embedder
        self.max_loaded = max_loaded
        self.max_stored = max_stored
        self.max_stored_mb = max_stored_mb
        self.loaded = OrderedDict()
        self.lock = threading.RLock()
        self._touched = {}
        if db is None:
            from ..db import Database, default_url
            db = Database(default_url(data_dir))
            db.upgrade()
        self.db = db

    def directory(self, repo_id):
        return os.path.join(self.root, repo_id)

    def _row(self, repo_id):
        """The visitor repo's row (detached), or None."""
        from ..db.models import Repo
        if not _REPO_ID.match(repo_id or ""):
            return None
        with self.db.session() as s:
            row = s.get(Repo, repo_id)
            return row if row is not None and row.kind == "user" else None

    def exists(self, repo_id):
        """A demo, or a visitor repo with a row (its files may be missing: see missing())."""
        return repo_id in self.demos or self._row(repo_id) is not None

    def missing(self, repo_id):
        """True for a visitor repo whose row exists but whose index files don't (rebuild it)."""
        if repo_id in self.demos or repo_id in self.loaded:
            return False
        row = self._row(repo_id)
        return row is not None and not has_index(row.index_path)

    def touch(self, repo_id, now=None):
        """Record a use of a visitor repo (last_used_at, at most every TOUCH_EVERY seconds)."""
        from ..db.models import Repo, utcnow
        clock = time.monotonic() if now is None else now
        if repo_id in self.demos or clock - self._touched.get(repo_id, -1e18) < self.TOUCH_EVERY:
            return
        self._touched[repo_id] = clock
        with self.db.session() as s:
            row = s.get(Repo, repo_id)
            if row is not None:
                row.last_used_at = utcnow()

    def summary(self, repo_id):
        """info()-like dict without loading the index, or None."""
        loaded = self.demos.get(repo_id) or self.loaded.get(repo_id)
        if loaded is not None:
            return loaded.info()
        row = self._row(repo_id)
        return row_info(row) if row is not None else None

    def summaries(self, repo_ids):
        """{repo_id: summary} for the ones that exist (one query for the rows)."""
        from ..db.models import Repo
        out = {i: self.summary(i) for i in repo_ids if i in self.demos or i in self.loaded}
        rest = [i for i in repo_ids if i not in out and _REPO_ID.match(i)]
        if rest:
            with self.db.session() as s:
                for row in s.query(Repo).filter(Repo.id.in_(rest), Repo.kind == "user"):
                    out[row.id] = row_info(row)
        return out

    def bridge_count(self, repo_id):
        """
        Cross-language bridges in a repo's saved call graph (graph.json), without loading
        the index; cached per file and modification time. 0 when unknown.
        """
        demo = self.demos.get(repo_id) or self.loaded.get(repo_id)
        if demo is not None:
            agent = demo.agents.get(demo.versions[-1]) if demo.agents and demo.versions else None
            graph = getattr(agent, "code_graph", None)
            if graph is not None and getattr(graph, "bridges", None) is not None:
                return len(graph.bridges)
        row = self._row(repo_id) if _REPO_ID.match(repo_id) else None
        path = os.path.join(row.index_path, "graph.json") if row is not None and row.index_path else ""
        try:
            mtime = os.path.getmtime(path) if path else None
        except OSError:
            return 0
        if mtime is None:
            return 0
        cache = self.__dict__.setdefault("_bridge_counts", {})
        key = (path, mtime)
        if key not in cache:
            try:
                with open(path, encoding="utf-8") as f:
                    cache[key] = len(json.load(f).get("bridges") or [])
            except (OSError, ValueError):
                return 0
        return cache[key]

    def private(self, repo_id):
        """True if `repo_id` is a visitor repo that is private on GitHub (read without loading it)."""
        info = self.summary(repo_id)
        return bool(info and info.get("private"))

    def get(self, repo_id):
        """Demo with indexes loaded, or None."""
        if repo_id in self.demos:
            return self.demos[repo_id]
        if not _REPO_ID.match(repo_id):
            return None
        with self.lock:
            if repo_id in self.loaded:
                self.loaded.move_to_end(repo_id)
                demo = self.loaded[repo_id]
            else:
                row = self._row(repo_id)
                if row is None or not has_index(row.index_path):
                    return None
                agent = RAGCodebaseAgent.load(row.index_path, load_llm=False, verbose=False, embedder=self.embedder)
                self._keep(repo_id, demo_of(row, agent))
                demo = self.loaded[repo_id]
        self.touch(repo_id)
        return demo

    def add(self, demo, index_path):
        """Record a freshly indexed visitor repo (insert or replace its row) and enforce the limit."""
        from ..db.models import Repo, utcnow
        from ..db.legacy import parse_time
        with self.lock:
            with self.db.session() as s:
                row = s.get(Repo, demo.id) or Repo(id=demo.id, kind="user", created_at=utcnow())
                row.kind, row.name = "user", demo.name
                row.owner = demo.name.split("/")[0] if "/" in demo.name else ""
                row.url, row.commit, row.ref = demo.url, demo.sha, demo.ref
                row.description, row.private = demo.description, bool(demo.private)
                row.indexed_with = demo.indexed_with or (demo.versions[-1] if demo.versions else "")
                row.stats = demo.stats or {}
                row.languages = (demo.stats or {}).get("languages", {})
                row.facts = demo.facts or {}
                row.index_path = index_path
                row.owner_id = demo.owner_id if row.owner_id is None else row.owner_id
                row.built_at = parse_time(demo.built) or utcnow()
                row.last_used_at = utcnow()
                row.disk_bytes = dir_bytes(self.directory(demo.id))
                s.add(row)
            self._keep(demo.id, demo)
        self.evict(keep=demo.id)

    def evict(self, keep=None):
        """
        Delete least recently used visitor repos until at most max_stored repos and
        max_stored_mb MB are on disk. Only repos nobody has in their list, never a
        demo or `keep`. A repo with questions (an older commit after "Update to latest")
        loses only its index files: its row stays, so history and shared answers keep
        working, and opening it rebuilds the index. Returns the removed ids (a warning
        is logged if the limits can't be met because every remaining repo is in someone's list).
        """
        from sqlalchemy import func
        from ..db.models import Question, Repo, UserRepo
        limit_bytes = self.max_stored_mb * 1024 * 1024 if self.max_stored_mb else None
        with self.db.session() as s:
            rows = (s.query(Repo).filter_by(kind="user")
                    .order_by(func.coalesce(Repo.last_used_at, Repo.created_at).asc()).all())
            linked = {r.repo_id for r in s.query(UserRepo.repo_id).distinct()}
            asked = {r.repo_id for r in s.query(Question.repo_id).distinct()}
            sizes = {}
            for row in rows:
                if not os.path.isdir(self.directory(row.id)):
                    continue                                 # its files are already gone: nothing to free
                if not row.disk_bytes:
                    row.disk_bytes = dir_bytes(self.directory(row.id))      # rows from before the size column
                sizes[row.id] = row.disk_bytes or 0
        count, total = len(sizes), sum(sizes.values())
        removed = []
        for repo_id in list(sizes):
            if count <= self.max_stored and (limit_bytes is None or total <= limit_bytes):
                break
            if repo_id == keep or repo_id in self.demos or repo_id in linked:
                continue
            if repo_id in asked:
                self.remove_files(repo_id)
            else:
                self.remove(repo_id)
            removed.append(repo_id)
            count, total = count - 1, total - sizes[repo_id]
        if count > self.max_stored or (limit_bytes is not None and total > limit_bytes):
            log.warning("stored repos (%d, %.0f MB) are over the limits, but the rest are in users' lists",
                        count, total / 2**20)
        return removed

    def remove_files(self, repo_id):
        """Delete a visitor repo's index files but keep its row (and its questions); opening it rebuilds them."""
        from ..db.models import Repo
        if repo_id in self.demos or not _REPO_ID.match(repo_id or ""):
            return False
        with self.lock:
            self.loaded.pop(repo_id, None)
            if os.path.isdir(self.directory(repo_id)):
                rmtree(self.directory(repo_id))
            with self.db.session() as s:
                row = s.get(Repo, repo_id)
                if row is not None:
                    row.disk_bytes = 0
        return True

    def remove(self, repo_id):
        """Delete a visitor repo: its row (questions and shares go with it), user links and index files."""
        from ..db.models import Repo, UserRepo
        if repo_id in self.demos or not _REPO_ID.match(repo_id or ""):
            return False
        with self.lock:
            with self.db.session() as s:
                row = s.get(Repo, repo_id)
                if row is not None and row.kind == "user":
                    s.delete(row)
                s.query(UserRepo).filter_by(repo_id=repo_id).delete()
            self.loaded.pop(repo_id, None)
            if os.path.isdir(self.directory(repo_id)):
                rmtree(self.directory(repo_id))
        return True

    def register_demos(self):
        """A row per curated demo (kind "demo"), so questions about a demo can point at it."""
        from ..db.models import Repo, utcnow
        with self.db.session() as s:
            for demo in self.demos.values():
                row = s.get(Repo, demo.id) or Repo(id=demo.id, created_at=utcnow())
                row.kind, row.name, row.url, row.commit = "demo", demo.name, demo.url, demo.sha
                row.description = demo.description
                row.indexed_with = demo.versions[-1] if demo.versions else ""
                row.stats, row.languages = demo.stats or {}, demo.languages() if demo.agents else {}
                row.facts = demo.facts or {}
                s.add(row)

    def import_legacy(self):
        """Rows for visitor repos saved on disk before the database (idempotent)."""
        from ..db.legacy import repo_rows
        from ..db.models import Repo, User
        with self.db.session() as s:
            known = {r.id for r in s.query(Repo.id)}
            users = {u.id for u in s.query(User.id)}
            for data in repo_rows(self.data_dir):
                if data["id"] not in known:
                    if data["owner_id"] not in users:
                        data["owner_id"] = None
                    s.add(Repo(**data))

    def _keep(self, repo_id, demo):
        self.loaded[repo_id] = demo
        self.loaded.move_to_end(repo_id)
        while len(self.loaded) > self.max_loaded:
            self.loaded.popitem(last=False)


def has_index(index_path):
    return bool(index_path) and os.path.isfile(os.path.join(index_path, "meta.json"))


def dir_bytes(path):
    """Total size of the files under `path` (0 if it doesn't exist)."""
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat(timespec="seconds") if value else ""


def row_info(row):
    """What Demo.info() gives, from a repos row (no index loaded: no counts)."""
    version = os.path.basename(row.index_path.rstrip("/\\")) if row.index_path else ""
    built_with = row.indexed_with or version
    return {"id": row.id, "name": row.name, "description": row.description, "url": row.url, "sha": row.commit,
            "root": "", "questions": [], "stats": row.stats or {}, "built": _iso(row.built_at),
            "versions": [version] if version else [], "kind": row.kind, "ref": row.ref, "private": row.private,
            "languages": row.languages or {}, "indexed_with": built_with,
            "bridges_checked": bridges_checked(built_with), "facts": row.facts or {}}


def demo_of(row, agent):
    """The in-memory Demo for a visitor repo row and its loaded index."""
    version = os.path.basename(row.index_path.rstrip("/\\"))
    return Demo(id=row.id, name=row.name, description=row.description, url=row.url, sha=row.commit,
                stats=row.stats or {}, built=_iso(row.built_at), kind="user", ref=row.ref,
                owner_id=row.owner_id, private=row.private, facts=row.facts or {},
                indexed_with=row.indexed_with, agents={version: agent})


# owner__repo__sha12, lowercase: also guards against path tricks in URLs
_REPO_ID = re.compile(r"^[a-z0-9-]{1,39}__[a-z0-9._-]{1,100}__[0-9a-f]{12}$")


def repo_id_for(owner, repo, sha):
    return f"{owner}__{repo}__{sha[:12]}".lower()


def repo_name_of(repo_id):
    """'owner/repo' (lowercase) of a visitor repo id; the repo name itself may contain '__'."""
    head = repo_id.rsplit("__", 1)[0]
    owner, _, repo = head.partition("__")
    return f"{owner}/{repo}"
