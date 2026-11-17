# Retrieval evaluation

Measures what each pipeline version actually puts in front of the LLM. No LLM is called: for each
question, the harness builds the exact context a version would send and scores it against
hand-labelled gold symbols.

```bash
python eval/fetch_repos.py                 # clone the pinned real repos into eval/.repos/
python eval/run_eval.py                    # every question set, every version
python eval/run_eval.py --by-type          # break down by question type
python eval/run_eval.py --versions v5 "v5+chunk_methods=True+max_file_chars=1000000"
python eval/run_eval.py --csv eval/results/run.csv
```

A version can be a preset (`v5`) or an ablation: `preset+field=value+...`, using any `RAGConfig`
field.

## Question sets

| Set | Repo | Pinned SHA | Questions |
|---|---|---|---|
| `sample_repo` | bundled toy repo | n/a | 19 |
| `requests` | psf/requests (`src/requests`) | `3c7baaa0` | 22 |
| `click` | pallets/click (`src/click`) | `2247b35e` | 21 |
| `commander` | tj/commander.js v14.0.3 (`lib`), **JavaScript** | `b7d43804` | 12 |
| `coderag` | this repository (frontend JS + web API Python), **cross-language** | working tree | 15 |
| `fastapi_react` | testdrivenio/fastapi-react (React TS + FastAPI), **cross-language** | `e0116876` | 10 |

Each question has a type (`locate`, `callers`, `callees`, `inheritance`, `flow`) and gold symbols
written as `path::Class.method`. Gold symbols in `.py` files are located with `ast`; in other
languages, with the tree-sitter chunker the index uses. Call relationships in the gold labels were checked against the
source at the pinned SHA. Overloaded names resolve to the last definition (the implementation, not
the `@overload` stub).

## Metrics

A chunk *covers* a gold symbol when it holds at least half of the symbol's lines, or at least half
of the chunk is made of the symbol. This works across granularities: whole classes, methods, and
v0's fixed windows (mapped back to file lines).

| Metric | Meaning |
|---|---|
| R@ctx | share of gold symbols covered by a retrieved chunk (ignores truncation) |
| Sig | share of gold symbols whose `def`/`class` line is actually visible in the context |
| Body | share of gold source lines visible in the context |
| Hit@1, Hit@3, MRR | rank of the first retrieved chunk covering any gold symbol |
| Chunks, Chars | context size |

`R@ctx` says whether retrieval found the right code. `Sig` and `Body` say whether the LLM gets to
see it after truncation and cleaning.

## Results (2026-11-03)

Per-question rows: [results/2026-11-03.csv](results/2026-11-03.csv).

| Set | Version | R@ctx | Sig | Body | MRR | Chars |
|---|---|---|---|---|---|---|
| requests | v0 | 0.60 | 0.40 | 0.48 | 0.47 | 5028 |
| requests | v1 | 0.36 | 0.20 | 0.09 | 0.34 | 1296 |
| requests | v5 | 0.38 | 0.20 | 0.06 | 0.33 | 3651 |
| requests | **v6** | **0.79** | **0.78** | **0.54** | **0.55** | 9760 |
| click | v0 | 0.48 | 0.39 | 0.32 | 0.48 | 5028 |
| click | v1 | 0.31 | 0.26 | 0.06 | 0.31 | 1478 |
| click | v5 | 0.33 | 0.26 | 0.05 | 0.25 | 4567 |
| click | **v6** | **0.86** | **0.67** | **0.48** | **0.62** | 9436 |
| sample_repo | v0 | 0.91 | 0.90 | 0.90 | 0.89 | 4289 |
| sample_repo | v5 | 0.82 | 0.78 | 0.69 | 0.76 | 2463 |
| sample_repo | **v6** | **1.00** | **1.00** | 0.89 | 0.79 | 2978 |

### Findings

1. **v1–v5 lost to the naive v0 baseline on real repos.** The main cause was `max_file_chars=30000`:
   the chunker silently skipped the core files (`requests/{models,sessions,utils}.py`,
   `click/{core,types,termui}.py`). Indexing them alone took v5 from 0.38 to 0.69 R@ctx (requests)
   and from 0.33 to 0.74 (click).
2. **Whole-class chunks hide methods.** `click.Command` is 700 lines in one chunk. MiniLM embeds
   only its start and the context keeps 400 chars. Method-level chunks raised Sig from 0.31 to 0.71
   (requests).
3. **The LLM saw almost none of the gold code.** Body was about 0.05 for v1–v5 because of the
   350–400 char budgets and line cleaning. Raising the budget to 1500 and turning cleaning off
   brought it to about 0.5.
4. **The keyword filter (v2) never helped.** It's a no-op on real repos and costs about 0.18 R@ctx
   on `sample_repo`.
5. **Callers questions need `called_by` edges.** Vector search for "who calls X" finds X itself.
   Following callers doubled callers recall, but it hurts other question types, so v6 only does it
   when the question asks about callers (`auto_callers`). That intent pattern was tuned on these
   question sets, so treat its numbers as optimistic.
6. **Graph expansion vs. retrieving more.** At an equal ~10k-char budget, v6 without graph
   expansion (`max_hops=0, top_k=10`) matched v6 on R@ctx (requests 0.77 vs 0.79, click 0.86 vs
   0.86). Name-only call resolution was the likely cause. See v7 below.

At equal budget, v6 still beats v0 (`v0+top_k=10`: requests 0.72 R@ctx, click 0.60).

## v7: scope-aware call resolution

Per-question rows: [results/v7-ablation.csv](results/v7-ablation.csv).

v6 linked every call to every chunk with the same bare name: `json.load(f)` linked to
`BaseLoader.load`, and one `convert()` call linked to every `convert` method. v7
([coderag/resolve.py](../coderag/resolve.py)) resolves each call from its form and the file's imports:
same-file definitions, imported names (following `__init__` re-exports), `self.`/`super()` through
the class hierarchy (including subclass overrides), and module attributes. Calls into external
modules get no edge. A call on an object of unknown type links only if exactly one class defines the
method.

| Graph | requests edges | click edges |
|---|---|---|
| name-based (v6) | 375 | 959 |
| scoped (v7) | 250 (116 exact, 112 import, 22 unique) | 546 (339 exact, 129 import, 78 unique) |

| Set | Version | R@ctx | Sig | Body | MRR | Chars |
|---|---|---|---|---|---|---|
| requests | v6 | 0.79 | 0.78 | 0.54 | 0.55 | 9760 |
| requests | **v7** | **0.83** | **0.82** | **0.55** | **0.56** | 9829 |
| requests | v7, vector only (`max_hops=0, top_k=10`) | 0.77 | 0.65 | 0.53 | 0.56 | 10575 |
| click | v6 | 0.86 | 0.67 | 0.48 | 0.62 | 9436 |
| click | **v7** | **0.88** | 0.74 | 0.52 | **0.63** | 9431 |
| click | v7, vector only (`max_hops=0, top_k=10`) | 0.86 | **0.75** | **0.55** | 0.62 | 11159 |

7. **Precise edges help most on relationship questions.** Callers R@ctx: requests 0.60 → 0.80,
   click 0.62 → 0.75. Click callees Sig: 0.39 → 0.78. It costs no extra context.
8. **Graph expansion now beats retrieving more on requests** (Sig 0.82 vs 0.65, with less context).
   On click it's about even overall: the graph wins on callers and callees but loses on inheritance
   (0.67 vs 0.83 R@ctx) and on locate signatures. On requests, flow questions also do better with
   more vector hits (0.75 vs 0.50). Mixing the two by question type is a possible next step.
9. **Allowing ambiguous `obj.foo()` links (`max_ambiguous=3`) changed nothing measurable**, so v7
   keeps the strict setting.

Caveat: each type has only 3–7 questions per repo, so one question moves a per-type score by
0.14–0.33. Trust the overall rows more than the per-type ones.

## v8: overload stubs, expansion order, type guards

Per-question rows: [results/v8-ablation.csv](results/v8-ablation.csv) and [results/v8.csv](results/v8.csv).
These findings came from a UI review, which reported four `group` nodes in click's graph, and from
the retrieval debug view (`?debug=1` on the site, `stream(debug=True)` in Python).

- **`@typing.overload` stubs were indexed as separate chunks.** `click.decorators.group` has 3 stubs
  plus the implementation, all with the same qualified name, so a call resolved correctly by import
  still linked to all 4. click had 33 such stubs (565 → 532 chunks). v8 drops stubs and keeps the
  implementation.
- **Graph expansion order was effectively arbitrary.** v4–v7 seeded the BFS in Python set order,
  which follows chunk index, not search rank. Dropping stubs alone *lowered* requests recall
  (0.83 → 0.77) only because the shifted indexes changed which seed expanded first before the cap of
  8 filled up. v8 expands the best vector hit first, and callers first for "who calls X?" questions.
- **Type-guard calls inside `assert`s** (`assert _is_prepared(request)`, 10 call sites in requests)
  turned a typing helper into a hub that connected unrelated code. v8 ignores calls made only inside
  `assert` statements. That had no measurable effect on recall but removed noise from the context.

| Set | Version | R@ctx | Sig | Body | MRR |
|---|---|---|---|---|---|
| click | v7 | 0.88 | 0.74 | 0.52 | 0.63 |
| click | v7 + skip overload stubs only | 0.94 | 0.77 | 0.54 | 0.64 |
| click | **v8** | **0.94** | **0.80** | **0.56** | **0.64** |
| requests | v7 | 0.83 | 0.82 | 0.55 | 0.56 |
| requests | v7 + skip overload stubs only | 0.77 | 0.73 | 0.52 | 0.55 |
| requests | **v8** | **0.83** | **0.82** | 0.54 | **0.57** |

## v9: other languages (reported separately)

v9 is v8 plus tree-sitter chunking for JavaScript, TypeScript, Java, Kotlin, C, C++, Go and Rust
([coderag/languages.py](../coderag/languages.py)). Python keeps its `ast` chunker and scope-aware
resolver. Calls in the other languages are linked by name within the same language (`name_match`
edges), never across languages.

**Python sets: unchanged.** v9 gives exactly v8's numbers on all three Python sets (same chunks, same
context), so the Python results above stay valid.

| Set | Version | R@ctx | Sig | Body | MRR |
|---|---|---|---|---|---|
| sample_repo | v8 = v9 | 1.00 | 1.00 | 0.88 | 0.79 |
| requests | v8 = v9 | 0.83 | 0.82 | 0.54 | 0.57 |
| click | v8 = v9 | 0.94 | 0.80 | 0.56 | 0.64 |

**JavaScript: commander.js.** 12 hand-labelled questions, 17 gold symbols, written from the source
before any retrieval was run. v1–v8 index Python only and find nothing here; v0's baseline reads
`.js` as plain text windows, so it is shown for reference. Per-question rows are in
[results/v9-commander.csv](results/v9-commander.csv).

```bash
python eval/run_eval.py eval/questions/commander.yaml --versions v9 --by-type
```

| Set | Version | n | R@ctx | Sig | Body | Hit@1 | Hit@3 | MRR | Chunks | Chars |
|---|---|---|---|---|---|---|---|---|---|---|
| commander | v0 (fixed 1000-char windows over all text files) | 12 | 0.62 | 0.58 | 0.62 | 0.50 | 0.58 | 0.54 | 5.0 | 4996 |
| commander | **v9** | 12 | **0.83** | **0.71** | 0.60 | 0.42 | 0.50 | 0.53 | 10.9 | 8377 |

| Type | n | R@ctx | Sig | MRR |
|---|---|---|---|---|
| locate | 8 | 0.88 | 0.75 | 0.61 |
| callers | 2 | 0.50 | 0.50 | 0.10 |
| callees | 1 | 1.00 | 0.50 | 1.00 |
| inheritance | 1 | 1.00 | 1.00 | 0.25 |

- Recall is in the same range as the Python sets (requests 0.83, click 0.94), with name matching
  instead of a resolver, and well above the v0 baseline (0.62). With 12 questions, one question moves R@ctx by about 0.08.
- **Both misses involve `suggestSimilar`**: "how does commander suggest a similar name" and "which
  functions call suggestSimilar". The graph is right (`unknownOption` and `unknownCommand` call it,
  and it calls `editDistance`), but vector search never ranks the function in the top 4, so
  expansion never reaches it. It is an embedding miss, not a chunking or linking one. The questions
  were not changed after the fact.
- The inheritance question (`InvalidArgumentError extends CommanderError`) is answered through a
  `name_match` inherits edge: the subclass is a vector hit and its base comes in at hop 1.

## v10: bridge edges across languages

v10 is v9 plus *bridge* edges ([coderag/bridges/](../coderag/bridges/)): an HTTP client call to the
route that serves it, a Kotlin/Java `external`/`native` method to its JNI C function, and a
ctypes/cffi call to its C function. It also adds project files (README, Procfile, ...) and repo
facts for project questions, which these code questions don't use. Graph expansion follows a bridge
as one hop.

- **Clients:** fetch/axios, jQuery, XMLHttpRequest, requests/httpx, OkHttp/Retrofit, and HTML forms
  and htmx.
- **Servers:** FastAPI/Flask (with router and blueprint prefixes), Express (with `app.use`
  prefixes), Django and DRF, Spring, and Go net/http.
- **URLs:** literal URLs match exactly. A template or f-string URL (`/api/repos/${id}/ask`) matches
  as a pattern, with each interpolation counting as one wildcard segment. A URL held in a variable
  or built as `base + path` is skipped.

Both sets are cross-language. Each question's gold pairs the code that sends the request with the
route handler that serves it, so a version has to cross the HTTP boundary to cover both. Per-question
rows: [results/v10-crosslang.csv](results/v10-crosslang.csv).

```bash
python eval/run_eval.py eval/questions/coderag.yaml eval/questions/fastapi_react.yaml --versions v9 v10
```

| Set | Version | n | R@ctx | Sig | Body | Hit@1 | Hit@3 | MRR | Chunks | Chars |
|---|---|---|---|---|---|---|---|---|---|---|
| coderag | v9 | 15 | 0.30 | 0.27 | 0.22 | 0.20 | 0.47 | 0.35 | 10.9 | 8043 |
| coderag | **v10** | 15 | **0.53** | 0.27 | 0.23 | 0.20 | 0.47 | 0.37 | 11.4 | 8515 |
| fastapi_react | v9 | 10 | 0.82 | 0.82 | 0.83 | 1.00 | 1.00 | 1.00 | 4.0 | 2036 |
| fastapi_react | **v10** | 10 | **0.97** | **0.97** | **0.96** | 1.00 | 1.00 | 1.00 | 5.7 | 3306 |

**coderag** (this repository, frontend JavaScript + web API Python, 15 questions, working tree, not
pinned). v10 finds 19 HTTP bridges here (6 exact, 13 pattern), up from 10, because template URLs
now count. The first 5 questions score as before (v9 0.50, v10 0.90). The 10 added questions are
harder:

- **The client side is never retrieved (4 questions: job progress, graph load, "Check if live",
  adding to the list).** Vector search doesn't rank the JavaScript function that makes the call, so
  there's nothing to cross from. Bridges only extend what search finds.
- **The client is retrieved but the bridge isn't followed (3 questions: `ask`, `loadFile`,
  `reindexRepo`).** These functions call many helpers (`el`, `$`, ...). Their name-matched callees
  come before bridges in expansion order and fill the budget of 8 chunks before the bridge is reached.
  Ordering bridges ahead of name-matched callees is the obvious next step. It hasn't been made,
  because the change would come from looking at this test set.
- **Sig stays low (0.27).** coderag's routes are nested inside `create_app` and `Auth.router`, so a
  bridge lands on that whole function. The hop's character budget shows its top, not the route's
  `def` line.

**fastapi_react** ([testdrivenio/fastapi-react](https://github.com/testdrivenio/fastapi-react)
pinned at `e0116876`: a React + TypeScript frontend and a FastAPI backend, 10 questions). All four
routes are bridged: `POST /todo` and `GET /todo` exact, `PUT /todo/{id}` and `DELETE /todo/{id}` as
patterns from template URLs. v10 covers both sides in 9 of 10 questions. The remaining 0.67 is a
three-symbol question whose third gold (the `Todos` component that re-fetches the list) isn't
reached. The repo has only four routes, so several questions ask different things about the same
pair, and the scores are close to the ceiling.

Small sets: one coderag question is 0.03 of R@ctx, one fastapi_react question 0.05.

## Answer quality

[run_answers.py](run_answers.py) runs the LLM on v7's retrieved context and scores what the answer
*says*. No LLM judge is used; answers are matched against the gold symbols.

```bash
python eval/run_answers.py --runs groq:legacy groq:cited          # callers + flow questions
python eval/run_answers.py --runs local:legacy --types callers
python eval/run_answers.py --rescore --csv eval/results/answers-groq.csv   # re-score, no LLM calls
```

| Metric | Meaning |
|---|---|
| G-Rec | gold symbols the answer names |
| Prec, Extra | callers questions: share of named functions that are gold; non-gold functions named per answer |
| Cited | answers with at least one valid `[S<n>]` citation |
| CiteGold | answers citing a source that contains a gold symbol |
| Abstain | answers saying the sources don't contain the answer |

`legacy` is the v1–v7 prompt ("Code context: …"). `cited` numbers the sources (`[S1] file:L10-L42
Class.method`) and requires inline citations, exact names, call claims only when the code shows
them, and saying so when the sources don't contain the answer.

**gpt-oss-120b on Groq**, callers + flow questions (24). Rows in
[results/answers-groq.csv](results/answers-groq.csv):

| Prompt | G-Rec | Prec (callers) | Cited | CiteGold | Abstain | s/answer |
|---|---|---|---|---|---|---|
| legacy | 0.92 | 0.83 | 0.00 | 0.00 | 0.12 | 1.3 |
| cited | 0.82 | 0.82 | **0.92** | **0.67** | 0.17 | 1.2 |

**Qwen2.5-Coder-1.5B on CPU vs gpt-oss-120b**, callers questions only (13). Rows in
[results/answers-local.csv](results/answers-local.csv):

| Model, prompt | G-Rec | Prec | Extra/answer | Cited | s/answer |
|---|---|---|---|---|---|
| Qwen 1.5B, legacy | 0.54 | 0.49 | 0.92 | 0.00 | 61 |
| Qwen 1.5B, cited | 0.38 | 0.42 | 0.15 | 0.62 | 52 |
| gpt-oss-120b, legacy | 0.96 | 0.83 | 0.46 | 0.00 | 0.9 |
| gpt-oss-120b, cited | 0.88 | 0.82 | 0.46 | **1.00** | 0.8 |

10. **Model size matters more than the prompt.** Qwen 1.5B makes up callers even when the context
    is right: "`SQLiteStore.save` calls `normalize_name`", "`resolve_redirects` is called by the
    `send` method of `SessionRedirectMixin`", six unrelated functions for `resolve_command`. I read
    every gpt-oss "extra" name and none was a false claim. They were legitimate context, such as
    `main` calling `report`, or `dispatch_hook` for a question about hooks. Treat Extra as an upper
    bound: it counts mentions, not claims.
11. **Small models can't follow the citation format.** With the cited prompt, Qwen 1.5B makes up
    less but often answers with nothing but `[O1] [S1]`. gpt-oss cites in 92% of answers. Reading
    the answers also exposed a scorer bug: gpt-oss writes citations as `【S7】` or `[​S3]`, and
    the parser missed them. `parse_citations` now normalises both.
12. **The legacy prompt's extra recall partly comes from memory, not the code.** Its answers say
    "`Session.send` (*not shown in the excerpt but*…)". gpt-oss knows `requests` and `click` from
    training, so it fills retrieval gaps itself. That's right for famous libraries and a guess on a
    private repo. With the cited prompt it says the sources don't show it instead. Answer scores on
    requests and click are therefore inflated for any prompt that allows guessing. `sample_repo` is
    the only test of grounding the model can't answer from memory.
13. **The remaining failures are retrieval gaps on flow questions.** For example, `Session.send`
    was not retrieved for "how does a Session send a request through an adapter". The cited prompt
    makes these gaps visible instead of hiding them.

Defaults that follow from this: remote models use the cited prompt, and the local model keeps
the legacy prompt.
