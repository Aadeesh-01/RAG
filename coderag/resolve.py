"""
Scope-aware call resolution.

The name-based graph (graph.py) links a call to every chunk with a matching
name, so `json.load(f)` links to `BaseLoader.load` and one `convert()` call
links to every `convert` method. CodeGraph resolves each call from its form
(see chunking._get_call_sites) and the file's imports instead:

  foo()          same-file definition -> imported name (following re-exports)
                 -> repo-wide name only if unique
  self.foo()     enclosing class, its bases (MRO order), and subclass overrides
  super().foo()  the base classes
  mod.foo()      the imported module's file; nothing if the module is external
  Class.foo()    that class's method
  obj.foo()      classes defining `foo`, only if there are few of them

Every edge records how it was resolved: exact | import | unique | ambiguous.
Calls that can't be resolved confidently get no edge.
"""

import ast
import os
from collections import defaultdict

from .chunking import get_imports, iter_source_files
from .context import display_name

EXACT, IMPORT, UNIQUE, AMBIGUOUS = "exact", "import", "unique", "ambiguous"
_RANK = {EXACT: 0, IMPORT: 0, UNIQUE: 1, AMBIGUOUS: 2}


def _rel(chunk):
    return chunk["qname"].split("::")[0]


def _module_of(rel):
    """'pkg/sub.py' -> 'pkg.sub', 'pkg/__init__.py' -> 'pkg'."""
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(rel):
    """Package a module lives in: 'pkg/sub.py' -> 'pkg', 'pkg/__init__.py' -> 'pkg'."""
    parts = rel[:-3].split("/")
    return ".".join(parts[:-1])


class CodeGraph:

    def __init__(self, chunks, repo_path, max_ambiguous=0, ignore_assert_calls=False):
        """
        chunks        : AST chunks of `repo_path` (qnames relative to it)
        max_ambiguous : link `obj.foo()` to up to this many candidate classes
                        (0 = only when exactly one class defines `foo`)
        ignore_assert_calls : no edges for calls made only inside `assert`
                        statements (type guards such as `assert is_prepared(r)`)
        """
        self.chunks = chunks
        self.max_ambiguous = max_ambiguous
        self.ignore_assert_calls = ignore_assert_calls
        self.root_package = os.path.basename(os.path.normpath(repo_path))

        n = len(chunks)
        self.callees = [[] for _ in range(n)]     # [(j, how)], best resolution first
        self.callers = [[] for _ in range(n)]
        self.bases = [[] for _ in range(n)]       # class chunk -> base class chunks
        self.parent = [None] * n                  # method chunk -> class chunk

        self._load_imports(repo_path)
        self._index()
        self._resolve_bases()
        self._resolve_calls()

    # =========================================================================
    # Indexes
    # =========================================================================
    def _load_imports(self, repo_path):
        self.imports = {}                         # rel -> {alias: [module, symbol, level]}
        self.modules = {}                         # dotted module -> rel
        for path in iter_source_files(repo_path, (".py",)):
            rel = os.path.relpath(path, repo_path).replace("\\", "/")
            self.modules[_module_of(rel)] = rel
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    self.imports[rel] = get_imports(ast.parse(f.read()))
            except (SyntaxError, ValueError, OSError):
                self.imports[rel] = {}

    def _index(self):
        self.top = defaultdict(lambda: defaultdict(list))   # rel -> name -> [i]
        self.global_top = defaultdict(list)                 # name -> [i]
        self.classes = {}                                   # (rel, name) -> i
        self.class_methods = defaultdict(dict)              # (rel, cls) -> {method: i}
        self.method_owners = defaultdict(list)              # method -> [(rel, cls)]

        for i, c in enumerate(self.chunks):
            rel = _rel(c)
            if c["type"] == "method":
                self.class_methods[(rel, c["parent"])][c["name"]] = i
                continue
            self.top[rel][c["name"]].append(i)
            self.global_top[c["name"]].append(i)
            if c["type"] == "class":
                self.classes[(rel, c["name"])] = i
                for m in c["methods"]:
                    self.method_owners[m].append((rel, c["name"]))

        for i, c in enumerate(self.chunks):
            if c["type"] == "method":
                self.parent[i] = self.classes.get((_rel(c), c["parent"]))

    def _find_module(self, dotted):
        """Repo file for a dotted module name, or None if it's not in the repo."""
        if dotted in self.modules:
            return self.modules[dotted]
        head, _, rest = dotted.partition(".")
        if head == self.root_package:        # absolute import of the indexed package
            return self.modules.get(rest)
        return None

    def _absolute(self, rel, module, level):
        if level == 0:
            return module
        package = _package_of(rel).split(".") if _package_of(rel) else []
        if level > 1:
            package = package[:max(len(package) - (level - 1), 0)]
        return ".".join(package + ([module] if module else []))

    # =========================================================================
    # Name lookup
    # =========================================================================
    def _resolve_import(self, rel, alias, depth=0):
        """
        What `alias` refers to in file `rel`:
          ("module", rel2) | ("chunks", [i]) | ("external", None) | None (not imported)
        """
        imported = self.imports.get(rel, {}).get(alias)
        if imported is None:
            return None
        module, symbol, level = imported
        target = self._absolute(rel, module, level)
        if not symbol:
            mrel = self._find_module(target)
            return ("module", mrel) if mrel else ("external", None)

        submodule = self._find_module(f"{target}.{symbol}" if target else symbol)
        if submodule:
            return ("module", submodule)
        mrel = self._find_module(target)
        if mrel is None:
            return ("external", None)
        if self.top[mrel].get(symbol):
            return ("chunks", self.top[mrel][symbol])
        if depth < 3:                         # re-exported from another module
            found = self._resolve_import(mrel, symbol, depth + 1)
            if found:
                return found
        return ("external", None)

    def lookup_name(self, rel, name):
        """[(i, how)] for a bare name used in file `rel`."""
        if self.top[rel].get(name):
            return [(i, EXACT) for i in self.top[rel][name]]
        found = self._resolve_import(rel, name)
        if found is not None:
            kind, value = found
            return [(i, IMPORT) for i in value] if kind == "chunks" else []
        return self._by_popularity(self.global_top.get(name, []))

    def _by_popularity(self, candidates):
        if len(candidates) == 1:
            return [(candidates[0], UNIQUE)]
        if 1 < len(candidates) <= self.max_ambiguous:
            return [(i, AMBIGUOUS) for i in candidates]
        return []

    # =========================================================================
    # Classes
    # =========================================================================
    def _class_key(self, i):
        c = self.chunks[i]
        return (_rel(c), c["name"])

    def _resolve_bases(self):
        self.subclasses = defaultdict(list)
        for i, c in enumerate(self.chunks):
            if c["type"] != "class":
                continue
            for base in c["base_classes"]:
                for j, _ in self.lookup_name(_rel(c), base):
                    if self.chunks[j]["type"] == "class" and j != i:
                        self.bases[i].append(j)
                        self.subclasses[j].append(i)

    def _mro(self, i):
        """Class chunk `i` and its ancestors, breadth-first."""
        order, queue = [], [i]
        while queue:
            k = queue.pop(0)
            if k not in order:
                order.append(k)
                queue.extend(self.bases[k])
        return order

    def _descendants(self, i):
        out, queue = [], list(self.subclasses[i])
        while queue:
            k = queue.pop(0)
            if k not in out:
                out.append(k)
                queue.extend(self.subclasses[k])
        return out

    def _method_target(self, class_i, method):
        """Chunk holding `method` of class chunk `class_i` (the class itself if unsplit)."""
        if class_i is None:
            return None
        key = self._class_key(class_i)
        if method in self.class_methods.get(key, {}):
            return self.class_methods[key][method]
        if method in self.chunks[class_i]["methods"]:
            return class_i
        return None

    def method_in_hierarchy(self, class_i, method, include_self=True, include_overrides=True):
        """First definition along the MRO, plus overrides in subclasses."""
        targets = []
        for k in self._mro(class_i)[0 if include_self else 1:]:
            t = self._method_target(k, method)
            if t is not None:
                targets.append(t)
                break
        if include_overrides:
            for k in self._descendants(class_i):
                t = self._method_target(k, method)
                if t is not None:
                    targets.append(t)
        return targets

    # =========================================================================
    # Calls
    # =========================================================================
    def _enclosing_class(self, i):
        c = self.chunks[i]
        if c["type"] == "method":
            return self.parent[i]
        if c["type"] == "class":
            return i
        return None

    def resolve_call(self, i, kind, base, name):
        rel = _rel(self.chunks[i])

        if kind == "name":
            return self.lookup_name(rel, name)

        if kind in ("self", "super"):
            cls = self._enclosing_class(i)
            if cls is not None:
                targets = self.method_in_hierarchy(cls, name, include_self=(kind == "self"),
                                                   include_overrides=(kind == "self"))
                return [(t, EXACT) for t in targets]
            kind = "attr"                      # self.x() outside a class: unknown object

        if base:
            found = self._resolve_import(rel, base)
            if found is not None:
                kind_, value = found
                if kind_ == "module":
                    return [(j, IMPORT) for j in self.top[value].get(name, [])]
                if kind_ == "external":
                    return []                  # json.load(), os.path.join(), ...
            # Class.method() on a class defined here or imported
            classes = [j for j, _ in self.lookup_name(rel, base)
                       if self.chunks[j]["type"] == "class"]
            if classes:
                return [(t, EXACT) for c in classes
                        for t in self.method_in_hierarchy(c, name, include_overrides=False)]

        # obj.foo() on an object of unknown type
        owners = [self.classes[key] for key in self.method_owners.get(name, [])
                  if key in self.classes]
        return [(t, how) for owner, how in self._by_popularity(owners)
                for t in [self._method_target(owner, name)] if t is not None]

    def _resolve_calls(self):
        for i, c in enumerate(self.chunks):
            best = {}
            skip = ({tuple(s) for s in c.get("assert_call_sites", [])}
                    if self.ignore_assert_calls else set())
            for kind, base, name in c.get("call_sites", []):
                if (kind, base, name) in skip:
                    continue
                for j, how in self.resolve_call(i, kind, base, name):
                    if j != i and (j not in best or _RANK[how] < _RANK[best[j]]):
                        best[j] = how
            self.callees[i] = sorted(best.items(), key=lambda e: (_RANK[e[1]], e[0]))
            for j, how in self.callees[i]:
                self.callers[j].append((i, how))

    # =========================================================================
    # Use by the agent
    # =========================================================================
    def apply_called_by(self):
        """Set each chunk's `called_by` to its resolved callers' display names."""
        for j, c in enumerate(self.chunks):
            c["called_by"] = sorted({display_name(self.chunks[i]) for i, _ in self.callers[j]})

    def typed_neighbors(self, i, include_callers=False, callers_first=False):
        """
        [(j, edge)] in BFS priority: bases, callees, owning class, then callers
        (or callers first, for "who calls X?" questions).
        """
        callers = ([(j, f"called by:{how}") for j, how in self.callers[i]]
                   if include_callers else [])
        out = [(j, "inherits") for j in self.bases[i]]
        out += [(j, f"calls:{how}") for j, how in self.callees[i]]
        if self.parent[i] is not None:
            out.append((self.parent[i], "member of"))
        return callers + out if callers_first else out + callers

    def neighbors(self, i, include_callers=False):
        return [j for j, _ in self.typed_neighbors(i, include_callers)]

    # =========================================================================
    # Persistence: only what retrieval needs (neighbors, called_by)
    # =========================================================================
    def to_dict(self):
        return {
            "max_ambiguous": self.max_ambiguous,
            "ignore_assert_calls": self.ignore_assert_calls,
            "callees": [[[j, how] for j, how in edges] for edges in self.callees],
            "callers": [[[j, how] for j, how in edges] for edges in self.callers],
            "bases": self.bases,
            "parent": self.parent,
        }

    @classmethod
    def from_dict(cls, chunks, data):
        graph = cls.__new__(cls)
        graph.chunks = chunks
        graph.max_ambiguous = data["max_ambiguous"]
        graph.ignore_assert_calls = data.get("ignore_assert_calls", False)
        graph.callees = [[(j, how) for j, how in edges] for edges in data["callees"]]
        graph.callers = [[(j, how) for j, how in edges] for edges in data["callers"]]
        graph.bases = data["bases"]
        graph.parent = data["parent"]
        return graph

    def edge_counts(self):
        counts = defaultdict(int)
        for edges in self.callees:
            for _, how in edges:
                counts[how] += 1
        return dict(counts)
