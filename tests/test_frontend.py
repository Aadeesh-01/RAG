"""Run the frontend's JavaScript unit tests (frontend/tests) when Node.js is available."""

import os
import shutil
import subprocess

import pytest

FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_frontend_js():
    result = subprocess.run(["node", "--test", os.path.join("tests", "lib.test.mjs")],
                            cwd=FRONTEND, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_app_js_parses():
    result = subprocess.run(["node", "--check", "app.js"], cwd=FRONTEND,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("page", ["index.html", "app.html", "login.html"])
def test_cdn_scripts_are_pinned_with_integrity(page):
    html = open(os.path.join(FRONTEND, page), encoding="utf-8").read()
    lines = html.splitlines()
    for i, line in enumerate(lines):
        if "cdn.jsdelivr.net" in line:
            assert "@" in line.split("/npm/")[1].split("/")[0], f"unpinned: {line}"
            assert "sha384-" in lines[i + 1], f"no integrity hash: {line}"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
@pytest.mark.parametrize("script", ["auth.js", "home.js", "login.js", "hero-graph.js"])
def test_page_scripts_parse(script):
    result = subprocess.run(["node", "--check", script], cwd=FRONTEND, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
