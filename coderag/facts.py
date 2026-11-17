"""
Project-level knowledge (v10): config files as chunks, GitHub metadata as
"repo facts", and routing of project questions ("is it deployed?", "how do I
run it?", "what does it depend on?") to both.

Config chunks: one per project file (README, Procfile, Dockerfile, workflow ...),
capped at MAX_CONFIG_BYTES, language "config". `.env` and other secret files
are never read; `.env.example` is.

Repo facts: homepage, GitHub Pages, the latest deployments and environment
names, fetched from the GitHub API while indexing. Data for the prompt and the
UI, not a chunk. They say what is configured or recorded, never that a site is up.
"""

import fnmatch
import os
import re

MAX_CONFIG_BYTES = 20_000
CONFIG = "config"

# file name patterns (any folder), plus workflow files under .github/workflows/
PROJECT_FILES = ("README*", "Procfile", "Dockerfile", "docker-compose*", "requirements*.txt",
                 "pyproject.toml", "package.json", "runtime.txt", "render.yaml", "vercel.json",
                 "netlify.toml", "railway.json", "fly.toml", "app.yaml", ".env.example")
WORKFLOWS = (".github/workflows/*.yml", ".github/workflows/*.yaml")
_SECRET = re.compile(r"^\.env(\..+)?$|\.(pem|key|p12|pfx)$|secret|credential", re.I)

# platform -> files that configure a deployment on it
PLATFORMS = {
    "Render": ("render.yaml",), "Vercel": ("vercel.json",), "Netlify": ("netlify.toml",),
    "Heroku": ("Procfile", "runtime.txt", "app.json"), "Railway": ("railway.json",), "Fly.io": ("fly.toml",),
    "Google App Engine": ("app.yaml",), "Docker": ("Dockerfile", "docker-compose*"),
    "GitHub Actions": WORKFLOWS,
}


def is_project_file(rel):
    """True for a project file to index as config ('.env' and secrets never are)."""
    rel = rel.replace("\\", "/")
    name = rel.rsplit("/", 1)[-1]
    if _SECRET.search(name) and name != ".env.example":
        return False
    return any(fnmatch.fnmatch(name, p) for p in PROJECT_FILES) or any(fnmatch.fnmatch(rel, p) or
                                                                       fnmatch.fnmatch(rel, "*/" + p) for p in WORKFLOWS)


def iter_project_files(repo_path, skip_dirs, ignore_dirs=()):
    """Project files under `repo_path`; dot-folders are skipped except .github."""
    skip = set(skip_dirs) | set(ignore_dirs)
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip and (not d.startswith(".") or d in (".github", "workflows"))]
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path) and is_project_file(os.path.relpath(path, repo_path)):
                yield path


def parse_config_file(path, repo_path):
    """One "config" chunk for a project file, capped at MAX_CONFIG_BYTES."""
    from .chunking import content_hash, make_qname
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        return []
    text = data[:MAX_CONFIG_BYTES].decode("utf-8", errors="replace")
    if len(data) > MAX_CONFIG_BYTES:
        text += "\n... (truncated at 20 KB)"
    if not text.strip():
        return []
    rel = os.path.relpath(path, repo_path).replace("\\", "/")
    return [{
        "content": text, "type": "config", "file": path, "name": rel.rsplit("/", 1)[-1],
        "qname": make_qname(path, rel.rsplit("/", 1)[-1], repo_path), "start_line": 1,
        "end_line": max(text.count("\n") + 1, 1), "content_hash": content_hash(text),
        "base_classes": [], "methods": [], "calls": [], "call_sites": [], "assert_call_sites": [],
        "decorators": [], "docstring": "", "parent": "", "called_by": [], "also_in": [],
        "language": CONFIG, "imports": [],
    }]


def rel_of(chunk):
    return chunk["qname"].split("::")[0]


def platforms(chunks):
    """[{"name": "Render", "file": "render.yaml"}] for the deployment platforms configured."""
    found = []
    for name, patterns in PLATFORMS.items():
        for c in chunks:
            if c.get("type") == "config" and any(fnmatch.fnmatch(rel_of(c), p) or fnmatch.fnmatch(c["name"], p)
                                                 for p in patterns):
                found.append({"name": name, "file": rel_of(c)})
                break
    return found


# =============================================================================
# QUESTIONS
# =============================================================================
TOPICS = {
    "deploy": (r"\b(deploy\w*|hosted|hosting|is it live|live (site|url|version)|in production|"
               r"heroku|render\.com|vercel|netlify|railway|fly\.io|docker\w*)\b",
               ("Procfile", "Dockerfile", "docker-compose*", "render.yaml", "vercel.json", "netlify.toml",
                "railway.json", "fly.toml", "app.yaml", "runtime.txt", ".github/workflows/*", "README*")),
    "run": (r"\b(how (do|can|should) (i|you|we) (run|start|launch|install|set ?up|build)|install(ation|ing)?|"
            r"set ?up|setup|get(ting)? started|run (it|this|the (app|project|server|site|tests))|locally|"
            r"environment variables?|env vars?)\b",
            ("README*", ".env.example", "requirements*.txt", "pyproject.toml", "package.json", "Dockerfile",
             "docker-compose*", "Procfile")),
    "deps": (r"\b(depend\w*|requirements?|packages?|librar(y|ies)|version of python|python version|node version)\b",
             ("requirements*.txt", "pyproject.toml", "package.json", "runtime.txt")),
    "ci": (r"\b(ci|continuous integration|github actions?|workflows?|ci pipeline)\b",
           (".github/workflows/*",)),
}


def project_topics(question):
    """Topics of a project-level question ('deploy', 'run', 'deps', 'ci'); [] for a code question."""
    return [t for t, (rx, _) in TOPICS.items() if re.search(rx, question or "", re.I)]


def is_project_question(question):
    return bool(project_topics(question))


def deployment_question(question):
    return "deploy" in project_topics(question)


def config_chunks_for(question, documents, limit=4):
    """Indexes of the config chunks matching the question's topics, most specific first."""
    wanted = [p for t in project_topics(question) for p in TOPICS[t][1]]
    out = []
    for pattern in wanted:
        for i, d in enumerate(documents):
            if d.get("type") == "config" and i not in out and (
                    fnmatch.fnmatch(d["name"], pattern) or fnmatch.fnmatch(rel_of(d), pattern)
                    or fnmatch.fnmatch(rel_of(d), "*/" + pattern)):
                out.append(i)
    return out[:limit]


def facts_block(facts, documents):
    """The "REPO FACTS" text put before the sources for project questions."""
    facts = facts or {}
    lines = ["REPO FACTS (GitHub metadata and config files; not a numbered source. These record what is "
             "configured or was deployed, not whether a site is up now.)"]
    lines.append(f"Homepage (GitHub repo settings): {facts.get('homepage') or 'none'}")
    if facts.get("has_pages"):
        lines.append(f"GitHub Pages: enabled{', ' + facts['pages_url'] if facts.get('pages_url') else ''}")
    else:
        lines.append("GitHub Pages: not enabled" if "has_pages" in facts else "GitHub Pages: unknown")
    deployments = facts.get("deployments") or []
    if deployments:
        lines.append("Deployments recorded on GitHub (newest first):")
        for d in deployments:
            lines.append(f"  - {d.get('environment')}: {d.get('state') or 'no status'} on {d.get('created_at')}"
                         + (f" -> {d['environment_url']}" if d.get("environment_url") else ""))
    elif "deployments" in facts:
        lines.append("Deployments recorded on GitHub: none")
    else:
        lines.append("Deployments recorded on GitHub: unknown (not fetched)")
    if facts.get("environments"):
        lines.append(f"GitHub environments: {', '.join(facts['environments'])}")
    found = platforms(documents)
    lines.append("Deployment config files: " + (", ".join(f"{p['name']} ({p['file']})" for p in found) or "none found"))
    return "\n".join(lines)


DEPLOYMENT_RULE = (
    "- For questions about deployment or hosting, answer in two parts: (1) Configured for deployment: "
    "say which config files show it (cite them) or that none do; (2) Deployment recorded on GitHub: "
    "the deployments, homepage or Pages listed in REPO FACTS, or that none are recorded. If neither "
    "part has evidence, say it can't be determined from the repository. Never claim the site is live "
    "or reachable: a config file, a homepage or a recorded deployment doesn't show that it is up now.")


# =============================================================================
# GITHUB
# =============================================================================
def web_url(value):
    """`value` if it is an http(s) URL with a host, else "" (repo owners set these fields: no javascript: etc.)."""
    from urllib.parse import urlsplit
    if not isinstance(value, str) or len(value) > 2000:
        return ""
    value = value.strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    return value if parts.scheme in ("http", "https") and parts.hostname else ""


def fetch_github_facts(owner, repo, token=None, transport=None, timeout=5):
    """
    {homepage, has_pages, pages_url, deployments: [{environment, state, created_at,
    environment_url}] (last 5), environments: [names]} from the GitHub API.
    Missing pieces are left out; it never raises for an HTTP error.
    """
    import httpx                                   # web extra: only needed while indexing from GitHub

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "coderag"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base = f"https://api.github.com/repos/{owner}/{repo}"
    facts = {}
    with httpx.Client(headers=headers, timeout=timeout, transport=transport) as client:
        def get(path):
            try:
                r = client.get(base + path if path.startswith("/") or not path else path)
                return r.json() if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                return None

        info = get("")
        if isinstance(info, dict):
            facts["homepage"] = web_url(info.get("homepage"))
            facts["has_pages"] = bool(info.get("has_pages"))
        if facts.get("has_pages"):
            pages = get("/pages")
            if isinstance(pages, dict) and pages.get("html_url"):
                facts["pages_url"] = web_url(pages["html_url"])
        deployments = get("/deployments?per_page=5")
        if isinstance(deployments, list):
            facts["deployments"] = []
            for d in deployments[:5]:
                url = d.get("statuses_url")
                statuses = get(url) if isinstance(url, str) and url.startswith(base + "/") else None   # token: GitHub only
                latest = statuses[0] if isinstance(statuses, list) and statuses else {}
                facts["deployments"].append({
                    "environment": d.get("environment"), "state": latest.get("state"),
                    "created_at": d.get("created_at"),
                    "environment_url": web_url(latest.get("environment_url") or latest.get("target_url"))})
        environments = get("/environments")
        if isinstance(environments, dict):
            facts["environments"] = [e.get("name") for e in environments.get("environments", []) if e.get("name")]
    return facts
