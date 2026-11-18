"""
The signed-in user's Home page, in one call:

  GET /api/home   {last_repo, last_question, recent_questions, repos, stats}

  last_repo         the repo they opened last (else the one added last), or null
  last_question     their newest question about last_repo (with the start of its answer), or null
  recent_questions  their last RECENT questions across all repos, newest first
  repos             their repos, last opened (else added) first, each with its newest indexing
                    job (status and progress) and reindex_to when an older preset built it;
                    a repo whose first indexing is still running comes from its job alone
  stats             questions_week, per_day (the last 7 days, oldest first), repos, bridges

Only the current user's rows are read (their repo links, their questions, their jobs);
nothing is cached, and nothing goes into the shared answer cache. Signed out: 401.
"""

from datetime import timedelta

from fastapi import HTTPException, Request

from ..db.models import Question, Repo, utcnow
from .history import iso
from .registry import is_older

RECENT = 8
ANSWER_PREVIEW = 600                      # characters of the last answer (Home shows its first two lines)
REPO_KEYS = ("id", "name", "description", "url", "sha", "ref", "private", "languages", "built", "kind",
             "indexed_with", "bridges_checked")
ACTIVE = ("queued", "fetching", "parsing", "embedding", "running", "indexing", "saving")


def cited_count(sources):
    items = sources.get("items", []) if isinstance(sources, dict) else (sources or [])
    return sum(1 for s in items if isinstance(s, dict) and s.get("cited"))


def question_brief(row, repo_name, preview=False):
    out = {"id": row.id, "repo_id": row.repo_id, "repo_name": repo_name or row.repo_id, "question": row.question,
           "created_at": iso(row.created_at), "cited": cited_count(row.sources)}
    if preview:
        out["answer"] = (row.answer or "")[:ANSWER_PREVIEW]
    return out


def add_routes(app, db, auth, repos, indexer, settings):
    """Register GET /api/home on `app`."""

    @app.get("/api/home")
    def home(request: Request):
        user = auth.current_user(request)
        if user is None:
            raise HTTPException(401, "Not signed in")
        links = auth.users.links(user.id)                          # last opened (else added) first
        summaries = repos.summaries([repo_id for repo_id, _, _ in links])
        jobs = {}
        for job in indexer.for_user(user.id):                      # newest first: keep each repo's newest
            jobs.setdefault(job["repo_id"], job)

        items = []
        for repo_id, added_at, opened_at in links:
            info, job = summaries.get(repo_id), jobs.get(repo_id)
            if info is None and not (job and job["status"] in ACTIVE):
                continue                                           # gone from the server, nothing running
            item = ({k: info.get(k) for k in REPO_KEYS} if info is not None
                    else {"id": repo_id, "name": job["name"], "languages": {}, "built": "", "kind": "user", "pending": True})
            item.update(added_at=iso(added_at), last_opened_at=iso(opened_at), job=job,
                        bridges=repos.bridge_count(repo_id) if info is not None else 0)
            if info is not None and is_older(info.get("indexed_with") or "", settings.default_version):
                item["reindex_to"] = settings.default_version
            items.append(item)
        last_repo = next((r for r in items if not r.get("pending")), None)

        week_ago = utcnow() - timedelta(days=7)
        with db.session() as s:
            mine = s.query(Question).filter(Question.user_id == user.id)
            rows = mine.order_by(Question.id.desc()).limit(RECENT).all()
            names = dict(s.query(Repo.id, Repo.name).filter(Repo.id.in_({r.repo_id for r in rows}))) if rows else {}
            recent = [question_brief(r, names.get(r.repo_id)) for r in rows]
            last_question = None
            if last_repo is not None:
                row = mine.filter(Question.repo_id == last_repo["id"]).order_by(Question.id.desc()).first()
                if row is not None:
                    last_question = question_brief(row, last_repo["name"], preview=True)
            times = [t for (t,) in mine.filter(Question.created_at >= week_ago).with_entities(Question.created_at)]
        today = utcnow().date()
        per_day = [sum(1 for t in times if t.date() == today - timedelta(days=6 - i)) for i in range(7)]
        stats = {"questions_week": len(times), "per_day": per_day, "repos": len(items),
                 "bridges": sum(r["bridges"] for r in items)}
        return {"last_repo": last_repo, "last_question": last_question, "recent_questions": recent,
                "repos": items, "stats": stats}
