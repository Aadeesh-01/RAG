"""
Code-structure graph over AST chunks.

Edges come from chunk metadata:
  • calls        caller -> callee   (resolved by bare name, same file first)
  • base_classes subclass -> base
  • called_by    reverse of calls, filled by build_reverse_index()
"""

import os
from collections import defaultdict, deque


def _resolve_targets(chunks, i, callee_name, name_to_indices, method_to_classes):
    """
    Chunk indices that a call to `callee_name` from chunk `i` refers to.

      1. Direct name match  – a chunk with that name (functions, class
         instantiations like Bat(...)).
      2. Method-owner match – a class chunk that defines a method with that
         name (handles obj.method() / self.method()).
    Same-file targets are preferred over cross-file ones.
    """
    caller_file = chunks[i]["file"]
    for pool in (name_to_indices.get(callee_name, []),
                 method_to_classes.get(callee_name, [])):
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
    for i, chunk in enumerate(chunks):
        resolved = set()
        for callee_name in chunk["calls"]:
            for j in _resolve_targets(chunks, i, callee_name,
                                      name_to_indices, method_to_classes):
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
    """Neighbours by bare name: every chunk named like a base, callee, parent or caller."""
    meta = chunks[idx]
    neighbor_names = list(meta["base_classes"]) + list(meta["calls"])
    if meta.get("parent"):
        neighbor_names.append(meta["parent"])      # method -> its class outline
    if include_callers:
        neighbor_names += list(meta["called_by"])
    return [j for name in neighbor_names for j in name_index.get(name, [])]


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
                if j != i:
                    edges.append({"source": i, "target": j, "kind": "inherits"})

    return nodes, edges
