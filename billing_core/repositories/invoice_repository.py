from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from billing_core.database import get_current_app_id
from billing_core.models.invoice import Invoice


class InvoiceRepository:
    def __init__(self, db: Session, app_id: UUID | None = None):
        self.db = db
        self.app_id = app_id or get_current_app_id()
        if self.app_id is None:
            raise ValueError(
                "app_id must be provided to InvoiceRepository or configured via billing_core.init()"
            )

    def create(self, invoice: Invoice) -> Invoice:
        invoice.app_id = self.app_id
        if not getattr(invoice, "app_name", None):
            from billing_core.database import get_current_app_id, get_current_app_name
            from billing_core.models.invoice import App

            if self.app_id == get_current_app_id() and get_current_app_name():
                invoice.app_name = get_current_app_name()
            else:
                app = self.db.query(App).filter(App.id == self.app_id).first()
                if app:
                    invoice.app_name = app.name
                else:
                    raise ValueError(f"App with id '{self.app_id}' not found.")
        self.db.add(invoice)
        self.db.commit()
        self.db.refresh(invoice)
        return invoice

    def get_by_id(self, invoice_id: UUID) -> Optional[Invoice]:
        return (
            self.db.query(Invoice)
            .options(joinedload(Invoice.app))
            .filter(Invoice.id == invoice_id, Invoice.app_id == self.app_id)
            .first()
        )

    def get_by_invoice_number(self, invoice_number: str) -> Optional[Invoice]:
        return (
            self.db.query(Invoice)
            .options(joinedload(Invoice.app))
            .filter(
                Invoice.invoice_number == invoice_number,
                Invoice.app_id == self.app_id,
            )
            .first()
        )

    def get_by_customer(self, customer_id: str) -> List[Invoice]:
        return (
            self.db.query(Invoice)
            .options(joinedload(Invoice.app))
            .filter(
                Invoice.customer_id == customer_id,
                Invoice.app_id == self.app_id,
            )
            .all()
        )

    def list(
        self,
        status: Optional[Any] = None,
        customer_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Invoice]:
        query = (
            self.db.query(Invoice)
            .options(joinedload(Invoice.app))
            .filter(Invoice.app_id == self.app_id)
        )
        if status is not None:
            query = query.filter(Invoice.status == status)
        if customer_id is not None:
            query = query.filter(Invoice.customer_id == customer_id)
        return (
            query.order_by(Invoice.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def update(self, invoice: Invoice) -> Invoice:
        if invoice.app_id != self.app_id:
            raise ValueError("Cannot update invoice belonging to another app")
        self.db.commit()
        self.db.refresh(invoice)
        return invoice

    def delete(self, invoice: Invoice) -> None:
        if invoice.app_id != self.app_id:
            raise ValueError("Cannot delete invoice belonging to another app")
        self.db.delete(invoice)
        self.db.commit()
