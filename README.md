# coderag: code-aware RAG over Python repositories

[![CI](https://github.com/Aadeesh-01/RAG/actions/workflows/ci.yml/badge.svg)](https://github.com/Aadeesh-01/RAG/actions/workflows/ci.yml)

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
pip install -e ".[dev]"         # or: pip install -r requirements-dev.txt (runtime only: requirements.txt)
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
python scripts/build_demos.py                     # fetch + index the demos into data/demos (~30 s)
uvicorn coderag.web.app:app --port 7860 --workers 1   # site at http://127.0.0.1:7860
```

Run **one worker** (`--workers 1`): the indexing queue, rate limits, the answer cache and the
concurrent-answer limit live in memory. `CODERAG_DOCS=1` turns on the API docs (`/docs`, `/redoc`,
`/openapi.json`; off by default).

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
| `GET /api/ready` | 200 when the database answers and every demo loaded, else 503 (for a platform health check) |
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
key (`CODERAG_SERVER_DAILY_CAP`, counted from the stored answers, so it survives restarts).
Popular questions are cached. At most `CODERAG_MAX_CONCURRENT_ASKS` (8) answers stream at once (more
get 503), and a stream stops after `CODERAG_ASK_TIMEOUT` (90) seconds. If no LLM is available, the
retrieved sources are still returned. Configuration uses `CODERAG_*` environment variables (see
[coderag/web/settings.py](coderag/web/settings.py)).

### Production settings

The server checks its settings at start and refuses to run, with one line per problem, when:

- a GitHub setting is set but another is missing, or `CODERAG_SESSION_SECRET` or `CODERAG_TOKEN_KEY` is
  shorter than 32 characters (or they are equal);
- `CODERAG_PUBLIC_URL` isn't `https://` and its host isn't `localhost` or `127.0.0.1`;
- `CODERAG_PUBLIC_URL` is `https://` but `CODERAG_TRUSTED_PROXY_HOPS` is 0. Behind a TLS proxy (Hugging
  Face Spaces, Render) set it to 1, so rate limits use the real client address. If TLS really ends at
  this server, `CODERAG_ALLOW_NO_PROXY=1` starts it anyway, with a warning.

| Setting | |
|---|---|
| `CODERAG_DATA_DIR` | visitor indexes, clones and the SQLite database: a persistent disk |
| `CODERAG_DEMOS_DIR` | the demo indexes (default `<data_dir>/demos`); keep demos baked into an image outside a mounted data volume |
| `CODERAG_MAX_STORED_REPOS`, `CODERAG_MAX_STORED_MB` | disk limits for visitor repos (100 repos; MB off by default). Past them, the least recently used repos that nobody has in their list are deleted |
| `CODERAG_INDEX_PER_USER_PER_HOUR`, `CODERAG_SUBMIT_PER_USER_PER_HOUR` | per-user limits, next to the per-IP ones |
| `CODERAG_INDEX_TIMEOUT` | seconds an indexing job may take in all (600) |

Every response carries a Content-Security-Policy (the inline scripts' hashes are computed from the
HTML at start), `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `X-Frame-Options: DENY` and,
with an https `PUBLIC_URL`, HSTS. Request bodies over 64 KB get 413. When `PUBLIC_URL` is set,
POST/PUT/PATCH/DELETE requests whose `Origin` (or `Referer`) is another site get 403. Logs have
timestamps, and the access log leaves out query strings.

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

On the site, **+ Add repo** takes a GitHub URL (`https://github.com/owner/repo`, `owner/repo`, or
`…/tree/<branch-or-tag>`). The API equivalent is `POST /api/repos {"url": ...}`, then poll
`GET /api/jobs/{job_id}` (only the user who started a job can see it).

- The branch is resolved to a commit with `git ls-remote`. A commit that was already indexed is
  served immediately and doesn't count against limits.
- Only source files are downloaded (blob-less partial fetch and sparse checkout): Python, the
  tree-sitter languages, templates and a few project files such as the README and CI workflows,
  never `.env`. Data and media in the repo are never transferred. Code is only parsed, never run.
  Symlinks are not followed, git runs with a minimal environment (no server secrets), and the clone
  is deleted after indexing. Clones left by a stopped server are deleted on the next start.
- `tests/`, `test/`, `docs/`, `examples/` and `benchmarks/` are skipped. A repo may have up to 2,000
  source files / 25 MB. Each IP and each user can index 3 new repos per hour, and a user has one job
  at a time. One job runs at a time, with a queue of 10, and a job stops after 10 minutes. Progress
  is reported by stage: fetching → parsing → embedding → ready.
- Indexes are saved under `data/repos/<owner>__<repo>__<sha12>/` and loaded on demand (at most 6 in
  memory). Past 100 stored repos (or `CODERAG_MAX_STORED_MB`), the least recently used repos that
  nobody has in their list are deleted. All limits are `CODERAG_*` settings
  ([coderag/web/settings.py](coderag/web/settings.py)).
- If a repo's index files are gone but its database row isn't (a disk that was reset), opening it
  rebuilds the index at the same commit: the site shows "Rebuilding index…" with progress, and
  submitting the URL again queues the rebuild too. A private repo is rebuilt with the token of the
  user who opens it.

Typical times on a laptop CPU: pallets/itsdangerous 9 s, pallets/flask 33 s (362 chunks).
django/django is rejected after about 20 s (2,939 Python files).

## Sign in with GitHub

Demos are open to everyone. Adding a repository needs a GitHub sign-in. Once signed in, you can also
index private repositories you have access to. They are visible only to the people who added them.
Your repository tabs belong to your GitHub account, not the browser: `GET /api/repos/mine` lists them,
`PUT /api/repos/mine/{id}` adds an indexed repo you can open, and `DELETE /api/repos/{id}/mine`
removes one from the list. The index is deleted when nobody else has the repo in their list.

1. On GitHub, go to **Settings → Developer settings → OAuth Apps → New OAuth App**.
2. Set **Homepage URL** to your site, e.g. `https://coderag.example.com`, and **Authorization
   callback URL** to `PUBLIC_URL/api/auth/github/callback`, e.g.
   `https://coderag.example.com/api/auth/github/callback` (for local use:
   `http://127.0.0.1:7860/api/auth/github/callback`).
3. Create a client secret, then set:

```bash
export CODERAG_GITHUB_CLIENT_ID=Ov23li...
export CODERAG_GITHUB_CLIENT_SECRET=...
export CODERAG_SESSION_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export CODERAG_TOKEN_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export CODERAG_PUBLIC_URL=https://coderag.example.com       # must match the callback URL's origin
export CODERAG_TRUSTED_PROXY_HOPS=1                         # behind the platform's TLS proxy
```

The app asks for the `read:user repo` scopes. The session is a signed, HttpOnly, SameSite=Lax cookie.
Users are stored in the database (see [Database](#database)). The GitHub token is encrypted with a key
derived from `CODERAG_TOKEN_KEY`, is never logged or returned, and reaches git only through
environment variables. Tokens stored before the token key existed (encrypted with a key derived from
`CODERAG_SESSION_SECRET`) are still read, and are re-encrypted with the token key on their next use.
Changing the session secret signs everyone out. Changing the token key makes the stored tokens
unreadable, so private repos need a fresh sign-in. Deleting your account (`DELETE /api/me`) also
revokes coderag's authorization on GitHub. Without these settings the server still runs: everyone is
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

**Migrations** (Alembic, in `coderag/db/migrations/`) run when the server starts; on Postgres they
take an advisory lock, so two processes starting together don't race. To run them yourself:

```bash
python -m coderag db upgrade      # create or update the schema
python -m coderag db current      # show the current revision
```

The first migration converts the database from before this change (users and their repo links
only) in place: users keep their ids and tokens, and the repos indexed under `data/repos/` become
rows. Indexing jobs left running when the server stopped are marked failed ("server restarted") on
the next start. A question keeps the version that answered it and the text of the chunks it cites
(at most 40 lines each), so a shared answer always shows the code as it was when it was answered.

| Endpoint | |
|---|---|
| `GET /api/repos/{id}/history?limit=20&before=<id>` | your questions about a repo, newest first, with the total and the last 7 days' counts |
| `POST /api/questions/{id}/share`, `DELETE` the same to revoke | a public read-only link, `/a/<slug>`. A private repo's answer needs `{"confirm_private": true}` and shows only the answer and the code it cites |
| `POST /api/questions/{id}/feedback` | `{"rating": "up" \| "down", "note": "..."}` |
| `GET /api/home` | your Home page in one call: the repo you opened last (`user_repos.last_opened_at`, set when you open one) with your last question about it, your last 8 questions, your repos with their status, and this week's counts. Only your own data; signed-in users land here when `/app` has no `?repo=` or `?q=` |
| `GET /api/jobs` | your indexing jobs |
| `DELETE /api/repos/{id}/mine` | removes the repo from your list; its index is deleted when nobody else has it (demos never are) |
| `DELETE /api/me` | deletes your account: your token (and coderag's GitHub authorization), repo list, history, feedback, shared links and indexing jobs |

Signed-out visitors' questions are not stored: their history lasts for the browser session only.

## Deploy

One Docker container ([Dockerfile](Dockerfile)), CPU only, here on a free Hugging Face Space with
a free Neon Postgres database. The image has everything baked in: the locked dependencies
([requirements.lock](requirements.lock), CPU-only torch), the embedding model, the tree-sitter grammars
and the demos. It never downloads anything at start. It runs one worker as uid 1000, and
`HEALTHCHECK` polls `/api/ready`.

```bash
docker build -t coderag .
cp deploy/local.env.example deploy/local.env        # SQLite, signed out
docker run --rm -p 7860:7860 --env-file deploy/local.env coderag
```

**1. Neon.** Create a project (any region near the Space, e.g. AWS us-east). Under **Connect**,
copy two connection strings: the **pooled** one (host contains `-pooler`) and the **direct** one
(with *Connection pooling* off). Use the direct URL for migrations, which take a Postgres advisory
lock that a transaction pooler doesn't hold. One worker needs few connections, so the simplest
choice is the direct URL for the app too:

```bash
python scripts/predeploy_check.py --env-file prod.env --database-url "postgresql://…@ep-….neon.tech/coderag?sslmode=require"
```

`prod.env` is any `KEY=value` file with the settings below. The check runs the startup rules,
connects, migrates to the head revision, and checks the demos and the data directory. It prints a
pass/fail table and never prints a secret.

**2. The Space.** On huggingface.co, go to **New Space**, choose **Docker** with a blank template on
the free CPU hardware, and leave it public or make it private. A Space reads its settings from the
`README.md` header, so push the repository with the Space header in place of the GitHub README:

```bash
git remote add space https://huggingface.co/spaces/<you>/<space>
git switch -c space
cp deploy/huggingface/README.md README.md && git commit -am "Space README"
git push --force space space:main
git switch main && git branch -D space
```

**3. Secrets and variables** (Space **Settings → Variables and secrets**). These are **secrets**:

| Name | Value |
|---|---|
| `CODERAG_DATABASE_URL` | the Neon URL (`postgresql://…?sslmode=require`) |
| `CODERAG_SESSION_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `CODERAG_TOKEN_KEY` | another one like it (must differ) |
| `CODERAG_GITHUB_CLIENT_ID`, `CODERAG_GITHUB_CLIENT_SECRET` | from the OAuth app (step 4) |
| `GROQ_API_KEY` | the server's LLM key (optional: without it, visitors use their own key) |

These are **variables**:

| Name | Value |
|---|---|
| `CODERAG_PUBLIC_URL` | `https://<you>-<space>.hf.space` (or your domain) |
| `CODERAG_TRUSTED_PROXY_HOPS` | `1` (the image's default; HF's proxy adds one `X-Forwarded-For` entry) |
| `CODERAG_MAX_LOADED_REPOS` | `3` on the free hardware (optional) |

**4. GitHub OAuth app.** On GitHub, go to **Settings → Developer settings → OAuth Apps**. Set the
homepage to `https://<you>-<space>.hf.space` and the **callback URL** to
`https://<you>-<space>.hf.space/api/auth/github/callback`. It must match `CODERAG_PUBLIC_URL`
exactly. Use a separate OAuth app for local development.

**Custom domain.** Many Spaces share `hf.space`. A custom domain (Space settings, on paid plans) gives
the session cookie a site of its own, so it doesn't rely on `hf.space` being treated as a public
suffix. Change `CODERAG_PUBLIC_URL` and the OAuth callback when you switch. The server's Origin check
refuses requests that change state from any other origin (DEPLOY_AUDIT.md #27).

**Known limitation: the disk resets.** A free Space's disk is wiped on every restart and rebuild.
Users, history, shares and the list of indexed repos are kept in Neon, but the index files under
`/data` are not. A visitor repo is rebuilt at the same commit the first time someone opens it after a
restart: the site shows "Rebuilding index…" with progress. A private repo is rebuilt with the token
of the user who opens it. The demos are in the image and are always there. With Hugging Face's
persistent storage, mounted at `/data`, nothing needs rebuilding.

## Deploy on Azure

The same image on [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/) (the serverless Consumption profile,
scale to zero), pulled from GHCR, with Neon Postgres for the database and an Azure Files share at `/data` so the
index files survive restarts. The scripts in [deploy/azure/](deploy/azure/) do everything with the az CLI and
are idempotent, so re-run one after fixing whatever stopped it. Use `deploy.ps1`/`update.ps1` on Windows and
`deploy.sh`/`update.sh` on Linux, macOS or Cloud Shell.

**Prerequisites**

- An Azure subscription. Azure for Students works and needs no credit card.
- The az CLI, logged in (`az login`), with the Container Apps extension (`az extension add -n containerapp --upgrade`)
  and the providers registered: `az provider register -n Microsoft.App` (and `Microsoft.OperationalInsights`,
  `Microsoft.Storage`).
- A Neon database (use the direct URL, as above), a GitHub OAuth app, and a Groq key.
- Python with this repository's dependencies. The deploy runs `scripts/predeploy_check.py` locally against
  Neon before it creates the app, and it uses `.venv` if one exists.

**CI publishes the image.** Every push to `main` that passes the tests, the smoke test and the secret scan
([scripts/scan_image.py](scripts/scan_image.py)) pushes `ghcr.io/<owner>/coderag:sha-<short>` and `:latest`.
Pull requests build the image without pushing it.

**One-time steps**

1. Make the package public. Container Apps pulls the image anonymously, and the deploy checks that first.
   After the first push to `main`, open `https://github.com/users/<owner>/packages/container/coderag/settings`,
   then go to **Danger Zone → Change visibility → Public**. The image holds no secrets: CI scans its files, `ENV` and
   build history before every push.
2. `cp deploy/azure/prod.env.example deploy/azure/prod.env` and fill it in. `prod.env` is git- and docker-ignored.
   It holds the five secrets (`CODERAG_SESSION_SECRET`, `CODERAG_TOKEN_KEY`, `CODERAG_GITHUB_CLIENT_SECRET`,
   `CODERAG_DATABASE_URL`, `GROQ_API_KEY`) and `CODERAG_GITHUB_CLIENT_ID`.
3. Deploy:

   ```powershell
   .\deploy\azure\deploy.ps1                            # RG coderag-rg, centralindia, app coderag
   .\deploy\azure\deploy.ps1 -Location southeastasia    # bash: LOCATION=southeastasia ./deploy/azure/deploy.sh
   ```

   Azure for Students only allows certain regions. If one is refused (`RequestDisallowedByPolicy` or
   `RequestDisallowedByAzure`), the script prints the regions the subscription allows, read from its
   "Allowed resource deployment regions" policy. Without that policy, it suggests `southeastasia`, `eastasia`,
   `westeurope` or `eastus`.
4. Add the callback URL that the script prints, `https://<app>.<env-domain>/api/auth/github/callback`, to the
   OAuth app, and set the app's homepage to the URL.

What `deploy` creates and sets:

| | |
|---|---|
| Resource group | `coderag-rg`, holding everything below |
| Log Analytics workspace | `coderag-logs`, 30-day retention |
| Container Apps environment | `coderag-env`, workload-profiles mode with only the serverless `Consumption` profile. An environment without workload profiles would be *Express*, which can't mount storage |
| Storage | account `coderag<8 hex>` (Standard LRS, TLS 1.2, no public blobs). File share `coderag-data` (5 GB), registered in the environment and mounted at `/data` with `uid=1000,gid=1000`, the image's user |
| Container app | `coderag`: the GHCR image, 1 vCPU / 2 GiB, min 0 / max 1 replicas, external HTTPS ingress on port 7860 (HTTP redirects to HTTPS) |
| Probes | startup and readiness on `/api/ready` (startup allows 120 s: 20 s delay plus 10 × 10 s), liveness on `/api/health` |
| Secrets | the five from `prod.env` as Container Apps secrets, referenced by env vars. They are passed to az as arguments, never written to a file or printed |
| Env | `CODERAG_GITHUB_CLIENT_ID`, `CODERAG_PUBLIC_URL=https://<fqdn>`, `CODERAG_TRUSTED_PROXY_HOPS=1`, `CODERAG_DATA_DIR=/data`, `CODERAG_DEMOS_DIR=/opt/demos`, `CODERAG_MAX_LOADED_REPOS=3`, `HF_HUB_OFFLINE=1`, `CODERAG_LIVE_CHECK=false` |

`CODERAG_PUBLIC_URL` is set in two phases. Phase 1 creates the app with its image, secrets, ingress and scale
settings. The script then reads the app's FQDN, and phase 2 applies the full template: the public URL, the
probes and the volume. The YAML for phase 2 holds no secret values.

**One replica, by design.** Rate limits, the daily LLM cap, the answer cache and the indexing queue all live in
the process's memory. A starting replica also marks running indexing jobs as interrupted. A second replica would
split the limits and fail the first one's jobs, so `max replicas` is 1 (and uvicorn runs one worker).

**Updating.** Point the app at a newer image:

```powershell
.\deploy\azure\update.ps1                    # the newest main commit whose CI passed
.\deploy\azure\update.ps1 -Tag sha-1234567   # bash: TAG=sha-1234567 ./deploy/azure/update.sh
```

`update` only changes the image. It waits until the new revision is provisioned and `/api/ready` answers 200
through the ingress. After editing `prod.env`, re-run `deploy`: it updates the secrets and restarts the revision.

**Logs.**

```bash
az containerapp logs show -n coderag -g coderag-rg --tail 100            # console; add --follow to stream
az containerapp logs show -n coderag -g coderag-rg --type system         # pulls, probes, scaling
```

Older lines are in Log Analytics: in the portal, open the workspace → **Logs** and run
`ContainerAppConsoleLogs_CL | where ContainerAppName_s == "coderag" | project TimeGenerated, Log_s`.

**Checking `CODERAG_TRUSTED_PROXY_HOPS=1`.** After each start, the first request that isn't a health probe logs
how many `X-Forwarded-For` entries it carried. It never logs the addresses. Probes reach the container directly, so
they are skipped.

```bash
az containerapp logs show -n coderag -g coderag-rg --tail 300 | grep "proxy hops"
# INFO coderag.web: proxy hops: X-Forwarded-For has 1 entry on the first request, CODERAG_TRUSTED_PROXY_HOPS=1 (ok: the client address is entry -1)
```

From a plain browser visit, one entry means the Container Apps ingress is the only proxy and the rate limits
key on the visitor's address. If a client sends its own `X-Forwarded-For`, the entry count is higher, and that is
expected.

**Cost.**

- **Scale to zero.** With min replicas 0, nothing is billed while no replica runs. The app scales to zero about
  5 minutes after the last request.
- **Free monthly grant.** Container Apps gives each subscription 180,000 vCPU-seconds, 360,000 GiB-seconds and
  2 million requests a month on the consumption plan. At 1 vCPU / 2 GiB that is about 50 hours of running
  replica a month. Past the grant, usage is billed per second.
- **Azure Files** (Standard LRS) bills the GiB actually used (indexes are a few MB each) plus transactions. The
  5 GB quota is a cap, not a charge. Expect cents a month.
- **Log Analytics.** The first 5 GB of ingestion a month is free, and this app logs a few MB.
- **Azure for Students.** Charges come out of the credit. When the credit runs out, the resources are disabled,
  and no card is charged. The remaining credit is at <https://www.microsoftazuresponsorships.com/Balance>.

**Budget alert.** In the portal, go to **Cost Management → Budgets → Add**. Choose the subscription as the scope,
set a monthly budget (for example $5), and add alert conditions at 50% and 100% of actual cost, sent to your
email. Budgets may be unavailable on some sponsorship offers. In that case, check the balance page above
instead.

**Cold start.** The first request after the app has scaled to zero waits for a replica to start. That means
pulling the image if the node doesn't have it, mounting `/data`, and loading the model and the demos. The
server itself is ready in about 10 s. Measured on eastasia, a request after more than 7 idle minutes took 73 s,
and the next one took 0.4 s. Most of the wait is the platform scheduling a replica and pulling the image. The
startup probe allows the server 120 s.
To avoid cold starts, use `--min-replicas 1`, but then a replica is billed for every second of the month,
well past the free grant.

**Teardown.** This deletes everything the scripts created, including the indexes on the file share and the logs:

```bash
az group delete -n coderag-rg --yes --no-wait
```

The Neon database, the GHCR package and the GitHub OAuth app are separate and stay.

## Debugging retrieval

Add `?debug=1` to the site's URL, or call `agent.stream(question, debug=True)` / `POST
/api/repos/{id}/retrieve` with `"debug": true`, to see how each source was retrieved: its vector
search rank and similarity, or the call-graph path that pulled it in
(`should_bypass_proxies —calls:exact→ is_valid_cidr`), and how many characters it adds to the context.
Share links like `/?repo=pallets/click&q=How+does+a+Group+invoke+a+subcommand%3F` open a repo and run
the question.
