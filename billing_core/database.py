"""billing-core database configuration and multi-tenant app management.

Lazily configured — the engine is not created until ``configure()`` or
``get_db()`` is called for the first time. Host apps call
``billing_core.init(url, app_name)`` on startup.
"""

import os
import re
from pathlib import Path
from typing import Generator
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

DATABASE_URL: str | None = None
engine = None
SessionLocal = None

CURRENT_APP_ID: UUID | None = None
CURRENT_APP_NAME: str | None = None


def normalize_app_name(raw_name: str) -> str:
    """Normalize and validate an app name.

    Converts to lowercase, replaces whitespace/special chars with hyphens,
    and strips leading/trailing dashes and underscores.
    """
    cleaned = raw_name.strip().lower()
    normalized = re.sub(r"[^a-z0-9_-]+", "-", cleaned)
    normalized = re.sub(r"-+", "-", normalized).strip("-_")
    if not normalized or len(normalized) > 100:
        raise ValueError(
            f"Invalid app name '{raw_name}'. App name must be 1-100 characters "
            "containing only alphanumeric characters, dashes, or underscores."
        )
    return normalized


def resolve_app_name(app_name: str | None = None) -> str:
    """Resolve the app name following the priority:

    1. Explicit `app_name` argument
    2. `BILLING_APP_NAME` environment variable
    3. Host project folder name (Path.cwd().name)
    4. Raise a clear error if none could be resolved.
    """
    if app_name is not None and app_name.strip():
        return normalize_app_name(app_name)

    env_name = os.environ.get("BILLING_APP_NAME", "").strip()
    if env_name:
        return normalize_app_name(env_name)

    cwd_name = Path.cwd().name.strip()
    if cwd_name:
        return normalize_app_name(cwd_name)

    raise ValueError(
        "Could not determine billing app name. Please specify app_name in init(), "
        "set the BILLING_APP_NAME environment variable, or run from a valid directory."
    )


def set_current_app(app_id: UUID, app_name: str) -> None:
    """Record the initialized app id and name."""
    global CURRENT_APP_ID, CURRENT_APP_NAME
    CURRENT_APP_ID = app_id
    CURRENT_APP_NAME = app_name


def get_current_app_id() -> UUID | None:
    """Return the active app_id."""
    return CURRENT_APP_ID


def get_current_app_name() -> str | None:
    """Return the active app_name."""
    return CURRENT_APP_NAME


def configure(database_url: str | None = None) -> None:
    """(Re)configure the billing-core database engine.

    Parameters
    ----------
    database_url:
        Connection string. Falls back to the ``BILLING_DATABASE_URL``
        environment variable, then to a MySQL localhost default.
    """
    global DATABASE_URL, engine, SessionLocal

    DATABASE_URL = database_url or os.environ.get(
        "BILLING_DATABASE_URL",
        "mysql+pymysql://user:password@localhost:3306/billing_db",
    )

    connect_args: dict = {}
    engine_kwargs: dict = {}

    if DATABASE_URL.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if ":memory:" in DATABASE_URL:
            engine_kwargs["poolclass"] = StaticPool
        else:
            engine_kwargs["pool_pre_ping"] = True
    else:
        engine_kwargs["pool_pre_ping"] = True

    engine = create_engine(
        DATABASE_URL,
        connect_args=connect_args,
        **engine_kwargs,
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _ensure_configured() -> None:
    if engine is None:
        configure()


def get_engine():
    """Return the SQLAlchemy engine, auto-configuring if needed."""
    _ensure_configured()
    return engine


def create_tables() -> None:
    """Create all missing billing-core tables in billing_db without Alembic."""
    from billing_core.base import Base
    import billing_core.models  # noqa: F401

    eng = get_engine()
    Base.metadata.create_all(bind=eng)
    add_missing_columns(eng, Base.metadata)


_tables_ready_for = None


def ensure_tables() -> None:
    """create_tables() once per configured engine (record_payment works without init())."""
    global _tables_ready_for
    eng = get_engine()
    if _tables_ready_for is not eng:
        create_tables()
        _tables_ready_for = eng


def add_missing_columns(eng, metadata) -> None:
    """Add nullable columns that newer versions introduced to tables that already exist.

    create_all() never alters an existing table, so an older billing_db would
    otherwise miss columns such as invoices.payment_mode.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    existing_tables = set(inspector.get_table_names())
    with eng.begin() as conn:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing or not column.nullable:
                    continue
                col_type = column.type.compile(dialect=eng.dialect)
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type} NULL"))


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency — yields a billing-core DB session.

    The session is rolled back on exception, then always closed.
    Commits are the responsibility of the repository layer.
    """
    _ensure_configured()
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
