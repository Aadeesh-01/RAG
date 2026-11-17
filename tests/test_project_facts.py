"""Project-level questions (v10): config chunks, repo facts from GitHub, routing, the live check."""

import json
import os
import socket

import pytest

httpx = pytest.importorskip("httpx")

from coderag import RAGCodebaseAgent, get_config  # noqa: E402
from coderag.chunking import load_chunks  # noqa: E402
from coderag.facts import (MAX_CONFIG_BYTES, fetch_github_facts, is_project_file, platforms,  # noqa: E402
                           project_topics)
from coderag.llm import build_messages  # noqa: E402
from coderag.web.live import LiveChecker, Unsafe, candidate_urls, check_target  # noqa: E402
from conftest import SAMPLE_REPO, FakeIndex  # noqa: E402

V10 = get_config("v10")


@pytest.fixture
def project(tmp_path):
    """sample_repo's code plus deployment and setup files, a secret .env and an oversized README."""
    import shutil
    shutil.copytree(SAMPLE_REPO, tmp_path / "repo")
    root = tmp_path / "repo"
    (root / "README.md").write_text("# Zoo\n\nRun it with `python main.py`.\n" + "x" * (MAX_CONFIG_BYTES + 500))
    (root / "Procfile").write_text("web: gunicorn app:app\n")
    (root / "render.yaml").write_text("services:\n  - type: web\n    name: zoo\n    startCommand: gunicorn app:app\n")
    (root / "requirements.txt").write_text("flask==3.0.0\ngunicorn==22.0.0\n")
    (root / ".env").write_text("SECRET_KEY=do-not-index-me\nGROQ_API_KEY=gsk_secret\n")
    (root / ".env.example").write_text("SECRET_KEY=change-me\nGROQ_API_KEY=\n")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n")
    return str(root)


# ── config chunks ─────────────────────────────────────────────────────────
def test_project_files_and_secrets():
    for name in ["README.md", "Procfile", "Dockerfile", "docker-compose.prod.yml", "requirements-dev.txt",
                 "pyproject.toml", "package.json", "runtime.txt", "render.yaml", "vercel.json", "netlify.toml",
                 "railway.json", "fly.toml", "app.yaml", ".env.example", ".github/workflows/ci.yml", "web/Dockerfile"]:
        assert is_project_file(name), name
    for name in [".env", ".env.local", ".env.production", "secrets.json", "server.pem", "id.key", "main.py",
                 "config.yaml", ".github/dependabot.yml"]:
        assert not is_project_file(name), name


def test_config_chunks_only_with_v10(project):
    chunks = {c["qname"].split("::")[0]: c for c in load_chunks(project, V10, verbose=False) if c["type"] == "config"}
    assert set(chunks) == {"README.md", "Procfile", "render.yaml", "requirements.txt", ".env.example",
                           ".github/workflows/ci.yml"}
    assert all(c["language"] == "config" for c in chunks.values())
    assert ".env" not in chunks and not any("do-not-index-me" in c["content"] for c in chunks.values())
    readme = chunks["README.md"]["content"]
    assert len(readme.encode()) <= MAX_CONFIG_BYTES + 40 and readme.endswith("(truncated at 20 KB)")
    for version in ("v8", "v9"):
        assert not any(c["type"] == "config" for c in load_chunks(project, get_config(version), verbose=False))
    assert {p["name"] for p in platforms(list(chunks.values()))} == {"Render", "Heroku", "GitHub Actions"}


# ── GitHub metadata ───────────────────────────────────────────────────────
def github_api(seen):
    def handler(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        path = request.url.path
        if path == "/repos/o/r":
            return httpx.Response(200, json={"homepage": "https://zoo.example.com", "has_pages": True})
        if path == "/repos/o/r/pages":
            return httpx.Response(200, json={"html_url": "https://o.github.io/r/"})
        if path == "/repos/o/r/deployments":
            assert request.url.params["per_page"] == "5"
            return httpx.Response(200, json=[
                {"environment": "production", "created_at": f"2026-11-0{i}T10:00:00Z",
                 "statuses_url": f"https://api.github.com/repos/o/r/deployments/{i}/statuses"} for i in range(1, 8)])
        if path.endswith("/statuses"):
            return httpx.Response(200, json=[{"state": "success", "environment_url": "https://zoo.example.com"}])
        if path == "/repos/o/r/environments":
            return httpx.Response(200, json={"environments": [{"name": "production"}, {"name": "preview"}]})
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


def test_github_facts():
    seen = []
    facts = fetch_github_facts("o", "r", token="gho_private", transport=github_api(seen))
    assert facts["homepage"] == "https://zoo.example.com" and facts["has_pages"] is True
    assert facts["pages_url"] == "https://o.github.io/r/"
    assert len(facts["deployments"]) == 5
    assert facts["deployments"][0] == {"environment": "production", "state": "success",
                                       "created_at": "2026-11-01T10:00:00Z", "environment_url": "https://zoo.example.com"}
    assert facts["environments"] == ["production", "preview"]
    assert all(auth == "Bearer gho_private" for _, auth in seen)
    # no token: anonymous requests; GitHub errors leave the pieces out
    seen.clear()
    assert fetch_github_facts("x", "y", transport=github_api(seen)) == {}
    assert all(auth is None for _, auth in seen)


def test_indexer_stores_facts_with_the_index(tmp_path):
    pytest.importorskip("fastapi")
    from test_indexing import make_app, wait
    calls = []
    client, app, _ = make_app(tmp_path)
    app.state.indexer.facts = lambda owner, repo, token=None: calls.append((owner, repo, token)) or {
        "homepage": "https://pkg.example.com", "deployments": []}
    job = wait(client, client.post("/api/repos", json={"url": "someone/pkg"}).json()["job_id"])
    assert calls == [("someone", "pkg", None)]                      # public repo: no token
    info = client.get(f"/api/repos/{job['repo_id']}").json()
    assert info["facts"]["homepage"] == "https://pkg.example.com" and info["facts"]["platforms"] == []
    saved = json.loads((tmp_path / "repos" / job["repo_id"] / "demo.json").read_text())
    assert saved["facts"]["homepage"] == "https://pkg.example.com"
    version = saved["versions"][0]
    assert json.loads((tmp_path / "repos" / job["repo_id"] / version / "facts.json").read_text())["homepage"]


# ── routing and the prompt ────────────────────────────────────────────────
def test_project_questions_are_recognised():
    assert project_topics("Is it deployed anywhere?") == ["deploy"]
    assert project_topics("How do I run it locally?") == ["run"]
    assert project_topics("What does it depend on?") == ["deps"]
    assert project_topics("Does it have CI?") == ["ci"]
    for code in ["Who calls run_pipeline?", "How is the url parsed?", "What does Bat inherit from?"]:
        assert project_topics(code) == [], code


def test_project_questions_get_config_sources_and_facts(project):
    agent = RAGCodebaseAgent(project, V10, index=FakeIndex(), load_llm=False, verbose=False)
    agent.facts = {"homepage": "https://zoo.example.com", "has_pages": False, "deployments": [
        {"environment": "production", "state": "success", "created_at": "2026-11-01", "environment_url": ""}]}
    first = next(agent.stream("Is it deployed?", llm=None, prompt_style="cited"))
    assert first["repo_facts"] is True
    context = first["context"]
    assert context.startswith("REPO FACTS") and "production: success on 2026-11-01" in context
    assert context.index("REPO FACTS") < context.index("[S1]")
    assert "Render (render.yaml)" in context and "Heroku (Procfile)" in context
    kinds = [s["kind"] for s in first["sources"]]
    assert kinds[0] == "config" and {"Procfile", "render.yaml"} <= {s["name"] for s in first["sources"]}
    config = [s for s in first["sources"] if s["kind"] == "config"]
    assert all(s["language"] == "config" and s["hop"] == 0 for s in config)
    # the deployment rule is in the system prompt only for these questions
    system = build_messages(get_config("v10", prompt_style="cited"), context, "Is it deployed?")[0]["content"]
    assert "Configured for deployment" in system and "Never claim the site is live" in system
    code = next(agent.stream("What does DataProcessor.transform do?", llm=None))
    assert code["repo_facts"] is False and "REPO FACTS" not in code["context"]
    assert "Never claim" not in build_messages(get_config("v10", prompt_style="cited"), code["context"], "q")[0]["content"]


def test_older_versions_never_route(project):
    agent = RAGCodebaseAgent(project, get_config("v9"), index=FakeIndex(), load_llm=False, verbose=False)
    first = next(agent.stream("Is it deployed?", llm=None))
    assert first["repo_facts"] is False and not any(s["kind"] == "config" for s in first["sources"])


# ── live check ────────────────────────────────────────────────────────────
def resolver(ip):
    return lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.0.10", "172.16.5.5",
                                "0.0.0.0", "::1", "fe80::1", "::ffff:127.0.0.1"])
def test_private_addresses_are_refused(ip):
    with pytest.raises(Unsafe):
        check_target("https://looks-public.example.com/", resolver(ip))


def test_only_https_and_literal_private_ips_refused():
    for url in ["http://example.com/", "https://127.0.0.1/", "https://10.0.0.1/admin",
                "https://169.254.169.254/latest/meta-data/", "https://user:pw@example.com/", "ftp://example.com/"]:
        with pytest.raises(Unsafe):
            check_target(url, socket.getaddrinfo if "example" not in url else resolver("93.184.216.34"))
    assert check_target("https://example.com/", resolver("93.184.216.34")) == {"93.184.216.34"}


def test_live_check_status_redirects_and_cache():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        assert request.method == "HEAD"
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://site.example.com/new"})
        if request.url.path == "/evil":
            return httpx.Response(302, headers={"location": "https://10.0.0.1/admin"})
        return httpx.Response(200)

    clock = [0.0]
    checker = LiveChecker(transport=httpx.MockTransport(handler), resolve=resolver("93.184.216.34"),
                          clock=lambda: clock[0])
    result = checker.check("https://site.example.com/old")
    assert result["status"] == 200 and result["ok"] and result["final_url"].endswith("/new")
    assert result["cached"] is False and result["ms"] >= 0
    assert checker.check("https://site.example.com/old")["cached"] is True and len(hits) == 2
    clock[0] += 601
    checker.check("https://site.example.com/old")
    assert len(hits) == 4                                           # cache expired after 10 min
    # a redirect to a private address is not followed
    evil = checker.check("https://site.example.com/evil")
    assert evil.get("unsafe") is True and not any("10.0.0.1" in h for h in hits)


def test_live_check_timeout():
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)
    checker = LiveChecker(transport=httpx.MockTransport(handler), resolve=resolver("93.184.216.34"))
    assert checker.check("https://slow.example.com/")["error"] == "No response within 5 s."


def test_live_endpoint(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from coderag.web.app import create_app
    from coderag.web.limits import RateLimiter
    from coderag.web.registry import Demo
    from coderag.web.settings import Settings
    from conftest import HashEncoder

    agent = RAGCodebaseAgent(SAMPLE_REPO, get_config("v8"), index=FakeIndex(), load_llm=False, verbose=False)
    with_url = Demo(id="zoo", name="Zoo", facts={"homepage": "https://zoo.example.com"}, agents={"v8": agent})
    without = Demo(id="bare", name="Bare", agents={"v8": agent})
    checked = []

    class Fake:
        def check(self, url):
            checked.append(url)
            return {"url": url, "status": 200, "ok": True, "ms": 12, "cached": False}

    def make(**settings):
        return TestClient(create_app(Settings(data_dir=str(tmp_path), **settings), demos={"zoo": with_url, "bare": without},
                                     llm_factory=lambda c: None, limiter=RateLimiter(), embedder=HashEncoder(),
                                     live=Fake()))

    off = make()                                                        # CODERAG_LIVE_CHECK is off by default
    assert off.post("/api/repos/zoo/live").status_code == 404 and checked == []
    assert off.get("/api/health").json()["live_check"] is False
    client = make(live_check=True)
    assert client.get("/api/health").json()["live_check"] is True
    assert client.post("/api/repos/zoo/live").json()["status"] == 200 and checked == ["https://zoo.example.com"]
    assert client.post("/api/repos/bare/live").status_code == 404
    assert candidate_urls({"deployments": [{"environment_url": "https://d.example.com"}],
                           "pages_url": "https://p.example.com"}) == ["https://d.example.com", "https://p.example.com"]
    for _ in range(30):
        client.post("/api/repos/zoo/live")
    assert client.post("/api/repos/zoo/live").status_code == 429


def test_the_checked_address_is_the_one_connected_to():
    """DNS rebinding: public on the first lookup, loopback afterwards. Only the first is ever used."""
    answers = ["93.184.216.34", "127.0.0.1", "127.0.0.1"]
    lookups, seen = [], []

    def rebinding(host, port, type=0):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[min(len(lookups) - 1, 2)], port))]

    def handler(request):
        seen.append((request.url.host, request.headers["host"], request.extensions.get("sni_hostname")))
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": "https://site.example.com/next"})
        return httpx.Response(200)

    checker = LiveChecker(transport=httpx.MockTransport(handler), resolve=rebinding)
    result = checker.check("https://site.example.com/")
    assert result["status"] == 200 and len(lookups) == 1
    assert seen == [("93.184.216.34", "site.example.com", "site.example.com")]   # pinned IP, original Host/SNI
    # a redirect is resolved again: the first hop gets the public address, the second 127.0.0.1,
    # which is refused, so the only connection made is to the public address
    lookups.clear()
    seen.clear()
    hop = LiveChecker(transport=httpx.MockTransport(handler), resolve=rebinding).check("https://site.example.com/hop")
    assert hop.get("unsafe") is True and len(lookups) == 2
    assert seen == [("93.184.216.34", "site.example.com", "site.example.com")]


def test_live_check_setting_from_env():
    from coderag.web.settings import Settings
    assert Settings().live_check is False
    assert Settings.from_env({"CODERAG_LIVE_CHECK": "true"}).live_check is True
    assert Settings.from_env({"CODERAG_LIVE_CHECK": "0"}).live_check is False
