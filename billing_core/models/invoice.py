import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import relationship

from billing_core.base import Base


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    SENT = "sent"
    PAID = "paid"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"
    PENDING = "pending"
    FAILED = "failed"


class App(Base):
    __tablename__ = "apps"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    name = Column(String(100), unique=True, nullable=False, index=True)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    invoices = relationship(
        "Invoice", back_populates="app", cascade="all, delete-orphan"
    )


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint(
            "app_id", "invoice_number", name="uq_invoices_app_id_invoice_number"
        ),
        Index("ix_invoices_app_id_customer_id", "app_id", "customer_id"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    app_id = Column(
        Uuid, ForeignKey("apps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalized copy of apps.name — filled automatically by billing-core.
    # Clients never send this field; it is derived from the resolved app name.
    app_name = Column(String(100), nullable=False, index=True)
    invoice_number = Column(String(100), nullable=False)
    customer_id = Column(String(255), nullable=False)
    reference_type = Column(String(50), nullable=True)
    status = Column(
        Enum(InvoiceStatus, native_enum=False),
        default=InvoiceStatus.DRAFT,
        nullable=False,
    )
    total_amount = Column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    items = Column(JSON, default=list, nullable=False)
    notes = Column(Text, nullable=True)
    # Payment details sent by the host app (record_payment); no gateway yet
    source_invoice_id = Column(String(100), nullable=True)
    payment_mode = Column(String(30), nullable=True)
    payment_reference = Column(String(100), nullable=True)
    issued_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    due_date = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    app = relationship("App", back_populates="invoices")
