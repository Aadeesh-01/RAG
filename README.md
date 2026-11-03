# coderag: code-aware RAG over Python repositories

Ask natural-language questions about a Python codebase. Retrieval is built up in steps, from a naive
fixed-window baseline to AST chunking with call/inheritance-graph expansion:

| Version | Adds |
|---|---|
| `v0` | Baseline: concatenate files, fixed 1000-char windows (200 overlap), flan-t5-base |
| `v1` | AST chunking: one chunk per top-level class/function; Qwen2.5-Coder-1.5B-Instruct |
| `v2` | Keyword filter on vector hits, keep top 3 |
| `v3` | Context cleaning (signatures, returns, keyword lines) |
| `v4` | Structural metadata (calls, bases, methods, docstrings), dedup, `called_by` index, 1-hop graph expansion |
| `v5` | Multi-hop BFS expansion (`max_hops`, `max_expansion`, `include_callers`) and a hop-weighted context budget |
| `v6` | Fixes found by the eval: index large files, method-level chunks, larger raw context, caller-aware expansion |
| `v7` | Scope-aware call resolution (imports, `self`/`super`, external modules) instead of bare-name matching. **Default.** |

Each version is a preset of a single `RAGConfig` ([coderag/config.py](coderag/config.py)), so comparing
versions means changing one flag. The code is not copied per version. v0–v5 are kept unchanged as the
research record.

## Evaluation

[eval/](eval/) scores the context each version would send to the LLM against hand-labelled gold symbols
on `sample_repo`, [psf/requests](https://github.com/psf/requests) and
[pallets/click](https://github.com/pallets/click). On the real repos, v7 more than doubles v5's recall
(requests 0.38 → 0.83, click 0.33 → 0.88) and beats the naive v0 baseline. See
[eval/README.md](eval/README.md) for the method, full results and findings.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on Linux/macOS)
pip install -e ".[dev]"         # or: pip install -r requirements.txt
```

The first run downloads `all-MiniLM-L6-v2` (~90 MB) and the LLM: Qwen2.5-Coder-1.5B-Instruct (~3 GB)
for v1–v5, or flan-t5-base (~1 GB) for v0. Everything runs on CPU. Set `device="cuda"` in the config to use a GPU.

## Usage

Commands default to the bundled [sample_repo/](sample_repo/) and the `v7` preset. Pass `--repo PATH` and
`--version vN` to change them.

```bash
python -m coderag versions                         # list presets
python -m coderag chunks --version v4              # chunks + metadata, no models needed
python -m coderag ask "How is data processed?"     # one question
python -m coderag ask                              # interactive session (models load once)
python -m coderag ask "..." --retrieve-only        # print the LLM context, skip the LLM
python -m coderag index --repo PATH --out .coderag/myrepo    # build once, save to disk
python -m coderag ask "..." --repo PATH --index .coderag/myrepo   # reuse; rebuilds if code/config changed
python -m coderag ask "..." --max-hops 1 --include-callers
python -m coderag demo --version v3                # the original demo queries for a version
python -m coderag graph -o chunk_graph.html        # interactive call/inheritance graph
```

From Python:

```python
from coderag import RAGCodebaseAgent, get_config

agent = RAGCodebaseAgent("path/to/repo", get_config("v7", top_k=6))
agent.query("Which functions call the data processing logic?", include_callers=True)

agent.save(".coderag/myrepo")                       # chunks + call graph + FAISS index
agent = RAGCodebaseAgent.load(".coderag/myrepo")    # no repo, re-chunking or re-embedding needed
```

A saved index stores the config fields that shape it (chunking, embedding model, call resolution)
and refuses to load with different ones. Query-time settings (`top_k`, hops, context budget, LLM) can
be changed when loading.

## Layout

```
coderag/
  config.py     RAGConfig + v0–v5 presets
  chunking.py   file discovery, fixed-window and AST chunkers, metadata, dedup
  graph.py      name-based call graph (v4-v6), called_by index, BFS expansion, graph export data
  resolve.py    scope-aware call resolution (v7): imports, self/super, re-exports
  index.py      sentence-transformers + FAISS vector index
  persist.py    save/load an index, freshness check (source fingerprint + config)
  context.py    keyword filter, chunk cleaning, headers, hop-weighted budgets
  llm.py        flan-t5 (seq2seq) and Qwen chat backends
  agent.py      RAGCodebaseAgent: ties the pipeline together
  visualize.py  D3 chunk-graph HTML
  cli.py        command-line interface
sample_repo/    toy target: inheritance zoo + CSV -> clean -> SQLite pipeline
eval/           retrieval benchmark: question sets, harness, results
tests/          pytest suite (no ML models needed: uses fake index/LLM)
```

## Tests

```bash
pytest
```

## History

The project began as one standalone script per version (`baseline/baseline.py`, `v1_chunking.py` …
`v5_multihop.py`, `visualize_graph.py`). Those scripts are preserved in commit `f873a1c`
(`git show f873a1c:v5_multihop.py`). The package reproduces their chunks, metadata and LLM
contexts exactly.
