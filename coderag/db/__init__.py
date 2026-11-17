"""
The database: SQLAlchemy 2.0 models (models.py) and Alembic migrations (migrations/).

    CODERAG_DATABASE_URL   default sqlite:///<data_dir>/coderag.db
                           postgres://... and postgresql://... use psycopg (v3) unchanged

Migrations run on server start and with `python -m coderag db upgrade`.
"""

import os
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

MIGRATIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
MIGRATION_LOCK = 7_316_524_901            # pg_advisory_xact_lock key: one migrator at a time


def normalize_url(url):
    """postgres://... (Neon, Supabase, Heroku style) -> postgresql+psycopg://...; others unchanged."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def default_url(data_dir):
    return "sqlite:///" + os.path.abspath(os.path.join(data_dir, "coderag.db")).replace("\\", "/")


def safe_url(url):
    """The URL with any password masked, for logs."""
    from sqlalchemy.engine import make_url
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:                              # noqa: BLE001
        return "<database url>"


class Database:
    def __init__(self, url):
        self.url = normalize_url(url)
        self.sqlite = self.url.startswith("sqlite")
        kwargs = {}
        if self.sqlite:
            path = self.url.split(":///", 1)[-1]
            if path and path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs["pool_pre_ping"] = True
        self.engine = create_engine(self.url, **kwargs)
        if self.sqlite:
            @event.listens_for(self.engine, "connect")
            def _pragmas(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")     # ON DELETE CASCADE needs it in SQLite
                cursor.execute("PRAGMA journal_mode=WAL")    # readers don't wait for the indexer's writes
                cursor.close()
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        """A session that commits on success and rolls back on error."""
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def alembic_config(self):
        from alembic.config import Config
        config = Config()
        config.set_main_option("script_location", MIGRATIONS)
        config.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        return config

    def upgrade(self, revision="head"):
        """
        Apply migrations up to `revision` (the old auth-only database is converted in place).
        On Postgres the migration runs under a transaction-scoped advisory lock, so two
        processes starting together don't race; the second waits, then finds nothing to do.
        """
        from alembic import command
        config = self.alembic_config()
        with self.engine.begin() as connection:
            if not self.sqlite:
                connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK})
            config.attributes["connection"] = connection
            command.upgrade(config, revision)

    def current(self):
        from alembic.runtime.migration import MigrationContext
        with self.engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()

    def ping(self):
        """SELECT 1 (readiness)."""
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def dispose(self):
        self.engine.dispose()
