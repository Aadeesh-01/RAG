"""
Answer-quality evaluation: run the LLM on the eval questions and score what
the answer *says* against the gold symbols. No LLM judge is involved.

    python eval/run_answers.py --runs groq:legacy groq:cited
    python eval/run_answers.py --runs local:cited --types callers
    python eval/run_answers.py --runs groq:cited --model openai/gpt-oss-20b --csv out.csv

A run is <llm>:<prompt> with llm = local | groq | openrouter | gemini and
prompt = legacy | cited. Retrieval uses --version (default v7) for every run.

Metrics (averaged over questions)
  G-Rec     gold symbols the answer names
  Prec      callers questions: named gold / (named gold + named other functions)
  Extra     callers questions: non-gold functions/methods named per answer
  Ground    named repo symbols that appear in the retrieved context
  Cited     answers with at least one valid [S<n>] citation
  CiteGold  answers citing a source that contains a gold symbol
  Abstain   answers saying the sources don't contain the answer
  s         seconds per answer
"""

import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_eval import (ROOT, covers, load_question_set, parse_version,  # noqa: E402
                      resolve_gold)

from coderag import RAGCodebaseAgent  # noqa: E402
from coderag.context import display_name, parse_citations  # noqa: E402
from coderag.llm import DEFAULT_REMOTE_MODELS, load_llm  # noqa: E402

METRICS = ["G-Rec", "Prec", "Extra", "Ground", "Cited", "CiteGold", "Abstain", "s"]
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
ABSTAIN = re.compile(r"\b(?:do(?:es)? not (?:contain|include|show|provide|mention)|"
                     r"no (?:information|source)|cannot (?:determine|find|tell)|"
                     r"not (?:enough|shown|present) in)", re.IGNORECASE)


# =============================================================================
# WHAT DOES AN ANSWER NAME?
# =============================================================================
def code_tokens(text):
    """
    Identifiers written as code: in backticks, called like name(), dotted,
    snake_case or capitalised. Plain lowercase words ("request", "get") are
    ignored so prose doesn't count as naming a function.
    """
    tokens = set()
    for span in re.findall(r"`([^`]+)`", text):
        tokens |= set(IDENT.findall(span))
    for match in re.finditer(r"([A-Za-z_][\w.]*)\s*\(", text):
        tokens.add(match.group(1))
    for t in IDENT.findall(text):
        if "_" in t or "." in t or t[:1].isupper():
            tokens.add(t)
    return tokens


class Answer:
    """An answer's text pre-split for symbol matching."""

    def __init__(self, text):
        self.text = text
        self.tokens = code_tokens(text)
        self.parts = {p for t in self.tokens for p in t.split(".")}
        self.words = {p for t in IDENT.findall(text) for p in t.split(".")}


def unique_names(documents):
    """Bare names defined by exactly one function or method in the repo."""
    counts = defaultdict(int)
    for d in documents:
        if d["type"] in ("function", "method"):
            counts[d["name"]] += 1
    return {n for n, c in counts.items() if c == 1}


def names(display, answer, unique):
    """
    Does `answer` name the symbol `display` ('func', 'Class' or 'Class.method')?
    A method counts when written as Class.method, as a bare name that is unique
    in the repo, or as "the `name` method" with the class also mentioned.
    """
    if display in answer.tokens or any(t.endswith("." + display) for t in answer.tokens):
        return True
    if "." not in display:
        return display in answer.parts
    parent, name = display.split(".", 1)
    if name in answer.parts and (name in unique or parent in answer.words):
        return True
    return parent in answer.words and bool(
        re.search(rf"\b{re.escape(name)}\b`?(?:\(\))?\s+method", answer.text))


def named_symbols(text, documents, unique):
    answer = Answer(text)
    return {d["qname"]: d for d in documents
            if d["type"] in ("function", "method", "class")
            and names(display_name(d), answer, unique)}


# =============================================================================
# SCORING
# =============================================================================
def score(result, question, qtype, golds, documents, root):
    answer = result.answer or ""
    unique = unique_names(documents)
    gold_qnames = {g.ref for g in golds}
    named = named_symbols(answer, documents, unique)
    gold_named = [g for g in golds if names(g.ref.split("::")[1], Answer(answer), unique)]
    subjects = set(named_symbols(question, documents, unique))

    row = {
        "G-Rec": len(gold_named) / len(golds),
        "Prec": "", "Extra": "",
        "Ground": "",
        "Cited": float(bool(result.cited)),
        "CiteGold": 0.0,
        "Abstain": float(bool(ABSTAIN.search(answer))),
        "s": result.timings.get("llm_ms", 0) / 1000,
    }
    row["extra_names"] = ""
    if qtype == "callers":
        extra = [q for q, d in named.items() if d["type"] != "class"
                 and q not in gold_qnames and q not in subjects]
        row["Extra"] = float(len(extra))
        row["extra_names"] = ", ".join(display_name(named[q]) for q in extra)
        found = len(gold_named)
        row["Prec"] = found / (found + len(extra)) if found + len(extra) else 0.0
    if named:
        context = result.context
        row["Ground"] = sum(display_name(d) in context or d["name"] in context
                            for d in named.values()) / len(named)

    by_id = {s.id: s for s in result.sources}
    for n in result.cited:
        s = by_id[n]
        meta = {"file": os.path.join(root, s.file), "start_line": s.start_line,
                "end_line": s.end_line}
        if any(covers(meta, g) for g in golds):
            row["CiteGold"] = 1.0
            break
    return row


def run_config(base, run, model):
    llm, _, prompt = run.partition(":")
    overrides = {"prompt_style": prompt or "legacy"}
    if llm in DEFAULT_REMOTE_MODELS:
        model = model or DEFAULT_REMOTE_MODELS[llm]
        overrides.update(llm_backend="remote", llm_provider=llm, llm_model=model,
                         max_new_tokens=1024,
                         reasoning_effort="low" if "gpt-oss" in model else None)
    elif llm != "local":
        raise ValueError(f"unknown llm in run {run!r}")
    return base.with_overrides(**overrides)


def ask_with_retry(agent, question, llm, prompt_style, max_wait=90):
    for attempt in range(8):
        result = agent.ask(question, llm=llm, prompt_style=prompt_style)
        code = (result.error or {}).get("code")
        if code not in ("rate_limited", "unavailable"):
            return result
        wait = (result.error.get("retry_after") or 10 * (attempt + 1)) + 1
        if wait > max_wait:
            raise RuntimeError(f"rate limit window too long ({wait:.0f}s); daily quota?")
        print(f"   [{code}] waiting {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question_sets", nargs="*", default=[
        os.path.join(ROOT, "eval", "questions", f"{n}.yaml")
        for n in ("sample_repo", "requests", "click")])
    parser.add_argument("--runs", nargs="+", default=["groq:legacy", "groq:cited"])
    parser.add_argument("--version", default="v7", help="retrieval preset for every run")
    parser.add_argument("--model", help="remote model id (default: per provider)")
    parser.add_argument("--types", nargs="+", default=["callers", "flow"],
                        help="question types to run, or 'all'")
    parser.add_argument("--csv", default=os.path.join(ROOT, "eval", "results", "answers.csv"))
    parser.add_argument("--rescore", action="store_true",
                        help="re-score the answers already in --csv (retrieval only, no LLM calls)")
    args = parser.parse_args(argv)

    base = parse_version(args.version)
    saved = {}
    if args.rescore:
        with open(args.csv, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                saved[(r["set"], r["run"], r["q"])] = r
        args.runs = sorted({run for _, run, _ in saved})

    rows = []
    for path in args.question_sets:
        spec, root = load_question_set(path)
        set_name = os.path.splitext(os.path.basename(path))[0]
        agent = RAGCodebaseAgent(root, base, load_llm=False, verbose=False)
        questions = [q for q in spec["questions"]
                     if "all" in args.types or q.get("type") in args.types]

        for run in args.runs:
            config = run_config(base, run, args.model)
            llm = None if args.rescore else load_llm(config)
            for q in questions:
                key = (set_name, run, q["q"])
                if args.rescore and key not in saved:
                    continue
                golds = [resolve_gold(root, ref) for ref in q["gold"]]
                if args.rescore:       # same retrieval, saved answer
                    result = agent.ask(q["q"], llm=None, prompt_style=config.prompt_style)
                    result.answer = saved[key]["answer"]
                    result.cited = parse_citations(result.answer, len(result.sources))[0]
                    result.timings["llm_ms"] = float(saved[key]["s"] or 0) * 1000
                else:
                    result = ask_with_retry(agent, q["q"], llm, config.prompt_style)
                row = {"set": set_name, "run": run, "type": q["type"], "q": q["q"]}
                row.update(score(result, q["q"], q["type"], golds, agent.documents, root))
                row["answer"] = result.answer or f"<{(result.error or {}).get('code')}>"
                rows.append(row)
                print(f"[{set_name} {run}] G-Rec {row['G-Rec']:.2f}  {q['q']}", file=sys.stderr)

    summary = defaultdict(list)
    for row in rows:
        summary[(row["run"], row["type"])].append(row)
        summary[(row["run"], "ALL")].append(row)
    print("| run | type | n | " + " | ".join(METRICS) + " |")
    print("|" + "---|" * (len(METRICS) + 3))
    for (run, qtype), group in sorted(summary.items()):
        cells = []
        for m in METRICS:
            values = [r[m] for r in group if r[m] != ""]
            cells.append(f"{sum(values) / len(values):.2f}" if values else "-")
        print(f"| {run} | {qtype} | {len(group)} | " + " | ".join(cells) + " |")

    os.makedirs(os.path.dirname(os.path.abspath(args.csv)), exist_ok=True)
    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nwrote {len(rows)} rows -> {args.csv}")


if __name__ == "__main__":
    main()
