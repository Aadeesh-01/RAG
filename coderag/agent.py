"""RAGCodebaseAgent: chunk -> embed -> retrieve (+ graph expansion) -> prompt -> answer."""

import os
import time
from dataclasses import asdict, dataclass, field

from .chunking import load_chunks
from .config import get_config
from .repomap import is_broad_question, repo_map
from .context import (asks_for_callers, build_context, display_name, format_chunk, public_name,
                      keyword_filter, parse_citations, source_location)
from .graph import build_name_index, build_reverse_index, expand_bfs
from .languages import language_of
from .llm import LLMError, LLMRateLimited, LLMUnavailable


class RAGCodebaseAgent:
    """
    Parameters
    ----------
    repo_path : folder to index
    config    : RAGConfig (defaults to the default preset)
    index     : object with build(texts) / search(query, k) -> [int];
                defaults to a FAISS VectorIndex (injectable for tests)
    llm       : object with generate(context, question) -> str;
                defaults to the backend named in the config
    load_llm  : False skips loading the LLM (retrieval-only use)
    progress  : optional callback(stage, fraction) reported while embedding

    Use save(directory) / RAGCodebaseAgent.load(directory) to reuse an index
    without re-chunking and re-embedding the repo.
    """

    def __init__(self, repo_path, config=None, index=None, llm=None,
                 load_llm=True, verbose=True, progress=None):
        self.config = config or get_config()
        self.verbose = verbose
        self.repo_path = os.path.abspath(repo_path)
        self._log(f"[RAG] Loading repo from: {repo_path}  (config {self.config.name})")

        documents = load_chunks(repo_path, self.config, verbose)
        if not documents:
            raise ValueError("No chunks found. Check repo path and file filters.")

        code_graph = None
        if self.config.structural and self.config.call_resolution == "scoped":
            from .resolve import CodeGraph
            code_graph = CodeGraph(documents, repo_path, self.config.max_ambiguous,
                                   self.config.ignore_assert_calls)
            code_graph.apply_called_by()
            self._log(f"[RAG] Scoped call graph: {code_graph.edge_counts()}")
            if self.config.bridges:
                from .bridges import detect
                code_graph.set_bridges(detect(documents, self.config.bridges))
                self._log(f"[RAG] Bridge edges: {code_graph.bridges.counts()}")
        elif self.config.structural:
            build_reverse_index(documents, verbose)
        self._set_documents(documents, code_graph)

        if index is None:
            from .index import VectorIndex
            index = VectorIndex(self.config.embed_model)
        self.index = index
        if progress is not None:          # progress(stage, fraction) while embedding
            self.index.build(self._embed_texts(), progress=lambda f: progress("embedding", f))
        else:
            self.index.build(self._embed_texts())
        self._set_llm(llm, load_llm)

    def _set_documents(self, documents, code_graph):
        self.documents = self.metadata = documents
        self.code_graph = code_graph
        self._name_index = build_name_index(documents)
        self.chunks = [d["content"] for d in documents]
        self._position = {id(d): i for i, d in enumerate(documents)}   # meta -> chunk index
        self._log(f"[RAG] {len(self.chunks)} chunks ready.")

    def _set_llm(self, llm, load_llm):
        if llm is None and load_llm:
            from .llm import load_llm as _load_llm
            self._log(f"[RAG] Loading LLM: {self.config.llm_model} ...")
            llm = _load_llm(self.config)
        self.llm = llm
        self._log("[RAG] Ready.\n")

    # =========================================================================
    # Persistence (see persist.py)
    # =========================================================================
    def save(self, directory):
        """Save chunks, call graph and vector index to `directory`."""
        from .persist import save_agent
        save_agent(self, directory)

    @classmethod
    def load(cls, directory, config=None, llm=None, load_llm=True, verbose=True, embedder=None):
        """
        Load a saved index; the repo itself is not needed. `config` may change
        query-time settings (top_k, hops, context, LLM) but not how the index
        was built. `embedder`: an already-loaded embedding model to reuse.
        """
        from .persist import load_agent
        return load_agent(cls, directory, config, llm, load_llm, verbose, embedder)

    @classmethod
    def load_or_build(cls, repo_path, directory, config=None, llm=None, load_llm=True,
                      verbose=True):
        """Load the index in `directory` if it is current for `repo_path`, else build and save."""
        from .persist import load_or_build
        return load_or_build(cls, repo_path, directory, config or get_config(), llm,
                             load_llm, verbose)

    def _log(self, *args):
        if self.verbose:
            print(*args)

    def _embed_texts(self):
        if self.config.structural:
            # name + docstring + code gives a richer semantic signal
            return [f"{display_name(d)}: {d['docstring']}\n{d['content']}" for d in self.documents]
        return self.chunks

    # =========================================================================
    # Retrieval
    # =========================================================================
    def retrieve(self, question, config=None, trace=None, vias=None):
        """
        Return [(chunk, meta, hop)]: vector hits (hop 0) then graph expansions.
        `trace` (a dict) is filled with chunk index -> {"rank", "similarity"} for
        vector hits and {"via": (from_index, edge)} for expansions.
        `vias` (a dict) gets only the expansions' chunk index -> (from_index, edge);
        unlike `trace` it doesn't switch vector search to the scored variant.
        """
        cfg = config or self.config

        results = []
        seeds = []
        seen_keys = set()

        scored = getattr(self.index, "search_scored", None)
        hits = (scored(question, cfg.top_k) if scored and trace is not None
                else [(i, None) for i in self.index.search(question, cfg.top_k)])

        self._log("\n--- RETRIEVED CHUNKS (vector search) ---")
        for rank, (idx, similarity) in enumerate(hits):
            chunk, meta = self.chunks[idx], self.metadata[idx]
            if cfg.dedup_prefix:
                key = chunk[:80]                # cheap content fingerprint
                if key in seen_keys:
                    continue
                seen_keys.add(key)
            seeds.append(idx)
            results.append((chunk, meta, 0))
            if trace is not None:
                trace[idx] = {"rank": rank + 1, "similarity": similarity}
            self._log_hit(rank, meta, chunk)

        if cfg.keyword_filter:
            results = keyword_filter(question, results)
        if cfg.max_direct is not None:
            results = results[:cfg.max_direct]

        # Expansion is seeded from every vector hit, including any the keyword
        # filter dropped (as in v4/v5). v4-v7 expand seeds in set order, i.e. by
        # chunk index; seed_order="rank" expands the best hit first.
        asked_callers = cfg.auto_callers and asks_for_callers(question)
        expansions = expand_bfs(
            self.metadata, self._name_index,
            seeds if cfg.seed_order == "rank" else set(seeds),
            max_hops=cfg.max_hops,
            max_expansion=cfg.max_expansion,
            include_callers=cfg.include_callers or asked_callers,
            code_graph=self.code_graph,
            with_via=True,
            callers_first=cfg.callers_first and asked_callers,
        )
        if expansions:
            self._log(f"\n[EXPAND] Graph BFS added {len(expansions)} chunk(s) "
                      f"(max_hops={cfg.max_hops}):")
            for idx, hop, via in expansions:
                if trace is not None:
                    trace[idx] = {"via": via}
                if vias is not None:
                    vias[idx] = via
                meta = self.metadata[idx]
                self._log(f"  [hop={hop}] {meta['type']} '{meta['name']}'  "
                          f"({os.path.basename(meta['file'])})")
                results.append((self.chunks[idx], meta, hop))

        return results

    def repo_map(self):
        """Map of the whole repository (built once from the chunks)."""
        if getattr(self, "_repo_map", None) is None:
            self._repo_map = repo_map(self.documents)
        return self._repo_map

    def _via_path(self, idx, vias):
        """Chunk indexes on the call-graph path to chunk `idx`, from the vector hit: [a, b]."""
        path, node, seen = [], idx, {idx}
        while node in vias:
            node = vias[node][0]
            if node in seen:                    # defensive: a path never revisits a chunk
                break
            seen.add(node)
            path.append(node)
        return path[::-1]

    def _mark_bridge(self, source, vias):
        """A source reached over a bridge edge says which one ("HTTP POST /api/repos")."""
        prev, edge = vias.get(source.chunk_id, (None, ""))
        if not (edge or "").startswith(("bridge", "bridged by")) or self.code_graph is None:
            return
        bridge = self.code_graph.bridges.between(prev, source.chunk_id)
        if bridge is not None:
            source.bridge = {"mechanism": bridge.mechanism, "confidence": bridge.confidence,
                             "label": bridge.label, "evidence": bridge.evidence,
                             "direction": "forward" if bridge.dst == source.chunk_id else "backward"}
            source.confidence = bridge.confidence

    def _explain(self, idx, trace, chunk, meta, hop, question, cfg, n):
        """Debug info for one source: how it was retrieved and what it costs."""
        info = {"context_chars": len(format_chunk(chunk, meta, hop, question, cfg, n))}
        path, node = [], idx
        while "via" in trace.get(node, {}):
            prev, edge = trace[node]["via"]
            path.append({"from": display_name(self.metadata[prev]), "edge": edge,
                         "to": display_name(self.metadata[node])})
            node = prev
        if path:
            info["path"] = path[::-1]           # from the vector hit outwards
        seed = trace.get(node, {})
        info["seed"] = display_name(self.metadata[node])
        info["rank"] = seed.get("rank")
        info["similarity"] = (round(seed["similarity"], 3)
                              if seed.get("similarity") is not None else None)
        return info

    def _log_hit(self, rank, meta, chunk):
        if not self.verbose:
            return
        where = f"  ({os.path.basename(meta['file'])})" if meta["file"] else ""
        print(f"\n[hop=0 | rank={rank+1}] {meta['type'].upper()}  {meta['name']}{where}")
        if meta.get("base_classes"):
            print(f"     extends  : {meta['base_classes']}")
        if meta.get("calls"):
            print(f"     calls    : {meta['calls'][:5]}")
        if meta.get("called_by"):
            print(f"     called_by: {meta['called_by'][:5]}")
        print(chunk[:200])

    # =========================================================================
    # Public query interface
    # =========================================================================
    def build_context(self, question, config=None):
        cfg = config or self.config
        return build_context(question, self.retrieve(question, cfg), cfg)

    def stream(self, question, llm=None, debug=False, **overrides):
        """
        Answer `question` as a stream of events (dicts with a "type"):
          sources  {"sources": [Source dicts], "retrieve_ms"}   always first
          token    {"text"}                                     answer pieces
          done     {"backend", "cited", "invalid_citations", "llm_ms"}
          error    {"code", "message"}   rate_limited | unavailable | llm_error | no_llm
        With no LLM (llm=None and none loaded) only sources + error/no_llm are
        emitted: the retrieved code is still useful on its own.
        Keyword overrides (top_k, max_hops, prompt_style, ...) apply to this call only.
        With debug=True each source carries a "debug" dict: vector rank and
        similarity, or the call-graph path that pulled it in, and its context size.
        """
        cfg = self.config.with_overrides(**overrides)
        llm = llm or self.llm
        self._log(f"\n{'='*60}\nQUERY: {question}")
        if overrides:
            self._log(f"OVERRIDES: {overrides}")

        t0 = time.perf_counter()
        trace = {} if debug else None
        vias = {}
        retrieved = self.retrieve(question, cfg, trace, vias)
        context = build_context(question, retrieved, cfg)
        # "what are the main modules?": a few chunks can't show the whole repo, the map can
        with_map = cfg.prompt_style == "cited" and is_broad_question(question)
        if with_map:
            context = f"{self.repo_map()}\n\n---\n\n{context}"
        sources = [Source.from_meta(n, meta, hop, self._position[id(meta)],
                                    edge=vias.get(self._position[id(meta)], (None, None))[1])
                   for n, (_, meta, hop) in enumerate(retrieved, 1)]
        for source in sources:
            if source.hop:
                source.via_ids = self._via_path(source.chunk_id, vias)
                source.via = [public_name(self.metadata[i]) for i in source.via_ids]
                self._mark_bridge(source, vias)
        if debug:
            for n, (source, (chunk, meta, hop)) in enumerate(zip(sources, retrieved), 1):
                source.debug = self._explain(source.chunk_id, trace, chunk, meta, hop,
                                             question, cfg, n)
        yield {"type": "sources", "sources": [asdict(s) for s in sources], "context": context,
               "repo_map": with_map,
               "retrieve_ms": round((time.perf_counter() - t0) * 1000, 1)}
        self._log(f"\n--- CONTEXT SENT TO LLM ---\n{context[:1000]}")

        if llm is None:
            yield {"type": "error", "code": "no_llm", "message": "No LLM configured; "
                   "showing retrieved sources only."}
            return

        t1 = time.perf_counter()
        pieces = []
        try:
            for piece in llm.stream(context, question, cfg):
                pieces.append(piece)
                yield {"type": "token", "text": piece}
        except LLMRateLimited as e:
            yield {"type": "error", "code": "rate_limited", "message": str(e),
                   "retry_after": e.retry_after}
            return
        except LLMUnavailable as e:
            yield {"type": "error", "code": "unavailable", "message": str(e)}
            return
        except LLMError as e:
            yield {"type": "error", "code": "llm_error", "message": str(e)}
            return

        answer = "".join(pieces)
        cited, invalid = parse_citations(answer, len(sources))
        yield {"type": "done", "backend": llm.name, "cited": cited,
               "invalid_citations": invalid,
               "llm_ms": round((time.perf_counter() - t1) * 1000, 1)}

    def ask(self, question, llm=None, **overrides):
        """Answer `question` and return a QueryResult (see stream() for the arguments)."""
        result = QueryResult(question=question,
                             version=self.config.with_overrides(**overrides).name)
        pieces = []
        for event in self.stream(question, llm, **overrides):
            kind = event["type"]
            if kind == "sources":
                result.sources = [Source(**s) for s in event["sources"]]
                result.context = event["context"]
                result.timings["retrieve_ms"] = event["retrieve_ms"]
            elif kind == "token":
                pieces.append(event["text"])
            elif kind == "done":
                result.backend = event["backend"]
                result.cited = event["cited"]
                result.invalid_citations = event["invalid_citations"]
                result.timings["llm_ms"] = event["llm_ms"]
            elif kind == "error":
                result.error = event
        result.answer = "".join(pieces) if pieces else None
        self._log(f"\n--- MODEL OUTPUT ({result.backend or 'no answer'}) ---\n"
                  f"{result.answer or result.error}")
        return result

    def query(self, question, **overrides):
        """Answer text only. Raises if no LLM is loaded or the LLM call fails."""
        if self.llm is None:
            raise RuntimeError("LLM not loaded (agent was created with load_llm=False)")
        result = self.ask(question, **overrides)
        if result.error:
            raise LLMError(f"{result.error['code']}: {result.error['message']}")
        return result.answer


@dataclass
class Source:
    """One retrieved chunk as shown to the LLM ([S<id>]) and to users."""
    id: int
    name: str           # display name, e.g. "Session.send"
    kind: str           # class | method | function | file | text
    file: str           # repo-relative path
    start_line: int
    end_line: int
    hop: int            # 0 = vector hit, n = reached through the call graph
    qname: str
    chunk_id: int = -1  # position in agent.documents (= graph node id)
    via: list | None = None     # hop > 0: names on the call-graph path from the vector hit
    via_ids: list | None = None  # the same path as chunk ids (graph node ids), for drawing it
    debug: dict | None = None   # stream(debug=True): rank/similarity or graph path
    relation: str = "match"     # how it relates to the code that reached it, see RELATIONS
    language: str = "python"    # python | javascript | go ... (languages.py)
    confidence: str | None = None   # hop > 0: "resolved" or "name_match" (the edge that reached it);
                                    # over a bridge, the bridge's: "exact" or "pattern"
    bridge: dict | None = None      # reached over a bridge: mechanism, confidence, label, evidence

    @classmethod
    def from_meta(cls, n, meta, hop, chunk_id=-1, edge=None):
        path, lo, hi = source_location(meta)
        return cls(id=n, name=public_name(meta), kind=meta["type"], file=path,
                   start_line=lo, end_line=hi, hop=hop, qname=meta.get("qname", meta["name"]),
                   chunk_id=chunk_id, relation=relation_of(hop, edge), language=language_of(meta),
                   confidence=edge_confidence(edge, language_of(meta)) if hop else None)


# A source's relation to the chunk that reached it (the previous step on its path):
#   match   hop 0, a vector hit
#   callee  reached along a call: the previous chunk calls it
#   caller  reached along called_by: it calls the previous chunk
#   parent  a base class of the previous chunk
#   child   a subclass of the previous chunk
#   method  membership: the class a matched method belongs to, or a method of a matched class
RELATIONS = ("match", "callee", "caller", "parent", "child", "method")


def edge_confidence(edge, language="python"):
    """
    'resolved' or 'name_match' for the edge that reached a chunk: only Python
    edges from the scope-aware resolver are resolved; the name-index fallback
    ("name match") and every edge in another language were matched by name.
    """
    if language != "python" or not edge or edge == "name match" or edge.endswith(":name_match"):
        return "name_match"
    return "resolved"


def relation_of(hop, edge):
    """Map a typed_neighbors edge label ("calls:exact", "called by:method", "inherits",
    "member of", or the name-index fallback "name match") onto RELATIONS."""
    if not hop:
        return "match"
    label = (edge or "").split(":", 1)[0].strip()
    return {"calls": "callee", "called by": "caller", "inherits": "parent",
            "subclass": "child", "member of": "method",
            "bridge": "callee", "bridged by": "caller"}.get(label, "callee")


@dataclass
class QueryResult:
    question: str
    version: str
    sources: list = field(default_factory=list)
    context: str = ""
    answer: str | None = None           # None: retrieve-only, or the LLM failed
    backend: str | None = None
    cited: list = field(default_factory=list)              # valid [S<n>] ids in the answer
    invalid_citations: list = field(default_factory=list)  # [S<n>] ids that don't exist
    error: dict | None = None
    timings: dict = field(default_factory=dict)
