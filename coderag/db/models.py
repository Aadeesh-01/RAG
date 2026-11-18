"""
Tables (SQLAlchemy 2.0, typed). Timestamps are naive UTC.

  users        GitHub accounts; the token is Fernet-encrypted (auth.py), never stored in clear
  repos        every repository the server can answer about: curated demos (kind "demo") and
               visitors' repos (kind "user"); the FAISS index stays on disk at index_path
  user_repos   which users added which repo (their repo list)
  jobs         indexing jobs (index / reindex); progress 0-100
  questions    questions asked by signed-in users, with the answer, its sources (and the
               subgraph node ids, to redraw a thumbnail), model, latency, cache hit
  shares       public, revocable links to one answer (/a/<slug>)
  feedback     thumbs up/down on an answer, one per user and question
"""

from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    login: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200), default="")
    avatar_url: Mapped[str] = mapped_column(String(500), default="")
    token_encrypted: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)     # owner__repo__sha12, or a demo id
    kind: Mapped[str] = mapped_column(String(10), default="user")      # user | demo
    owner: Mapped[str] = mapped_column(String(100), default="")
    name: Mapped[str] = mapped_column(String(200))                     # "owner/repo" as shown
    url: Mapped[str] = mapped_column(String(500), default="")
    commit: Mapped[str] = mapped_column(String(40), default="")
    ref: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    indexed_with: Mapped[str] = mapped_column(String(20), default="")  # the preset (v10 ...)
    languages: Mapped[dict] = mapped_column(JSON, default=dict)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    index_path: Mapped[str] = mapped_column(String(1000), default="")  # <data>/repos/<id>/<preset>
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
                                                 index=True)           # who indexed it
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)   # eviction: least recently used
    disk_bytes: Mapped[int] = mapped_column(BigInteger, default=0)                   # index + sources on disk


class UserRepo(Base):
    __tablename__ = "user_repos"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    # no FK: a user is linked to a repo as soon as they submit it, before indexing creates its row;
    # Repos.remove() deletes the links of a repo it removes
    repo_id: Mapped[str] = mapped_column(String(200), primary_key=True, index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)   # Home: the last repo opened


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    repo_id: Mapped[str] = mapped_column(String(200), index=True)      # no FK: the repo row comes at the end
    repo_name: Mapped[str] = mapped_column(String(200), default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
                                                index=True)
    kind: Mapped[str] = mapped_column(String(10), default="index")     # index | reindex
    status: Mapped[str] = mapped_column(String(10), default="queued")  # queued | running | done | failed
    stage: Mapped[str] = mapped_column(String(20), default="queued")   # fetching | parsing | embedding ...
    progress: Mapped[int] = mapped_column(Integer, default=0)           # 0-100
    message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    commit: Mapped[str] = mapped_column(String(40), default="")
    ref: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_jobs_user_created", "user_id", "created_at"),)


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[str] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True,
                                                index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[dict] = mapped_column(JSON, default=dict)          # {items, subgraph, hops}: web/history.py
    model: Mapped[str] = mapped_column(String(100), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[str] = mapped_column(String(20), default="")        # the preset that answered (v10 ...)
    server_key: Mapped[bool] = mapped_column(Boolean, default=False)    # answered with the server's LLM key
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    shares = relationship("Share", cascade="all, delete-orphan", passive_deletes=True)

    __table_args__ = (Index("ix_questions_repo_created", "repo_id", "created_at"),
                      Index("ix_questions_user_repo_created", "user_id", "repo_id", "created_at"),
                      Index("ix_questions_server_key_created", "server_key", "created_at"))


class Share(Base):
    __tablename__ = "shares"

    slug: Mapped[str] = mapped_column(String(32), primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    rating: Mapped[str] = mapped_column(String(4))                      # up | down
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("question_id", "user_id", name="uq_feedback_question_user"),)
