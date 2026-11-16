"""
Code-structure graph over AST chunks.

Edges come from chunk metadata:
  • calls        caller -> callee   (resolved by bare name, same file first)
  • base_classes subclass -> base
  • called_by    reverse of calls, filled by build_reverse_index()

Names only match within one language: a Go call never links to a Python
function of the same name. Every edge has a confidence (see confidence()):
"resolved" for Python's scope-aware resolution (v7+, resolve.py), "name_match"
for everything linked by name (v1-v6, and every other language in v9).
"""

import os
import re
from collections import defaultdict, deque

from .languages import language_of

NAME_MATCH = "name_match"          # how an edge found by name is recorded in a CodeGraph
MAX_NAME_MATCHES = 5               # repo-wide: link a name to at most this many definitions


def confidence(how):
    """'resolved' (Python, scope-aware) or 'name_match' (linked by name only)."""
    return NAME_MATCH if how in (NAME_MATCH, "name", None) else "resolved"


def _resolve_targets(chunks, i, callee_name, name_to_indices, method_to_classes, languages=None):
    """
    Chunk indices that a call to `callee_name` from chunk `i` refers to.

      1. Direct name match  – a chunk with that name (functions, class
         instantiations like Bat(...)).
      2. Method-owner match – a class chunk that defines a method with that
         name (handles obj.method() / self.method()).
    Same-file targets are preferred over cross-file ones; only chunks in the
    caller's language count.
    """
    caller_file = chunks[i]["file"]
    languages = languages or [language_of(c) for c in chunks]
    for pool in (name_to_indices.get(callee_name, []),
                 method_to_classes.get(callee_name, [])):
        pool = [j for j in pool if languages[j] == languages[i]]
        same_file = [j for j in pool if chunks[j]["file"] == caller_file and j != i]
        targets = same_file if same_file else [j for j in pool if j != i]
        if targets:
            return targets
    return []


def _build_lookup(chunks):
    name_to_indices = defaultdict(list)     # chunk name  -> [indices]
    method_to_classes = defaultdict(list)   # method name -> [class chunk indices]
    for i, chunk in enumerate(chunks):
        name_to_indices[chunk["name"]].append(i)
        if chunk["type"] == "class":
            for method in chunk.get("methods", []):
                method_to_classes[method].append(i)
    return name_to_indices, method_to_classes


def iter_call_edges(chunks):
    """Yield (caller_index, callee_index) pairs, each pair at most once per caller."""
    name_to_indices, method_to_classes = _build_lookup(chunks)
    languages = [language_of(c) for c in chunks]
    for i, chunk in enumerate(chunks):
        resolved = set()
        for callee_name in chunk["calls"]:
            for j in _resolve_targets(chunks, i, callee_name,
                                      name_to_indices, method_to_classes, languages):
                if j not in resolved:
                    resolved.add(j)
                    yield i, j


def build_reverse_index(chunks, verbose=True):
    """Populate `called_by` (sorted caller names) on every chunk, in place."""
    for chunk in chunks:
        chunk["called_by"] = []
    for i, j in iter_call_edges(chunks):
        chunks[j]["called_by"].append(chunks[i]["name"])
    for chunk in chunks:
        chunk["called_by"] = sorted(set(chunk["called_by"]))
    if verbose:
        print("[RAG] Reverse index built (called_by populated).")


def build_name_index(chunks):
    index = defaultdict(list)
    for i, chunk in enumerate(chunks):
        index[chunk["name"]].append(i)
    return index


def _name_neighbors(chunks, name_index, idx, include_callers):
    """Neighbours by bare name: every chunk in its language named like a base, callee, parent or caller."""
    meta = chunks[idx]
    neighbor_names = list(meta["base_classes"]) + list(meta["calls"])
    if meta.get("parent"):
        neighbor_names.append(meta["parent"])      # method -> its class outline
    if include_callers:
        neighbor_names += list(meta["called_by"])
    language = language_of(meta)
    return [j for name in neighbor_names for j in name_index.get(name, [])
            if language_of(chunks[j]) == language]


def expand_bfs(chunks, name_index, seeds, max_hops=3, max_expansion=8,
               include_callers=False, code_graph=None, with_via=False, callers_first=False):
    """
    BFS from `seeds` (hop 0) over base_classes + calls edges, and called_by
    edges when `include_callers` is set. Neighbours come from `code_graph`
    (scope-aware resolution, resolve.CodeGraph) when given, otherwise from
    `name_index` (bare chunk names).

    max_hops=1, max_expansion=2 reproduces v4's 1-hop expansion.

    Returns [(chunk_index, hop)] for the newly reached chunks, closest first,
    or [(chunk_index, hop, (from_index, edge))] with `with_via`.
    """
    if max_hops <= 0 or max_expansion <= 0:
        return []

    frontier = deque((idx, 0) for idx in seeds)
    seen = set(seeds)
    expanded = []

    while frontier and len(expanded) < max_expansion:
        idx, hop = frontier.popleft()
        if hop >= max_hops:
            continue

        if code_graph is not None:
            neighbors = code_graph.typed_neighbors(idx, include_callers, callers_first)
        else:
            neighbors = [(j, "name match")
                         for j in _name_neighbors(chunks, name_index, idx, include_callers)]

        for neighbor_idx, edge in neighbors:
            if neighbor_idx in seen:
                continue
            seen.add(neighbor_idx)
            expanded.append((neighbor_idx, hop + 1, (idx, edge)) if with_via
                            else (neighbor_idx, hop + 1))
            if len(expanded) >= max_expansion:
                return expanded
            frontier.append((neighbor_idx, hop + 1))

    return expanded


def build_graph(chunks):
    """
    Nodes/edges for visualisation.
      nodes : one per chunk
      edges : {'source', 'target', 'kind'} with kind 'calls' | 'inherits'
    """
    nodes = [{
        "id":      i,
        "name":    c["name"],
        "qname":   c.get("qname", c["name"]),
        "type":    c["type"],
        "file":    os.path.basename(c["file"]),
        "doc":     c.get("docstring", "")[:80],
        "methods": c.get("methods", []),
        "bases":   c.get("base_classes", []),
        "also_in": c.get("also_in", []),
    } for i, c in enumerate(chunks)]

    edges = [{"source": i, "target": j, "kind": "calls"} for i, j in iter_call_edges(chunks)]

    name_index = build_name_index(chunks)
    for i, c in enumerate(chunks):
        for base in c.get("base_classes", []):
            for j in name_index.get(base, []):
                if j != i and language_of(chunks[j]) == language_of(c):
                    edges.append({"source": i, "target": j, "kind": "inherits"})

    return nodes, edges


# =============================================================================
# NAME MATCHING FOR LANGUAGES WITHOUT A RESOLVER  (v9: everything but Python)
# =============================================================================
def _import_tokens(chunk):
    """Words in the file's import paths: './lib/help.js' -> {'lib', 'help', 'js'}."""
    return {t.lower() for module in chunk.get("imports", ())
            for t in re.split(r"[^A-Za-z0-9_]+", module) if t}


def _stem(chunk):
    return os.path.splitext(os.path.basename(chunk["qname"].split("::")[0]))[0].lower()


class NameMatcher:
    """
    Links names to definitions within one language, for chunks that have no
    scope-aware resolver. A name is looked up among the language's functions,
    classes and methods (and classes listing it as a method), preferring, in
    order: the caller's own file, files its imports name, then the whole repo
    if at most MAX_NAME_MATCHES definitions share the name.
    """

    def __init__(self, chunks, indices):
        self.chunks = chunks
        self.by_name = defaultdict(list)        # (language, name) -> [i]
        for i in indices:
            c = chunks[i]
            lang = language_of(c)
            self.by_name[(lang, c["name"])].append(i)
            if c["type"] == "class":
                for method in c.get("methods", ()):
                    self.by_name[(lang, method)].append(i)

    def targets(self, i, name, kinds=None):
        caller = self.chunks[i]
        found = list(dict.fromkeys(self.by_name.get((language_of(caller), name), ())))
        # a class listing the method stands for it only when the method has no chunk of its own
        owners = {self.chunks[k]["parent"] for k in found if self.chunks[k]["name"] == name}
        pool = [j for j in found if j != i and (kinds is None or self.chunks[j]["type"] in kinds)
                and (self.chunks[j]["name"] == name or self.chunks[j]["name"] not in owners)]
        same_file = [j for j in pool if self.chunks[j]["file"] == caller["file"]]
        if same_file:
            return same_file
        tokens = _import_tokens(caller)
        imported = [j for j in pool if _stem(self.chunks[j]) in tokens
                    or self.chunks[j]["name"].lower() in tokens
                    or self.chunks[j].get("parent", "").lower() in tokens]
        if imported:
            return imported
        return pool if len(pool) <= MAX_NAME_MATCHES else []

    def calls(self, i):
        """[(j, NAME_MATCH)] for every call in chunk i."""
        found = {}
        for name in self.chunks[i].get("calls", ()):
            for j in self.targets(i, name):
                found.setdefault(j, NAME_MATCH)
        return sorted(found.items())

    def bases(self, i):
        """Class chunks named as chunk i's bases."""
        return [j for base in self.chunks[i].get("base_classes", ())
                for j in self.targets(i, base, kinds=("class",))]

    def owner(self, i):
        """The class chunk a method chunk belongs to."""
        c = self.chunks[i]
        if not c.get("parent"):
            return None
        found = self.targets(i, c["parent"], kinds=("class",))
        return found[0] if found else None



# =============================================================================
# BRIDGES (v10): edges across languages and processes, found by coderag/bridges/
# =============================================================================
BRIDGE = "bridge"                  # edge kind; typed_neighbors labels: "bridge:<mechanism>" and
                                   # "bridged by:<mechanism>" (followed backwards)


class BridgeIndex:
    """
    A graph's bridge edges (bridges.BridgeEdge: kind "bridge", with mechanism,
    confidence and evidence), looked up from either end. Graph expansion
    follows them like calls: forwards with the callees, backwards last.
    """

    def __init__(self, edges=()):
        self.edges = list(edges)
        self.out, self.into = defaultdict(list), defaultdict(list)
        for k, e in enumerate(self.edges):
            self.out[e.src].append(k)
            self.into[e.dst].append(k)

    def __len__(self):
        return len(self.edges)

    def forward(self, i):
        """[(j, "bridge:http")] from chunk i to what it reaches across a boundary."""
        return [(self.edges[k].dst, f"{BRIDGE}:{self.edges[k].mechanism}") for k in self.out.get(i, ())]

    def backward(self, j):
        """[(i, "bridged by:http")]: the chunks that reach chunk j across a boundary."""
        return [(self.edges[k].src, f"bridged by:{self.edges[k].mechanism}") for k in self.into.get(j, ())]

    def between(self, a, b):
        """The bridge edge joining chunks a and b, in either direction, or None."""
        for k in self.out.get(a, ()):
            if self.edges[k].dst == b:
                return self.edges[k]
        for k in self.out.get(b, ()):
            if self.edges[k].dst == a:
                return self.edges[k]
        return None

    def counts(self):
        out = defaultdict(int)
        for e in self.edges:
            out[e.mechanism] += 1
        return dict(out)

    def to_list(self):
        return [e.to_dict() for e in self.edges]

    @classmethod
    def from_list(cls, data):
        from .bridges import BridgeEdge
        return cls(BridgeEdge.from_dict(d) for d in data or ())
