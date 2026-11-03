"""
Saving and loading a built index, so a repo is chunked and embedded once.

Layout of an index directory:
    meta.json     format, config, source fingerprint, sizes (written last)
    chunks.json   chunk dicts with metadata (called_by already filled in)
    graph.json    resolved call graph (scoped call resolution only)
    faiss.index   serialized FAISS index

The repo itself is not needed to load an index.
"""

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone

from .chunking import source_fingerprint
from .config import RAGConfig

INDEX_FORMAT = 1
META_FILE, CHUNKS_FILE, GRAPH_FILE = "meta.json", "chunks.json", "graph.json"


def _write_json(directory, name, data):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
        json.dump(data, f)


def _read_json(directory, name):
    with open(os.path.join(directory, name), encoding="utf-8") as f:
        return json.load(f)


def save_agent(agent, directory):
    os.makedirs(directory, exist_ok=True)
    meta_path = os.path.join(directory, META_FILE)
    if os.path.exists(meta_path):
        os.remove(meta_path)            # an interrupted save must not look complete

    cfg = agent.config
    _write_json(directory, CHUNKS_FILE, agent.documents)
    graph_path = os.path.join(directory, GRAPH_FILE)
    if agent.code_graph is not None:
        _write_json(directory, GRAPH_FILE, agent.code_graph.to_dict())
    elif os.path.exists(graph_path):
        os.remove(graph_path)
    agent.index.save(directory)

    _write_json(directory, META_FILE, {
        "format":      INDEX_FORMAT,
        "created":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_path":   agent.repo_path,
        "source_hash": source_fingerprint(agent.repo_path, cfg.extensions, cfg.prune_dirs),
        "n_chunks":    len(agent.documents),
        "config":      asdict(cfg),
    })
    agent._log(f"[RAG] Saved index ({len(agent.documents)} chunks) -> {directory}")


def read_meta(directory):
    """meta.json of a complete index, or None."""
    try:
        meta = _read_json(directory, META_FILE)
    except (OSError, ValueError):
        return None
    return meta if meta.get("format") == INDEX_FORMAT else None


def check_index(directory, config, repo_path=None):
    """(usable, reason): whether the index in `directory` fits `config` and `repo_path`."""
    meta = read_meta(directory)
    if meta is None:
        return False, "no saved index"
    mismatch = RAGConfig.from_dict(meta["config"]).index_mismatch(config)
    if mismatch:
        return False, f"built with different {', '.join(mismatch)}"
    if repo_path is not None and os.path.isdir(repo_path):    # gone repo: trust the index
        if os.path.normcase(os.path.abspath(repo_path)) != os.path.normcase(meta["repo_path"]):
            return False, f"built for another repo ({meta['repo_path']})"
        current = source_fingerprint(repo_path, config.extensions, config.prune_dirs)
        if current != meta["source_hash"]:
            return False, "source files changed"
    return True, "up to date"


def load_agent(cls, directory, config=None, llm=None, load_llm=True, verbose=True,
               embedder=None):
    from .index import VectorIndex

    meta = read_meta(directory)
    if meta is None:
        raise FileNotFoundError(f"No saved index in {directory}")
    saved = RAGConfig.from_dict(meta["config"])
    if config is None:
        config = saved
    elif saved.index_mismatch(config):
        raise ValueError(f"Index in {directory} was built with different "
                         f"{', '.join(saved.index_mismatch(config))}; rebuild it")

    agent = cls.__new__(cls)
    agent.config = config
    agent.verbose = verbose
    agent.repo_path = meta["repo_path"]
    agent._log(f"[RAG] Loading index from: {directory}  (config {config.name}, "
               f"built {meta['created']})")

    documents = _read_json(directory, CHUNKS_FILE)
    code_graph = None
    if os.path.exists(os.path.join(directory, GRAPH_FILE)):
        from .resolve import CodeGraph
        code_graph = CodeGraph.from_dict(documents, _read_json(directory, GRAPH_FILE))
    agent._set_documents(documents, code_graph)

    agent.index = VectorIndex.load(directory, config.embed_model, embedder)
    if agent.index.size != len(documents):
        raise ValueError(f"Corrupt index in {directory}: {agent.index.size} vectors "
                         f"for {len(documents)} chunks")
    agent._set_llm(llm, load_llm)
    return agent


def load_or_build(cls, repo_path, directory, config, llm=None, load_llm=True, verbose=True):
    usable, reason = check_index(directory, config, repo_path)
    if usable:
        return load_agent(cls, directory, config, llm, load_llm, verbose)
    if verbose:
        print(f"[RAG] Building index ({reason}) ...")
    agent = cls(repo_path, config, llm=llm, load_llm=load_llm, verbose=verbose)
    agent.save(directory)
    return agent
