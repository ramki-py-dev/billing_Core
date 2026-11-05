"""billing-core: standalone, reusable billing package with shared schema and JSON items."""

from billing_core.base import Base
from billing_core.database import get_db
from billing_core.models.invoice import App, Invoice, InvoiceStatus
from billing_core.repositories.invoice_repository import InvoiceRepository
from billing_core.routes.invoice_routes import create_router
from billing_core.schemas.invoice import (
    InvoiceCreate,
    InvoiceItemCreate,
    InvoiceItemResponse,
    InvoiceResponse,
    InvoiceUpdate,
    PaymentRequest,
)
from billing_core.services.invoice_service import InvoiceService
from billing_core.sync import auto_sync_if_configured, sync_invoices


def init(
    database_url: str | None = None,
    app_name: str | None = None,
    source_database_url: str | None = None,
    auto_sync: bool = True,
) -> App:
    """Initialize billing-core for the host application.

    1. Configures database connection (explicit URL or BILLING_DATABASE_URL).
    2. Automatically creates missing billing-core tables in billing_db via create_all
       (host apps never run migrations or create tables in their own DB).
    3. Resolves and validates app name:
       app_name arg -> BILLING_APP_NAME env var -> host project folder name.
    4. Finds or creates the app row in `apps` table and caches its app_id.
    5. If source database is configured (or SOURCE_DATABASE_URL env var exists) and auto_sync=True,
       automatically synchronizes bills from the source database into billing_db.

    Returns
    -------
    App:
        The registered App ORM model instance for this host app.
    """
    from billing_core import database
    from billing_core.models.invoice import App

    # 1. Configure engine
    database.configure(database_url)

    # 2. Automatically create missing tables in billing_db
    database.create_tables()

    # 3. Resolve & normalize app name
    resolved_name = database.resolve_app_name(app_name)

    # 4. Find or create app row in billing_db
    Session = database.SessionLocal
    with Session() as session:
        app = session.query(App).filter(App.name == resolved_name).first()
        if not app:
            app = App(name=resolved_name)
            session.add(app)
            session.commit()
            session.refresh(app)
        database.set_current_app(app.id, app.name)

    # 5. Automatically synchronize from source database if configured
    if auto_sync:
        try:
            from billing_core.sync import auto_sync_if_configured

            auto_sync_if_configured(
                app_name=resolved_name,
                source_url=source_database_url,
                min_interval_seconds=0.0,
                force_full=True,
            )
        except Exception as exc:
            import logging

            logging.getLogger("billing_core").warning(
                "Initial sync failed on startup: %s", exc
            )

    return app


def record_payment(
    app_name: str,
    invoice_number: str,
    customer_id: str,
    items: list | None = None,
    **details,
) -> InvoiceResponse:
    """Record a paid invoice sent by a host app and return it.

    Any app can call this with its own ``app_name``; billing_db only needs to be
    configured (``BILLING_DATABASE_URL`` or ``database.configure(url)``).
    ``details``: source_invoice_id, payment_mode, payment_reference,
    reference_type, due_date, notes.
    """
    from billing_core import database
    from billing_core.routes.invoice_routes import _invoice_to_response

    data = PaymentRequest(
        app_name=app_name,
        invoice_number=invoice_number,
        customer_id=customer_id,
        items=items or [],
        **details,
    )
    database.ensure_tables()
    with database.SessionLocal() as session:
        invoice = InvoiceService(session).record_payment(data)
        return _invoice_to_response(invoice)


__all__ = [
    "Base",
    "init",
    "get_db",
    "App",
    "Invoice",
    "InvoiceStatus",
    "InvoiceCreate",
    "InvoiceUpdate",
    "InvoiceResponse",
    "InvoiceItemCreate",
    "InvoiceItemResponse",
    "PaymentRequest",
    "record_payment",
    "InvoiceRepository",
    "InvoiceService",
    "create_router",
    "sync_invoices",
    "auto_sync_if_configured",
]
