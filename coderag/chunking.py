"""
Turning a repository into chunks.

Two strategies:
  • fixed : concatenate every file and cut fixed-size character windows (v0)
  • ast   : one chunk per top-level class / function of each .py file (v1+),
            carrying structural metadata (calls, bases, methods, docstring ...)

Every chunk is a dict with at least: content, type, file, name.
"""

import ast
import hashlib
import os
import re

from .config import SKIP_DIRS


# =============================================================================
# FILE DISCOVERY
# =============================================================================
def iter_source_files(repo_path, extensions, prune_dirs=True):
    """Yield paths of files under `repo_path` whose name ends with `extensions`."""
    for root, dirs, files in os.walk(repo_path, topdown=True):
        if prune_dirs:
            # prune IN PLACE so os.walk doesn't descend into noise dirs
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for file in files:
            if file.endswith(tuple(extensions)):
                yield os.path.join(root, file)


# =============================================================================
# FIXED-SIZE CHUNKING  (v0 baseline)
# =============================================================================
def load_concatenated(repo_path, extensions, prune_dirs=False):
    """Concatenate every matching file into one string with FILE markers."""
    parts = []
    for file_path in iter_source_files(repo_path, extensions, prune_dirs):
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                parts.append(f"\n\n### FILE: {file_path} ###\n" + f.read())
        except OSError:
            continue
    return "".join(parts)


def chunk_fixed(text, chunk_size=1000, overlap=200):
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


def _make_chunk(content, type_, file_path, name, repo_path, node=None):
    is_class = isinstance(node, ast.ClassDef)
    is_def = isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    return {
        "content":      content,
        "type":         type_,
        "file":         file_path,
        "name":         name,
        "qname":        make_qname(file_path, name, repo_path),
        "content_hash": content_hash(content),
        # ── structural metadata ──
        "base_classes": _get_base_classes(node) if is_class else [],
        "methods":      _get_methods(node) if is_class else [],
        "calls":        _get_calls(node) if node is not None else [],
        "decorators":   _get_decorators(node) if is_def else [],
        "docstring":    _get_docstring(node) if is_def else "",
        # filled later by build_reverse_index / dedup_chunks
        "called_by":    [],
        "also_in":      [],
    }


# =============================================================================
# AST CHUNKING  (v1+)
# =============================================================================
def parse_python_file(file_path, repo_path=None, max_file_chars=30_000, min_segment_chars=60):
    """
    Return the chunks for one .py file.

    • Top-level classes and functions become one chunk each (methods stay inside
      their class chunk, so nothing is indexed twice).
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

    chunks = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            type_ = "class"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            type_ = "function"
        else:
            continue
        segment = ast.get_source_segment(code, node)
        if not segment or len(segment) < min_segment_chars:
            continue
        chunks.append(_make_chunk(segment, type_, file_path, node.name, repo_path, node))

    if chunks:
        return chunks

    name = os.path.basename(file_path)
    return [_make_chunk(code, "file", file_path, name, repo_path, tree)]


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
    for file_path in iter_source_files(repo_path, config.extensions, config.prune_dirs):
        if verbose:
            print(f"[RAG]   Parsing: {os.path.relpath(file_path, repo_path)}")
        chunks.extend(parse_python_file(
            file_path, repo_path,
            max_file_chars=config.max_file_chars,
            min_segment_chars=config.min_segment_chars,
        ))
        file_count += 1

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
        text = load_concatenated(repo_path, config.extensions, config.prune_dirs)
        chunks = chunk_fixed(text, config.chunk_size, config.chunk_overlap)
    elif config.chunker == "ast":
        chunks = load_ast_chunks(repo_path, config, verbose)
    else:
        raise ValueError(f"Unknown chunker: {config.chunker!r}")

    return [c for c in chunks if len(c["content"]) > config.min_chunk_chars]
