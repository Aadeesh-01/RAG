"""
Whole source files for the code viewer.

An index keeps chunks, not files (chunks skip imports and module-level code,
and class chunks are outlines), so a file is read from, in order:

  1. the copy saved beside the index the first time it was read
  2. the local checkout, if the index was built from one that still exists
  3. GitHub, at the exact commit the index was built from

Only files that are in the index can be read, so a path from a request never
reaches the filesystem or GitHub unless it is one the indexer already saw.
"""

import os
import re
import urllib.parse
import urllib.request

from ..context import source_location

MAX_FILE_BYTES = 1_000_000
_GITHUB = re.compile(r"^https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class SourceUnavailable(Exception):
    """The file is indexed but its text can't be read right now."""


def indexed_files(agent):
    """Repo-relative paths of the indexed files (the paths the UI shows)."""
    return {source_location(d)[0] for d in agent.documents if "qname" in d}


def _safe_join(root, rel):
    path = os.path.normpath(os.path.join(root, rel))
    if os.path.commonpath([os.path.abspath(root), os.path.abspath(path)]) != os.path.abspath(root):
        raise ValueError(f"path escapes {root!r}")
    return path


def _read(path):
    with open(path, "rb") as f:
        data = f.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise SourceUnavailable("This file is too large to show.")
    return _text(data)


def _text(data):
    """Decoded text with \\n line endings (the viewer numbers lines by \\n)."""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def raw_url(demo, rel):
    """raw.githubusercontent.com URL of `rel` at the pinned commit, or None."""
    m = _GITHUB.match(demo.url or "")
    if not m or not _SHA.match(demo.sha or ""):
        return None
    full = f"{demo.root.strip('/')}/{rel}" if demo.root else rel
    quoted = "/".join(urllib.parse.quote(part) for part in full.split("/"))
    return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{demo.sha}/{quoted}"


def fetch_url(url, timeout=10):
    request = urllib.request.Request(url, headers={"User-Agent": "coderag"})
    with urllib.request.urlopen(request, timeout=timeout) as response:     # noqa: S310 (fixed host)
        data = response.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise SourceUnavailable("This file is too large to show.")
    return _text(data)


def read_source(demo, agent, rel, cache_dir, fetch=fetch_url):
    """(text, origin) for indexed file `rel`; origin is "cache" | "local" | "github"."""
    if rel not in indexed_files(agent):
        raise KeyError(rel)
    cached = _safe_join(cache_dir, rel)
    if os.path.isfile(cached):
        return _read(cached), "cache"

    text, origin = None, None
    if agent.repo_path and os.path.isdir(agent.repo_path):
        local = _safe_join(agent.repo_path, rel)
        if os.path.isfile(local):
            text, origin = _read(local), "local"
    if text is None:
        url = raw_url(demo, rel)
        if url is None:
            raise SourceUnavailable("The source of this repository isn't available.")
        try:
            text, origin = fetch(url), "github"
        except SourceUnavailable:
            raise
        except Exception as e:                                  # network, 404, timeout
            raise SourceUnavailable("Couldn't load this file from GitHub. Try again in a moment.") from e
        os.makedirs(os.path.dirname(cached), exist_ok=True)     # keep it with the index
        tmp = f"{cached}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, cached)
    return text, origin
