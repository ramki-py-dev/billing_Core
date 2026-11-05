"""billing-core route factory.

Usage::

    from billing_core import create_router
    app.include_router(create_router("my-app"), prefix="/api/v1")
    # or if billing_core.init() was already called:
    app.include_router(create_router(), prefix="/api/v1")

The router uses billing-core's own database (``BILLING_DATABASE_URL``).
Endpoints return 404 when the invoice belongs to a different app_id,
providing per-app isolation.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from billing_core.database import (
    get_current_app_id,
    get_current_app_name,
    get_db,
    normalize_app_name,
)
from billing_core.models.invoice import App, Invoice, InvoiceStatus
from billing_core.schemas.invoice import (
    InvoiceCreate,
    InvoiceResponse,
    InvoiceUpdate,
)
from billing_core.services.invoice_service import InvoiceService
from billing_core.sync import auto_sync_if_configured


def _invoice_to_response(invoice: Invoice) -> InvoiceResponse:
    """Serialize an Invoice ORM object to InvoiceResponse."""
    app_name = invoice.app_name or (invoice.app.name if invoice.app else "")
    return InvoiceResponse(
        id=invoice.id,
        app_id=invoice.app_id,
        app_name=app_name,
        invoice_number=invoice.invoice_number,
        customer_id=invoice.customer_id,
        reference_type=invoice.reference_type,
        status=invoice.status,
        total_amount=invoice.total_amount,
        items=invoice.items or [],
        notes=invoice.notes,
        source_invoice_id=invoice.source_invoice_id,
        payment_mode=invoice.payment_mode,
        payment_reference=invoice.payment_reference,
        issued_at=invoice.issued_at,
        due_date=invoice.due_date,
        paid_at=invoice.paid_at,
        created_at=invoice.created_at,
        updated_at=invoice.updated_at,
    )


def create_router(app_name: str | None = None) -> APIRouter:
    """Return a billing APIRouter scoped to *app_name* (or the initialized app)."""
    router = APIRouter(prefix="/billing", tags=["billing"])

    def _resolve_app_id(db: Session) -> UUID:
        if app_name is not None and app_name.strip():
            norm_name = normalize_app_name(app_name)
            app = db.query(App).filter(App.name == norm_name).first()
            if not app:
                app = App(name=norm_name)
                db.add(app)
                db.commit()
                db.refresh(app)
            return app.id
        current_id = get_current_app_id()
        if current_id is None:
            raise HTTPException(
                status_code=500,
                detail="Billing app is not configured. Call billing_core.init() first or provide app_name.",
            )
        return current_id

    def _get_app_name() -> str | None:
        if app_name is not None and app_name.strip():
            return normalize_app_name(app_name)
        return get_current_app_name()

    @router.post("/invoices", response_model=InvoiceResponse, status_code=201)
    def create_invoice(data: InvoiceCreate, db: Session = Depends(get_db)):
        app_id = _resolve_app_id(db)
        service = InvoiceService(db, app_id=app_id)
        try:
            invoice = service.create_invoice(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _invoice_to_response(invoice)

    @router.get("/invoices", response_model=list[InvoiceResponse])
    def list_invoices(
        status: InvoiceStatus | None = None,
        customer_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        db: Session = Depends(get_db),
    ):
        auto_sync_if_configured(app_name=_get_app_name(), min_interval_seconds=1.0)
        app_id = _resolve_app_id(db)
        service = InvoiceService(db, app_id=app_id)
        invoices = service.list_invoices(
            status=status, customer_id=customer_id, limit=limit, offset=offset
        )
        return [_invoice_to_response(inv) for inv in invoices]

    @router.get("/invoices/{invoice_id}", response_model=InvoiceResponse)
    def get_invoice(invoice_id: UUID, db: Session = Depends(get_db)):
        auto_sync_if_configured(app_name=_get_app_name(), min_interval_seconds=1.0)
        app_id = _resolve_app_id(db)
        service = InvoiceService(db, app_id=app_id)
        invoice = service.get_invoice(invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        return _invoice_to_response(invoice)

    @router.get("/invoices/number/{invoice_number}", response_model=InvoiceResponse)
    def get_invoice_by_number(invoice_number: str, db: Session = Depends(get_db)):
        auto_sync_if_configured(app_name=_get_app_name(), min_interval_seconds=1.0)
        app_id = _resolve_app_id(db)
        service = InvoiceService(db, app_id=app_id)
        invoice = service.get_invoice_by_number(invoice_number)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        return _invoice_to_response(invoice)

    @router.get(
        "/invoices/customer/{customer_id}",
        response_model=list[InvoiceResponse],
    )
    def get_customer_invoices(customer_id: str, db: Session = Depends(get_db)):
        auto_sync_if_configured(app_name=_get_app_name(), min_interval_seconds=1.0)
        app_id = _resolve_app_id(db)
        service = InvoiceService(db, app_id=app_id)
        invoices = service.get_customer_invoices(customer_id)
        return [_invoice_to_response(inv) for inv in invoices]

    @router.post("/sync")
    def trigger_sync(db: Session = Depends(get_db)):
        stats = auto_sync_if_configured(app_name=_get_app_name(), min_interval_seconds=0.0)
        return {
            "status": "success",
            "stats": stats or {"synced": 0, "created": 0, "updated": 0, "skipped": 0},
        }

    @router.patch("/invoices/{invoice_id}", response_model=InvoiceResponse)
    def update_invoice(
        invoice_id: UUID,
        data: InvoiceUpdate,
        db: Session = Depends(get_db),
    ):
        app_id = _resolve_app_id(db)
        service = InvoiceService(db, app_id=app_id)
        invoice = service.update_invoice(invoice_id, data)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        return _invoice_to_response(invoice)

    @router.post("/invoices/{invoice_id}/pay", response_model=InvoiceResponse)
    def mark_invoice_paid(invoice_id: UUID, db: Session = Depends(get_db)):
        app_id = _resolve_app_id(db)
        service = InvoiceService(db, app_id=app_id)
        invoice = service.mark_paid(invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        return _invoice_to_response(invoice)

    @router.post(
        "/invoices/{invoice_id}/cancel", response_model=InvoiceResponse
    )
    def cancel_invoice(invoice_id: UUID, db: Session = Depends(get_db)):
        app_id = _resolve_app_id(db)
        service = InvoiceService(db, app_id=app_id)
        invoice = service.cancel_invoice(invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        return _invoice_to_response(invoice)

    return router
