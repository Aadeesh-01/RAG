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

The first run downloads `all-MiniLM-L6-v2` (~90 MB). Answers come from one of these:

| `--llm` | Model | Notes |
|---|---|---|
| `local` (default) | Qwen2.5-Coder-1.5B-Instruct (~3 GB download), flan-t5-base for v0 | offline, CPU, about 1 minute per answer |
| `groq` | `openai/gpt-oss-120b` | free tier, about 1 s per answer, needs `GROQ_API_KEY` |
| `openrouter` / `gemini` | provider default, or `--model` | needs `OPENROUTER_API_KEY` / `GEMINI_API_KEY` |
| `none` | | retrieved sources only |

Set `CODERAG_LLM=groq` to make a remote model the default. API keys are read from the environment and
never logged.

Remote models answer with numbered, cited sources (`--prompt cited`). In the answer eval,
gpt-oss-120b made up no callers and cited sources in 92% of answers, while Qwen 1.5B invented
callers and can't follow the citation format. See [eval/README.md](eval/README.md#answer-quality).

## Usage

Commands default to the bundled [sample_repo/](sample_repo/) and the `v7` preset. Pass `--repo PATH` and
`--version vN` to change them.

```bash
python -m coderag versions                         # list presets
python -m coderag chunks --version v4              # chunks + metadata, no models needed
python -m coderag ask "How is data processed?"     # one question
python -m coderag ask "Who calls resolve_redirects?" --llm groq   # remote model, cited answer
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

result = agent.ask("Who calls resolve_redirects?", prompt_style="cited")
result.answer, result.cited, result.sources         # answer, cited [S<n>] ids, Source records
for event in agent.stream("..."):                   # sources first, then tokens, then done
    ...

agent.save(".coderag/myrepo")                       # chunks + call graph + FAISS index
agent = RAGCodebaseAgent.load(".coderag/myrepo")    # no repo, re-chunking or re-embedding needed
```

A saved index stores the config fields that shape it (chunking, embedding model, call resolution)
and refuses to load with different ones. Query-time settings (`top_k`, hops, context budget, LLM) can
be changed when loading.

## Web server

A FastAPI server answers questions about pre-indexed demo repositories
([demos.yaml](demos.yaml): `sample_repo`, psf/requests and pallets/click, pinned to commits).

```bash
pip install -e ".[web]"
python scripts/build_demos.py                     # fetch + index the demos into data/ (~30 s)
uvicorn coderag.web.app:app --port 7860           # API docs at http://127.0.0.1:7860/docs
```

| Endpoint | |
|---|---|
| `GET /api/health`, `GET /api/demos`, `GET /api/repos/{id}` | status, demo list, demo info and example questions |
| `GET /api/repos/{id}/graph` | call/inheritance graph; node ids match `chunk_id` in sources |
| `POST /api/repos/{id}/retrieve` | retrieved sources with GitHub links, no LLM |
| `POST /api/repos/{id}/ask` | Server-Sent Events: `sources`, then `token`s, then `done` (or `error`) |

```bash
curl -N -X POST http://127.0.0.1:7860/api/repos/requests/ask \
     -H "Content-Type: application/json" -d '{"question": "Who calls resolve_redirects?"}'
```

Answers use the server's key (`GROQ_API_KEY`) unless the request brings its own
(`"llm": {"provider": "groq", "api_key": "..."}`). A visitor's key is used for that request only and
is never logged, cached or returned. Limits are per IP and per hour, with a daily cap on the server
key. Popular questions are cached. If no LLM is available, the retrieved sources are still returned.
Configuration uses `CODERAG_*` environment variables (see
[coderag/web/settings.py](coderag/web/settings.py)). Behind a reverse proxy, set
`CODERAG_TRUSTED_PROXY_HOPS=1` so rate limits use the real client address.

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
  llm.py        LLM backends: local flan-t5/Qwen, remote OpenAI-compatible APIs, fallback chain
  agent.py      RAGCodebaseAgent: ties the pipeline together
  visualize.py  D3 chunk-graph HTML
  cli.py        command-line interface
  gitfetch.py   safe shallow fetch of one GitHub commit
  web/          FastAPI server: app, rate limits + answer cache, demo registry, settings
scripts/        build_demos.py
demos.yaml      demo repositories served by the web server
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
