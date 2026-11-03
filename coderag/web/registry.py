"""
Demo repositories: building their indexes (offline) and loading them (server).

    <data_dir>/demos/<demo_id>/demo.json      name, url, sha, questions, stats
    <data_dir>/demos/<demo_id>/<version>/     saved index (see coderag/persist.py)
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import yaml

from ..agent import RAGCodebaseAgent
from ..config import get_config
from ..gitfetch import fetch_commit, rmtree

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

    def info(self):
        return {"id": self.id, "name": self.name, "description": self.description,
                "url": self.url, "sha": self.sha, "root": self.root, "questions": self.questions,
                "stats": self.stats, "built": self.built, "versions": self.versions}


def _stats(agent):
    edges = agent.code_graph.edge_counts() if agent.code_graph else {}
    return {"files": len({d["qname"].split("::")[0] for d in agent.documents if "qname" in d}),
            "chunks": len(agent.documents), "call_edges": sum(edges.values())}


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
            stats = _stats(agent)
            log(f"[demos] {entry['id']} {version}: {stats}")

        meta = {k: entry.get(k, "") for k in ("id", "name", "description", "url", "sha", "root")}
        meta.update(questions=entry.get("questions", []), stats=stats, versions=versions, order=order,
                    built=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with open(os.path.join(out, "demo.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

    if not keep_clones and os.path.isdir(clones):
        rmtree(clones)


# =============================================================================
# LOAD (server startup)
# =============================================================================
def load_demos(data_dir, embedder=None):
    """{demo_id: Demo} for every built demo in `data_dir`, indexes loaded."""
    demos = {}
    root = os.path.join(data_dir, "demos")
    if not os.path.isdir(root):
        return demos
    for demo_id in sorted(os.listdir(root)):
        meta_path = os.path.join(root, demo_id, "demo.json")
        if not os.path.exists(meta_path):
            continue
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        versions = meta.pop("versions", [])
        demo = Demo(**meta)
        for version in versions:
            demo.agents[version] = RAGCodebaseAgent.load(
                os.path.join(root, demo_id, version), load_llm=False, verbose=False,
                embedder=embedder)
        demos[demo_id] = demo
    return dict(sorted(demos.items(), key=lambda item: item[1].order))
