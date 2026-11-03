# Retrieval evaluation

Measures what each pipeline version actually puts in front of the LLM. No LLM is called: for each
question, the harness builds the exact context a version would send and scores it against
hand-labelled gold symbols.

```bash
python eval/fetch_repos.py                 # clone the pinned real repos into eval/.repos/
python eval/run_eval.py                    # every question set, v0-v6
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

Each question has a type (`locate`, `callers`, `callees`, `inheritance`, `flow`) and gold symbols
written as `path::Class.method`. Call relationships in the gold labels were checked against the
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
