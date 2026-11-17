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
| `v7` | Scope-aware call resolution (imports, `self`/`super`, external modules) instead of bare-name matching |
| `v8` | Drops `@overload` stubs, ignores calls made only inside `assert`, expands best search hits first (callers first for "who calls" questions). **Default.** |

Each version is a preset of a single `RAGConfig` ([coderag/config.py](coderag/config.py)), so comparing
versions means changing one flag. The code is not copied per version. v0–v5 are kept unchanged as the
research record.

## Evaluation

[eval/](eval/) scores the context each version would send to the LLM against hand-labelled gold symbols
on `sample_repo`, [psf/requests](https://github.com/psf/requests) and
[pallets/click](https://github.com/pallets/click). On the real repos, v8 more than doubles v5's recall
(requests 0.38 → 0.83, click 0.33 → 0.94) and beats the naive v0 baseline. See
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

Commands default to the bundled [sample_repo/](sample_repo/) and the `v8` preset. Pass `--repo PATH` and
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
uvicorn coderag.web.app:app --port 7860           # site at http://127.0.0.1:7860, API docs at /docs
```

The site ([frontend/](frontend/)) is plain HTML, CSS and JavaScript served by the same server, with no
build step. Pick a repo, ask a question or click an example, and the answer streams in with numbered
citations. Click one to open that source's code, linked to GitHub at the pinned commit. The graph panel
shows the part of the call graph the answer used: direct matches, code reached through the graph,
and their neighbours. Settings let visitors use their own API key (kept in their browser only) or ask
for sources only. Markdown in answers is sanitised with DOMPurify. The CDN libraries (D3, marked,
DOMPurify) are pinned to exact versions with integrity hashes. JavaScript tests: `node --test
frontend/tests/lib.test.mjs` (also run by `pytest`).

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
frontend/       web page: index.html, styles.css, app.js (UI), lib.js (tested helpers)
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

## Indexing any GitHub repository

On the site, **+ Add repo** takes a public GitHub URL (`https://github.com/owner/repo`, `owner/repo`, or
`…/tree/<branch-or-tag>`). The API equivalent is `POST /api/repos {"url": ...}`, then poll
`GET /api/jobs/{job_id}`.

- The branch is resolved to a commit with `git ls-remote`. A commit that was already indexed is
  served immediately and doesn't count against limits.
- Only `*.py` files are downloaded (blob-less partial fetch and sparse checkout), so data and media in
  the repo are never transferred. Code is only parsed with `ast`, never run. Symlinks are not followed,
  and the clone is deleted after indexing.
- `tests/`, `test/`, `docs/`, `examples/` and `benchmarks/` are skipped. A repo may have up to 2,000
  Python files / 25 MB. Each IP can index 3 new repos per hour. One job runs at a time, with a queue of
  10. Progress is reported by stage: fetching → parsing → embedding → ready.
- Indexes are saved under `data/repos/<owner>__<repo>__<sha12>/` (the 100 most recent are kept) and
  loaded on demand (at most 6 in memory). All limits are `CODERAG_*` settings
  ([coderag/web/settings.py](coderag/web/settings.py)).

Typical times on a laptop CPU: pallets/itsdangerous 9 s, pallets/flask 33 s (362 chunks).
django/django is rejected after about 20 s (2,939 Python files).

## Sign in with GitHub

Demos are open to everyone. Adding a repository needs a GitHub sign-in. Once signed in, you can also
index private repositories you have access to. They are visible only to the people who added them.
Your repository tabs belong to your GitHub account, not the browser: `GET /api/repos/mine` lists them,
`PUT /api/repos/mine/{id}` adds an indexed repo you can open, and `DELETE /api/repos/mine/{id}`
removes one from the list (the index stays on the server).

1. On GitHub, go to **Settings → Developer settings → OAuth Apps → New OAuth App**.
2. Set **Homepage URL** to your site, e.g. `https://coderag.example.com`, and **Authorization
   callback URL** to `PUBLIC_URL/api/auth/github/callback`, e.g.
   `https://coderag.example.com/api/auth/github/callback` (for local use:
   `http://127.0.0.1:7860/api/auth/github/callback`).
3. Create a client secret, then set:

```bash
export CODERAG_GITHUB_CLIENT_ID=Ov23li...
export CODERAG_GITHUB_CLIENT_SECRET=...
export CODERAG_SESSION_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export CODERAG_PUBLIC_URL=https://coderag.example.com       # must match the callback URL's origin
```

The app asks for the `read:user repo` scopes. The session is a signed, HttpOnly, SameSite=Lax cookie.
Users are stored in `data/coderag.db` (SQLite). The GitHub token is encrypted with a key derived from
`CODERAG_SESSION_SECRET`, is never logged or returned, and reaches git only through environment
variables. Changing the session secret signs everyone out and makes the stored tokens unreadable, so
private repos need a fresh sign-in. Without these settings the server still runs: everyone is
signed out and demos work as usual.

## Database

The web server keeps its state in a database: users (with their GitHub token, encrypted), the
repositories it has indexed, indexing jobs, signed-in users' questions and answers (history), shared
answer links and feedback. Index files (FAISS, chunks) stay on disk under `data/repos/`, and the
database stores their path. The answer cache and rate limits are in memory, behind an interface,
so a shared store such as Redis can be added later.

**Local (SQLite, the default).** Nothing to set up: the database is `data/coderag.db` (or
`<CODERAG_DATA_DIR>/coderag.db`).

**Postgres.** Set `CODERAG_DATABASE_URL`. A `postgres://` or `postgresql://` URL works as given and
uses psycopg 3. A free Neon or Supabase database is enough:

```bash
export CODERAG_DATABASE_URL="postgresql://user:password@host/dbname?sslmode=require"
```

**Migrations** (Alembic, in `coderag/db/migrations/`) run when the server starts. To run them
yourself:

```bash
python -m coderag db upgrade      # create or update the schema
python -m coderag db current      # show the current revision
```

The first migration converts the database from before this change (users and their repo links
only) in place: users keep their ids and tokens, and the repos indexed under `data/repos/` become
rows. Indexing jobs left running when the server stopped are marked failed ("server restarted") on
the next start.

| Endpoint | |
|---|---|
| `GET /api/repos/{id}/history?limit=20&before=<id>` | your questions about a repo, newest first, with the total and the last 7 days' counts |
| `POST /api/questions/{id}/share`, `DELETE` the same to revoke | a public read-only link, `/a/<slug>`. A private repo's answer needs `{"confirm_private": true}` and shows only the answer and the code it cites |
| `POST /api/questions/{id}/feedback` | `{"rating": "up" \| "down", "note": "..."}` |
| `GET /api/jobs` | your indexing jobs |
| `DELETE /api/repos/{id}/mine` | removes the repo from your list; its index is deleted when nobody else has it (demos never are) |
| `DELETE /api/me` | deletes your account: your token, repo list, history, feedback and shared links |

Signed-out visitors' questions are not stored: their history lasts for the browser session only.

## Debugging retrieval

Add `?debug=1` to the site's URL, or call `agent.stream(question, debug=True)` / `POST
/api/repos/{id}/retrieve` with `"debug": true`, to see how each source was retrieved: its vector
search rank and similarity, or the call-graph path that pulled it in
(`should_bypass_proxies —calls:exact→ is_valid_cidr`), and how many characters it adds to the context.
Share links like `/?repo=pallets/click&q=How+does+a+Group+invoke+a+subcommand%3F` open a repo and run
the question.
