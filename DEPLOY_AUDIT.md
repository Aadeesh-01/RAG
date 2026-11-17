# coderag deployment audit

Target: one Docker container on Hugging Face Spaces or Render (~2 GB RAM, CPU only), Postgres on Neon,
HTTPS terminated by the platform's reverse proxy. Audited at commit `c6e6888` (clean tree), 2026-11-18.
No code was changed.

## Verdict: **not ready**

Five blockers stand in the way, and each fix is small:

1. There is no Dockerfile or start command.
2. `requirements.txt` cannot run the server.
3. The demos are not in the image.
4. Per-IP rate limits collapse into one bucket behind the proxy unless `CODERAG_TRUSTED_PROXY_HOPS` is set.
5. On an ephemeral disk with Neon, a user's repos become permanently stuck after a restart (measured, below).

With those fixed and the security headers added, it is **ready with fixes**. The core security work
holds up under test:

- tokens are encrypted and never appear in logs;
- cookies are HttpOnly + SameSite=Lax + Secure on https;
- `next` is validated;
- private repos are isolated;
- pip-audit is clean;
- the 284 tests pass on a clean install.

---

## Findings

Severity: **BLOCKER** must be fixed before deploying, **SHOULD FIX** before real users, **NICE** at any time.

| # | Severity | Area | Where | What's wrong | Fix |
|---|---|---|---|---|---|
| 1 | BLOCKER | Storage | [registry.py:237](coderag/web/registry.py#L237), [jobs.py:185](coderag/web/jobs.py#L185), [registry.py:276](coderag/web/registry.py#L276) | When the disk resets but the DB survives (Neon), the repo row outlives its index files. Measured: re-submitting returns **200 "Already indexed"**, `GET /api/repos/{id}` returns **404**, reindex returns **404**, and the repo is still listed in `/api/repos/mine`. The user can never get it back. | Make `exists()` require `meta.json` under `index_path` (otherwise delete the row or re-queue). Also, or instead, mount a persistent disk for `CODERAG_DATA_DIR`. |
| 2 | BLOCKER | Deploy | repo root | No Dockerfile, no `.dockerignore`, no start command, no healthcheck, no README deploy section. | Add a Dockerfile (python:3.12-slim + git, CPU torch, bake the model, grammars and demos; `uvicorn coderag.web.app:app --host 0.0.0.0 --port $PORT --workers 1`) and a `.dockerignore` covering `.env`, `data/`, `.venv`, `eval/.repos`, `.hallmark`, `.impeccable`. |
| 3 | BLOCKER | Deps | [requirements.txt](requirements.txt) | `fastapi` and `uvicorn` are missing. Measured: after `pip install -r requirements.txt`, `import fastapi` fails, so a requirements-based image cannot start. `pytest` is listed as a runtime dependency. | Add `fastapi` and `uvicorn[standard]` and move `pytest` to a dev file. Or build with `pip install ".[web]"` only. |
| 4 | BLOCKER | Storage | [.gitignore:28](.gitignore#L28), [registry.py:188](coderag/web/registry.py#L188) | `data/` is git-ignored, so an image built from git has **no demos**: `/api/demos` returns `[]` and signed-out visitors see nothing. Demos also live under `CODERAG_DATA_DIR`, so a persistent volume mounted there would hide demos baked into the image. | Run `python scripts/build_demos.py` in the Docker build into a separate directory, and add a demos-dir setting, or copy the demos into the data dir at start. |
| 5 | BLOCKER | Proxy | [settings.py:20](coderag/web/settings.py#L20), [limits.py:76](coderag/web/limits.py#L76) | `trusted_proxy_hops` defaults to 0. Behind the HF/Render proxy every visitor then has the proxy's IP, so the whole site shares one bucket: 30 asks/h, 3 new repos/h and 600 retrievals/h. | Set `CODERAG_TRUSTED_PROXY_HOPS=1`, then confirm on the live host with one logged request that `parts[-1]` is the real client. |
| 6 | SHOULD FIX | Headers | [app.py:221](coderag/web/app.py#L221) | Measured on `/app`: no CSP, no `X-Content-Type-Options`, no `X-Frame-Options`/`frame-ancestors` (clickjacking of `/app`, including Delete account), no `Referrer-Policy`, no HSTS. | Add a small ASGI middleware with the CSP below, plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin` and `Strict-Transport-Security: max-age=31536000`. |
| 7 | SHOULD FIX | Privacy | [history.py:172-181](coderag/web/history.py#L172-L181) | The shared page reads snippets by `chunk_id` from the repo's **latest** version, but the question was answered with whatever version was asked (and may predate a re-index). Chunk ids differ between versions, so a private repo's share can show a different, uncited chunk. | Store the version (or the cited snippet text, capped at 40 lines) on the question when it is recorded, and serve that. |
| 8 | SHOULD FIX | XSS | [lib.js:52](frontend/lib.js#L52) | After DOMPurify runs, the citation-chip regex rewrites the HTML. `[^\s<>]+` can match inside an attribute, so it can break tags. Measured in Edge 157: `<img alt="a b[S1]…">` becomes a split tag, but the payload stayed inert because Edge escapes `<` in attributes. Engines that don't escape `<` there would turn the text after the split into live HTML. Answers can be steered by the question and by indexed code, and shared answers render on the site's own origin. | Wrap the chips before sanitizing (emit the `<span class="cite-run">` and `<a class="cite">` markup, then run DOMPurify on that), or walk text nodes instead of using a regex on HTML. |
| 9 | SHOULD FIX | XSS | [app.js:716-717](frontend/app.js#L716-L717) | The repo facts `homepage`/`pages_url`/`environment_url` come from the GitHub API, are set by the repo owner, and become `href` with no scheme check. A `javascript:` URL is possible if GitHub stores one. | Allow only `http:`/`https:` in facts.py when storing and in `el()` for `href`. |
| 10 | SHOULD FIX | Config | [auth.py:288](coderag/web/auth.py#L288), [settings.py:37-38](coderag/web/settings.py#L37-L38) | A missing or short `SESSION_SECRET` silently disables sign-in. The only notice is an INFO log, and that is invisible (#15). There is no length check. Without `PUBLIC_URL`, the callback is built from the proxied request (`http://…`, so GitHub rejects it) and cookies lose `Secure`. | Fail at start when any GitHub var is set and the secret is under 32 chars, or when `PUBLIC_URL` is not `https://` outside localhost. |
| 11 | SHOULD FIX | Abuse | [app.py:475](coderag/web/app.py#L475) (all JSON routes) | There is no request body limit. Measured: a 20 MB JSON body was read completely, then rejected with 422. Bodies of hundreds of MB would sit in RAM. | Middleware that rejects `Content-Length` over 64 KB with 413; the largest legitimate body is under 2 KB. |
| 12 | SHOULD FIX | Memory | [app.py:216](coderag/web/app.py#L216), [app.py:450-453](coderag/web/app.py#L450-L453) | The `graphs` cache is unbounded. It holds one entry per (repo, version, agent id), including agents already evicted from the LRU and those replaced by re-indexing, so it grows with every repo anyone views. | Make it an LRU with about `max_loaded_repos` entries, or store the graph on the `Demo`. |
| 13 | SHOULD FIX | Storage | [registry.py:301-305](coderag/web/registry.py#L301-L305) | At `max_stored_repos` (100), the **oldest-created** repo is deleted even if users have it in their list or use it daily. Its questions and shares go with it through the cascade. There is no byte cap. | Evict by last use, and only repos with no users. Add a disk budget (for example `CODERAG_MAX_STORED_MB`). |
| 14 | SHOULD FIX | Storage | [jobs.py:334](coderag/web/jobs.py#L334), [app.py:208](coderag/web/app.py#L208) | Clones of interrupted jobs are never removed. Measured: killed mid-embedding of pallets/flask, `data/.clones/<job_id>` was still there after the restart; the job itself was correctly marked failed ("server restarted"). | `rmtree(data_dir/.clones)` in `Indexer.recover()`. |
| 15 | SHOULD FIX | Logging | [app.py:65](coderag/web/app.py#L65), uvicorn access log | The `coderag.*` loggers have no handler, so INFO lines such as "database: …" and "GitHub sign-in is off" never appear. Only WARNING+ reaches stderr, through Python's last-resort handler, without timestamps. uvicorn's access log records full query strings, including `/api/auth/github/callback?code=…&state=…` (a single-use code), `/app?q=<question>` and `/api/repos/<private id>/file?path=…`. | Call `logging.basicConfig(level=INFO)` at app start. Use an access-log format, or a filter, that drops query strings. |
| 16 | SHOULD FIX | Single instance | [app.py:201](coderag/web/app.py#L201), [limits.py:30](coderag/web/limits.py#L30), [jobs.py:153-207](coderag/web/jobs.py#L153-L207) | Several things break with more than one worker or replica. Rate limits, the daily cap, the answer cache and the job queue are per process. `recover()` in a starting worker marks another worker's running jobs failed. Alembic runs on every start with no lock, so concurrent starts race. A second worker also doubles RAM (≈570 MB each). | Run with `--workers 1` and document it. Run `python -m coderag db upgrade` as a pre-start step, or take `pg_advisory_lock` around `upgrade()`. |
| 17 | SHOULD FIX | Abuse | [app.py:528-552](coderag/web/app.py#L528-L552), [llm.py:228](coderag/llm.py#L228) | All routes are sync, so each one, and each SSE stream, holds an anyio worker thread (40 by default). The LLM read timeout is 120 s *per read*, with no total cap, so about 40 slow streams stall every endpoint, health included. | Cap the total stream time (for example 90 s) and the number of concurrent asks (a semaphore that returns 503); make `/api/health` async. |
| 18 | SHOULD FIX | Abuse | [jobs.py:332-358](coderag/web/jobs.py#L332-L358), [jobs.py:197](coderag/web/jobs.py#L197) | Indexing has no wall-clock limit. Fetching allows 120 s per git command, three times over, and parsing and embedding are unbounded. Size limits are checked only after the download. The queue (10) is global and the only limit is per IP (3/h), so four IPs fill the queue for everyone. | Add a total job deadline (for example 10 min) checked in `progress()`, a one-job-per-user limit, and per-user hourly limits next to the per-IP ones. |
| 19 | SHOULD FIX | Heavy deps | [index.py:14](coderag/index.py#L14), [languages.py:271](coderag/languages.py#L271) | The embedding model (88 MB) is downloaded from the HF Hub when the server first starts, and tree-sitter grammars (40 MB) on first use. Measured: cold start 35.2 s versus 14.8 s with the model cached. Every container restart on an ephemeral disk downloads again, unauthenticated ("set a HF_TOKEN" warning), and fails if the Hub is unreachable. | In the Dockerfile, run `SentenceTransformer('all-MiniLM-L6-v2')` and `coderag.languages.prefetch()` at build time, then set `HF_HUB_OFFLINE=1`. |
| 20 | SHOULD FIX | Deps | [requirements.txt](requirements.txt), [pyproject.toml](pyproject.toml) | Everything except tree-sitter-language-pack is `>=`. The clean install pulled transformers **5.23**, sentence-transformers **6.3**, starlette **1.10** and torch **2.15**, major versions newer than this code was written against. Tests pass today, but tomorrow's build may differ. On Linux, plain `torch` pulls the CUDA wheels (several GB). | Commit a lock (`uv pip compile` / `pip-compile --generate-hashes`) and install CPU torch from `https://download.pytorch.org/whl/cpu`. |
| 21 | SHOULD FIX | Auth | [auth.py:42](coderag/web/auth.py#L42), [auth.py:158](coderag/web/auth.py#L158) | The OAuth scope `repo` grants **write** access to all of a user's repos. The tokens are encrypted with a key derived from `SESSION_SECRET`, so a leak of the Neon DB plus the env var exposes every user's write-scoped token. | Move to a GitHub App with read-only Contents access, or at least use a separate `CODERAG_TOKEN_KEY`. |
| 22 | SHOULD FIX | Privacy | [auth.py:259-274](coderag/web/auth.py#L259-L274) | `DELETE /api/me` leaves the user's `jobs` rows: `repo_name`, `url` and `commit`, including private repo names, with `user_id` set to NULL. It does not revoke the GitHub grant, so the token stays valid at GitHub. Their answers stay in the in-memory answer cache until evicted. | Delete the user's jobs rows. Call `DELETE /applications/{client_id}/grant` with the token before deleting the user. |
| 23 | SHOULD FIX | DB | [db/__init__.py:51](coderag/db/__init__.py#L51), [test_db.py](tests/test_db.py) | Postgres is untested. `CODERAG_TEST_POSTGRES_URL` was not set, so the Postgres test was skipped and this run used SQLite. `pool_pre_ping=True` is set (good). | Before the switch, run `CODERAG_TEST_POSTGRES_URL=<Neon branch URL> pytest tests/test_db.py`. Use the **direct** (non-pooler) Neon URL for migrations. |
| 24 | NICE | Robustness | [settings.py:19](coderag/web/settings.py#L19), [app.py:524](coderag/web/app.py#L524) | The daily server-key cap (800) lives in memory and resets on every restart or deploy. | Count from the `questions` table, or a small `counters` table. |
| 25 | NICE | Privacy | [app.py:395-400](coderag/web/app.py#L395-L400) | `GET /api/jobs/{job_id}` needs no sign-in. Measured: 200. It returns a private repo's name and commit to anyone who has the id (uuid4, so not guessable). | Require the job's owner (`job.owner_id == user.id`). |
| 26 | NICE | Auth | [auth.py:419-424](coderag/web/auth.py#L419-L424) | Sessions are stateless. Logout only clears the browser cookie, so a copied cookie stays valid for 30 days. | Add a `session_version` column, put it in the cookie, and bump it on logout. |
| 27 | NICE | CSRF | all POST/PUT/DELETE | Cross-site requests are blocked by SameSite=Lax, and FastAPI won't parse `text/plain` bodies as JSON. But the body-less POSTs (`/api/auth/logout`, `/reindex`, `/share`, `/live`) rely on SameSite alone. That would fail if a sibling subdomain counted as same-site, which happens when `hf.space` or `onrender.com` is not on the Public Suffix List. | Reject unsafe methods whose `Origin` is not `PUBLIC_URL`, or use a custom domain. |
| 28 | NICE | Exposure | [app.py:218](coderag/web/app.py#L218) | `/docs` and `/openapi.json` are public (measured 200). | `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` in production. |
| 29 | NICE | Robustness | [app.py:265-279](coderag/web/app.py#L265-L279) | `/api/health` is always `ok` and doesn't touch the DB. There is no readiness endpoint. The app is built on first attribute access, so the port opens only after the model loads (15–35 s). | Add `/api/ready`, which runs `SELECT 1` and checks the demos loaded, and give the platform's health check a start period of 60 s or more. |
| 30 | NICE | Memory | [limits.py:35](coderag/web/limits.py#L35) | Rate-limit keys (one per IP per bucket) are never removed, so memory grows slowly with every distinct visitor. | Drop empty deques during `hit()` (for example sweep every 1,000 calls). |
| 31 | NICE | Secrets | [gitfetch.py:64](coderag/gitfetch.py#L64), [gitfetch.py:184](coderag/gitfetch.py#L184) | `git` subprocesses inherit the whole environment, including `GROQ_API_KEY`, `CODERAG_SESSION_SECRET`, the GitHub client secret and the DB URL. git runs no repo code, so the risk is low. | Pass a minimal env (`PATH`, `HOME`, plus the GIT_* vars). |
| 32 | NICE | Robustness | [facts.py:183](coderag/facts.py#L183) | GitHub facts for public repos are fetched unauthenticated, at 60 requests/h per server IP shared by all users, and silently come back empty once that is used up. (`git ls-remote` and `/api/github/repos` are not affected.) | Use the signed-in user's token for public repos too. |
| 33 | NICE | Perf | [registry.py:270-279](coderag/web/registry.py#L270-L279) | The registry-wide lock is held while an index loads from disk, so every other repo lookup waits for it. | Load outside the lock and take a per-repo lock. |
| 34 | NICE | Frontend deps | [app.html](frontend/app.html), [answer.html](frontend/answer.html) | pip-audit does not cover the CDN libraries. DOMPurify 3.1.6 is affected by CVE-2025-26791 only with `SAFE_FOR_TEMPLATES`, which is not used. marked 12.0.2, d3 7.9.0 and highlight.js 11.9.0 are pinned with SRI. | Bump DOMPurify to ≥3.2.4 at the next frontend change. |
| 35 | NICE | Docs | [README.md:172-221](README.md#L172-L221) | README claims are out of date: "Only `*.py` files are downloaded", "`DELETE /api/repos/mine/{id}` … the index stays" and "Users are stored in `data/coderag.db` (SQLite)". `.hallmark/` and `.impeccable/` (tool output, including a 9-byte `salt`) are committed. | Update those sections and keep the tool folders out of the image. |
| 36 | NICE | Python | [pyproject.toml](pyproject.toml) | `requires-python = ">=3.10"`, but only 3.12.10 was tested. There are no tree-sitter-language-pack 1.25.2 wheels checked for other versions. | Pin the image to `python:3.12-slim`. |

### Proposed CSP (works with the pinned CDN scripts and SRI)

```
default-src 'self'; script-src 'self' https://cdn.jsdelivr.net 'sha256-+10F0DYLviFtRiqEtSvzzyi19x4vIyTKjoYwORE4/T4='
'sha256-B2VVdypnZnA19oPgLhI9nymDnS2liIuZvBixwQMKOo4=' 'sha256-qnc+Sd3dbm638AgiTNrGkLLb7Y5ztMjHOal5YVp4cjw=';
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com;
img-src 'self' data: https://avatars.githubusercontent.com; connect-src 'self'; frame-ancestors 'none';
base-uri 'none'; object-src 'none'; form-action 'self'
```

The three hashes belong to the inline theme script: `answer.html` and `login.html` share the first, `app.html` uses the second
and `index.html` the third. Recompute them whenever those scripts change. `style-src 'unsafe-inline'` is needed for the
inline `style=` attributes the JS sets (code-viewer line counters, graph).

---

## Measured numbers

Clean venv on Windows 11, Python 3.12.10, CPU only. The server was started from that venv with
`CODERAG_PUBLIC_URL=https://example.test`, `CODERAG_TRUSTED_PROXY_HOPS=1`, GitHub sign-in on, the real
`GROQ_API_KEY`, a fresh data dir holding the three demos, and an **empty HF cache**. The DB was SQLite
because `CODERAG_TEST_POSTGRES_URL` is not set. RSS is the uvicorn process plus its children, sampled every 0.25 s.

| Measurement | Value |
|---|---|
| Clean install (`pip install -r requirements.txt` then `pip install -e ".[web]"`) | 7 min 0 s, 71 packages, no errors |
| `import fastapi` after `requirements.txt` only | **fails** (#3) |
| Full test suite in the clean venv | **284 passed, 1 skipped** (Postgres) in 21.7 s |
| Cold start, including the 88 MB model download | **35.2 s** to the first 200 from `/api/health` |
| Warm start (model cached) | **14.8 s** |
| RSS idle after start (3 demos loaded) | **572 MB** (warm start: 560 MB) |
| RSS after loading each demo (info + graph + retrieve): click / requests / sample | 579 / 579 / 579 MB (demos load at start; graphs add ~7 MB) |
| Indexing pallets/click (25 files, 541 chunks) | 35.3 s; **peak 811 MB**, 716 MB afterwards; 1.73 MB on disk |
| Ask (Groq, gpt-oss-120b, streamed) | 1.2 s; peak 717 MB (no growth) |
| 20 MB JSON body to `/retrieve` | read completely, then 422; RSS 715 MB afterwards |
| Kill during indexing (pallets/flask, at embedding 41%) | job marked `failed: server restarted` on restart; **clone left in `.clones/`** |
| Disk reset with DB kept (#1) | resubmit 200 "Already indexed", open 404, reindex 404, still listed in `/mine` |
| Headers on `/app` | only `Cache-Control: no-cache`; CSP, nosniff, XFO, Referrer-Policy and HSTS all absent |
| OAuth start | `redirect_uri=https://example.test/api/auth/github/callback`, `scope=read:user repo`; state cookie `HttpOnly; Max-Age=600; Path=/api/auth; SameSite=lax; Secure` |
| `/docs`, `/openapi.json`, `/api/jobs/{id}` signed out | 200, 200, 200 |

**Memory budget for 2 GB.** Idle is ~570 MB. Indexing a small repo adds ~240 MB at peak. Each loaded
visitor repo stays in RAM, up to `max_loaded_repos=6`. A 2,000-file repo was not measured, but peak use
grows with its chunk count. 2 GB is enough with one worker; consider `CODERAG_MAX_LOADED_REPOS=3`.
Render's 512 MB instances (Free and Starter) would run out of memory at idle.

**Image size estimate** (no image was built, because there is no Dockerfile):

| Part | Size |
|---|---|
| `python:3.12-slim` | ~125 MB |
| git + CA certificates | ~60 MB |
| site-packages, measured in the clean venv | 1,256 MB, of which torch (CPU) is 552 MB, transformers 123, scipy 116, sympy 74, scikit-learn 45, numpy 55, faiss 18 |
| all-MiniLM-L6-v2 | 88 MB |
| tree-sitter grammars | 40 MB |
| demos | 3 MB |
| **Total with CPU torch** | **≈1.6 GB uncompressed (≈0.6–0.7 GB compressed)** |
| Default PyPI torch on Linux | adds the CUDA wheels, several GB, for **≈5 GB+** |

**pip-audit 2.10.1.**
- `-r requirements.txt`: *No known vulnerabilities found*.
- Whole clean venv (all 71 resolved packages; the editable `coderag` skipped): *No known vulnerabilities found*.
- Not covered: the CDN JS libraries (#34) and the system `git`.

**Log grep** (both server logs, 82 lines; the run included sign-in setup, indexing, a Groq ask and a restart):
- none of these appear: the session secret, the GitHub client secret, the `GROQ_API_KEY` value, the user's GitHub token, or its base64 git header;
- zero matches for `gsk_`, `gh[opsu]_` and `sk-` tokens, `Bearer`, `Authorization`, `Cookie`, `Set-Cookie`, `coderag_session=`, `api_key`, and `password=`/`secret=`;
- the log holds only uvicorn access lines plus the HF Hub "unauthenticated requests" warning;
- the OAuth callback wasn't exercised: GitHub login can't run without real credentials. Its `?code=` would be logged (#15).

**Answer-renderer check** (Edge 157, with the pinned marked and DOMPurify):
- 4 crafted answers (an `img onerror`, citations inside an `alt` and a `title` attribute, and one inside a code span);
- 0 event handlers survived and nothing executed;
- 2 of the 4 came out as split tags (#8).

**Local LLM path.** Every agent load in the web server passes `load_llm=False`
([registry.py:153](coderag/web/registry.py#L153), [registry.py:184](coderag/web/registry.py#L184),
[registry.py:277](coderag/web/registry.py#L277), [jobs.py:351](coderag/web/jobs.py#L351)), and `LLMFactory` builds only
`RemoteLLM`. Qwen and flan-t5 are reachable only from the CLI, so the server can't load them by accident.
torch and transformers are still required because sentence-transformers needs them.

---

## Configuration

All settings are `CODERAG_<NAME>` ([settings.py](coderag/web/settings.py)). Parsing:
- ints go through `int()`, so a bad value crashes at start, which is good;
- bools are true only for `1/true/yes/on`;
- tuples are comma-separated.

| Variable | Default | Production | Notes |
|---|---|---|---|
| `CODERAG_DATA_DIR` | `data` (relative to the working directory) | **required**: a persistent path | indexes, sources, `.clones`, SQLite; demos are under it too (#4) |
| `CODERAG_DATABASE_URL` | `""` → `sqlite:///<data_dir>/coderag.db` | **required** (Neon) | `postgres://` is normalised to psycopg 3 |
| `CODERAG_PUBLIC_URL` | `""` | **required**, `https://…` | OAuth callback and the `Secure` cookie flag depend on it (#10) |
| `CODERAG_SESSION_SECRET` | `""` | **required**, ≥32 random chars | signs cookies **and** derives the token encryption key; changing it signs everyone out and makes stored tokens unreadable |
| `CODERAG_GITHUB_CLIENT_ID` | `""` | **required** | without all three GitHub settings, sign-in is off and nothing warns you |
| `CODERAG_GITHUB_CLIENT_SECRET` | `""` | **required** | kept out of `repr` |
| `CODERAG_TRUSTED_PROXY_HOPS` | `0` | **required: `1`** | unsafe default behind a proxy (#5); setting it without a proxy lets clients forge `X-Forwarded-For` |
| `CODERAG_SERVER_LLM_PROVIDER` | `groq` | optional | key read from `GROQ_API_KEY` / `OPENROUTER_API_KEY` / `GEMINI_API_KEY` |
| `CODERAG_SERVER_LLM_MODELS` | `openai/gpt-oss-120b,openai/gpt-oss-20b` | optional | tried in order on 429 or 5xx |
| `CODERAG_SERVER_DAILY_CAP` | `800` | set below the provider's quota | in memory; resets on restart (#24) |
| `CODERAG_ASK_PER_IP_PER_HOUR` | `30` | ok | |
| `CODERAG_OWN_KEY_ASK_PER_IP_PER_HOUR` | `300` | ok | |
| `CODERAG_RETRIEVE_PER_IP_PER_HOUR` | `600` | ok | also covers the file viewer and the GitHub repo list |
| `CODERAG_ANSWER_CACHE_SIZE` | `500` | ok | in memory |
| `CODERAG_INDEX_PER_IP_PER_HOUR` | `3` | ok | per IP only (#18) |
| `CODERAG_SUBMIT_PER_IP_PER_HOUR` | `30` | ok | |
| `CODERAG_MAX_INDEX_QUEUE` | `10` | ok | global |
| `CODERAG_MAX_LOADED_REPOS` | `6` | `3` on 2 GB | LRU of visitor indexes in RAM |
| `CODERAG_MAX_STORED_REPOS` | `100` | size to the disk | evicts the oldest-created repo, even one in use (#13) |
| `CODERAG_MAX_PY_FILES` | `2000` | ok | covers all languages despite the name |
| `CODERAG_MAX_PY_MB` | `25` | ok | checked after the download |
| `CODERAG_FETCH_TIMEOUT` | `120` | ok | per git command |
| `CODERAG_INDEX_IGNORE_DIRS` | `tests,test,docs,examples,benchmarks` | ok | |
| `CODERAG_DEFAULT_VERSION` | `v10` | ok | unknown values fall back to v10 |
| `CODERAG_LIVE_CHECK` | `false` | keep `false` unless needed | SSRF-guarded (https only, public IPs, DNS pinned) |
| `GROQ_API_KEY` | none | **required** for server answers | without it visitors get sources only, or must bring their own key |
| `HF_HOME` / `HF_HUB_OFFLINE` | `~/.cache/huggingface` / off | bake the model, then `1` | #19 |
| `CODERAG_LLM` | none | not used by the server | CLI only |
| `CODERAG_TEST_POSTGRES_URL` | none | tests only | |

**Unsafe defaults.**
- **Off, as they should be:** there is no debug mode, live check is off, no CORS middleware is installed (same-origin only), and `--reload` isn't used.
- **Unsafe:** `TRUSTED_PROXY_HOPS=0` behind a proxy (#5), public `/docs` (#28), SQLite in a relative `data/` dir (#1, #4), and sign-in silently off without a secret (#10).

**Secrets in the repo.**
- `git log -p --all` (18 commits) was scanned for GitHub, Groq, OpenAI, OpenRouter, Google, HF, AWS, Slack and private-key patterns, and for `postgres://user:pass@` URLs.
- The only hits were test fixtures: `gho_test`, `gho_bob`, `gho_legacy`, `"gho_" + "x" * 36`, `gsk_secret` in a do-not-index fixture, and `postgres://u:p@h/db` in a URL-normalisation test.
- No `.env`, `.db`, key or credential file was ever committed. `.env` is ignored by `.gitignore:31`, and `data/` (with `coderag.db`) by `.gitignore:28`.
- The local `.env` holds only the four `CODERAG_*` sign-in settings.

**Proxy and scheme.** The app does not trust `X-Forwarded-Proto`, and uvicorn trusts it only from `127.0.0.1`. HTTPS
detection therefore comes entirely from `CODERAG_PUBLIC_URL` starting with `https://`
([auth.py:358-359](coderag/web/auth.py#L358-L359)). That works as long as `PUBLIC_URL` is set. The client IP comes from the
`X-Forwarded-For` entry `TRUSTED_PROXY_HOPS` from the right ([limits.py:82-86](coderag/web/limits.py#L82-L86)), which is the correct rule.

**Privacy (what holds).**
- A private repo opens only for users linked to it; anyone else gets 404.
- History is filtered by user.
- Sharing a private repo's answer needs `confirm_private`, and the share shows only cited chunks, at most 40 lines each, with no URLs.
- Signed-out questions are never stored.
- Account deletion removes the user and token, their links, questions, shares and feedback, plus the index and sources of repos nobody else has.
- Gaps: #7, #22, #25.

---

## Required production environment

```bash
CODERAG_PUBLIC_URL=https://coderag.example.com
CODERAG_SESSION_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(48))">
CODERAG_GITHUB_CLIENT_ID=Ov23liXXXXXXXXXXXXXX
CODERAG_GITHUB_CLIENT_SECRET=<from the GitHub OAuth app>
CODERAG_DATABASE_URL=postgresql://coderag_owner:<password>@ep-example-123456.eu-central-1.aws.neon.tech/coderag?sslmode=require
CODERAG_TRUSTED_PROXY_HOPS=1
CODERAG_DATA_DIR=/data                      # a persistent disk (HF persistent storage / Render disk)
GROQ_API_KEY=gsk_<server key>
HF_HOME=/opt/hf                             # model baked here at build time
HF_HUB_OFFLINE=1

# recommended
CODERAG_MAX_LOADED_REPOS=3
CODERAG_SERVER_DAILY_CAP=800                # below the Groq plan's daily quota
CODERAG_LIVE_CHECK=false
```

Start command: `uvicorn coderag.web.app:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1 --no-server-header`.
The GitHub OAuth app's callback URL must be `https://coderag.example.com/api/auth/github/callback`.
