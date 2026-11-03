"""
Retrieval evaluation: no LLM involved.

For every question we build the exact context a version would send to the LLM
and score it against hand-labelled gold symbols.

    python eval/run_eval.py                                  # all question sets, v0-v5
    python eval/run_eval.py eval/questions/sample_repo.yaml --versions v1 v5
    python eval/run_eval.py --by-type --csv eval/results.csv

Metrics (averaged over questions)
  R@ctx  gold symbols whose code lies in a retrieved chunk (ignores truncation)
  Sig    gold symbols whose def/class line is actually visible in the context
  Body   fraction of gold source lines visible in the context
  Hit@1 / Hit@3 / MRR   rank of the first retrieved chunk covering any gold
  Chunks, Chars         context size;  ms  retrieval latency
"""

import argparse
import ast
import csv
import glob
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from coderag import RAGCodebaseAgent, VERSIONS, get_config  # noqa: E402
from coderag.context import build_context  # noqa: E402

REPOS_DIR = os.path.join(ROOT, "eval", ".repos")
METRICS = ["R@ctx", "Sig", "Body", "Hit@1", "Hit@3", "MRR", "Chunks", "Chars", "ms"]


# =============================================================================
# GOLD SYMBOLS
# =============================================================================
@dataclass
class Gold:
    ref: str            # "pkg/mod.py::Class.method"
    path: str           # normalised absolute path
    start: int          # 1-based, decorators included
    end: int
    signature: str      # stripped def/class line
    body: frozenset     # stripped non-blank source lines


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def resolve_gold(root, ref):
    rel, dotted = ref.split("::")
    path = os.path.join(root, rel)
    with open(path, encoding="utf-8", errors="ignore") as f:
        source = f.read()
    nodes = ast.parse(source).body
    node = None
    for part in dotted.split("."):
        matches = [n for n in nodes
                   if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == part]
        if not matches:
            raise KeyError(f"gold symbol not found: {ref}")
        node = matches[-1]      # last definition wins (skips @overload stubs)
        nodes = node.body
    lines = source.splitlines()
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return Gold(
        ref=ref,
        path=_norm(path),
        start=start,
        end=node.end_lineno,
        signature=lines[node.lineno - 1].strip(),
        body=frozenset(l.strip() for l in lines[start - 1:node.end_lineno] if l.strip()),
    )


def chunk_spans(meta):
    if "spans" in meta:
        return [(_norm(p), lo, hi) for p, lo, hi in meta["spans"]]
    return [(_norm(meta["file"]), meta["start_line"], meta["end_line"])]


def covers(meta, gold):
    """A chunk covers a gold symbol if it holds most of it, or is mostly made of it."""
    spans = chunk_spans(meta)
    chunk_len = sum(hi - lo + 1 for _, lo, hi in spans)
    overlap = sum(max(0, min(hi, gold.end) - max(lo, gold.start) + 1)
                  for path, lo, hi in spans if path == gold.path)
    gold_len = gold.end - gold.start + 1
    return overlap >= 0.5 * gold_len or overlap >= 0.5 * chunk_len


# =============================================================================
# SCORING
# =============================================================================
def score_question(agent, question, golds):
    t0 = time.perf_counter()
    retrieved = agent.retrieve(question)
    ms = (time.perf_counter() - t0) * 1000
    context = build_context(question, retrieved, agent.config)
    visible = {l.strip() for l in context.splitlines()}

    covered = [any(covers(meta, g) for _, meta, _ in retrieved) for g in golds]
    first = next((rank for rank, (_, meta, _) in enumerate(retrieved, 1)
                  if any(covers(meta, g) for g in golds)), None)

    return {
        "R@ctx":  sum(covered) / len(golds),
        "Sig":    sum(g.signature in visible for g in golds) / len(golds),
        "Body":   sum(len(g.body & visible) / len(g.body) for g in golds) / len(golds),
        "Hit@1":  float(first == 1),
        "Hit@3":  float(first is not None and first <= 3),
        "MRR":    1 / first if first else 0.0,
        "Chunks": len(retrieved),
        "Chars":  len(context),
        "ms":     ms,
    }


def parse_version(spec):
    """'v5' or an ablation like 'v5+chunk_methods=True+chars_per_chunk=1500'."""
    import ast as _ast

    base, *assignments = spec.split("+")
    overrides = {}
    for assignment in assignments:
        key, value = assignment.split("=", 1)
        try:
            overrides[key] = _ast.literal_eval(value)
        except (ValueError, SyntaxError):
            overrides[key] = value
    return get_config(base, **overrides)


def load_question_set(path):
    with open(path, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    if "url" in spec:
        name = spec["url"].rstrip("/").split("/")[-1]
        repo = os.path.join(REPOS_DIR, name)
        if not os.path.isdir(repo):
            raise FileNotFoundError(f"{repo} missing - run: python eval/fetch_repos.py")
    else:
        repo = os.path.join(ROOT, spec["repo"])
    root = os.path.join(repo, spec.get("root", ""))
    return spec, root


def evaluate(paths, versions, verbose=False):
    from coderag.index import VectorIndex

    rows = []
    shared = {}   # one embedding model for all agents
    for path in paths:
        spec, root = load_question_set(path)
        name = os.path.splitext(os.path.basename(path))[0]
        questions = [(q["q"], q.get("type", "other"),
                      [resolve_gold(root, ref) for ref in q["gold"]])
                     for q in spec["questions"]]

        for version in versions:
            config = parse_version(version)
            if config.embed_model not in shared:
                shared[config.embed_model] = VectorIndex(config.embed_model).model
            index = VectorIndex(config.embed_model, model=shared[config.embed_model])
            agent = RAGCodebaseAgent(root, config, index=index, load_llm=False,
                                     verbose=False)
            for question, qtype, golds in questions:
                row = {"set": name, "version": version, "type": qtype, "q": question}
                row.update(score_question(agent, question, golds))
                rows.append(row)
                if verbose:
                    print(f"{name} {version} {row['R@ctx']:.2f} {row['Sig']:.2f}  {question}")
            print(f"[eval] {name} {version}: {len(questions)} questions, "
                  f"{len(agent.chunks)} chunks", file=sys.stderr)
    return rows


# =============================================================================
# REPORTING
# =============================================================================
def summarise(rows, keys):
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[k] for k in keys)].append(row)
    out = []
    for key, group in groups.items():
        summary = dict(zip(keys, key))
        summary["n"] = len(group)
        for m in METRICS:
            summary[m] = sum(r[m] for r in group) / len(group)
        out.append(summary)
    return out


def print_table(summary, keys):
    header = keys + ["n"] + METRICS
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for s in summary:
        cells = [str(s[k]) for k in keys] + [str(s["n"])]
        for m in METRICS:
            v = s[m]
            cells.append(f"{v:.0f}" if m in ("Chars", "ms") else
                         f"{v:.1f}" if m == "Chunks" else f"{v:.2f}")
        print("| " + " | ".join(cells) + " |")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question_sets", nargs="*",
                        default=sorted(glob.glob(os.path.join(ROOT, "eval", "questions", "*.yaml"))))
    parser.add_argument("--versions", nargs="+", default=list(VERSIONS))
    parser.add_argument("--by-type", action="store_true", help="also break down by question type")
    parser.add_argument("--csv", help="write per-question rows to this file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    rows = evaluate(args.question_sets, args.versions, args.verbose)

    print()
    print_table(summarise(rows, ["set", "version"]), ["set", "version"])
    if args.by_type:
        print()
        print_table(summarise(rows, ["set", "type", "version"]), ["set", "type", "version"])

    if args.csv:
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)), exist_ok=True)
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nwrote {len(rows)} rows -> {args.csv}")


if __name__ == "__main__":
    main()
