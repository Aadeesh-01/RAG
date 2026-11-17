"""Repo last use and disk size (eviction), question version and server-key flag.

  repos.last_used_at, repos.disk_bytes   evict the least recently used unlinked repos,
                                         within CODERAG_MAX_STORED_REPOS / _MAX_STORED_MB
  questions.version                      the preset that answered (shares serve its stored snippets)
  questions.server_key                   answered with the server's key: the daily cap counts these

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("repos", sa.Column("last_used_at", sa.DateTime(), nullable=True))
    op.add_column("repos", sa.Column("disk_bytes", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("questions", sa.Column("version", sa.String(20), nullable=False, server_default=""))
    op.add_column("questions", sa.Column("server_key", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_questions_server_key_created", "questions", ["server_key", "created_at"])


def downgrade():
    op.drop_index("ix_questions_server_key_created", "questions")
    with op.batch_alter_table("questions") as batch:
        batch.drop_column("server_key")
        batch.drop_column("version")
    with op.batch_alter_table("repos") as batch:
        batch.drop_column("disk_bytes")
        batch.drop_column("last_used_at")
