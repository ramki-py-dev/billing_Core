"""Tests for billing-core read-only sync from external source database into billing_db."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Table,
    MetaData,
    create_engine,
    text,
)
from sqlalchemy.pool import StaticPool

import billing_core
from billing_core import database
from billing_core.models.invoice import Invoice, InvoiceStatus
from billing_core.sync import (
    map_status,
    sync_invoices,
)


@pytest.fixture()
def source_engine():
    """Create an in-memory SQLite database mimicking preventive_care schema."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    meta = MetaData()

    Table(
        "invoices",
        meta,
        Column("id", Integer, primary_key=True),
        Column("invoice_number", String(50), nullable=True),
        Column("patient_id", Integer, nullable=False),
        Column("diagnosis", String(255), nullable=True),
        Column("due_date", Date, nullable=True),
        Column("amount", Numeric(10, 2), nullable=False),
        Column("status", String(30), nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=True),
        Column("deleted_at", Integer, default=0),
        Column("lab_order_id", Integer, nullable=True),
        Column("paid_at", DateTime, nullable=True),
    )

    Table(
        "invoice_items",
        meta,
        Column("id", Integer, primary_key=True),
        Column("invoice_id", Integer, nullable=False),
        Column("description", String(255), nullable=False),
        Column("quantity", Integer, nullable=False),
        Column("unit_price", Numeric(10, 2), nullable=False),
        Column("amount", Numeric(10, 2), nullable=False),
        Column("deleted_at", Integer, default=0),
    )

    meta.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def setup_billing_db():
    """Initialize fresh in-memory billing_db."""
    billing_core.init("sqlite:///:memory:", app_name="preventive-care")
    yield


# ---------------------------------------------------------------------------
# Status Mapping Unit Tests
# ---------------------------------------------------------------------------

def test_map_status_conversion():
    assert map_status("PAID", 0) == InvoiceStatus.PAID
    assert map_status("PENDING", 0) == InvoiceStatus.PENDING
    assert map_status("FAILED", 0) == InvoiceStatus.FAILED
    assert map_status("PAID", 1) == InvoiceStatus.CANCELLED
    assert map_status("PENDING", 1) == InvoiceStatus.CANCELLED
    assert map_status("FAILED", 1) == InvoiceStatus.CANCELLED


# ---------------------------------------------------------------------------
# Sync Tests
# ---------------------------------------------------------------------------

def test_sync_field_mapping_lab_and_non_lab(source_engine):
    """Test mapping of lab bill vs non-lab bill, dates, customer_id, amounts, and items."""
    now = datetime(2026, 4, 16, 10, 0, 0)
    with source_engine.begin() as conn:
        # Row 1: Lab bill (has lab_order_id, has child items)
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, diagnosis, due_date, amount, status, created_at, updated_at, deleted_at, lab_order_id, paid_at) "
                "VALUES (1, 'INV-LAB-001', 101, 'Blood Work', '2026-05-01', 450.00, 'PAID', :now, :now, 0, 5001, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO invoice_items (id, invoice_id, description, quantity, unit_price, amount, deleted_at) "
                "VALUES (1, 1, 'CBC Test', 1, 300.00, 300.00, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO invoice_items (id, invoice_id, description, quantity, unit_price, amount, deleted_at) "
                "VALUES (2, 1, 'Lipid Panel', 1, 150.00, 150.00, 0)"
            )
        )

        # Row 2: Non-lab bill (lab_order_id is None, no child items -> fallback to diagnosis)
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, diagnosis, due_date, amount, status, created_at, updated_at, deleted_at, lab_order_id, paid_at) "
                "VALUES (2, 'INV-GEN-002', 202, 'General Consultation', '2026-05-10', 500.00, 'PENDING', :now, :now, 0, NULL, NULL)"
            ),
            {"now": now},
        )

    with database.SessionLocal() as session:
        stats = sync_invoices(
            source_engine=source_engine,
            billing_session=session,
            app_name="preventive-care",
        )
        assert stats["synced"] == 2
        assert stats["created"] == 2

        # Verify Lab Bill in billing_db
        inv_lab = session.query(Invoice).filter(Invoice.invoice_number == "INV-LAB-001").first()
        assert inv_lab is not None
        assert inv_lab.customer_id == "101"
        assert inv_lab.reference_type == "lab_order"
        assert inv_lab.status == InvoiceStatus.PAID
        assert inv_lab.total_amount == Decimal("450.00")
        assert inv_lab.app_name == "preventive-care"
        assert len(inv_lab.items) == 2
        assert inv_lab.items[0]["description"] == "CBC Test"
        assert inv_lab.items[0]["amount"] == "300.00"
        assert inv_lab.items[1]["description"] == "Lipid Panel"
        assert inv_lab.items[1]["amount"] == "150.00"

        # Verify Non-Lab Bill in billing_db
        inv_gen = session.query(Invoice).filter(Invoice.invoice_number == "INV-GEN-002").first()
        assert inv_gen is not None
        assert inv_gen.customer_id == "202"
        assert inv_gen.reference_type is None
        assert inv_gen.status == InvoiceStatus.PENDING
        assert inv_gen.total_amount == Decimal("500.00")
        # Created one fallback item from diagnosis
        assert len(inv_gen.items) == 1
        assert inv_gen.items[0]["description"] == "General Consultation"
        assert inv_gen.items[0]["quantity"] == "1.000"
        assert inv_gen.items[0]["unit_price"] == "500.00"
        assert inv_gen.items[0]["amount"] == "500.00"


def test_sync_status_conversion_and_deleted_cancelled(source_engine):
    """Test PAID->paid, PENDING->pending, FAILED->failed, and deleted_at=1->cancelled."""
    now = datetime(2026, 4, 16, 12, 0, 0)
    with source_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, diagnosis, amount, status, created_at, updated_at, deleted_at) "
                "VALUES (10, 'INV-FAILED', 301, 'Failed Check', 120.00, 'FAILED', :now, :now, 0)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, diagnosis, amount, status, created_at, updated_at, deleted_at) "
                "VALUES (11, 'INV-DEL', 302, 'Deleted Check', 200.00, 'PAID', :now, :now, 1)"
            ),
            {"now": now},
        )

    with database.SessionLocal() as session:
        sync_invoices(source_engine, session, app_name="preventive-care")

        inv_failed = session.query(Invoice).filter(Invoice.invoice_number == "INV-FAILED").first()
        assert inv_failed.status == InvoiceStatus.FAILED

        inv_del = session.query(Invoice).filter(Invoice.invoice_number == "INV-DEL").first()
        assert inv_del.status == InvoiceStatus.CANCELLED


def test_sync_idempotence_no_duplicates(source_engine):
    """Running sync twice does not duplicate records in billing_db."""
    now = datetime(2026, 4, 16, 12, 0, 0)
    with source_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, diagnosis, amount, status, created_at, updated_at, deleted_at) "
                "VALUES (20, 'INV-IDEMPOTENT', 401, 'Checkup', 350.00, 'PENDING', :now, :now, 0)"
            ),
            {"now": now},
        )

    with database.SessionLocal() as session:
        # First sync run
        res1 = sync_invoices(source_engine, session, app_name="preventive-care")
        assert res1["created"] == 1

        total_first = session.query(Invoice).filter(Invoice.invoice_number == "INV-IDEMPOTENT").count()
        assert total_first == 1

        # Second sync run
        res2 = sync_invoices(source_engine, session, app_name="preventive-care")
        # No duplicates created
        assert res2["created"] == 0

        total_second = session.query(Invoice).filter(Invoice.invoice_number == "INV-IDEMPOTENT").count()
        assert total_second == 1


def test_sync_update_in_source_reaches_billing_db(source_engine):
    """An update to a row in source updates the existing billing_db record."""
    t1 = datetime(2026, 4, 16, 12, 0, 0)
    with source_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, diagnosis, amount, status, created_at, updated_at, deleted_at) "
                "VALUES (30, 'INV-UPDATEME', 501, 'Initial Diagnosis', 100.00, 'PENDING', :t1, :t1, 0)"
            ),
            {"t1": t1},
        )

    with database.SessionLocal() as session:
        sync_invoices(source_engine, session, app_name="preventive-care")
        inv = session.query(Invoice).filter(Invoice.invoice_number == "INV-UPDATEME").first()
        assert inv.status == InvoiceStatus.PENDING
        assert inv.total_amount == Decimal("100.00")

    # Update row in source (status changed to PAID, amount updated, updated_at advanced)
    t2 = datetime(2026, 4, 17, 14, 0, 0)
    with source_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE invoices SET status = 'PAID', amount = 150.00, updated_at = :t2, paid_at = :t2 WHERE id = 30"
            ),
            {"t2": t2},
        )

    with database.SessionLocal() as session:
        res = sync_invoices(source_engine, session, app_name="preventive-care")
        assert res["updated"] == 1

        inv_updated = session.query(Invoice).filter(Invoice.invoice_number == "INV-UPDATEME").first()
        assert inv_updated.status == InvoiceStatus.PAID
        assert inv_updated.total_amount == Decimal("150.00")


def test_sync_duplicate_invoice_number_in_source_skipped(source_engine):
    """If two source rows share the same invoice_number, the duplicate is skipped without crashing."""
    now = datetime(2026, 4, 16, 12, 0, 0)
    with source_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, amount, status, created_at, updated_at, deleted_at) "
                "VALUES (41, 'INV-DUPLICATE', 601, 100.00, 'PENDING', :now, :now, 0)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, amount, status, created_at, updated_at, deleted_at) "
                "VALUES (42, 'INV-DUPLICATE', 602, 200.00, 'PAID', :now, :now, 0)"
            ),
            {"now": now},
        )

    with database.SessionLocal() as session:
        stats = sync_invoices(source_engine, session, app_name="preventive-care")
        assert stats["created"] == 1
        assert stats["skipped"] == 1

        invoices = session.query(Invoice).filter(Invoice.invoice_number == "INV-DUPLICATE").all()
        assert len(invoices) == 1
        assert invoices[0].customer_id == "601"


def test_auto_sync_on_init(source_engine, monkeypatch, tmp_path):
    """Verify that billing_core.init automatically syncs when source_database_url is provided."""
    sqlite_file = tmp_path / "test_billing.db"
    billing_url = f"sqlite:///{sqlite_file}"
    now = datetime(2026, 4, 16, 12, 0, 0)

    with source_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, amount, status, created_at, updated_at, deleted_at) "
                "VALUES (50, 'INV-AUTOINIT', 701, 250.00, 'PENDING', :now, :now, 0)"
            ),
            {"now": now},
        )

    monkeypatch.setattr("billing_core.sync.get_source_engine", lambda url: source_engine)

    billing_core.init(
        billing_url,
        app_name="preventive-care",
        source_database_url="sqlite:///dummy_source.db",
        auto_sync=True,
    )

    with database.SessionLocal() as session:
        inv = session.query(Invoice).filter(Invoice.invoice_number == "INV-AUTOINIT").first()
        assert inv is not None
        assert inv.customer_id == "701"
        assert inv.status == InvoiceStatus.PENDING
        assert inv.total_amount == Decimal("250.00")


def test_list_invoices_and_get_by_number(source_engine):
    """Test InvoiceService.list_invoices() and get_invoice_by_number()."""
    now = datetime(2026, 4, 16, 12, 0, 0)
    with source_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, amount, status, created_at, updated_at, deleted_at) "
                "VALUES (60, 'INV-LIST-1', 801, 100.00, 'PAID', :now, :now, 0)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, amount, status, created_at, updated_at, deleted_at) "
                "VALUES (61, 'INV-LIST-2', 801, 200.00, 'PENDING', :now, :now, 0)"
            ),
            {"now": now},
        )

    with database.SessionLocal() as session:
        sync_invoices(source_engine, session, app_name="preventive-care")
        service = billing_core.InvoiceService(session)

        # Test get_invoice_by_number
        inv1 = service.get_invoice_by_number("INV-LIST-1")
        assert inv1 is not None
        assert inv1.invoice_number == "INV-LIST-1"
        assert inv1.status == InvoiceStatus.PAID

        # Test list_invoices
        all_invs = service.list_invoices()
        assert len(all_invs) >= 2

        # Filter by customer
        cust_invs = service.list_invoices(customer_id="801")
        assert len(cust_invs) == 2

        # Filter by status
        paid_invs = service.list_invoices(status=InvoiceStatus.PAID, customer_id="801")
        assert len(paid_invs) == 1
        assert paid_invs[0].invoice_number == "INV-LIST-1"


def test_auto_sync_updates_status(source_engine, monkeypatch):
    """Test that auto_sync_if_configured automatically pulls status changes into billing_db."""
    now = datetime(2026, 4, 16, 12, 0, 0)
    with source_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices (id, invoice_number, patient_id, amount, status, created_at, updated_at, deleted_at) "
                "VALUES (70, 'INV-AUTOSTATUS', 901, 300.00, 'PENDING', :now, :now, 0)"
            ),
            {"now": now},
        )

    monkeypatch.setattr("billing_core.sync.get_source_engine", lambda url: source_engine)

    # First sync via auto_sync_if_configured
    billing_core.auto_sync_if_configured(
        app_name="preventive-care",
        source_url="sqlite:///dummy.db",
        min_interval_seconds=0.0,
    )

    with database.SessionLocal() as session:
        service = billing_core.InvoiceService(session)
        inv = service.get_invoice_by_number("INV-AUTOSTATUS")
        assert inv is not None
        assert inv.status == InvoiceStatus.PENDING

    # Now simulate preventive care updating status to PAID
    later = datetime(2026, 4, 16, 12, 5, 0)
    with source_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE invoices SET status = 'PAID', updated_at = :later WHERE id = 70"
            ),
            {"later": later},
        )

    # Next call to auto_sync_if_configured detects and updates the status
    billing_core.auto_sync_if_configured(
        app_name="preventive-care",
        source_url="sqlite:///dummy.db",
        min_interval_seconds=0.0,
    )

    with database.SessionLocal() as session:
        service = billing_core.InvoiceService(session)
        inv = service.get_invoice_by_number("INV-AUTOSTATUS")
        assert inv.status == InvoiceStatus.PAID
