"""Deployment files: the predeploy check, the lock, the Dockerfile, .dockerignore, CI. No network, no Docker."""

import os
import re
import subprocess
import sys

import pytest

pytest.importorskip("fastapi")
from conftest import AUTH_SETTINGS, ROOT  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import predeploy_check  # noqa: E402
from sqlalchemy import text  # noqa: E402

from coderag.db import Database  # noqa: E402


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def env(tmp_path, **extra):
    demos = tmp_path / "demos" / "sample"
    demos.mkdir(parents=True, exist_ok=True)
    (demos / "demo.json").write_text("{}")
    return {"CODERAG_DATA_DIR": str(tmp_path / "data"), "CODERAG_DEMOS_DIR": str(tmp_path / "demos"),
            "CODERAG_DATABASE_URL": f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
            "CODERAG_GITHUB_CLIENT_ID": AUTH_SETTINGS["github_client_id"],
            "CODERAG_GITHUB_CLIENT_SECRET": AUTH_SETTINGS["github_client_secret"],
            "CODERAG_SESSION_SECRET": AUTH_SETTINGS["session_secret"], "CODERAG_TOKEN_KEY": AUTH_SETTINGS["token_key"],
            "CODERAG_PUBLIC_URL": "https://coderag.example.com", "CODERAG_TRUSTED_PROXY_HOPS": "1",
            "GROQ_API_KEY": "gsk_" + "x" * 20, **extra}


def statuses(rows):
    return {name: status for status, name, _ in rows}


# ── scripts/predeploy_check.py ───────────────────────────────────────────────
def test_a_complete_production_environment_passes_without_migrating(tmp_path):
    values = env(tmp_path)
    Database(values["CODERAG_DATABASE_URL"]).upgrade()              # what the deployed server does on start
    rows = predeploy_check.check(values)
    assert all(s == "PASS" for s, _, _ in rows), rows
    detail = dict((n, d) for _, n, d in rows)
    assert detail["migrations"] == "at 0004 (head 0004; not migrated by this check)"
    assert "https://coderag.example.com/api/auth/github/callback" in detail["CODERAG_PUBLIC_URL"]
    text = predeploy_check.table(rows)
    assert text.splitlines()[-1].startswith("OK: 0 failed")
    for secret in (AUTH_SETTINGS["session_secret"], AUTH_SETTINGS["token_key"], "gsk_" + "x" * 20):
        assert secret not in text                                    # secrets are never printed


def test_the_check_never_migrates(tmp_path):
    """Only the deployed image migrates (on start): a checkout ahead of it must not move the database."""
    values = env(tmp_path)
    url = values["CODERAG_DATABASE_URL"]

    def migrations():
        [(status, detail)] = [(s, d) for s, n, d in predeploy_check.check(values) if n == "migrations"]
        return status, detail

    status, detail = migrations()                                   # an empty database stays empty
    assert status == "WARN" and "no schema yet" in detail and "creates it when it starts" in detail
    assert Database(url).current() is None

    Database(url).upgrade("0002")                                   # behind this checkout: reported, not upgraded
    status, detail = migrations()
    assert status == "WARN" and detail.startswith("at 0002, behind this checkout's head 0004")
    assert Database(url).current() == "0002"

    db = Database(url)                                              # ahead of this checkout (a newer image migrated it)
    db.upgrade()
    with db.engine.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    status, detail = migrations()
    assert status == "WARN" and "a revision this checkout doesn't have" in detail
    assert Database(url).current() == "9999"


def test_the_startup_rules_fail_one_row_each(tmp_path):
    rows = predeploy_check.check(env(tmp_path, CODERAG_SESSION_SECRET="short", CODERAG_TRUSTED_PROXY_HOPS="0"))
    failed = [d for s, n, d in rows if s == "FAIL" and n == "settings"]
    assert len(failed) == 2
    assert any("CODERAG_SESSION_SECRET must be at least 32" in d for d in failed)
    assert any("CODERAG_TRUSTED_PROXY_HOPS is 0" in d for d in failed)


def test_an_unreachable_database_fails(tmp_path):
    rows = predeploy_check.check(env(tmp_path), database_url="postgresql://u:hunter2@127.0.0.1:1/x?connect_timeout=2")
    [(status, detail)] = [(s, d) for s, n, d in rows if n == "database connection"]
    assert status == "FAIL" and "hunter2" not in predeploy_check.table(rows)


def test_missing_pieces_are_warnings(tmp_path):
    values = {"CODERAG_DATA_DIR": str(tmp_path / "data"), "CODERAG_DEMOS_DIR": str(tmp_path / "none")}
    s = statuses(predeploy_check.check(values))
    assert s["GitHub sign-in"] == s["GROQ_API_KEY"] == s["demos"] == s["database"] == s["migrations"] == "WARN"
    assert s["settings"] == s["data dir"] == "PASS"


def test_env_files_and_the_exit_code(tmp_path):
    good = tmp_path / "good.env"
    good.write_text("\n".join(["# production", *[f"export {k}='{v}'" for k, v in env(tmp_path).items()]]) + "\n")
    assert predeploy_check.read_env_file(good)["CODERAG_TRUSTED_PROXY_HOPS"] == "1"
    run = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "predeploy_check.py"), "--env-file", str(good)],
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    bad = tmp_path / "bad.env"
    bad.write_text("CODERAG_PUBLIC_URL=http://coderag.example.com\n")
    run = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "predeploy_check.py"), "--env-file", str(bad)],
                         capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert run.returncode == 1 and "must be https://" in run.stdout


# ── dependency files ─────────────────────────────────────────────────────────
def test_requirements_run_the_server_and_leave_dev_tools_out():
    names = {re.split(r"[<>=\[ ]", line)[0].lower() for line in read("requirements.txt").splitlines()
             if line.strip() and not line.startswith("#")}
    assert {"fastapi", "uvicorn"} <= names and "pytest" not in names
    # uvicorn's fast parser and loop, without the [standard] extras the server never uses
    assert {"httptools", "uvloop"} <= names and "uvicorn[standard]" not in read("requirements.txt")
    dev = read("requirements-dev.txt")
    assert "-r requirements.txt" in dev and "pytest" in dev


def test_the_lock_is_hashed_and_cpu_only():
    lock = read("requirements.lock")
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==(\S+) \\$", lock, re.M)
    assert len(pins) > 40
    names = {n.lower() for n, _ in pins}
    assert dict(pins)["torch"].endswith("+cpu")
    assert not {n for n in names if n.startswith("nvidia") or n == "triton"}
    assert {"fastapi", "uvicorn", "sentence-transformers", "psycopg-binary", "tree-sitter-language-pack"} <= names
    assert "--extra-index-url https://download.pytorch.org/whl/cpu" in lock
    blocks = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", lock.split("\n\n", 1)[1])
    assert all("--hash=sha256:" in b for b in blocks if "==" in b.split("\n")[0])
    assert re.search(r"^tree-sitter-language-pack==1\.25\.2 ", lock, re.M)        # the pyproject pin


# ── Dockerfile, .dockerignore, Space header, CI ─────────────────────────────
def test_the_dockerfile_bakes_everything_and_runs_one_worker():
    d = read("Dockerfile")
    assert d.count("FROM python:3.12-slim") == 2                                 # build + runtime
    assert "pip install --require-hashes -r /tmp/requirements.lock" in d
    assert "useradd --create-home --uid 1000 user" in d and "\nUSER user\n" in d
    assert "SentenceTransformer('all-MiniLM-L6-v2')" in d and "prefetch()" in d
    assert "--demos-dir /opt/demos" in d and "CODERAG_DEMOS_DIR=/opt/demos" in d
    assert "HF_HOME=/opt/hf HF_HUB_OFFLINE=1" in d and "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=/opt/tslp" in d
    assert d.index("HF_HUB_OFFLINE=1") > d.index("SentenceTransformer(")      # offline only after the download
    cmd = re.search(r'^CMD \[.*\]$', d, re.M).group(0)
    for flag in ("uvicorn coderag.web.app:app", "--host 0.0.0.0", "--port ${PORT:-7860}", "--workers 1",
                 "--no-server-header", "--proxy-headers"):
        assert flag in cmd, flag
    assert re.search(r"HEALTHCHECK .*--start-period=90s", d) and "/api/ready" in d
    assert "git ca-certificates" in d


def test_dockerignore_keeps_secrets_and_state_out():
    ignored = set(read(".dockerignore").split())
    assert {".env", "data/", ".venv/", "eval/.repos/", ".git/", ".hallmark/", ".impeccable/", "tests/",
            "__pycache__/"} <= ignored


def test_the_space_header_is_separate_from_the_github_readme():
    space = read("deploy/huggingface/README.md")
    header = space.split("---")[1]
    assert "sdk: docker" in header and "app_port: 7860" in header
    assert not read("README.md").startswith("---")


def test_ci_tests_then_builds_without_pushing():
    ci = read(".github/workflows/ci.yml")
    assert "needs: test" in ci and "push: false" in ci and "pytest -q" in ci
    assert "requirements.lock" in ci
    assert "actions/workflows/ci.yml/badge.svg" in read("README.md")
