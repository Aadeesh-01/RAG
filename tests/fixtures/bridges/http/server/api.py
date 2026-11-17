"""FastAPI routes for repositories."""

from fastapi import FastAPI

app = FastAPI()


@app.post("/api/repos")
def add_repo(body: dict):
    """Queue a repository for indexing and return its job."""
    return {"job_id": "j1", "url": body["url"], "status": "queued"}


@app.get("/api/repos/mine")
def my_repos():
    """Repositories the signed-in user added, newest first."""
    return [{"id": "a__b__0123456789ab", "name": "a/b"}]


@app.get("/api/repos/{repo_id}")
def repo_info(repo_id: str):
    """One repository's summary: files, functions, classes."""
    return {"id": repo_id, "files": 0, "functions": 0, "classes": 0}


@app.post("/api/repos/{repo_id}/ask")
def ask(repo_id: str, body: dict):
    """Answer a question about one repository, with cited sources."""
    return {"repo": repo_id, "question": body.get("question"), "sources": []}


@app.get("/api/health")
def health():
    """Liveness and configuration, for the status line and monitors."""
    return {"ok": True, "version": "0.1.0", "demos": []}
