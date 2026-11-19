# coderag security audit

**Date:** 2026-11-19
**Scope:**
- The code at `0fd4732` plus the uncommitted fixes listed below.
- Every commit in the git history.
- The Python and CDN dependencies.
- The public GHCR image `ghcr.io/aadeesh-01/coderag:sha-0fd4732`.
- The live deployment at <https://coderag.ambitioussmoke-a1b7ff88.eastasia.azurecontainerapps.io> (Azure Container Apps `coderag-rg/coderag`, eastasia).

**Method:**
- gitleaks 8.21.2 over the full history and the working tree.
- A layer-by-layer scan of the GHCR image through the registry API.
- Read-only httpx probes against the live site.
- A Playwright XSS harness, run once with CSP bypassed and once with CSP enforced.
- pip-audit and OSV.
- The `az` CLI and the Log Analytics query API.
- New tests in `tests/test_security.py`.

**Rule:** no secret value was printed or recorded anywhere in this audit. Where a check involves a secret, it reports only whether the secret is present, its length, or its masked shape.

## Verdict

**No CRITICAL or HIGH findings. Safe to keep running, and safer once the fixes below are deployed.**

The audit found:
- no secret in git history, the image or the logs;
- no cross-user data access: every id-taking route answers 404 to another user;
- no XSS execution on any of the 8 surfaces tested;
- no exposed debug or admin surface;
- no known vulnerable dependency.

Fixed in this change:
- **Session revocation.** Sign-out now ends the session on the server, so a copied cookie stops working. Before this, sign-out only deleted the cookie in the browser.
- **Rate limits.** The auth, Home and graph routes now have them.
- **Hardening.** A Permissions-Policy header, a stricter answer sanitizer, and `database_url` kept out of `repr`.
- **Unused dependencies.** Three packages the server never imports are no longer installed.

Two MEDIUM findings need your action:
- **OAuth scope.** The `repo` scope grants write access to every repo the user can reach.
- **Database role.** The app connects to Neon as the owner role, `neondb_owner`.

## Scorecard

| # | Item | Result | Summary |
|---|---|---|---|
| 1 | Secrets & env vars | **FIXED** | History, image and logs are clean. The 5 Azure secrets are all `secretRef`s. `database_url` is now kept out of `repr`. |
| 2 | Authentication | **FIXED** (one item NEEDS ACTION) | OAuth state is single-use, signed and bound to the browser. Cookies are HttpOnly, Secure and SameSite=Lax. Sign-out now ends the session on the server. The OAuth scope is too broad (needs action). |
| 3 | Authorization | **PASS** | All 16 id-taking routes return 404 to another user. Share links serve only cited snippets. Account deletion leaves no user rows. |
| 4 | Admin / exposed surface | **PASS** | 31 sensitive paths and traversal variants return 404 on live, with no stack traces. Docs are off, there is no reload, and a forced 500 is generic. |
| 5 | Input, XSS, injection, SSRF | **FIXED** (hardening) | No execution in 2 Playwright passes × 8 surfaces. SQL is ORM or parameterized. Subprocess calls use list args. Fetches are pinned to github.com. The DOMPurify config is now stricter. |
| 6 | Rate limiting & abuse | **FIXED** | Auth, Home and graph routes now have limits. A forged X-Forwarded-For can't bypass them. Live returns 413 on large bodies. Ask timeout and concurrency cap are in place. |
| 7 | CORS, headers, CSRF | **FIXED** | No CORS. CSP has no unsafe-eval. HSTS, nosniff, XFO and Referrer-Policy are present. Permissions-Policy was added. Cross-site POST/DELETE is rejected with 403. |
| 8 | Database | **NEEDS ACTION** | `sslmode=require` is confirmed in production. Tokens are encrypted. Minimal PII. Foreign keys and indexes are sound. The app uses the owner role (see follow-up F2). Free-plan restore window is 6 h. |
| 9 | Dependencies | **FIXED** | pip-audit and OSV are clean. SRI is verified on all 7 CDN scripts. 3 unused packages were removed. No security-relevant updates are pending. |
| 10 | Azure infrastructure | **PASS** | HTTPS only, max 1 replica, no public blob access, TLS 1.2 minimum. Non-root container. Logs hold no secrets or query strings. |

## Findings

| # | Severity | Area | Location | Evidence | Fix | Status |
|---|---|---|---|---|---|---|
| 1 | MEDIUM | AuthN | `coderag/web/auth.py:44` `SCOPE = "read:user repo"` | GitHub's `repo` scope grants **read and write** to all of the user's public and private repos. coderag only reads them, so a stolen token (DB plus `TOKEN_KEY`) could push code. | Move to a GitHub App with **Contents: read-only** and **Metadata: read**, installed per repo. Its user-to-server tokens are scoped to the installation and expire after 8 h. Short term: keep `repo` only for users who add private repos, and use `read:user` for everyone else. | NEEDS ACTION (F1) |
| 2 | MEDIUM | DB | Production `CODERAG_DATABASE_URL` | Log Analytics shows the role is `neondb_owner` in all 12 startup lines, which are masked as `neondb_owner:***@…neon.tech`. That role is a member of `neon_superuser`, so it can create roles and databases. | Create a dedicated `coderag_app` role (SQL below). | NEEDS ACTION (F2) |
| 3 | MEDIUM | AuthN | `coderag/web/auth.py` (logout) | Logout only deleted the cookie in the browser. A copied cookie stayed valid for up to the 30-day `SESSION_MAX_AGE`. | Added `users.session_version` (migration `0004`). The cookie now carries `sv`. `current_user` rejects a stale `sv` (`auth.py:393`). `logout` calls `Users.end_sessions` (`auth.py:226`), which bumps the version. | FIXED, test `test_signing_out_ends_the_session_on_the_server` |
| 4 | MEDIUM | Abuse | `coderag/web/app.py` | The auth routes (the OAuth callback calls GitHub), `GET /api/home` (several DB queries) and the graph route had no per-IP limit. | Auth router gets `dependencies=[Depends(auth_limit)]` at 300/h per IP (`app.py:313-315`). Graph uses `retrieve_per_ip_per_hour` (`app.py:563`). Home is limited per IP and per user (`home.py:56-57`). | FIXED, test `test_the_auth_routes_home_and_the_graph_are_rate_limited` |
| 5 | LOW | Headers | `coderag/web/middleware.py:88` | No `Permissions-Policy` header was sent. | Added `camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()`. | FIXED, test `test_every_response_has_the_security_headers` |
| 6 | LOW | XSS (defense in depth) | `frontend/lib.js:53`, `app.js:119`, `answer.js:25` | DOMPurify defaults allow `<style>`, `style=`, `<form>`, `<input>` and `<iframe>`. These could be used for page-covering overlays or fake sign-in boxes in LLM output, even without script execution. | Shared `SANITIZE_OPTIONS` forbids style, form controls, frames, media, `dialog`, `srcset`, `formaction` and `autofocus`. `data-*` attributes stay for citation chips. | FIXED, Playwright: 0 dangerous DOM |
| 7 | LOW | XSS | `frontend/lib.js:60-72` | The citation regexes run on the Markdown and HTML **before** sanitizing. A citation inside an attribute value turns into garbled text. | No security impact: the result goes through `sanitize()` last (`lib.js:72`), so injected markup is still filtered. Optional: run the citation pass on text nodes after sanitizing. | ACCEPTED (cosmetic) |
| 8 | LOW | Secrets | `coderag/web/settings.py:18` | `database_url` (which includes the password) appeared in `repr(Settings)`. | `field(default="", repr=False)`. Logs already used `safe_url`. | FIXED, test `test_settings_never_show_secrets_in_repr` |
| 9 | LOW | Secrets | `frontend/app.js:134-141` | A visitor's own LLM key is kept in `localStorage` (`SETTINGS_KEY`), which survives the tab and is readable by any script on the origin. It is sent only per request (`app.js:147`). The server never logs or stores it, and the answer cache key excludes it. | Store the key in `sessionStorage` by default, with an opt-in "remember on this device" checkbox. CSP and the sanitizer already limit script on the origin. | NEEDS ACTION (optional) |
| 10 | LOW | Headers | CSP `style-src` | `style-src` includes `'unsafe-inline'`, which the graph and layout need. Scripts are strict: `'self'`, jsdelivr and per-file hashes, with no `unsafe-eval`. | Move inline styles to classes or CSSOM, then drop `'unsafe-inline'`. Low value: style injection is also blocked by finding 6. | ACCEPTED |
| 11 | LOW | Infra | Storage account `coderagdcecf6e5` | `networkRuleSet.defaultAction = Allow` and shared-key access is on (the default). The SMB share is reachable from the internet with the account key. | Container Apps on a Consumption environment without a VNet can't use a private endpoint. Keep the key secret and rotate it if exposed. Longer term, use a VNet-integrated environment with a private endpoint. | ACCEPTED |
| 12 | LOW | Deps | `requirements.txt` | `uvicorn[standard]` pulled in `watchfiles` (only for `--reload`), `websockets` (there are no websocket routes) and `python-dotenv` (no `.env` loading). None is imported, so they were unused code in the image. | Pin `uvicorn`, `httptools` and `uvloop` explicitly, and regenerate the lock. | FIXED |
| 13 | INFO | Secrets | Git history: `.hallmark/salt` | This UI-tool salt was added in `12d8a24` and deleted in `831473e`. It is not a credential and nothing in coderag uses it. `.hallmark/` is now gitignored. | None needed. | INFO |
| 14 | INFO | Ops | Log Analytics | 11 alembic `Can't locate revision '0003'` tracebacks came from an older image starting against a newer database during a rollout. 5 faiss `swigfaiss_avx512/avx2` import fallbacks are normal. Neither leaks anything. | None. With single-revision mode and 1 replica, the old revision is replaced. | INFO |

There are no CRITICAL or HIGH findings, so none needed fixing beyond the above.

## Evidence by item

### 1. Secrets & env vars

**Git history.**
- `gitleaks detect` covered 30 commits, all refs (`main`, `origin/main`, no other branches), including deleted files.
- It found **1 hit**: `generic-api-key` in `tests/test_deploy_azure.py` (commit `0ef097a`). That is the `GPG_KEY` name in a fixture, an 8-character placeholder, so a false positive.
- A regex pass over `git log -p --all` found DB URLs only as placeholders (`user:pass@host`).

**Working tree.** 25 hits, none tracked except the false positive above:

| Location | Hits | What they are |
|---|---|---|
| `tests/test_deploy_azure.py` (tracked) | 1 | The same `GPG_KEY` false positive |
| `.env` | 1 | Gitignored, never committed |
| `deploy/azure/*.env` (`prod.env`) | 4 | Gitignored (`.gitignore:41`), never committed: `git log --all -- deploy/azure/prod.env .env` is empty |
| `.venv/Lib` | 15 | Package test fixtures |
| `eval/.repos` | 4 | Cloned third-party repos' test keys; gitignored and dockerignored |

**Image.** `ghcr.io/aadeesh-01/coderag:sha-0fd4732` (public), scanned layer by layer by streaming through the GHCR API.
- 0 findings: no `.env`, `data/`, `.git`, `tests/`, private keys or tokens.
- `USER user` (`Dockerfile:69`). The CMD is `uvicorn … --workers 1 --no-server-header`, with no `--reload` (`Dockerfile:76`).

**Settings.**
- `Settings.validate()` (`settings.py:62`) fails fast on missing GitHub or session settings, and on secrets shorter than 32 characters (`settings.py:82`).
- `github_client_secret`, `session_secret`, `token_key` and now `database_url` are `repr=False`.
- Error pages are generic (item 4). API responses never include tokens: `User.public()` returns only name, login and avatar.

**Azure.** The secrets are Container Apps secrets, each referenced by an env var through `secretRef` with no inline value (names only):

| Secret | Env var |
|---|---|
| `session-secret` | `CODERAG_SESSION_SECRET` |
| `token-key` | `CODERAG_TOKEN_KEY` |
| `github-client-secret` | `CODERAG_GITHUB_CLIENT_SECRET` |
| `database-url` | `CODERAG_DATABASE_URL` |
| `groq-api-key` | `GROQ_API_KEY` |

**Frontend.** No API keys in `frontend/`. The visitor's LLM key is covered in finding 9.

### 2. Authentication

**OAuth state.**
- Random state in a signed cookie with `Max-Age=600`, HttpOnly, Secure and SameSite=Lax.
- Compared with `hmac.compare_digest` (`auth.py:486`), then deleted (single use).
- A bad or missing state redirects to `/login`.

**Redirect URI.** Built from `CODERAG_PUBLIC_URL` (`auth.py:446`), never from the Host header. Tested.

**Token storage.** GitHub tokens are **encrypted, not hashed**: Fernet, with a key derived from `TOKEN_KEY` by HKDF (`users.token_encrypted`). No passwords exist anywhere in the system.

**Session cookie.**
- itsdangerous-signed (`{"uid", "sv"}`), HttpOnly, Secure, SameSite=Lax, 30 days.
- Tampered, wrong-salt and expired cookies return 401. Tested in `test_security.py`.

**Logout.** Revokes the session server-side (finding 3). Scope: finding 1.

**Route/auth table.** Generated from `app.routes`. The status is for a **signed-out** request with placeholder ids.

| Route | Signed out | Notes |
|---|---|---|
| GET /a/{slug} | 200 | Public share page shell |
| GET /api/demos | 200 | Public |
| GET /api/github/repos | 401 | |
| GET /api/health | 200 | Public |
| GET /api/ready | 200 | Public |
| GET /api/home | **401** | Asked about specifically |
| GET /api/jobs | 401 | |
| GET /api/jobs/{job_id} | 404 | A visitor's job id is unguessable; another user's job returns 404 (tested) |
| DELETE /api/me | 401 | |
| POST /api/questions/{id}/feedback | 422 → 401 | Body validated first; with a valid body, 401/404 (tested) |
| POST /api/questions/{id}/share | 401 | |
| DELETE /api/questions/{id}/share | 401 | |
| POST /api/repos | 422 | Public **by design**: visitors may index public repos. Rate-limited per IP; private repos need sign-in. |
| GET /api/repos/mine | 401 | |
| PUT /api/repos/mine/{id} | 401 | |
| DELETE /api/repos/mine/{id} | 401 | |
| GET /api/repos/{id} | 404 | Demos and public visitor repos only |
| POST /api/repos/{id}/ask | 422 | Public for demos and public repos (by design). Rate-limited; private repos return 404. |
| GET /api/repos/{id}/file | 404 | Only indexed files of an accessible repo |
| GET /api/repos/{id}/freshness | **401** | |
| GET /api/repos/{id}/graph | 404 | Accessible repos only; now rate-limited |
| GET /api/repos/{id}/history | 401 | |
| POST /api/repos/{id}/live | 404 | Live check is off in production |
| DELETE /api/repos/{id}/mine | 401 | |
| POST /api/repos/{id}/rebuild | **401** | |
| POST /api/repos/{id}/reindex | 401 | |
| POST /api/repos/{id}/retrieve | 422 | Same as ask |
| POST /api/repos/{id}/update | **401** | |
| GET /api/shares/{slug} | 404 | |
| GET /app, GET /login | 200 | Static shells |
| GET /api/auth/github | 302 | To GitHub, with state |
| GET /api/auth/github/callback | 303 → /login | When the state is bad or missing |
| GET /api/auth/me | 401 | |
| POST /api/auth/logout | 204 | Harmless signed out |

No state-changing or user-data route works signed out, apart from the public-by-design compute on public repos. Those are limited by the submit, ask and retrieve buckets.

### 3. Authorization

The `tests/test_security.py` fixture `world` sets up:
- Alice, who indexes the **private** repo `corp/secret`, asks a question, shares it with `confirm_private`, and leaves feedback;
- Bob, who is signed in with a different token;
- a signed-out visitor.

Results:
- **All 16 id-taking routes** return **404** to Bob, and 401 or 404 to the visitor, never 403. The routes are repo info, graph, file, retrieve, ask, history, live, reindex, freshness, update, rebuild, PUT mine, jobs/{id}, share POST and DELETE, and feedback.
- Lists stay isolated. Bob removing the repo from his own list doesn't touch Alice's index.
- Alice's private share serves only the cited snippets (at most 40 lines each), with `repo.url == ""`. There is no file, graph or extra snippet access through the share.
- `DELETE /api/me` leaves no row with the user's id: questions, shares and feedback cascade, and repos and jobs have their `owner_id`/`user_id` set to NULL. Tested.

### 4. Exposed surface: live results

Every path below returned **404** with the same 22-byte JSON `{"detail":"Not Found"}`, and no stack trace or server banner:

| Category | Paths |
|---|---|
| API docs | `/docs`, `/redoc`, `/openapi.json` |
| Config and VCS | `/.env`, `/.git/config`, `/.git/HEAD`, `/.dockerignore`, `/deploy/azure/prod.env` |
| Data | `/data/`, `/data/coderag.db`, `/coderag.db` |
| Source and build | `/requirements.lock`, `/Dockerfile`, `/pyproject.toml`, `/app.py`, `/coderag/web/app.py`, `/tests/lib.test.mjs` |
| Admin and debug | `/admin`, `/debug`, `/api/internal`, `/api/internal/x`, `/server-status`, `/api/nope` |
| Traversal | `/static/../…`, `/..%2f..`, `/%2e%2e/…`, `/app/../../etc/passwd`, `/frontend/../…`, `/..\..\windows\win.ini`, `/a/../../etc/passwd` |

**File viewer.** `GET /api/repos/{demo}/file?path=` answered **404 "Not an indexed file"** for every variant: `../`, `%2f`, `%2e%2e`, absolute paths, `C:\`, `....//`, a null byte, and a 600-character path. Paths are allowlisted against the index, and reads are confined by `_safe_join`.

**Debug off.**
- Docs are disabled unless a flag is set (`app.py:251-252`), and there is no `--reload`.
- A forced 500 (`test_an_unexpected_error_shows_no_details`) returns `{"detail":"Internal Server Error"}` with no traceback.

### 5. Input handling, XSS, injection

**Server-side validation.** Pydantic enforces lengths, types and bounds (`top_k`, question length). Oversized or invalid input gets 422 or 413.

**XSS.** The Playwright harness seeds payloads into:
- the question;
- the repo name and description;
- file paths;
- code content;
- README and config chunks;
- GitHub facts;
- the user's name;
- the answer markdown, including `javascript:` links, `onerror`, `<svg onload>`, `<iframe>`, `<form>` and `<style>` overlays.

It then visits 8 surfaces: overview and facts, answer, code viewer, graph hover, saved-answers dialog, Home, Repos page and `/a/{slug}`. It runs this twice, once with CSP bypassed (sanitizer only) and once with CSP on. Result: **0 executed, 0 dangerous DOM nodes, 0 requests to other hosts**.

**Sinks.** `innerHTML` is used only through `renderAnswer`, which is sanitized last, and `highlightLines`, which is HTML-escaped. There are no `outerHTML` or `insertAdjacentHTML` sinks with user data.

**SQL.** Only ORM queries, or constant `text()` with bound parameters.

**Commands.** `subprocess` is always called with a list and `--` before arguments. There is no `shell=True`.

**Repo URLs.** Must match an anchored `owner/repo` regex, and are fetched only from `https://github.com/`.

**SSRF.** The live check resolves the host, rejects private and reserved addresses, and pins the resolved IP for each request. It is off in production (`POST /live` returns 404).

### 6. Rate limiting & abuse

**Buckets.** Per IP, and per user where signed in:

| Route | Bucket |
|---|---|
| Ask | `ask` / `ask-own` |
| Retrieve | `retrieve` |
| Index and submit | `submit` |
| Update and rebuild | `submit` |
| Live check | 30/h |
| Share | Limited |
| File viewer | `file` |
| GitHub repo list | `github-repos` |
| Freshness | Limited |
| Auth (new) | `auth`, 300/h |
| Graph (new) | `graph` |
| Home (new) | `home` |

**X-Forwarded-For.** `client_ip` takes the Nth entry from the right, with `TRUSTED_PROXY_HOPS=1`, so it uses the entry Envoy appended. In the local test with forged XFF headers and a limit of 2, the responses were `[200, 200, 429, 429]` (`test_a_forged_x_forwarded_for_cannot_dodge_the_per_ip_limit`). On live, Envoy appends the real client address and that entry is the one used. Kept light: no flooding.

**Body limit.** Live returned **413** for a 70 KB body.

**Asks.** `ask_timeout` is 90 s and `max_concurrent_asks` is 8, both covered by existing tests (`test_hardening.py`). These were not load-tested on live, on purpose.

### 7. CORS, headers, CSRF (live)

**CORS.** No CORS middleware. An `OPTIONS` preflight from `https://evil.example` got **405**, with no `Access-Control-Allow-Origin`.

**Headers** on `/`, `/app`, `/a/{slug}` and API JSON:
- `Content-Security-Policy`: `script-src 'self' https://cdn.jsdelivr.net 'sha256-…'`. The inline-script hashes are computed at startup from the served HTML, so they stay current. No `unsafe-eval`; `frame-ancestors 'none'`.
- `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`.
- No `Server` or `X-Powered-By`.
- `Permissions-Policy` was missing and is added by this change; it appears on live after the redeploy.

**CSRF.** `POST` and `DELETE` with `Origin: https://evil.example` or `Origin: null` got **403**.

**SRI.** All 7 jsDelivr scripts have `integrity="sha384-…" crossorigin="anonymous"`, and each hash matched the live file.

### 8. Database

**SSL.** Every production startup line in Log Analytics (12 of 12) logs the masked URL with `sslmode=require` and `channel_binding` in its query. The password field is exactly `***` in all 12. `prod.env` was not read.

**Role.** `neondb_owner` (finding 2, follow-up F2).

**DB URL exposure.** The URL is never returned by the API, is kept out of `repr`, and logs use the masked `safe_url`.

**PII.** `users` holds GitHub id, login, display name, avatar URL and the encrypted token. There is no email. IPs are used only as in-memory rate-limit keys and are never stored. `questions` stores the question, answer and sources the user asked for.

**Foreign keys.**

| Column | References | On delete |
|---|---|---|
| `user_repos.user_id` | users | CASCADE |
| `questions.user_id` | users | CASCADE |
| `questions.repo_id` | repos | CASCADE |
| `shares.question_id` | questions | CASCADE |
| `feedback.question_id` | questions | CASCADE |
| `feedback.user_id` | users | CASCADE |
| `repos.owner_id` | users | SET NULL |
| `jobs.user_id` | users | SET NULL |

`jobs.repo_id` has no foreign key on purpose, because the repo row is written at the end of indexing.

**Indexes.** They match the query paths:
- `ix_jobs_user_created`;
- `ix_questions_user_repo_created`, `ix_questions_repo_created`, `ix_questions_server_key_created`;
- an index on every foreign key;
- `uq_feedback_question_user`.

**Migrations.**
- `0004_session_version` only adds a NOT NULL column with `server_default "0"`. That is safe on a live table: existing cookies without `sv` read as 0 and stay valid.
- Migrations run on container start. The predeploy check never migrates (tested).

**Backups.** The Neon **Free plan** restore window (history window for instant restore and branching) is **6 hours, capped at 1 GB of change history**. Paid plans allow up to 7 days (Launch) and 30 days (Scale). The Free plan also includes **1 manual snapshot**. Sources: [Neon docs: History window](https://neon.com/docs/postgres/backup-restore/history-window), [Neon docs: Backups](https://neon.com/docs/postgres/backup-restore/backups).

### 9. Dependencies

**Vulnerabilities.**
- `pip-audit -r requirements.lock` reports no known vulnerabilities.
- `torch 2.15.0+cpu` can't be audited from PyPI (local version tag), so it was checked on the OSV API: 0 vulnerabilities.
- The CDN libraries (marked 12.0.2, DOMPurify 3.2.6, highlight.js 11.9.0, d3 7.9.0, all via jsDelivr) have no CVEs for the pinned versions, and SRI is verified.

**Outdated.**

| Gap | Packages | Action |
|---|---|---|
| Minor | `fastapi 0.163 → 0.165`, `mpmath`, `sqlalchemy 2.0 → 2.1` (`<2.1` is pinned on purpose) | None fixes a security issue, so not updated (follow-up F4) |
| Patch | 4 packages, none with a security fix | Left as is |
| Major | None needed | |

**Removed (unused).** `websockets==17.4`, `watchfiles==1.3.0`, `python-dotenv==1.2.4`. They came only from `uvicorn[standard]` and are never imported, which was confirmed by grep across `coderag/`, `frontend/` and the Dockerfile. deptry reported only false positives.

**Kept explicitly.** `httptools==0.9.0` and `uvloop==0.23.0`, uvicorn's fast HTTP parser and event loop, so runtime behaviour is unchanged.

**Lock.**
- Regenerated with the command in the lock header, using the previous lock as the starting point, so every other pin is unchanged.
- The diff is exactly the 3 removed packages, going from 70 to 67 pins.
- The header and both index lines are kept, torch stays `2.15.0+cpu`, and all 1,431 `--hash` lines are present.

**Image size.**

| | Compressed |
|---|---|
| Before (`sha-0fd4732` on GHCR, from the layer scan) | **≈ 608 MB** |
| After (estimate) | **≈ 607 MB** |

- The removed manylinux/py3 wheels total 0.68 MB compressed and 2.05 MB installed: websockets 221 KB / 817 KB, watchfiles 448 KB / 1,220 KB, python-dotenv 23 KB / 64 KB.
- The saving is small. The real benefit is less code that could run in the image (`watchfiles` is native Rust, `websockets` is a network protocol stack).
- Docker isn't available locally. The CI smoke step prints the measured size (`docker image ls coderag:ci --format 'image size: {{.Size}}'`, uncompressed) on the next push.

### 10. Azure infrastructure

**Ingress.** `allowInsecure: false`, external, transport Auto, target port 7860. Live: `http://` redirects to `https://`.

**Scale and revisions.** `minReplicas 0`, `maxReplicas 1`, `activeRevisionsMode: Single`.

**Image.** `ghcr.io/aadeesh-01/coderag:sha-0fd4732`. It is public and free of secrets (item 1), and runs as non-root `user`.

**Storage account `coderagdcecf6e5`** (StorageV2, Standard_LRS):

| Setting | Value |
|---|---|
| `allowBlobPublicAccess` | `false` |
| `supportsHttpsTrafficOnly` | `true` |
| `minimumTlsVersion` | `TLS1_2` |
| `allowCrossTenantReplication` | `false` |
| `networkRuleSet.defaultAction` | `Allow`, 0 IP rules (finding 11) |
| `publicNetworkAccess` | Not set (Enabled by default) |
| Private endpoints | 0 |
| Blob containers | None |

**File share.** `coderag-data` (SMB, 5 GiB) is mounted ReadWrite in the environment as `coderag-data`. Azure Files has no anonymous or public access mode, so every access needs the account key or Entra ID, and there are no blob containers that could be made public.

**Logs.** 2,312 console lines from Log Analytics `ContainerAppConsoleLogs_CL`, 2026-11-18 14:55 to 2026-11-19 05:27 UTC, scanned with regexes:

| Pattern | Matches |
|---|---|
| GitHub tokens (`gh?_`, `github_pat_`) | 0 |
| `sk-` keys | 0 |
| Bearer headers | 0 |
| Fernet-shaped keys | 0 |
| Session or OAuth cookie values | 0 |
| `password=`, `token=` and similar | 0 |
| Private keys | 0 |
| DB URLs | 12, all with the password masked as `***` |

- 816 access-log request lines, **0 with a query string**, and 0 with `?code=` or `&state=`. The OAuth callback is logged as `GET /api/auth/github/callback`. `DropQueryStrings` on `uvicorn.access` (`middleware.py:223`, tested in `test_hardening.py:265`) strips queries.
- The live tail (`az containerapp logs show --tail 300`) had no buffered lines after the replica woke.

## Tests

**New: `tests/test_security.py`** (43 tests) covers:
- the cross-user 404 matrix;
- list isolation;
- private-share snippet limits;
- tampered, wrong-salt and expired cookies;
- logout revocation;
- cookie flags and `redirect_uri`;
- a generic 500;
- security headers, including Permissions-Policy;
- the forged-XFF limit;
- account deletion;
- `repr` of the settings;
- auth, graph and Home rate limits.

**Existing tests changed** because behaviour intentionally changed:

| Test | Change | Reason |
|---|---|---|
| `tests/test_db.py:92` and `:105` | Migration head `0003` → `0004` | New migration |
| `tests/test_hardening.py:211` | `0003` → `0004` | New migration |
| `tests/test_deploy.py:49` | `"at 0004 (head 0004; …)"` | New migration |
| `tests/test_deploy.py:72` | `"…behind this checkout's head 0004"` | New migration |
| `tests/test_deploy.py:124` | Asserts `httptools` and `uvloop` are listed and `uvicorn[standard]` is not | Unused uvicorn extras removed |

**Results:**
- `pytest`: **421 passed, 1 skipped**. The skipped test is the Postgres test, which needs `CODERAG_TEST_POSTGRES_URL`.
- `node --test frontend/tests/lib.test.mjs`: **30/30**.
- `node --check` passes for `app.js`, `answer.js` and `lib.js`.

## Follow-ups for you

### F1. Narrow GitHub access (finding 1)

1. Create a GitHub App (*Settings → Developer settings → GitHub Apps*) with these repository permissions:
   - **Contents: Read-only**
   - **Metadata: Read-only**
   - No write permissions.
   - Callback URL: `<PUBLIC_URL>/api/auth/github/callback`.
2. Turn on "Request user authorization (OAuth) during installation".
3. Switch `CODERAG_GITHUB_CLIENT_ID` and the `github-client-secret` secret to the App's values.
4. Use user-to-server tokens, which expire after 8 h and come with a refresh token. This needs a small code change to store the refresh token and refresh it.

Until then, the `repo` scope stays the main residual risk: a stolen token (DB plus `TOKEN_KEY`) could push code.

### F2. Restricted Neon role (finding 2)

Run this as `neondb_owner` in the Neon SQL editor, on the database named in your URL (`neondb` below). It creates a role that **owns only coderag's tables**, so startup migrations keep working, but isn't `neon_superuser` and can't create roles or databases:

```sql
-- 1. The role. Generate the password; don't reuse one: openssl rand -base64 33
CREATE ROLE coderag_app WITH LOGIN PASSWORD '<new random password>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10;

-- 2. Database and schema access: only coderag_app may create objects in public
REVOKE ALL ON DATABASE neondb FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE neondb TO coderag_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO coderag_app;

-- 3. Hand over coderag's tables (incl. alembic_version) and their sequences
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO coderag_app', r.tablename);
  END LOOP;
  FOR r IN SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = 'public' LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO coderag_app', r.sequence_name);
  END LOOP;
END $$;

-- 4. Check: should list only coderag_app as owner, and show rolsuper/rolcreaterole/rolcreatedb = false
SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public';
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = 'coderag_app';
```

Then point the app at the new role, keeping the same host and query, including `?sslmode=require&channel_binding=require`:

```powershell
$env:PATH = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin;$env:PATH"
az containerapp secret set -g coderag-rg -n coderag --secrets "database-url=<postgresql://coderag_app:...@...neon.tech/neondb?sslmode=require&channel_binding=require>"
az containerapp revision restart -g coderag-rg -n coderag --revision (az containerapp show -g coderag-rg -n coderag --query properties.latestRevisionName -o tsv)
```

Also update `deploy/azure/prod.env` so a later `deploy.ps1` doesn't put the owner URL back.

For a stricter setup, use a DML-only app role (`SELECT, INSERT, UPDATE, DELETE` and sequence `USAGE`), with migrations run by a separate owner URL. That needs a small app change, a separate migration URL, so it's optional.

### F3. Rotate secrets

Nothing found in this audit needs rotating: no secret was found in git history, the image, logs or API responses. Rotate only if you know a value left your machine, for example pasted into chat, shared, or on a lost laptop:

| Secret | How to rotate |
|---|---|
| `database-url` | Reset the role password in Neon |
| `github-client-secret` | Generate a new one in the OAuth App |
| `groq-api-key` | Revoke it in the Groq console |
| `session-secret` | Rotating it signs everyone out |
| `token-key` | Stored GitHub tokens become unreadable, so users must sign in again |

To apply a rotation: `az containerapp secret set … --secrets name=value`, then restart the revision.

### F4. Optional updates

None of these fixes a security issue:
- `fastapi 0.165` (minor);
- `sqlalchemy 2.1` (minor, currently pinned `<2.1`; test it first);
- `mpmath` (minor);
- the 4 patch-level updates.

### F5. Optional hardening

- Visitor key in `sessionStorage` (finding 9).
- Drop `style-src 'unsafe-inline'` (finding 10).
- VNet-integrated environment with a storage private endpoint (finding 11).
- After the redeploy, check the new header is live: `curl -sI https://coderag.ambitioussmoke-a1b7ff88.eastasia.azurecontainerapps.io/ | findstr /i permissions-policy`.
