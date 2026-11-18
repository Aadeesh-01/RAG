"""Azure deploy (deploy/azure), the GHCR push in CI, the image secret scan, and the proxy-hops log. No network, no Azure."""

import logging
import os
import shutil
import subprocess
import sys

import pytest

pytest.importorskip("fastapi")
yaml = pytest.importorskip("yaml")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from conftest import ROOT, HashEncoder  # noqa: E402
from coderag.web.app import create_app  # noqa: E402
from coderag.web.middleware import ProxyHopsLog  # noqa: E402
from coderag.web.settings import Settings  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import predeploy_check  # noqa: E402
import scan_image  # noqa: E402

AZURE = os.path.join(ROOT, "deploy", "azure")
SECRETS = ["CODERAG_SESSION_SECRET", "CODERAG_TOKEN_KEY", "CODERAG_GITHUB_CLIENT_SECRET", "CODERAG_DATABASE_URL",
           "GROQ_API_KEY"]


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def find_bash():
    """Git Bash or a real bash (not the WSL launcher in System32, which can't see Windows paths the same way)."""
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate and os.path.exists(candidate) and "system32" not in candidate.lower():
            return candidate
    return None


# ── the scripts parse ────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["lib.sh", "deploy.sh", "update.sh"])
def test_the_bash_scripts_parse(name):
    bash = find_bash()
    if not bash:
        pytest.skip("no bash")
    run = subprocess.run([bash, "-n", f"deploy/azure/{name}"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize("name", ["lib.ps1", "deploy.ps1", "update.ps1"])
def test_the_powershell_scripts_parse(name):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("no PowerShell")
    path = os.path.join(AZURE, name).replace("'", "''")
    script = (f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$e); "
              "$e | ForEach-Object { \"line $($_.Extent.StartLineNumber): $($_.Message)\" }; exit @($e).Count")
    run = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize("deploy", ["deploy.ps1", "deploy.sh"])
def test_both_deploy_scripts_set_up_the_same_app(deploy):
    s = read("deploy", "azure", deploy)
    for needle in ("coderag-rg", "centralindia", "coderag-env", "coderag-data", "uid=1000,gid=1000",
                   "mountPath: /data", "--min-replicas", "minReplicas: 0", "maxReplicas: 1", "--target-port",
                   "path: /api/ready", "path: /api/health", "type: Startup", "type: Readiness", "type: Liveness",
                   "failureThreshold: 10", "initialDelaySeconds: 20", "predeploy_check.py", "CODERAG_PUBLIC_URL",
                   "/api/auth/github/callback", "--enable-workload-profiles", "Standard_LRS", "--quota",
                   "secretRef", "secretref:", "CODERAG_TRUSTED_PROXY_HOPS", "HF_HUB_OFFLINE", "CODERAG_LIVE_CHECK",
                   "CODERAG_MAX_LOADED_REPOS", "/opt/demos", "/app", "proxy hops"):
        assert needle in s, needle
    for name in ("session-secret", "token-key", "github-client-secret", "database-url", "groq-api-key", *SECRETS):
        assert name in s, name
    lib = read("deploy", "azure", "lib." + deploy.rsplit(".", 1)[1])
    for needle in ("RequestDisallowedByPolicy", "southeastasia", "eastasia", "westeurope", "eastus"):
        assert needle in lib, needle


def test_the_startup_probe_allows_120_seconds():
    s = read("deploy", "azure", "deploy.sh")
    startup = s.split("type: Startup", 1)[1].split("- type:", 1)[0]
    values = dict(line.strip().split(": ", 1) for line in startup.splitlines() if line.strip().endswith(tuple("0123456789")))
    assert int(values["initialDelaySeconds"]) + int(values["periodSeconds"]) * int(values["failureThreshold"]) == 120


# ── prod.env stays out of git and the image ─────────────────────────────────
def test_prod_env_is_gitignored_and_the_template_is_not():
    if not shutil.which("git") or not os.path.isdir(os.path.join(ROOT, ".git")):
        pytest.skip("not a git checkout")

    def ignored(path):
        return subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT).returncode == 0

    assert ignored("deploy/azure/prod.env")
    assert ignored("deploy/azure/staging.env")
    assert not ignored("deploy/azure/prod.env.example")
    dockerignore = read(".dockerignore").split()
    assert "deploy/" in dockerignore and "**/*.env" in dockerignore


def test_the_template_has_every_key_and_no_values():
    values = predeploy_check.read_env_file(os.path.join(AZURE, "prod.env.example"))
    assert set(values) == set(SECRETS) | {"CODERAG_GITHUB_CLIENT_ID"}
    assert all(v == "" for v in values.values())


# ── CI: push to GHCR on main only ───────────────────────────────────────────
def test_ci_is_valid_yaml_and_pushes_only_from_main():
    ci = yaml.safe_load(read(".github", "workflows", "ci.yml"))
    triggers = ci.get("on", ci.get(True))                      # PyYAML reads the key `on` as True
    assert "push" in triggers and "pull_request" in triggers
    assert ci["permissions"] == {"contents": "read"}
    docker = ci["jobs"]["docker"]
    assert docker["needs"] == "test"
    assert docker["permissions"] == {"contents": "read", "packages": "write"}
    steps = docker["steps"]
    names = [s.get("name", s.get("uses", "")) for s in steps]

    meta = next(s for s in steps if s.get("uses", "").startswith("docker/metadata-action"))
    assert meta["with"]["images"] == "ghcr.io/${{ github.repository_owner }}/coderag"
    assert "type=sha,prefix=sha-,format=short" in meta["with"]["tags"]
    assert "type=raw,value=latest,enable={{is_default_branch}}" in meta["with"]["tags"]

    build = next(s for s in steps if s.get("uses", "").startswith("docker/build-push-action"))
    assert build["with"]["push"] is False and build["with"]["load"] is True

    gate = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    pushing = [s for s in steps if "docker push" in s.get("run", "") or s.get("uses", "").startswith("docker/login-action")
               or (s.get("with") or {}).get("push") is True]
    assert len(pushing) == 2                                   # log in, push
    assert all(s.get("if") == gate for s in pushing)
    assert pushing[0]["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"
    push_at = steps.index(pushing[-1])
    assert names.index("Smoke test (SQLite, no secrets)") < push_at
    assert names.index("No secrets in the image") < push_at
    assert "scripts/scan_image.py coderag:ci" in steps[names.index("No secrets in the image")]["run"]


# ── scripts/scan_image.py ────────────────────────────────────────────────────
def tree(tmp_path, files):
    for path, text in files.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return [line for line in scan_image.scan_files(scan_image.dir_entries(str(tmp_path)))[0]]


def test_the_scan_passes_a_clean_tree_and_names_what_it_finds(tmp_path):
    assert tree(tmp_path / "clean", {"app/coderag/x.py": "print('hi')\n", "opt/demos/d/demo.json": "{}"}) == []
    key = "gsk_" + "A1b2" * 8
    found = tree(tmp_path / "dirty", {
        "app/deploy/azure/prod.env": "X=1\n", "app/.env": "", "data/coderag.db": "",
        "app/coderag/settings.py": f"KEY = '{key}'\n",
        "opt/demos/d/src.py": "URL = 'postgresql://owner:s3cret@ep-x.neon.tech/db'\n",
        "root/.azure/msal_token_cache.json": "{}", "home/user/.ssh/id_ed25519": "",
        "app/.git/config": "",
    })
    assert len(found) == 8, found
    text = "\n".join(found)
    assert key not in text and "s3cret" not in text                       # never the value
    assert "/app/coderag/settings.py: Groq key" in text
    assert "/opt/demos/d/src.py: database URL with a password" in text
    assert "/app/deploy/azure/prod.env: an .env file" in text


def test_the_scan_checks_third_party_code_by_name_only(tmp_path):
    assert tree(tmp_path, {"opt/venv/lib/site-packages/pkg/test_keys.py": "-----BEGIN RSA PRIVATE KEY-----\n",
                           "usr/share/doc/x.db": "", "etc/x.env": ""}) == []
    assert tree(tmp_path / "b", {"opt/venv/.git-credentials": ""}) == ["/opt/venv/.git-credentials: a credentials file"]


def test_the_scan_checks_env_and_build_history():
    assert scan_image.scan_config(["PATH=/opt/venv/bin", "GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305",
                                   "CODERAG_TRUSTED_PROXY_HOPS=1", "HF_HUB_OFFLINE=1"],
                                  ["RUN pip install -r /tmp/requirements.lock"]) == []
    found = scan_image.scan_config(["CODERAG_SESSION_SECRET=abc", "GROQ_API_KEY=", "X=ghp_" + "a" * 36],
                                   ["ENV T=hf_" + "b" * 34])
    assert found == ["ENV CODERAG_SESSION_SECRET: has a value", "ENV X: GitHub token", "build history step 0: Hugging Face token"]


def test_the_files_that_go_into_the_image_are_clean():
    """What the Dockerfile copies into /app (and the local demos, as /opt/demos) passes the scan CI runs."""
    entries = []
    for top in ("coderag", "frontend", "scripts", "sample_repo", "demos.yaml", "pyproject.toml"):
        full = os.path.join(ROOT, top)
        if os.path.isfile(full):
            entries.append((f"app/{top}", lambda f=full: open(f, "rb").read()))
            continue
        for rel, read_file in scan_image.dir_entries(full):
            if "__pycache__" not in rel and "node_modules" not in rel and not rel.startswith("tests"):
                entries.append((f"app/{top}/{rel}", read_file))
    demos = os.path.join(ROOT, "data", "demos")
    if os.path.isdir(demos):
        entries += [(f"opt/demos/{rel}", r) for rel, r in scan_image.dir_entries(demos)]
    findings, files = scan_image.scan_files(entries)
    assert findings == [] and files > 50


# ── the one-time proxy-hops log ──────────────────────────────────────────────
def hops_lines(caplog, hops, requests):
    app = FastAPI()
    app.get("/api/ready")(lambda: {"ready": True})
    app.get("/x")(lambda: {"ok": True})
    app.add_middleware(ProxyHopsLog, trusted_proxy_hops=hops)
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="coderag.web"):
        for path, forwarded in requests:
            assert client.get(path, headers={"X-Forwarded-For": forwarded} if forwarded else {}).status_code == 200
    return [r.getMessage() for r in caplog.records if "proxy hops" in r.getMessage()]


def test_the_first_request_logs_the_chain_length_once_without_addresses(caplog):
    lines = hops_lines(caplog, 1, [("/api/ready", None), ("/x", "203.0.113.9"), ("/x", "198.51.100.1, 203.0.113.9")])
    assert lines == ["proxy hops: X-Forwarded-For has 1 entry on the first request, CODERAG_TRUSTED_PROXY_HOPS=1 "
                     "(ok: the client address is entry -1)"]


@pytest.mark.parametrize("hops, forwarded, expected", [
    (1, None, "has 0 entries"),
    (1, None, "too few entries"),
    (1, "198.51.100.1, 203.0.113.9", "more entries than trusted hops"),
    (0, "203.0.113.9", "trusted hops is 0"),
])
def test_the_log_says_what_the_chain_means(caplog, hops, forwarded, expected):
    [line] = hops_lines(caplog, hops, [("/x", forwarded)])
    assert expected in line and "203.0.113.9" not in line and "198.51.100.1" not in line


def test_the_server_has_the_proxy_hops_log(tmp_path):
    app = create_app(Settings(data_dir=str(tmp_path), trusted_proxy_hops=1), demos={}, llm_factory=lambda c: None,
                     embedder=HashEncoder())
    [m] = [m for m in app.user_middleware if m.cls is ProxyHopsLog]
    assert m.kwargs == {"trusted_proxy_hops": 1}
