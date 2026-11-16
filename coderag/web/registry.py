"""
Demo repositories: building their indexes (offline) and loading them (server).

    <data_dir>/demos/<demo_id>/demo.json      name, url, sha, questions, stats
    <data_dir>/demos/<demo_id>/<version>/     saved index (see coderag/persist.py)
"""

import json
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone

import yaml

from ..agent import RAGCodebaseAgent
from ..config import get_config
from ..gitfetch import fetch_commit, rmtree
from ..languages import label_of, language_of

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
                "kind": self.kind, "ref": self.ref, "private": self.private}


def language_files(documents):
    """{"Python": 12, "Go": 3}: files per language label, most files first."""
    files = {}
    for d in documents:
        if "qname" in d:
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
def build_demos(spec_path, data_dir, only=None, keep_clones=False, embedder=None, log=print):
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

        out = os.path.join(data_dir, "demos", entry["id"])
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


def load_demos(data_dir, embedder=None):
    """{demo_id: Demo} for every built demo in `data_dir`, indexes loaded."""
    demos = {}
    root = os.path.join(data_dir, "demos")
    if not os.path.isdir(root):
        return demos
    for demo_id in sorted(os.listdir(root)):
        if os.path.exists(os.path.join(root, demo_id, "demo.json")):
            demos[demo_id] = load_repo_dir(os.path.join(root, demo_id), embedder)
    return dict(sorted(demos.items(), key=lambda item: item[1].order))


class Repos:
    """
    Curated demos (always loaded) plus repos indexed by visitors, which live in
    <data_dir>/repos/<repo_id>/ and are loaded on demand. At most `max_loaded`
    visitor repos stay in memory (least recently used are unloaded); at most
    `max_stored` stay on disk (oldest are deleted).
    """

    def __init__(self, data_dir, demos, embedder=None, max_loaded=6, max_stored=100):
        self.root = os.path.join(data_dir, "repos")
        self.demos = demos
        self.embedder = embedder
        self.max_loaded = max_loaded
        self.max_stored = max_stored
        self.loaded = OrderedDict()
        self.lock = threading.RLock()

    def directory(self, repo_id):
        return os.path.join(self.root, repo_id)

    def exists(self, repo_id):
        return repo_id in self.demos or os.path.exists(
            os.path.join(self.directory(repo_id), "demo.json"))

    def summary(self, repo_id):
        """info()-like dict from demo.json without loading the index, or None."""
        loaded = self.demos.get(repo_id) or self.loaded.get(repo_id)
        if loaded is not None:
            return loaded.info()
        path = os.path.join(self.directory(repo_id), "demo.json")
        if not _REPO_ID.match(repo_id) or not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
        keys = ("id", "name", "description", "url", "sha", "root", "questions", "stats", "built",
                "versions", "kind", "ref", "private")
        return {**{k: meta.get(k) for k in keys}, "languages": (meta.get("stats") or {}).get("languages", {})}

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
                return self.loaded[repo_id]
            if not os.path.exists(os.path.join(self.directory(repo_id), "demo.json")):
                return None
            self._keep(repo_id, load_repo_dir(self.directory(repo_id), self.embedder))
            return self.loaded[repo_id]

    def add(self, demo):
        """Register a freshly indexed visitor repo and enforce the disk limit."""
        with self.lock:
            self._keep(demo.id, demo)
            os.makedirs(self.root, exist_ok=True)
            stored = sorted((d for d in os.listdir(self.root)
                             if os.path.exists(os.path.join(self.root, d, "demo.json"))),
                            key=lambda d: os.path.getmtime(os.path.join(self.root, d, "demo.json")))
            for old in stored[:max(len(stored) - self.max_stored, 0)]:
                if old != demo.id:
                    self.loaded.pop(old, None)
                    rmtree(os.path.join(self.root, old))

    def _keep(self, repo_id, demo):
        self.loaded[repo_id] = demo
        self.loaded.move_to_end(repo_id)
        while len(self.loaded) > self.max_loaded:
            self.loaded.popitem(last=False)


# owner__repo__sha12, lowercase: also guards against path tricks in URLs
_REPO_ID = re.compile(r"^[a-z0-9-]{1,39}__[a-z0-9._-]{1,100}__[0-9a-f]{12}$")


def repo_id_for(owner, repo, sha):
    return f"{owner}__{repo}__{sha[:12]}".lower()


def repo_name_of(repo_id):
    """'owner/repo' (lowercase) of a visitor repo id; the repo name itself may contain '__'."""
    head = repo_id.rsplit("__", 1)[0]
    owner, _, repo = head.partition("__")
    return f"{owner}/{repo}"
