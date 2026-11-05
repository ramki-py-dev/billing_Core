"""Table creation runner for billing-core.

Maintained for backwards compatibility so existing calls to ``run_migrations()``
continue to work without breaking. Creates missing tables in billing_db directly.
"""

from billing_core.database import _ensure_configured, configure, create_tables


def run_migrations(database_url: str | None = None) -> None:
    """Create all missing billing-core tables in billing_db."""
    if database_url:
        configure(database_url)
    else:
        _ensure_configured()
    create_tables()
