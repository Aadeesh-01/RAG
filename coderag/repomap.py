"""
A map of the whole repository, for questions about the whole repository.

A handful of retrieved chunks can't answer "what are the main modules?": the
model generalises from a sample and names three of twelve areas as "the main
modules". For broad questions the context starts with this map instead: every
top-level area with its size and sub-areas, and the likely entry points. It is
built from the indexed chunks, so existing indexes get it without re-indexing.
"""

import os
import re
from collections import defaultdict

BROAD = re.compile(
    r"\b(what does (this|the) (repo|repository|project|codebase|code|library|package)( \w+)? do"
    r"|main (modules|parts|components|areas|packages|pieces)"
    r"|overview|architecture|high[- ]level"
    r"|structure of (this|the) (repo|repository|project|codebase|code)"
    r"|how is (this|the) (repo|repository|project|codebase|code) (organi[sz]ed|structured|laid out)"
    r"|what('s| is) in (this|the) (repo|repository|project|codebase))\b",
    re.I)

ENTRY_NAMES = {"main", "cli", "run", "app", "serve"}


def is_broad_question(question):
    return bool(BROAD.search(question))


def _rel(doc):
    return doc["qname"].split("::")[0]


def repo_map(documents, max_areas=30, max_children=10):
    """Plain-text map: top-level areas (files, functions, sub-areas) and entry points."""
    docs = [d for d in documents if "qname" in d]
    files = defaultdict(set)                     # area -> files
    functions = defaultdict(int)
    children = defaultdict(set)                  # area -> sub-folders or file stems
    for d in docs:
        rel = _rel(d)
        parts = rel.split("/")
        area = f"{parts[0]}/" if len(parts) > 1 else "(top level)"
        files[area].add(rel)
        if d["type"] in ("function", "method"):
            functions[area] += 1
        if len(parts) > 2:
            children[area].add(f"{parts[1]}/")
        else:
            children[area].add(os.path.splitext(parts[-1])[0])
    areas = sorted(files, key=lambda a: (-functions[a], a))[:max_areas]

    lines = ["REPO MAP: the whole repository (for overview questions; not a numbered source)",
             f"{len({_rel(d) for d in docs})} files, "
             f"{sum(d['type'] in ('function', 'method') for d in docs)} functions and methods, "
             f"{sum(d['type'] == 'class' for d in docs)} classes.",
             "Top-level areas:"]
    for area in areas:
        names = sorted(n for n in children[area] if n != "__init__")
        shown = ", ".join(names[:max_children]) + (f", and {len(names) - max_children} more"
                                                   if len(names) > max_children else "")
        lines.append(f"  {area}  {_count(len(files[area]), 'file')}, {_count(functions[area], 'function')}"
                     + (f": {shown}" if shown else ""))
    if len(files) > len(areas):
        lines.append(f"  ... and {len(files) - len(areas)} smaller areas")

    entries = sorted(f"{d['name']} ({_rel(d)})" for d in docs
                     if d["type"] == "function" and d["name"] in ENTRY_NAMES and not d.get("called_by"))
    if len(entries) > 8:                         # a collection of scripts, each runnable alone
        lines.append(f"Entry points: {len(entries)} scripts each have their own main().")
    elif entries:
        lines.append("Entry points: " + "; ".join(entries))
    return "\n".join(lines)


def _count(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"
