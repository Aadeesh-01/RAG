"""
Check a deployment's settings before (or right after) going live; prints a pass/fail table.

    python scripts/predeploy_check.py                       # the CODERAG_* environment as the server sees it
    python scripts/predeploy_check.py --env-file prod.env   # or from a file (KEY=value lines)
    python scripts/predeploy_check.py --database-url "$NEON_DIRECT_URL"   # migrate over the direct URL

Checks: the start-up rules (the same as the server's, Settings.validate), the server's
LLM key, connecting to the database, running the migrations (to the head revision),
the demos, and that the data directory is writable. Secrets are never printed.
Exit code 0 when nothing failed (warnings are allowed), else 1.
"""

import argparse
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from coderag.db import Database, default_url, safe_url  # noqa: E402
from coderag.web.settings import Settings  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def read_env_file(path):
    """KEY=value lines (# comments, optional quotes, optional `export `) -> dict."""
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def check(environ, database_url=None):
    """[(status, check, detail)] for `environ` (a dict of environment variables)."""
    rows = []
    settings = Settings.from_env(environ)
    errors, warnings = settings.problems()
    rows += [(FAIL, "settings", e) for e in errors]
    rows += [(WARN, "settings", w) for w in warnings]
    if not errors:
        rows.append((PASS, "settings", "the start-up rules pass"))

    signed_in = bool(settings.github_client_id)
    rows.append((PASS if signed_in else WARN, "GitHub sign-in",
                 "configured" if signed_in else "off: visitors can't add repositories"))
    rows.append((PASS if settings.public_url else (FAIL if signed_in else WARN), "CODERAG_PUBLIC_URL",
                 (f"{settings.public_url} (OAuth callback: {settings.public_url.rstrip('/')}/api/auth/github/callback)"
                  if settings.public_url else "not set: the OAuth callback would be built from the proxied request")))

    key_var = {"groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY",
               "gemini": "GEMINI_API_KEY"}.get(settings.server_llm_provider, "")
    rows.append((PASS if environ.get(key_var) else WARN, key_var or "server LLM",
                 "set" if environ.get(key_var) else "not set: answers need the visitor's own key"))

    url = database_url or settings.database_url or default_url(settings.data_dir)
    if not (database_url or settings.database_url):
        rows.append((WARN, "database", f"SQLite in the data dir ({safe_url(url)}): set CODERAG_DATABASE_URL "
                                       "to keep users and history when the disk resets"))
    try:
        db = Database(url)
        db.ping()
        rows.append((PASS, "database connection", safe_url(db.url)))
        try:
            db.upgrade()
            from alembic.script import ScriptDirectory
            head = ScriptDirectory.from_config(db.alembic_config()).get_current_head()
            current = db.current()
            rows.append((PASS if current == head else FAIL, "migrations", f"at {current} (head {head})"))
        except Exception as e:                      # noqa: BLE001
            rows.append((FAIL, "migrations", f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"))
        db.dispose()
    except Exception as e:                          # noqa: BLE001
        rows.append((FAIL, "database connection", f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"))

    demos = sorted(d for d in (os.listdir(settings.demos_path) if os.path.isdir(settings.demos_path) else [])
                   if os.path.isfile(os.path.join(settings.demos_path, d, "demo.json")))
    rows.append((PASS if demos else WARN, "demos", f"{', '.join(demos)} in {settings.demos_path}" if demos
                 else f"none in {settings.demos_path}: run scripts/build_demos.py"))

    try:
        os.makedirs(settings.data_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=settings.data_dir):
            pass
        rows.append((PASS, "data dir", f"{os.path.abspath(settings.data_dir)} is writable"))
    except OSError as e:
        rows.append((FAIL, "data dir", f"{settings.data_dir}: {e.strerror or e}"))
    return rows


def table(rows):
    width = max(len(name) for _, name, _ in rows)
    lines = [f"{'':4}  {'check':<{width}}  detail", f"{'':-<4}  {'':-<{width}}  {'':-<40}"]
    lines += [f"{status:4}  {name:<{width}}  {detail}" for status, name, detail in rows]
    failed = sum(1 for s, _, _ in rows if s == FAIL)
    warned = sum(1 for s, _, _ in rows if s == WARN)
    lines.append(f"\n{'FAILED' if failed else 'OK'}: {failed} failed, {warned} warning(s), "
                 f"{len(rows) - failed - warned} passed")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", help="read the settings from this file instead of the environment")
    parser.add_argument("--database-url", help="connect and migrate over this URL (e.g. Neon's direct URL)")
    args = parser.parse_args(argv)
    environ = read_env_file(args.env_file) if args.env_file else dict(os.environ)
    rows = check(environ, args.database_url)
    print(table(rows))
    return 1 if any(s == FAIL for s, _, _ in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
