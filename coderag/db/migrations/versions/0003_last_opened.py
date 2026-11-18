"""When each user last opened each of their repos (the Home page's "Continue where you left off").

  user_repos.last_opened_at    set when the user opens the repo (GET /api/repos/{id}); NULL until then

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_repos", sa.Column("last_opened_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("user_repos") as batch:
        batch.drop_column("last_opened_at")
