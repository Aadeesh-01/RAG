"""RAGCodebaseAgent: chunk -> embed -> retrieve (+ graph expansion) -> prompt -> answer."""

import os

from .chunking import load_chunks
from .config import get_config
from .context import asks_for_callers, build_context, display_name, keyword_filter
from .graph import build_name_index, build_reverse_index, expand_bfs


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

    def query(self, question, **overrides):
        """
        Answer `question`. Keyword overrides (top_k, max_hops, max_expansion,
        include_callers, ...) apply to this call only.
        """
        cfg = self.config.with_overrides(**overrides)
        self._log(f"\n{'='*60}\nQUERY: {question}")
        if overrides:
            self._log(f"OVERRIDES: {overrides}")

        context = self.build_context(question, cfg)
        self._log(f"\n--- CONTEXT SENT TO LLM ---\n{context[:1000]}")

        if self.llm is None:
            raise RuntimeError("LLM not loaded (agent was created with load_llm=False)")
        answer = self.llm.generate(context, question)
        self._log(f"\n--- MODEL OUTPUT ---\n{answer}")
        return answer
