"""
Questions, shares and feedback (the `questions`, `shares`, `feedback` tables).

  GET    /api/repos/{id}/history          the signed-in user's questions about a repo, newest first,
                                          paginated (?limit=20&before=<question id>), with the total
                                          and the last 7 days' counts (the "Questions asked" tile)
  POST   /api/questions/{id}/share        -> {slug, url}; a private repo's answer needs
                                          {"confirm_private": true}
  DELETE /api/questions/{id}/share        revoke its links
  GET    /api/shares/{slug}               the shared answer (works signed out)
  GET    /a/{slug}                        the read-only answer page (frontend/answer.html)
  POST   /api/questions/{id}/feedback     {"rating": "up"|"down", "note": "..."}

Only signed-in users' questions are stored; each user sees only their own. A
shared answer from a private repo shows the answer and the code it cites (each
cited chunk, at most SNIPPET_LINES lines), never a whole file or the other sources.
"""

import secrets
from datetime import timedelta, timezone
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..context import parse_citations
from ..db.models import Feedback, Question, Repo, Share, utcnow

SNIPPET_LINES = 40
PAGE = 20
SOURCE_KEYS = ("id", "chunk_id", "name", "kind", "file", "start_line", "end_line", "hop", "relation",
               "language", "via", "via_ids", "url", "confidence", "qname")


def iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat(timespec="seconds") if value else ""


def stored_sources(sources, answer):
    """
    What a question keeps of its sources: their metadata (no code), which ones the
    answer cites, and the subgraph's node ids (the sources and the call-graph paths
    that reached them), so the thumbnail can be redrawn from history.
    """
    cited = set(parse_citations(answer or "", len(sources))[0])
    items = []
    for s in sources:
        item = {k: s.get(k) for k in SOURCE_KEYS if s.get(k) is not None}
        if s.get("bridge"):
            item["bridge"] = {k: s["bridge"].get(k) for k in ("mechanism", "confidence", "label")}
        item["cited"] = s.get("id") in cited
        items.append(item)
    nodes = {s["chunk_id"] for s in sources if isinstance(s.get("chunk_id"), int) and s["chunk_id"] >= 0}
    nodes |= {i for s in sources for i in (s.get("via_ids") or [])}
    return {"items": items, "subgraph": sorted(nodes),
            "hops": {str(s["chunk_id"]): s.get("hop", 0) for s in sources if isinstance(s.get("chunk_id"), int)}}


def record(db, repo_id, user_id, question, answer, sources, model, latency_ms, cached):
    """Store a signed-in user's answered question; returns its id."""
    with db.session() as s:
        row = Question(repo_id=repo_id, user_id=user_id, question=question, answer=answer or "",
                       sources=stored_sources(sources, answer), model=model or "",
                       latency_ms=int(latency_ms or 0), cached=bool(cached), created_at=utcnow())
        s.add(row)
        s.flush()
        return row.id


def question_view(row, rating=None, share=None):
    sources = row.sources if isinstance(row.sources, dict) else {"items": row.sources or []}
    return {"id": row.id, "repo_id": row.repo_id, "question": row.question, "answer": row.answer,
            "sources": sources.get("items", []), "subgraph": sources.get("subgraph", []),
            "hops": sources.get("hops", {}), "model": row.model, "latency_ms": row.latency_ms,
            "cached": row.cached, "created_at": iso(row.created_at), "rating": rating,
            "share": share}


class ShareRequest(BaseModel):
    confirm_private: bool = False


class FeedbackRequest(BaseModel):
    rating: Literal["up", "down"]
    note: str = Field("", max_length=1000)


def add_routes(app, db, auth, repos, can_open, limit, frontend_dir=None):
    """Register the history, share and feedback endpoints on `app`."""

    def user_of(request):
        user = auth.current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in to keep your questions.")
        return user

    def own_question(s, question_id, user):
        row = s.get(Question, question_id)
        if row is None or row.user_id != user.id:
            raise HTTPException(404, "Unknown question")          # someone else's: as if it didn't exist
        return row

    @app.get("/api/repos/{repo_id}/history")
    def history(repo_id: str, request: Request, limit: int = PAGE, before: int | None = None):
        user = user_of(request)
        if not can_open(repo_id, request) or repos.summary(repo_id) is None:
            raise HTTPException(404, f"Unknown repository {repo_id!r}")
        limit = max(1, min(limit, 50))
        with db.session() as s:
            mine = s.query(Question).filter(Question.user_id == user.id, Question.repo_id == repo_id)
            page = mine.filter(Question.id < before) if before else mine
            rows = page.order_by(Question.id.desc()).limit(limit + 1).all()
            more = len(rows) > limit
            rows = rows[:limit]
            ids = [r.id for r in rows]
            ratings = {f.question_id: f.rating for f in s.query(Feedback).filter(
                Feedback.question_id.in_(ids), Feedback.user_id == user.id)} if ids else {}
            shares = {sh.question_id: sh.slug for sh in s.query(Share).filter(
                Share.question_id.in_(ids), Share.revoked.is_(False))} if ids else {}
            total = mine.count()
            today = utcnow().date()
            since = today - timedelta(days=6)
            recent = [r.created_at.date() for r in mine.filter(
                Question.created_at >= utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=6)).with_entities(Question.created_at)]
            per_day = [sum(1 for d in recent if d == since + timedelta(days=i)) for i in range(7)]
            items = [question_view(r, ratings.get(r.id), shares.get(r.id)) for r in rows]
        return {"items": items, "next": items[-1]["id"] if more else None, "total": total,
                "per_day": per_day}

    @app.post("/api/questions/{question_id}/share")
    def share(question_id: int, request: Request, body: ShareRequest | None = None):
        user = user_of(request)
        limit(request, "share", 60)
        body = body or ShareRequest()
        with db.session() as s:
            row = own_question(s, question_id, user)
            repo = s.get(Repo, row.repo_id)
            private = bool(repo and repo.private)
            if private and not body.confirm_private:
                raise HTTPException(409, "This answer is about a private repository. Sharing it publishes "
                                         "the answer and the code it cites; confirm to share.")
            existing = s.query(Share).filter_by(question_id=row.id, revoked=False).first()
            slug = existing.slug if existing else secrets.token_urlsafe(9)
            if not existing:
                s.add(Share(slug=slug, question_id=row.id, created_at=utcnow()))
        return {"slug": slug, "url": f"/a/{slug}", "private": private}

    @app.delete("/api/questions/{question_id}/share", status_code=204)
    def revoke(question_id: int, request: Request):
        user = user_of(request)
        with db.session() as s:
            own_question(s, question_id, user)
            for sh in s.query(Share).filter_by(question_id=question_id, revoked=False):
                sh.revoked = True
        return Response(status_code=204)

    @app.get("/api/shares/{slug}")
    def shared(slug: str, request: Request):
        limit(request, "shared", 600)
        with db.session() as s:
            sh = s.get(Share, slug) if len(slug) <= 32 else None
            row = s.get(Question, sh.question_id) if sh and not sh.revoked else None
            if row is None:
                raise HTTPException(404, "This link was revoked, or never existed.")
            repo = s.get(Repo, row.repo_id)
            view = question_view(row)
            created = iso(sh.created_at)
        private = bool(repo and repo.private)
        demo = repos.get(row.repo_id)
        agent = demo.agents[demo.versions[-1]] if demo is not None and demo.agents else None
        sources = []
        for src in view["sources"]:
            if private and not src.get("cited"):
                continue                                  # private: only what the answer cites
            item = {k: src.get(k) for k in ("id", "name", "kind", "file", "start_line", "end_line", "cited",
                                            "language")}
            if src.get("cited") and agent is not None and 0 <= src.get("chunk_id", -1) < len(agent.chunks):
                lines = agent.chunks[src["chunk_id"]].splitlines()
                item["snippet"] = "\n".join(lines[:SNIPPET_LINES])
                item["truncated"] = len(lines) > SNIPPET_LINES
            if not private:
                item["url"] = src.get("url")
            sources.append(item)
        return {"question": view["question"], "answer": view["answer"], "model": view["model"],
                "asked_at": view["created_at"], "shared_at": created, "sources": sources,
                "repo": {"name": repo.name if repo else "", "private": private,
                         "url": "" if private or not repo else repo.url}}

    @app.post("/api/questions/{question_id}/feedback")
    def feedback(question_id: int, body: FeedbackRequest, request: Request):
        user = user_of(request)
        with db.session() as s:
            own_question(s, question_id, user)
            row = s.query(Feedback).filter_by(question_id=question_id, user_id=user.id).one_or_none()
            if row is None:
                row = Feedback(question_id=question_id, user_id=user.id, created_at=utcnow())
                s.add(row)
            row.rating, row.note = body.rating, body.note
        return {"question_id": question_id, "rating": body.rating, "note": body.note}

    if frontend_dir:
        import os
        from fastapi.responses import FileResponse

        @app.get("/a/{slug}", include_in_schema=False)
        def answer_page(slug: str):
            return FileResponse(os.path.join(frontend_dir, "answer.html"),
                                headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex"})
