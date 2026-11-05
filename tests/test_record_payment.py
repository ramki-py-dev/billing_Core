"""Tests for billing_core.record_payment(): host apps send a paid invoice, billing_db marks it paid.

Covers:
  - New invoice is created with the sent details and marked paid
  - Existing pending invoice is updated and marked paid
  - Invoice already PAID without details gets them filled
  - A repeated call keeps the first paid_at
  - Each request names its app; same invoice_number in two apps stays separate
  - Older billing_db tables get the new payment columns added
"""

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect, text

import billing_core
from billing_core import database
from billing_core.models.invoice import App, Invoice, InvoiceStatus
from billing_core.schemas.invoice import InvoiceCreate, InvoiceItemCreate
from billing_core.services.invoice_service import InvoiceService


@pytest.fixture(autouse=True)
def setup_db():
    """Fresh in-memory billing_db; no init(), record_payment needs only a configured database."""
    database.configure("sqlite:///:memory:")
    database.set_current_app(None, None)
    yield


def _items() -> list[dict]:
    return [
        {"description": "CBC", "quantity": 1, "unit_price": "200.00"},
        {"description": "LFT", "quantity": 2, "unit_price": "125.50"},
    ]


def _pay(**overrides):
    fields = {
        "app_name": "preventive-care",
        "invoice_number": "INV-000093",
        "customer_id": "7",
        "items": _items(),
        "source_invoice_id": "93",
        "payment_mode": "Cash",
        "payment_reference": "R-1",
        "reference_type": "lab_order",
    }
    fields.update(overrides)
    return billing_core.record_payment(**fields)


def test_new_invoice_is_created_and_paid():
    result = _pay()

    assert result.app_name == "preventive-care"
    assert result.invoice_number == "INV-000093"
    assert result.customer_id == "7"
    assert result.status == InvoiceStatus.PAID
    assert result.paid_at is not None
    assert result.total_amount == Decimal("451.00")
    assert [i.amount for i in result.items] == [Decimal("200.00"), Decimal("251.00")]
    assert result.source_invoice_id == "93"
    assert result.payment_mode == "Cash"
    assert result.payment_reference == "R-1"
    assert result.reference_type == "lab_order"


def test_existing_pending_invoice_is_marked_paid():
    database.ensure_tables()
    with database.SessionLocal() as session:
        app = App(name="preventive-care")
        session.add(app)
        session.commit()
        InvoiceService(session, app_id=app.id).create_invoice(
            InvoiceCreate(
                invoice_number="INV-000093",
                customer_id="7",
                items=[InvoiceItemCreate(description="CBC", unit_price=Decimal("200.00"))],
            )
        )

    result = _pay()

    assert result.status == InvoiceStatus.PAID
    assert result.payment_mode == "Cash"
    with database.SessionLocal() as session:
        assert session.query(Invoice).count() == 1


def test_already_paid_invoice_without_details_gets_them_filled():
    database.ensure_tables()
    with database.SessionLocal() as session:
        app = App(name="preventive-care")
        session.add(app)
        session.commit()
        session.add(
            Invoice(
                app_id=app.id,
                app_name=app.name,
                invoice_number="INV-000093",
                customer_id="7",
                status=InvoiceStatus.PAID,
                items=[],
            )
        )
        session.commit()

    result = _pay()

    assert result.paid_at is not None
    assert result.payment_mode == "Cash"
    assert result.payment_reference == "R-1"
    assert result.total_amount == Decimal("451.00")


def test_repeated_call_keeps_first_paid_at():
    first = _pay()
    second = _pay(payment_reference="R-2")

    assert second.id == first.id
    assert second.paid_at == first.paid_at
    assert second.payment_reference == "R-2"


def test_apps_are_named_per_request_and_kept_apart():
    a = _pay(app_name="preventive-care")
    b = _pay(app_name="Pharmacy App", customer_id="99")

    assert a.id != b.id
    assert b.app_name == "pharmacy-app"
    assert b.customer_id == "99"
    with database.SessionLocal() as session:
        assert {name for (name,) in session.query(App.name)} == {"preventive-care", "pharmacy-app"}


def test_older_invoices_table_gets_payment_columns(tmp_path):
    database.configure(f"sqlite:///{tmp_path / 'old_billing.db'}")
    with database.get_engine().begin() as conn:
        conn.execute(text("CREATE TABLE apps (id CHAR(32) PRIMARY KEY, name VARCHAR(100) NOT NULL UNIQUE, created_at DATETIME NOT NULL)"))
        conn.execute(text(
            "CREATE TABLE invoices (id CHAR(32) PRIMARY KEY, app_id CHAR(32) NOT NULL, app_name VARCHAR(100) NOT NULL, "
            "invoice_number VARCHAR(100) NOT NULL, customer_id VARCHAR(255) NOT NULL, reference_type VARCHAR(50), "
            "status VARCHAR(9) NOT NULL, total_amount NUMERIC(12, 2) NOT NULL, items JSON NOT NULL, notes TEXT, "
            "issued_at DATETIME, due_date DATETIME, paid_at DATETIME, created_at DATETIME, updated_at DATETIME)"
        ))

    result = _pay()

    columns = {c["name"] for c in inspect(database.get_engine()).get_columns("invoices")}
    assert {"source_invoice_id", "payment_mode", "payment_reference"} <= columns
    assert result.payment_mode == "Cash"
    assert isinstance(result.paid_at, datetime)
