"""
Turning retrieved chunks into the LLM prompt context.

Retrieved items are (chunk_text, meta, hop) triples:
  hop = 0  ->  vector-search hit
  hop >= 1 ->  reached by graph expansion
"""

import os
import re

# "who calls X", "which functions use X", "callers of X", "where is X called"
_CALLER_INTENT = re.compile(
    r"\b(?:who|what|which(?:\s+\w+){0,2})\s+(?:calls?|uses?|invokes?)\b"
    r"|\bcallers?\s+of\b|\bcalled\s+(?:by|from)\b|\bwhere\s+is\s+\S+\s+(?:used|called)\b",
    re.IGNORECASE,
)


def asks_for_callers(question):
    """True when the question is about who calls / uses something."""
    return bool(_CALLER_INTENT.search(question))


def display_name(meta):
    """'Class.method' for method chunks, the plain name otherwise."""
    return f"{meta['parent']}.{meta['name']}" if meta.get("parent") else meta["name"]


def keyword_filter(query, results):
    """Keep results containing any query word; fall back to all if none match."""
    keywords = query.lower().split()
    filtered = [r for r in results if any(k in r[0].lower() for k in keywords)]
    return filtered if filtered else results


def clean_chunk(chunk, query):
    """
    Keep only signature, return and query-keyword lines of a chunk. Falls back
    to the first 10 raw lines when fewer than 3 lines survive.
    """
    lines = chunk.split("\n")
    keywords = query.lower().split()
    cleaned = []
    for line in lines:
        l = line.strip()
        if l.startswith(("def ", "class ", "return")) or any(k in l.lower() for k in keywords):
            cleaned.append(l)
    return "\n".join(cleaned) if len(cleaned) >= 3 else "\n".join(lines[:10])


def char_budget(config, hop):
    """
    Characters of body allowed for a chunk at `hop`.
    With hop_budget_decay: 400 -> 200 -> 100 -> ... floored at min_chars_per_chunk.
    """
    if config.chars_per_chunk is None:
        return None
    if not config.hop_budget_decay:
        return config.chars_per_chunk
    return max(config.chars_per_chunk >> hop, config.min_chars_per_chunk)


def build_rich_header(meta, hop=0, hop_label=False):
    """
    Compact, information-dense header the LLM can parse, e.g.

        # class: DataProcessor  [file: processing.py]  [retrieval: hop-1]
        # extends  : BaseProcessor
        # methods  : transform
        # calls    : int, items, normalize_name, str, strip
        # called_by: run_pipeline
        # doc: Cleans people records ...
    """
    first = f"# {meta['type']}: {display_name(meta)}  [file: {os.path.basename(meta['file'])}]"
    if hop_label:
        first += f"  [retrieval: {'direct' if hop == 0 else f'hop-{hop}'}]"
    lines = [first]

    if meta.get("base_classes"):
        lines.append(f"# extends  : {', '.join(meta['base_classes'])}")
    if meta.get("methods"):
        lines.append(f"# methods  : {', '.join(meta['methods'][:8])}")
    if meta.get("calls"):
        lines.append(f"# calls    : {', '.join(meta['calls'][:6])}")
    if meta.get("called_by"):
        lines.append(f"# called_by: {', '.join(meta['called_by'][:5])}")
    if meta.get("decorators"):
        lines.append(f"# decorators: {', '.join(meta['decorators'])}")
    if meta.get("docstring"):
        lines.append(f"# doc: {meta['docstring'][:120]}")
    if meta.get("also_in"):
        lines.append(f"# also_in: {', '.join(meta['also_in'])}")

    return "\n".join(lines)


def source_location(meta):
    """(path, first_line, last_line) of a chunk; path is repo-relative for AST chunks."""
    if "spans" in meta:                       # fixed-size window: first file it covers
        if not meta["spans"]:
            return "", 0, 0
        path, lo, hi = meta["spans"][0]
        return os.path.basename(path), lo, hi
    path = meta["qname"].split("::")[0] if "qname" in meta else os.path.basename(meta["file"])
    return path, meta.get("start_line", 0), meta.get("end_line", 0)


def source_label(n, meta, hop):
    """'[S1] pkg/mod.py:L10-L42  method Class.name  (direct)'"""
    path, lo, hi = source_location(meta)
    where = f"{path}:L{lo}-L{hi}" if lo else path
    how = "direct" if hop == 0 else f"via call graph, hop {hop}"
    return f"[S{n}] {where}  {meta['type']} {display_name(meta)}  ({how})"


_CITATION = re.compile(r"\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]")
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


def normalize_citations(answer):
    """
    Rewrite citation variants models produce into plain [S3]: full-width
    brackets 【S3】 (gpt-oss's native style) and zero-width characters inside
    brackets ([​S3]).
    """
    return (answer or "").translate(_INVISIBLE).replace("【", "[").replace("】", "]")


def parse_citations(answer, n_sources):
    """(valid, invalid): sorted source numbers cited as [S3] / [S1, S4] in `answer`."""
    cited = {int(ref.strip()[1:]) for group in _CITATION.findall(normalize_citations(answer))
             for ref in re.split(r"[,;]", group)}
    return (sorted(n for n in cited if 1 <= n <= n_sources),
            sorted(n for n in cited if not 1 <= n <= n_sources))


def format_chunk(chunk, meta, hop, question, config, n=None):
    body = clean_chunk(chunk, question) if config.clean_chunks else chunk
    budget = char_budget(config, hop)
    if budget is not None:
        body = body[:budget]

    if config.header == "cite" or config.prompt_style == "cited":
        # numbered source the LLM can cite; relationship lines from the rich header
        extra = build_rich_header(meta, hop).split("\n")[1:]
        return "\n".join([source_label(n, meta, hop)] + extra + ["```python", body, "```"])
    if config.header == "none":
        return body
    if config.header == "name":
        return f"# {meta['type']}: {display_name(meta)}\n" + body
    if config.header == "file":
        return f"# {meta['type']}: {display_name(meta)} ({os.path.basename(meta['file'])})\n" + body
    if config.header == "rich":
        return build_rich_header(meta, hop, config.hop_label) + "\n" + body
    raise ValueError(f"Unknown header style: {config.header!r}")


def build_context(question, retrieved, config):
    return "\n\n---\n\n".join(
        format_chunk(chunk, meta, hop, question, config, n)
        for n, (chunk, meta, hop) in enumerate(retrieved, 1)
    )
