from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from billing_core.database import get_current_app_id
from billing_core.models.invoice import App, Invoice, InvoiceStatus
from billing_core.repositories.invoice_repository import InvoiceRepository
from billing_core.schemas.invoice import (
    InvoiceCreate,
    InvoiceItemCreate,
    InvoiceUpdate,
    PaymentRequest,
)


def _calculate_items(items: List[InvoiceItemCreate]) -> tuple[list[dict], Decimal]:
    """Server-side line amounts (quantity * unit_price, rounded) and their total."""
    calculated_items = []
    total = Decimal("0.00")
    for item in items:
        desc = item.description or item.name
        qty = Decimal(str(item.quantity))
        price = Decimal(str(item.unit_price))
        amt = (qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total += amt
        calculated_items.append(
            {
                "description": desc,
                "quantity": str(qty),
                "unit_price": str(price),
                "amount": str(amt),
            }
        )
    return calculated_items, total


class InvoiceService:
    def __init__(self, db: Session, app_id: UUID | None = None):
        self.db = db
        self.app_id = app_id or get_current_app_id()
        if self.app_id:
            self.repository = InvoiceRepository(db, app_id=self.app_id)
        else:
            self.repository = None

    def _get_repo(self, app_id: UUID | None = None) -> InvoiceRepository:
        target_app_id = app_id or self.app_id or get_current_app_id()
        if target_app_id is None:
            raise ValueError(
                "app_id must be provided to InvoiceService or configured via billing_core.init()"
            )
        return InvoiceRepository(self.db, app_id=target_app_id)

    def create_invoice(
        self,
        arg1: Any,
        arg2: Optional[InvoiceCreate] = None,
    ) -> Invoice:
        """Create an invoice with server-side calculated amounts and JSON items.

        Accepts either ``create_invoice(data)`` using the service's configured app_id,
        or ``create_invoice(app_identifier, data)`` where app_identifier is an app_name or app_id.
        """
        app_name = None
        if isinstance(arg1, InvoiceCreate):
            data = arg1
            repo = self._get_repo()
        else:
            data = arg2
            if isinstance(arg1, UUID):
                repo = self._get_repo(arg1)
            elif isinstance(arg1, str):
                from billing_core.database import normalize_app_name

                norm_name = normalize_app_name(arg1)
                app = self.db.query(App).filter(App.name == norm_name).first()
                if not app:
                    app = App(name=norm_name)
                    self.db.add(app)
                    self.db.commit()
                    self.db.refresh(app)
                repo = self._get_repo(app.id)
                app_name = app.name
            else:
                repo = self._get_repo()

        if not app_name:
            from billing_core.database import get_current_app_id, get_current_app_name
            if repo.app_id == get_current_app_id() and get_current_app_name():
                app_name = get_current_app_name()
            else:
                app = self.db.query(App).filter(App.id == repo.app_id).first()
                if app:
                    app_name = app.name

        if data is None:
            raise ValueError("InvoiceCreate data is required")

        # Reject duplicate invoice_number within the same app
        existing = repo.get_by_invoice_number(data.invoice_number)
        if existing:
            raise ValueError(
                f"Invoice number '{data.invoice_number}' already exists for this app."
            )

        calculated_items, total = _calculate_items(data.items)

        invoice = Invoice(
            app_id=repo.app_id,
            app_name=app_name,
            invoice_number=data.invoice_number,
            customer_id=data.customer_id,
            reference_type=data.reference_type,
            notes=data.notes,
            due_date=data.due_date,
            total_amount=total,
            items=calculated_items,
        )
        return repo.create(invoice)

    def get_invoice(
        self, invoice_id: UUID, app_id: UUID | None = None
    ) -> Optional[Invoice]:
        repo = self._get_repo(app_id)
        return repo.get_by_id(invoice_id)

    def get_invoice_by_number(
        self, invoice_number: str, app_id: UUID | None = None
    ) -> Optional[Invoice]:
        repo = self._get_repo(app_id)
        return repo.get_by_invoice_number(invoice_number)

    def list_invoices(
        self,
        status: Optional[InvoiceStatus] = None,
        customer_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        app_id: UUID | None = None,
    ) -> List[Invoice]:
        repo = self._get_repo(app_id)
        return repo.list(status=status, customer_id=customer_id, limit=limit, offset=offset)

    def get_customer_invoices(
        self, customer_id: str, app_identifier: str | UUID | None = None
    ) -> List[Invoice]:
        if isinstance(app_identifier, UUID):
            repo = self._get_repo(app_identifier)
        elif isinstance(app_identifier, str):
            from billing_core.database import normalize_app_name

            norm_name = normalize_app_name(app_identifier)
            app = self.db.query(App).filter(App.name == norm_name).first()
            if not app:
                return []
            repo = self._get_repo(app.id)
        else:
            repo = self._get_repo()
        return repo.get_by_customer(customer_id)

    def update_invoice(
        self,
        invoice_id: UUID,
        data: InvoiceUpdate,
        app_id: UUID | None = None,
    ) -> Optional[Invoice]:
        repo = self._get_repo(app_id)
        invoice = repo.get_by_id(invoice_id)
        if not invoice:
            return None
        if data.notes is not None:
            invoice.notes = data.notes
        if data.due_date is not None:
            invoice.due_date = data.due_date
        if data.status is not None:
            invoice.status = data.status
        return repo.update(invoice)

    def mark_paid(
        self, invoice_id: UUID, app_id: UUID | None = None
    ) -> Optional[Invoice]:
        repo = self._get_repo(app_id)
        invoice = repo.get_by_id(invoice_id)
        if not invoice:
            return None
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = datetime.now(timezone.utc)
        return repo.update(invoice)

    def _get_or_create_app(self, app_name: str) -> App:
        from billing_core.database import normalize_app_name

        norm_name = normalize_app_name(app_name)
        app = self.db.query(App).filter(App.name == norm_name).first()
        if app:
            return app
        try:
            app = App(name=norm_name)
            self.db.add(app)
            self.db.commit()
        except IntegrityError:
            # another request registered the same app first
            self.db.rollback()
            app = self.db.query(App).filter(App.name == norm_name).one()
        return app

    def record_payment(self, data: PaymentRequest, _retried: bool = False) -> Invoice:
        """Mark an invoice of ``data.app_name`` paid, creating it first if billing_db doesn't have it.

        Every detail sent is written, also when the invoice already exists or is
        already paid. A repeated call keeps the first ``paid_at``.
        """
        app = self._get_or_create_app(data.app_name)
        repo = self._get_repo(app.id)
        invoice = repo.get_by_invoice_number(data.invoice_number)
        if invoice is None:
            invoice = Invoice(
                app_id=app.id,
                app_name=app.name,
                invoice_number=data.invoice_number,
                status=InvoiceStatus.PENDING,
            )
            self.db.add(invoice)

        invoice.customer_id = data.customer_id
        if data.items:
            invoice.items, invoice.total_amount = _calculate_items(data.items)
        for field in ("source_invoice_id", "payment_mode", "payment_reference", "reference_type", "due_date", "notes"):
            value = getattr(data, field)
            if value is not None:
                setattr(invoice, field, value)
        invoice.status = InvoiceStatus.PAID
        if invoice.paid_at is None:
            invoice.paid_at = datetime.now(timezone.utc)

        try:
            self.db.commit()
        except IntegrityError:
            # created concurrently by another request: update that row instead
            self.db.rollback()
            if _retried:
                raise
            return self.record_payment(data, _retried=True)
        self.db.refresh(invoice)
        return invoice

    def cancel_invoice(
        self, invoice_id: UUID, app_id: UUID | None = None
    ) -> Optional[Invoice]:
        repo = self._get_repo(app_id)
        invoice = repo.get_by_id(invoice_id)
        if not invoice:
            return None
        invoice.status = InvoiceStatus.CANCELLED
        return repo.update(invoice)
