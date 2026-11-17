"""Alembic environment: uses the connection coderag passes in (Database.upgrade), else the URL."""

from alembic import context
from sqlalchemy import create_engine

from coderag.db.models import Base

config = context.config
target_metadata = Base.metadata


def run(connection):
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    with engine.begin() as conn:
        run(conn)
