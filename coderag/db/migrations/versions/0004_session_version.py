"""Server-side sign-out: a per-user session version that every session cookie carries.

  users.session_version    bumped by POST /api/auth/logout: every cookie signed with an older
                           version (on any device, or copied) stops working at once

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("session_version")
