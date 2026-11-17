"""Initial schema; converts the auth-only database (users, user_repos) in place.

Before this migration coderag kept only `users` and `user_repos` in SQLite (plain
sqlite3, ISO-8601 text timestamps, no alembic_version). Those rows are kept:
users gain last_seen_at and get SQLAlchemy-style timestamps; the visitor repos
saved on disk (repos/<id>/demo.json beside the database) become `repos` rows, and
each user's links to them are copied into the new user_repos (links to repos
whose index is gone are dropped: they could not be opened anyway).

Revision ID: 0001
Revises:
Create Date: 2026-11-17
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _repos_table():
    return op.create_table(
        "repos",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("kind", sa.String(10), nullable=False, server_default="user"),
        sa.Column("owner", sa.String(100), nullable=False, server_default=""),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("url", sa.String(500), nullable=False, server_default=""),
        sa.Column("commit", sa.String(40), nullable=False, server_default=""),
        sa.Column("ref", sa.String(200), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("private", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("indexed_with", sa.String(20), nullable=False, server_default=""),
        sa.Column("languages", sa.JSON(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("index_path", sa.String(1000), nullable=False, server_default=""),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("built_at", sa.DateTime(), nullable=True),
    )


def _user_repos_table(name="user_repos"):
    return op.create_table(
        name,
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("repo_id", sa.String(200), primary_key=True),          # no FK: linked before indexed
        sa.Column("added_at", sa.DateTime(), nullable=False),
    )


def _data_dir(bind):
    """The folder of a SQLite database file (where the old layout kept repos/), else None."""
    if bind.dialect.name != "sqlite":
        return None
    path = bind.engine.url.database
    return os.path.dirname(os.path.abspath(path)) if path and path != ":memory:" else None


def upgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    legacy = "users" in existing

    if legacy:
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("last_seen_at", sa.DateTime(), nullable=True))
        # '2026-11-16T20:28:20+00:00' -> '2026-11-16 20:28:20' (what SQLAlchemy reads back for SQLite)
        op.execute("UPDATE users SET created_at = replace(substr(created_at, 1, 19), 'T', ' ') "
                   "WHERE created_at LIKE '____-__-__T%'")
    else:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("github_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("login", sa.String(100), nullable=False),
            sa.Column("name", sa.String(200), nullable=False, server_default=""),
            sa.Column("avatar_url", sa.String(500), nullable=False, server_default=""),
            sa.Column("token_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        )

    repos = _repos_table()
    op.create_index("ix_repos_owner_id", "repos", ["owner_id"])

    # visitor repos saved on disk before the database (SQLite only: they sit beside its file)
    data_dir = _data_dir(bind)
    known_users = {r[0] for r in bind.execute(sa.text("SELECT id FROM users"))} if legacy else set()
    imported = set()
    if data_dir:
        from coderag.db.legacy import repo_rows
        rows = repo_rows(data_dir)
        for row in rows:
            if row["owner_id"] not in known_users:
                row["owner_id"] = None
        if rows:
            op.bulk_insert(repos, rows)
            imported = {row["id"] for row in rows}

    if "user_repos" in existing:
        op.rename_table("user_repos", "user_repos_legacy")
    _user_repos_table()
    op.create_index("ix_user_repos_repo_id", "user_repos", ["repo_id"])
    if "user_repos" in existing:
        old = bind.execute(sa.text("SELECT user_id, repo_id, added_at FROM user_repos_legacy")).fetchall()
        from coderag.db.legacy import parse_time
        keep = [{"user_id": u, "repo_id": r, "added_at": parse_time(a)} for u, r, a in old
                if u in known_users and r in imported]
        if keep:
            op.bulk_insert(sa.table("user_repos", sa.column("user_id", sa.Integer()),
                                    sa.column("repo_id", sa.String()), sa.column("added_at", sa.DateTime())),
                           keep)
        op.drop_table("user_repos_legacy")

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("repo_id", sa.String(200), nullable=False),
        sa.Column("repo_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(10), nullable=False, server_default="index"),
        sa.Column("status", sa.String(10), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.String(500), nullable=False, server_default=""),
        sa.Column("commit", sa.String(40), nullable=False, server_default=""),
        sa.Column("ref", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_jobs_repo_id", "jobs", ["repo_id"])
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"])
    op.create_index("ix_jobs_user_created", "jobs", ["user_id", "created_at"])

    op.create_table(
        "questions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("repo_id", sa.String(200), sa.ForeignKey("repos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False, server_default=""),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(100), nullable=False, server_default=""),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_questions_user_id", "questions", ["user_id"])
    op.create_index("ix_questions_repo_created", "questions", ["repo_id", "created_at"])
    op.create_index("ix_questions_user_repo_created", "questions", ["user_id", "repo_id", "created_at"])

    op.create_table(
        "shares",
        sa.Column("slug", sa.String(32), primary_key=True),
        sa.Column("question_id", sa.Integer(), sa.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_shares_question_id", "shares", ["question_id"])

    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("question_id", sa.Integer(), sa.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating", sa.String(4), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("question_id", "user_id", name="uq_feedback_question_user"),
    )
    op.create_index("ix_feedback_question_id", "feedback", ["question_id"])
    op.create_index("ix_feedback_user_id", "feedback", ["user_id"])


def downgrade():
    for table in ("feedback", "shares", "questions", "jobs", "user_repos", "repos", "users"):
        op.drop_table(table)
