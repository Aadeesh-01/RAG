"""RAGCodebaseAgent: chunk -> embed -> retrieve (+ graph expansion) -> prompt -> answer."""

import os
import time
from dataclasses import asdict, dataclass, field

from .chunking import load_chunks
from .config import get_config
from .context import (asks_for_callers, build_context, display_name, keyword_filter,
                      parse_citations, source_location)
from .graph import build_name_index, build_reverse_index, expand_bfs
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

    Use save(directory) / RAGCodebaseAgent.load(directory) to reuse an index
    without re-chunking and re-embedding the repo.
    """

    def __init__(self, repo_path, config=None, index=None, llm=None,
                 load_llm=True, verbose=True):
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
            code_graph = CodeGraph(documents, repo_path, self.config.max_ambiguous)
            code_graph.apply_called_by()
            self._log(f"[RAG] Scoped call graph: {code_graph.edge_counts()}")
        elif self.config.structural:
            build_reverse_index(documents, verbose)
        self._set_documents(documents, code_graph)

        if index is None:
            from .index import VectorIndex
            index = VectorIndex(self.config.embed_model)
        self.index = index
        self.index.build(self._embed_texts())
        self._set_llm(llm, load_llm)

    def _set_documents(self, documents, code_graph):
        self.documents = self.metadata = documents
        self.code_graph = code_graph
        self._name_index = build_name_index(documents)
        self.chunks = [d["content"] for d in documents]
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
    def retrieve(self, question, config=None):
        """Return [(chunk, meta, hop)]: vector hits (hop 0) then graph expansions."""
        cfg = config or self.config

        results = []
        seeds = []
        seen_keys = set()

        self._log("\n--- RETRIEVED CHUNKS (vector search) ---")
        for rank, idx in enumerate(self.index.search(question, cfg.top_k)):
            chunk, meta = self.chunks[idx], self.metadata[idx]
            if cfg.dedup_prefix:
                key = chunk[:80]                # cheap content fingerprint
                if key in seen_keys:
                    continue
                seen_keys.add(key)
            seeds.append(idx)
            results.append((chunk, meta, 0))
            self._log_hit(rank, meta, chunk)

        if cfg.keyword_filter:
            results = keyword_filter(question, results)
        if cfg.max_direct is not None:
            results = results[:cfg.max_direct]

        # Expansion is seeded from every vector hit, including any the keyword
        # filter dropped, and in set order (both as in v4/v5).
        include_callers = cfg.include_callers or (cfg.auto_callers and asks_for_callers(question))
        expansions = expand_bfs(
            self.metadata, self._name_index, set(seeds),
            max_hops=cfg.max_hops,
            max_expansion=cfg.max_expansion,
            include_callers=include_callers,
            code_graph=self.code_graph,
        )
        if expansions:
            self._log(f"\n[EXPAND] Graph BFS added {len(expansions)} chunk(s) "
                      f"(max_hops={cfg.max_hops}):")
            for idx, hop in expansions:
                meta = self.metadata[idx]
                self._log(f"  [hop={hop}] {meta['type']} '{meta['name']}'  "
                          f"({os.path.basename(meta['file'])})")
                results.append((self.chunks[idx], meta, hop))

        return results

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

    def stream(self, question, llm=None, **overrides):
        """
        Answer `question` as a stream of events (dicts with a "type"):
          sources  {"sources": [Source dicts], "retrieve_ms"}   always first
          token    {"text"}                                     answer pieces
          done     {"backend", "cited", "invalid_citations", "llm_ms"}
          error    {"code", "message"}   rate_limited | unavailable | llm_error | no_llm
        With no LLM (llm=None and none loaded) only sources + error/no_llm are
        emitted: the retrieved code is still useful on its own.
        Keyword overrides (top_k, max_hops, prompt_style, ...) apply to this call only.
        """
        cfg = self.config.with_overrides(**overrides)
        llm = llm or self.llm
        self._log(f"\n{'='*60}\nQUERY: {question}")
        if overrides:
            self._log(f"OVERRIDES: {overrides}")

        t0 = time.perf_counter()
        retrieved = self.retrieve(question, cfg)
        context = build_context(question, retrieved, cfg)
        sources = [Source.from_meta(n, meta, hop) for n, (_, meta, hop) in enumerate(retrieved, 1)]
        yield {"type": "sources", "sources": [asdict(s) for s in sources], "context": context,
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

    @classmethod
    def from_meta(cls, n, meta, hop):
        path, lo, hi = source_location(meta)
        return cls(id=n, name=display_name(meta), kind=meta["type"], file=path,
                   start_line=lo, end_line=hi, hop=hop, qname=meta.get("qname", meta["name"]))


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
