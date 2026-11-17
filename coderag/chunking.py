"""
Turning a repository into chunks.

Two strategies:
  • fixed : concatenate every file and cut fixed-size character windows (v0)
  • ast   : one chunk per top-level class / function of each .py file (v1+),
            carrying structural metadata (calls, bases, methods, docstring ...)

With `config.languages` (v9), files of the registered languages (languages.py)
are chunked too, by one generic tree-sitter walker driven by each language's
config, into the same schema plus a `language` field. Python keeps `ast`.

Every chunk is a dict with at least: content, type, file, name.
"""

import ast
import hashlib
import os
import re
import textwrap

from .config import SKIP_DIRS
from .languages import TEMPLATE, TEMPLATE_EXTENSIONS, language_for


# =============================================================================
# FILE DISCOVERY
# =============================================================================
def iter_source_files(repo_path, extensions, prune_dirs=True, ignore_dirs=()):
    """
    Yield paths of files under `repo_path` whose name ends with `extensions`.
    Symlinked directories are not followed (os.walk default).
    """
    skip = SKIP_DIRS | set(ignore_dirs)
    for root, dirs, files in os.walk(repo_path, topdown=True):
        if prune_dirs:
            # prune IN PLACE so os.walk doesn't descend into noise dirs
            dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for file in files:
            if file.endswith(tuple(extensions)):
                yield os.path.join(root, file)


def source_fingerprint(repo_path, extensions, prune_dirs=True, ignore_dirs=()):
    """sha256 over the relative paths and bytes of every indexed file."""
    digest = hashlib.sha256()
    paths = sorted(iter_source_files(repo_path, extensions, prune_dirs, ignore_dirs))
    for path in paths:
        rel = os.path.relpath(path, repo_path).replace("\\", "/")
        digest.update(rel.encode() + b"\0")
        try:
            with open(path, "rb") as f:
                digest.update(f.read())
        except OSError:
            pass
        digest.update(b"\0")
    return digest.hexdigest()


# =============================================================================
# FIXED-SIZE CHUNKING  (v0 baseline)
# =============================================================================
def load_concatenated(repo_path, extensions, prune_dirs=False):
    """
    Concatenate every matching file into one string with FILE markers.

    Returns (text, files) where `files` holds (offset, path, content) for each
    file, so positions in `text` can be mapped back to file lines.
    """
    text = ""
    files = []
    for file_path in iter_source_files(repo_path, extensions, prune_dirs):
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            continue
        text += f"\n\n### FILE: {file_path} ###\n"
        files.append((len(text), file_path, content))
        text += content
    return text, files


def _window_spans(start, end, files):
    """[(path, first_line, last_line)] of file content inside text[start:end]."""
    spans = []
    for offset, path, content in files:
        lo, hi = max(start, offset), min(end, offset + len(content))
        if lo < hi:
            first = content.count("\n", 0, lo - offset) + 1
            last = content.count("\n", 0, hi - offset - 1) + 1
            spans.append((path, first, last))
    return spans


def chunk_fixed(text, chunk_size=1000, overlap=200, files=()):
    """Cut `text` into overlapping character windows."""
    if overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")
    chunks = []
    for i, start in enumerate(range(0, len(text), chunk_size - overlap)):
        chunks.append({
            "content": text[start:start + chunk_size],
            "type":    "text",
            "file":    "",
            "name":    f"chunk_{i}",
            "spans":   _window_spans(start, start + chunk_size, files),
        })
    return chunks


# =============================================================================
# AST HELPERS
# =============================================================================
def _get_docstring(node):
    """Extract the first docstring from a function or class node, if any."""
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        return node.body[0].value.value.strip()[:200]   # cap at 200 chars
    return ""


def _get_calls(node):
    """
    Return the sorted set of function/method names directly called inside `node`.
      • Simple name calls:    foo()      -> 'foo'
      • Attribute calls:      obj.bar()  -> 'bar'
      • Class instantiations: Bat("x")   -> 'Bat'
    """
    calls = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            func = n.func
            if isinstance(func, ast.Name):
                calls.add(func.id)
            elif isinstance(func, ast.Attribute):
                calls.add(func.attr)
    return sorted(calls)


def _walk(node, skip_asserts):
    """ast.walk, optionally without descending into `assert` statements."""
    stack = [node]
    while stack:
        n = stack.pop()
        if skip_asserts and isinstance(n, ast.Assert):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _get_call_sites(nodes, skip_asserts=False):
    """
    Sorted unique [kind, base, name] for every call inside `nodes`, keeping
    the form of the call so it can be resolved with scope information:
      foo()            -> ["name",  "",    "foo"]
      self.foo()       -> ["self",  "",    "foo"]   (also cls.foo())
      super().foo()    -> ["super", "",    "foo"]
      mod.foo()        -> ["attr",  "mod", "foo"]   (base is a plain name)
      a.b.foo()        -> ["attr",  "",    "foo"]   (base unknown)
    """
    sites = set()
    for root in nodes:
        for n in _walk(root, skip_asserts):
            if not isinstance(n, ast.Call):
                continue
            func = n.func
            if isinstance(func, ast.Name):
                sites.add(("name", "", func.id))
            elif isinstance(func, ast.Attribute):
                value = func.value
                if isinstance(value, ast.Name) and value.id in ("self", "cls"):
                    sites.add(("self", "", func.attr))
                elif isinstance(value, ast.Name):
                    sites.add(("attr", value.id, func.attr))
                elif (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                      and value.func.id == "super"):
                    sites.add(("super", "", func.attr))
                else:
                    sites.add(("attr", "", func.attr))
    return [list(s) for s in sorted(sites)]


def _assert_only_sites(nodes):
    """Call sites that appear only inside `assert` statements (type guards, checks)."""
    everywhere = {tuple(s) for s in _get_call_sites(nodes)}
    outside = {tuple(s) for s in _get_call_sites(nodes, skip_asserts=True)}
    return [list(s) for s in sorted(everywhere - outside)]


def get_imports(tree):
    """
    Map each name bound by an import anywhere in the file to
    [module, symbol, level]:
      import os.path          -> {"os": ["os", "", 0]}
      import numpy as np      -> {"np": ["numpy", "", 0]}
      from .core import Cmd   -> {"Cmd": ["core", "Cmd", 1]}
      from . import types     -> {"types": ["", "types", 1]}
    """
    imports = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imports[alias.asname] = [alias.name, "", 0]
                else:
                    top = alias.name.split(".")[0]
                    imports[top] = [top, "", 0]
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    imports[alias.asname or alias.name] = [node.module or "", alias.name,
                                                           node.level]
    return imports


def _get_decorators(node):
    """Return a list of decorator name strings on a function/class node."""
    names = []
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Call):
            dec = dec.func
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.append(dec.attr)
    return names


def _get_base_classes(class_node):
    """Return the list of base-class names for an ast.ClassDef node."""
    bases = []
    for base in class_node.bases:
        if isinstance(base, ast.Name):
            bases.append(base.id)
        elif isinstance(base, ast.Attribute):
            bases.append(base.attr)
    return bases


def _get_methods(class_node):
    """Return the names of all direct methods of a class."""
    return [
        item.name
        for item in class_node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def content_hash(code_text):
    """md5 of source with comments stripped and whitespace collapsed."""
    text = re.sub(r'#[^\n]*', '', code_text)
    text = re.sub(r'\s+', ' ', text).strip()
    return hashlib.md5(text.encode()).hexdigest()


def make_qname(file_path, name, repo_path=None):
    """Qualified name like 'pkg/module.py::Name' to disambiguate same-named chunks."""
    if repo_path:
        rel = os.path.relpath(file_path, repo_path).replace("\\", "/")
    else:
        rel = os.path.basename(file_path)
    return f"{rel}::{name}"


def _make_chunk(content, type_, file_path, name, repo_path, node, start_line, end_line):
    is_class = isinstance(node, ast.ClassDef)
    is_def = isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    return {
        "content":      content,
        "type":         type_,
        "file":         file_path,
        "name":         name,
        "qname":        make_qname(file_path, name, repo_path),
        "start_line":   start_line,     # 1-based, inclusive, decorators included
        "end_line":     end_line,
        "content_hash": content_hash(content),
        # ── structural metadata ──
        "base_classes": _get_base_classes(node) if is_class else [],
        "methods":      _get_methods(node) if is_class else [],
        "calls":        _get_calls(node) if node is not None else [],
        "call_sites":   _get_call_sites([node]) if node is not None else [],
        "assert_call_sites": _assert_only_sites([node]) if node is not None else [],
        "decorators":   _get_decorators(node) if is_def else [],
        "docstring":    _get_docstring(node) if is_def else "",
        "parent":       "",             # owning class, for method chunks
        # filled later by build_reverse_index / dedup_chunks
        "called_by":    [],
        "also_in":      [],
    }


_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _is_overload_stub(node):
    """@typing.overload signature stub: the real implementation follows it."""
    return isinstance(node, _DEF_TYPES) and "overload" in _get_decorators(node)


def _start_line(node):
    """First line of a node, decorators included."""
    return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])


def _class_outline(node, lines):
    """
    Class source with every method body replaced by `...`: signature,
    docstring, class attributes and method signatures stay.
    """
    out = []
    line = _start_line(node)
    for item in node.body:
        if not isinstance(item, _DEF_TYPES):
            continue
        out.extend(lines[line - 1:_start_line(item) - 1])
        sig_end = max(item.body[0].lineno - 1, item.lineno)   # one-liners keep their line
        out.extend(lines[_start_line(item) - 1:sig_end])
        if sig_end < item.end_lineno:
            indent = lines[item.lineno - 1][:len(lines[item.lineno - 1]) -
                                            len(lines[item.lineno - 1].lstrip())]
            out.append(indent + "    ...\n")
        line = item.end_lineno + 1
    out.extend(lines[line - 1:node.end_lineno])
    return "".join(out).rstrip("\n")


def _class_chunks(node, file_path, repo_path, lines, min_segment_chars, skip_overloads=False):
    """One outline chunk for the class plus one chunk per method."""
    outline = _make_chunk(_class_outline(node, lines), "class", file_path, node.name,
                          repo_path, node, _start_line(node), node.end_lineno)
    # the outline's calls are those outside method bodies (attributes, nested classes)
    non_methods = [item for item in node.body if not isinstance(item, _DEF_TYPES)]
    outline["calls"] = sorted({c for item in non_methods for c in _get_calls(item)})
    outline["call_sites"] = _get_call_sites(non_methods)
    outline["assert_call_sites"] = _assert_only_sites(non_methods)
    chunks = [outline]

    for item in node.body:
        if not isinstance(item, _DEF_TYPES) or (skip_overloads and _is_overload_stub(item)):
            continue
        start = _start_line(item)
        source = textwrap.dedent("".join(lines[start - 1:item.end_lineno])).rstrip("\n")
        if len(source) < min_segment_chars:
            continue
        method = _make_chunk(source, "method", file_path, item.name, repo_path, item,
                             start, item.end_lineno)
        method["qname"] = make_qname(file_path, f"{node.name}.{item.name}", repo_path)
        method["parent"] = node.name
        chunks.append(method)
    return chunks


# =============================================================================
# AST CHUNKING  (v1+)
# =============================================================================
def parse_python_file(file_path, repo_path=None, max_file_chars=30_000, min_segment_chars=60,
                      chunk_methods=False, skip_overloads=False):
    """
    Return the chunks for one .py file.

    • Top-level classes and functions become one chunk each (methods stay inside
      their class chunk, so nothing is indexed twice).
    • With `chunk_methods`, a class becomes an outline chunk (methods reduced
      to signatures) plus one chunk per method.
    • With `skip_overloads`, @typing.overload stubs are dropped: they share the
      implementation's name, waste retrieval slots and multiply call edges.
    • A file with no top-level classes/functions becomes a single 'file' chunk
      so plain scripts are not silently lost.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            code = f.read()
    except OSError as e:
        print(f"[ERROR] Cannot open {file_path}: {e}")
        return []

    if len(code) > max_file_chars:
        print(f"[SKIP] Too large ({len(code)//1000}k chars): {file_path}")
        return []

    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        print(f"[SKIP] Cannot parse: {file_path}")
        return []

    lines = code.splitlines(keepends=True)
    chunks = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            type_ = "class"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            type_ = "function"
        else:
            continue
        if skip_overloads and _is_overload_stub(node):
            continue
        if type_ == "class" and chunk_methods:
            chunks.extend(_class_chunks(node, file_path, repo_path, lines, min_segment_chars,
                                        skip_overloads))
            continue
        segment = ast.get_source_segment(code, node)
        if not segment:
            continue
        # get_source_segment starts at `def`/`class`; prepend the decorator lines
        start_line = _start_line(node)
        segment = "".join(lines[start_line - 1:node.lineno - 1]) + segment
        if len(segment) < min_segment_chars:
            continue
        chunks.append(_make_chunk(segment, type_, file_path, node.name, repo_path, node,
                                  start_line, node.end_lineno))

    if chunks:
        return chunks

    name = os.path.basename(file_path)
    return [_make_chunk(code, "file", file_path, name, repo_path, tree, 1, max(len(lines), 1))]


# =============================================================================
# TREE-SITTER CHUNKING  (v9: every registered language except Python)
# =============================================================================
_COMMENT_MARKS = re.compile(r"^(?:/\*+!?|\*+/?|//[/!]?|#|--)\s?")
_DECORATIONS = ("attribute_item", "decorator", "annotation", "marker_annotation")


def _ts_text(node, src):
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _ts_tail_name(node, lang, src, depth=0):
    """Last name in a (possibly qualified) name or callee: a.b.c() -> 'c', ns::f -> 'f'."""
    if node is None or depth > 12:
        return ""
    if node.type in lang.name_types:
        return _ts_text(node, src)
    for name in ("name", "field", "property", "function", "type"):
        child = node.child_by_field_name(name)
        if child is not None:
            return _ts_tail_name(child, lang, src, depth + 1)
    kids = [c for c in node.named_children if c.type not in lang.skip]
    return _ts_tail_name(kids[-1], lang, src, depth + 1) if kids else ""


def _ts_type_names(node, lang, src):
    """Names of the types listed under `node` (bases), ignoring type and call arguments."""
    if node is None or node.type in lang.skip:
        return []
    if node.type in lang.name_types:
        return [_ts_text(node, src)]
    if node.type in lang.qualified:
        return [_ts_tail_name(node, lang, src)]
    return [n for child in node.named_children for n in _ts_type_names(child, lang, src)]


def _ts_first_name(node, lang, src):
    """The type named under `node`: a receiver's or impl block's owner (not a parameter name)."""
    if node is None:
        return ""
    typed = _ts_find(node, lang.owner_types)
    if typed:
        return _ts_text(typed[0], src)
    names = _ts_type_names(node, lang, src)
    return names[0] if names else ""


def _ts_name(node, lang, src):
    """
    Name of a definition: its `name` field, else the end of its `declarator`
    chain (C/C++: int *f(void) -> 'f'; out-of-class C++ methods keep
    'Class::method'), else its first name-holding child (Kotlin).
    """
    target = node.child_by_field_name("name")
    if target is None and node.child_by_field_name("declarator") is not None:
        target = node.child_by_field_name("declarator")
        while target.child_by_field_name("declarator") is not None:
            target = target.child_by_field_name("declarator")
    if target is None:
        target = next((c for c in node.named_children if c.type in lang.name_types), None)
    if target is None:
        return ""
    text = _ts_text(target, src).strip()
    if target.type in lang.name_types or ("::" in text and text.replace("::", "").replace("~", "").isidentifier()):
        return text
    return _ts_tail_name(target, lang, src)


def _ts_is_class(node, lang):
    if node.type not in lang.classes:
        return False
    condition = lang.classes[node.type]
    if not condition:
        return True
    for field_name, types in condition.items():
        child = node.child_by_field_name(field_name)
        if child is not None and child.type in types:
            return True
    return False


def _ts_body(node, lang):
    """The node holding a definition's members."""
    if node is None:
        return None
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    queue = list(node.named_children)
    while queue:                                  # e.g. Go: type_spec > struct_type > field list
        child = queue.pop(0)
        if child.type in lang.class_bodies:
            return child
        if child.type not in lang.skip:
            queue.extend(child.named_children)
    return None


def _ts_code_body(node):
    """A function's statement body (None for a signature): elided in class outlines."""
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    kids = node.named_children
    if kids and ("body" in kids[-1].type or "block" in kids[-1].type):
        return kids[-1]
    return None


def _ts_scan(node, lang):
    """Top-level definitions under `node`, looking through containers (export, namespace ...)."""
    for child in node.named_children:
        if child.type in lang.containers:
            yield from _ts_scan(child, lang)
        else:
            yield child


def _ts_members(body, lang):
    for child in body.named_children if body is not None else ():
        if child.type in lang.containers:
            yield from _ts_members(child, lang)
        elif child.type in lang.methods:
            yield child


def _ts_find(node, types):
    """`node` and its descendants of the given types (not looking inside matches)."""
    if node.type in types:
        return [node]
    return [m for child in node.named_children for m in _ts_find(child, types)]


def _ts_span(node, lang, root):
    """The node a chunk covers: climbs single-child wrappers (export, const, Go's type)."""
    while (node.parent is not None and node.parent != root and node.parent.type in lang.containers
           and [c for c in node.parent.named_children if c.type not in lang.comments] == [node]):
        node = node.parent
    return node


def _ts_docstring(node, lang, src):
    """
    Comments directly above a definition: attributes or decorators in between
    are fine, and so is one blank line under the closest comment (JSDoc often has one).
    """
    comments, line = [], node.start_point[0]
    sibling, gap = node.prev_named_sibling, 2
    while sibling is not None and sibling.end_point[0] >= line - gap:
        gap = 1
        if sibling.type in lang.comments:
            comments.append(_ts_text(sibling, src))
        elif sibling.type not in _DECORATIONS:
            break
        line = sibling.start_point[0]
        sibling = sibling.prev_named_sibling
    lines = []
    for text in reversed(comments):
        for raw in text.splitlines():
            cleaned = re.sub(r"\s*\*+/$", "", _COMMENT_MARKS.sub("", raw.strip())).strip()
            if cleaned:
                lines.append(cleaned)
    return " ".join(lines)[:200]


def _ts_calls(nodes, lang, src, skip=()):
    """Sorted names called inside `nodes`, not descending into the nodes in `skip`."""
    calls, stack = set(), list(nodes)
    skipped = {(n.start_byte, n.end_byte, n.type) for n in skip}
    while stack:
        n = stack.pop()
        if (n.start_byte, n.end_byte, n.type) in skipped:
            continue
        if n.type in lang.calls:
            field_name = lang.calls[n.type]
            callee = (n.child_by_field_name(field_name) if field_name
                      else next(iter(n.named_children), None))
            name = _ts_tail_name(callee, lang, src)
            if name.isidentifier():
                calls.add(name)
        stack.extend(n.named_children)
    return sorted(calls)


def _ts_imports(root, lang, src):
    """Module paths a file imports ('./util.js', 'com.x.Util', 'fmt'), outside definitions."""
    found, stack = set(), [root]
    while stack:
        n = stack.pop()
        if n.type in lang.imports:
            target = next((c for c in (n.child_by_field_name(f) for f in ("source", "path", "argument"))
                           if c is not None), None) or next(iter(n.named_children), None)
            if target is not None:
                found.add(_ts_text(target, src).strip("\"'<>` "))
        elif n.type not in lang.functions and not _ts_is_class(n, lang):
            stack.extend(n.named_children)
    return sorted(found)


def _ts_outline(span, members, lang, src):
    """Class source with every method body replaced by lang.elide."""
    bodies = sorted((b for m in members for b in [_ts_code_body(m)] if b is not None),
                    key=lambda b: b.start_byte)
    out, pos = [], src.rfind(b"\n", 0, span.start_byte) + 1    # from the start of its first line
    for body in bodies:
        out += [src[pos:body.start_byte], lang.elide.encode()]
        pos = body.end_byte
    out.append(src[pos:span.end_byte])
    return textwrap.dedent(b"".join(out).decode("utf-8", errors="replace")).rstrip("\n")


def _ts_chunk(lang, content, type_, file_path, name, repo_path, start, end, doc="",
              calls=(), bases=(), methods=(), parent="", imports=()):
    """A chunk in parse_python_file's schema, plus `language` and the file's `imports`."""
    return {
        "content":      content,
        "type":         type_,
        "file":         file_path,
        "name":         name,
        "qname":        make_qname(file_path, f"{parent}.{name}" if parent else name, repo_path),
        "start_line":   start,
        "end_line":     end,
        "content_hash": content_hash(content),
        "base_classes": list(bases),
        "methods":      list(methods),
        "calls":        list(calls),
        "call_sites":   [],                       # Python only: scoped resolution
        "assert_call_sites": [],
        "decorators":   [],
        "docstring":    doc,
        "parent":       parent,
        "called_by":    [],
        "also_in":      [],
        "language":     lang.name,
        "imports":      list(imports),
    }


def parse_tree_sitter_file(file_path, lang, repo_path=None, max_file_chars=30_000,
                           min_segment_chars=60, chunk_methods=False):
    """
    Chunks of one file in a registered language (languages.py), in the schema
    of parse_python_file: top-level functions and classes; with `chunk_methods`
    a class becomes an outline plus one chunk per method. Methods defined away
    from their type are attached to it: Go receivers, Rust impl blocks (whose
    trait becomes a base) and C++ Class::method definitions. A file without
    definitions becomes one 'file' chunk.
    """
    from .languages import parser_for

    try:
        with open(file_path, "rb") as f:
            src = f.read()
    except OSError as e:
        print(f"[ERROR] Cannot open {file_path}: {e}")
        return []
    if len(src) > max_file_chars:
        print(f"[SKIP] Too large ({len(src)//1000}k chars): {file_path}")
        return []
    try:
        root = parser_for(file_path).parse(src).root_node
    except Exception as e:                         # grammar unavailable, parser failure
        print(f"[SKIP] Cannot parse: {file_path} ({type(e).__name__}: {e})")
        return []

    lines = src.decode("utf-8", errors="replace").splitlines(keepends=True)
    imports = _ts_imports(root, lang, src)
    classes, chunks, methods_by_owner, extra_bases = {}, [], {}, {}

    def span_lines(node):
        return node.start_point[0] + 1, node.end_point[0] + 1

    def text_of(node):
        lo, hi = span_lines(node)
        return textwrap.dedent("".join(lines[lo - 1:hi])).rstrip("\n")

    def method_chunk(node, owner, name=None):
        name = name or _ts_name(node, lang, src)
        if not name:
            return
        methods_by_owner.setdefault(owner, []).append(name)
        if _ts_code_body(node) is None:
            return                                 # a signature: listed, not chunked
        content = text_of(node)
        if len(content) >= min_segment_chars:
            lo, hi = span_lines(node)
            chunks.append(_ts_chunk(lang, content, "method", file_path, name, repo_path, lo, hi,
                                    _ts_docstring(node, lang, src), _ts_calls([node], lang, src),
                                    parent=owner, imports=imports))

    def class_chunk(node, span):
        name = _ts_name(node, lang, src)
        if not name:
            return
        body = _ts_body(node, lang)
        members = list(_ts_members(body, lang))
        bases = [b for child in node.named_children if child != body
                 for inheritance in _ts_find(child, lang.inheritance)
                 for b in _ts_type_names(inheritance, lang, src)]
        for member in (body.named_children if body is not None else ()):    # Go: embedded types
            if member.type in lang.embedded_bases and member.child_by_field_name("name") is None:
                bases += _ts_type_names(member.child_by_field_name("type"), lang, src)
        lo, hi = span_lines(span)
        if chunk_methods:
            content, calls = _ts_outline(span, members, lang, src), _ts_calls([node], lang, src, members)
        else:
            content, calls = text_of(span), _ts_calls([node], lang, src)
        chunk = _ts_chunk(lang, content, "class", file_path, name, repo_path, lo, hi,
                          _ts_docstring(span, lang, src), calls, list(dict.fromkeys(bases)),
                          imports=imports)
        classes[name] = chunk
        chunks.append(chunk)
        for member in members:
            method_chunk(member, name)

    for node in _ts_scan(root, lang):
        span = _ts_span(node, lang, root)
        if _ts_is_class(node, lang):
            class_chunk(node, span)
        elif node.type in lang.receiver_methods:
            receiver = node.child_by_field_name(lang.receiver_methods[node.type])
            method_chunk(node, _ts_first_name(receiver, lang, src))
        elif node.type in lang.impl_blocks:
            type_field, trait_field = lang.impl_blocks[node.type]
            owner = _ts_first_name(node.child_by_field_name(type_field), lang, src)
            trait = _ts_first_name(node.child_by_field_name(trait_field), lang, src)
            if owner and trait:
                extra_bases.setdefault(owner, []).append(trait)
            for member in _ts_members(_ts_body(node, lang), lang):
                method_chunk(member, owner)
        elif node.type in lang.functions or node.type in lang.function_values:
            if node.type in lang.function_values:
                value_field, kinds = lang.function_values[node.type]
                value = node.child_by_field_name(value_field)
                if value is None or value.type not in kinds:
                    continue
            name = _ts_name(node, lang, src)
            if "::" in name:                       # C++: double Circle::area() { ... }
                owner, _, method = name.rpartition("::")
                method_chunk(node, owner.split("::")[-1], method)
                continue
            content = text_of(span)
            if not name or len(content) < min_segment_chars:
                continue
            lo, hi = span_lines(span)
            chunks.append(_ts_chunk(lang, content, "function", file_path, name, repo_path, lo, hi,
                                    _ts_docstring(span, lang, src), _ts_calls([node], lang, src),
                                    imports=imports))

    for owner, names in methods_by_owner.items():
        if owner in classes:
            known = classes[owner]["methods"]
            known.extend(n for n in names if n not in known)
    for owner, traits in extra_bases.items():
        if owner in classes:
            known = classes[owner]["base_classes"]
            known.extend(t for t in traits if t not in known)
    if not chunk_methods:                          # methods stay inside their class's chunk
        chunks = [c for c in chunks if c["type"] != "method" or c["parent"] not in classes]

    if chunks:
        return chunks
    return [_ts_chunk(lang, "".join(lines), "file", file_path, os.path.basename(file_path),
                      repo_path, 1, max(len(lines), 1), imports=imports)]


# =============================================================================
# TEMPLATES  (v10 HTTP bridges: forms and htmx requests)
# =============================================================================
_TEMPLATE_REQUEST = re.compile(r"<form\b[^>]*\baction\s*=|\bhx-(?:get|post|put|patch|delete)\s*=", re.I)


def parse_template_file(file_path, repo_path=None, max_file_chars=1_000_000):
    """
    One "template" chunk for an HTML template that sends requests (a <form
    action> or an hx-get/hx-post/... attribute), so an HTTP bridge has a node
    to start from; [] for any other HTML file. Its requests are read by
    bridges/http.py, not here.
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    if len(text) > max_file_chars or not _TEMPLATE_REQUEST.search(text):
        return []
    name = os.path.basename(file_path)
    lines = text.splitlines()
    return [{
        "content": text.rstrip("\n"), "type": "template", "file": file_path, "name": name,
        "qname": make_qname(file_path, name, repo_path), "start_line": 1, "end_line": max(len(lines), 1),
        "content_hash": content_hash(text), "base_classes": [], "methods": [], "calls": [],
        "call_sites": [], "assert_call_sites": [], "decorators": [], "docstring": "", "parent": "",
        "called_by": [], "also_in": [], "language": TEMPLATE, "imports": [],
    }]


def dedup_chunks(chunks, verbose=True):
    """
    Drop chunks with the same name + normalised content hash. The first
    occurrence is kept and the duplicate's file is recorded in `also_in`.
    """
    seen = {}           # (name, content_hash) -> index in `unique`
    unique = []

    for chunk in chunks:
        key = (chunk["name"], chunk["content_hash"])
        if key in seen:
            kept = unique[seen[key]]
            kept["also_in"].append(os.path.basename(chunk["file"]))
            if verbose:
                print(f"[DEDUP] Dropped duplicate: {chunk['name']} "
                      f"(from {os.path.basename(chunk['file'])}, "
                      f"kept {os.path.basename(kept['file'])})")
        else:
            seen[key] = len(unique)
            unique.append(chunk)

    if verbose and len(chunks) != len(unique):
        print(f"[DEDUP] {len(chunks)} -> {len(unique)}  "
              f"({len(chunks) - len(unique)} duplicates removed)")
    return unique


def load_ast_chunks(repo_path, config, verbose=True):
    chunks = []
    file_count = 0
    for file_path in iter_source_files(repo_path, config.source_extensions(), config.prune_dirs,
                                       config.ignore_dirs):
        if verbose:
            print(f"[RAG]   Parsing: {os.path.relpath(file_path, repo_path)}")
        lang = language_for(file_path, config.languages)
        if file_path.lower().endswith(TEMPLATE_EXTENSIONS):
            chunks.extend(parse_template_file(file_path, repo_path, config.max_file_chars))
        elif lang is not None:
            chunks.extend(parse_tree_sitter_file(
                file_path, lang, repo_path,
                max_file_chars=config.max_file_chars,
                min_segment_chars=config.min_segment_chars,
                chunk_methods=config.chunk_methods,
            ))
        elif file_path.endswith(tuple(config.extensions)):
            found = parse_python_file(
                file_path, repo_path,
                max_file_chars=config.max_file_chars,
                min_segment_chars=config.min_segment_chars,
                chunk_methods=config.chunk_methods,
                skip_overloads=config.skip_overloads,
            )
            if config.languages:                 # multi-language index: say what each chunk is
                for chunk in found:
                    chunk["language"] = "python"
            chunks.extend(found)
        else:
            continue
        file_count += 1

    if config.indexes_project_files():           # v10: README, Procfile, Dockerfile ... as config
        from .facts import iter_project_files, parse_config_file
        for path in iter_project_files(repo_path, SKIP_DIRS, config.ignore_dirs):
            chunks.extend(parse_config_file(path, repo_path))

    if verbose:
        print(f"[RAG] Parsed {file_count} files -> {len(chunks)} chunks")
    if config.dedup_content:
        chunks = dedup_chunks(chunks, verbose)
    return chunks


def load_chunks(repo_path, config, verbose=True):
    """Chunk `repo_path` according to `config` and drop chunks that are too small."""
    if not os.path.isdir(repo_path):
        raise FileNotFoundError(f"Repo path not found: {repo_path}")

    if verbose:
        print(f"[RAG] Scanning repo: {repo_path}")

    if config.chunker == "fixed":
        text, files = load_concatenated(repo_path, config.extensions, config.prune_dirs)
        chunks = chunk_fixed(text, config.chunk_size, config.chunk_overlap, files)
    elif config.chunker == "ast":
        chunks = load_ast_chunks(repo_path, config, verbose)
    else:
        raise ValueError(f"Unknown chunker: {config.chunker!r}")

    # config files are kept however short: a one-line runtime.txt still answers "which Python?"
    return [c for c in chunks if len(c["content"]) > config.min_chunk_chars or c.get("type") == "config"]
