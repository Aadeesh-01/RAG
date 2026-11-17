"""
Reading what coderag kept before the database: one demo.json per visitor repo
under <data_dir>/repos/<repo_id>/ (with the index beside it in <preset>/).
Used by the first migration and, idempotently, at server start.
"""

import json
import os
from datetime import datetime, timezone


def parse_time(value):
    """An ISO timestamp (old rows: '2026-11-16T20:28:20+00:00') as naive UTC, or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.replace(tzinfo=None)


def repo_rows(data_dir):
    """Repo table rows for every visitor repo saved on disk with its demo.json and an index."""
    root = os.path.join(data_dir, "repos")
    rows = []
    if not os.path.isdir(root):
        return rows
    for repo_id in sorted(os.listdir(root)):
        folder = os.path.join(root, repo_id)
        try:
            with open(os.path.join(folder, "demo.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        versions = meta.get("versions") or []
        index_path = next((os.path.join(folder, v) for v in reversed(versions)
                           if os.path.isfile(os.path.join(folder, v, "meta.json"))), "")
        if not index_path:
            continue                                   # no usable index: nothing to serve
        name = meta.get("name") or repo_id
        stats = meta.get("stats") or {}
        rows.append({
            "id": repo_id, "kind": "user", "owner": name.split("/")[0] if "/" in name else "",
            "name": name, "url": meta.get("url", ""), "commit": meta.get("sha", ""), "ref": meta.get("ref", ""),
            "description": meta.get("description", ""), "private": bool(meta.get("private")),
            "indexed_with": meta.get("indexed_with") or (versions[-1] if versions else ""),
            "languages": stats.get("languages", {}), "stats": stats, "facts": meta.get("facts") or {},
            "index_path": index_path, "owner_id": meta.get("owner_id"),
            "created_at": parse_time(meta.get("built")) or datetime.now(timezone.utc).replace(tzinfo=None),
            "built_at": parse_time(meta.get("built")),
        })
    return rows
