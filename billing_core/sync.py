"""Read-only synchronization of invoices from an external database into billing_db.

Never writes to the source database.
Idempotent upsert by (app_id, invoice_number).
Supports incremental sync tracking via the sync_state table.
"""

import argparse
import logging
import os
import time
import uuid

from dotenv import load_dotenv

load_dotenv()
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import Session

from billing_core.database import (
    get_engine,
    normalize_app_name,
)
from billing_core.models.invoice import App, Invoice, InvoiceStatus

logger = logging.getLogger("billing_core.sync")

# Metadata and table definition for sync_state (kept independent from Base.metadata)
sync_metadata = MetaData()

sync_state_table = Table(
    "sync_state",
    sync_metadata,
    Column("id", Uuid, primary_key=True, default=uuid.uuid4),
    Column("app_id", Uuid, nullable=False, index=True),
    Column("source_table", String(100), nullable=False),
    Column("last_synced_at", DateTime, nullable=True),
    Column(
        "updated_at",
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    ),
    UniqueConstraint("app_id", "source_table", name="uq_sync_state_app_source"),
)

DEFAULT_INVOICE_COLUMN_MAPPING = {
    "id": "id",
    "invoice_number": "invoice_number",
    "customer_id": "patient_id",
    "amount": "amount",
    "status": "status",
    "diagnosis": "diagnosis",
    "lab_order_id": "lab_order_id",
    "due_date": "due_date",
    "paid_at": "paid_at",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "deleted_at": "deleted_at",
}

DEFAULT_ITEM_COLUMN_MAPPING = {
    "id": "id",
    "invoice_id": "invoice_id",
    "description": "description",
    "quantity": "quantity",
    "unit_price": "unit_price",
    "amount": "amount",
    "deleted_at": "deleted_at",
}


def ensure_sync_state_table(engine) -> None:
    """Create the sync_state table in billing_db if not already present."""
    sync_metadata.create_all(bind=engine, checkfirst=True)


def map_status(source_status: Any, deleted_at: Any = None) -> InvoiceStatus:
    """Convert source status and deleted flag to billing InvoiceStatus."""
    if deleted_at == 1 or str(deleted_at) == "1":
        return InvoiceStatus.CANCELLED

    val = str(source_status or "").strip().upper()
    if val == "PAID":
        return InvoiceStatus.PAID
    elif val == "PENDING":
        return InvoiceStatus.PENDING
    elif val == "FAILED":
        return InvoiceStatus.FAILED
    elif val == "CANCELLED":
        return InvoiceStatus.CANCELLED
    elif val == "DRAFT":
        return InvoiceStatus.DRAFT
    elif val == "SENT":
        return InvoiceStatus.SENT
    elif val == "OVERDUE":
        return InvoiceStatus.OVERDUE
    return InvoiceStatus.PENDING


def get_last_synced_timestamp(
    billing_engine, app_id: UUID, source_table: str
) -> Optional[datetime]:
    """Retrieve the last synced timestamp for this app and source table."""
    ensure_sync_state_table(billing_engine)
    with billing_engine.connect() as conn:
        stmt = select(sync_state_table.c.last_synced_at).where(
            sync_state_table.c.app_id == app_id,
            sync_state_table.c.source_table == source_table,
        )
        result = conn.execute(stmt).scalar()
        return result


def update_last_synced_timestamp(
    billing_engine, app_id: UUID, source_table: str, last_synced_at: datetime
) -> None:
    """Record the latest synced source timestamp in sync_state."""
    ensure_sync_state_table(billing_engine)
    with billing_engine.begin() as conn:
        stmt = select(sync_state_table.c.id).where(
            sync_state_table.c.app_id == app_id,
            sync_state_table.c.source_table == source_table,
        )
        existing_id = conn.execute(stmt).scalar()
        now = datetime.now(timezone.utc)
        if existing_id:
            conn.execute(
                sync_state_table.update()
                .where(sync_state_table.c.id == existing_id)
                .values(last_synced_at=last_synced_at, updated_at=now)
            )
        else:
            conn.execute(
                sync_state_table.insert().values(
                    id=uuid.uuid4(),
                    app_id=app_id,
                    source_table=source_table,
                    last_synced_at=last_synced_at,
                    updated_at=now,
                )
            )


def sync_invoices(
    source_engine,
    billing_session: Session,
    app_name: str = "preventive-care",
    source_table: str = "invoices",
    source_items_table: Optional[str] = "invoice_items",
    column_mapping: Optional[Dict[str, str]] = None,
    item_column_mapping: Optional[Dict[str, str]] = None,
    force_full: bool = False,
) -> Dict[str, int]:
    """Execute a single sync pass from source to billing_db.

    Returns stats dict: {synced, created, updated, skipped}.
    """
    col_map = {**DEFAULT_INVOICE_COLUMN_MAPPING, **(column_mapping or {})}
    item_map = {**DEFAULT_ITEM_COLUMN_MAPPING, **(item_column_mapping or {})}

    # Ensure app exists in billing_db
    norm_app_name = normalize_app_name(app_name)
    app = billing_session.query(App).filter(App.name == norm_app_name).first()
    if not app:
        app = App(name=norm_app_name)
        billing_session.add(app)
        billing_session.commit()
        billing_session.refresh(app)

    billing_engine = billing_session.get_bind()
    last_synced = None if force_full else get_last_synced_timestamp(billing_engine, app.id, source_table)

    # Check available tables in source
    source_inspector = inspect(source_engine)
    available_tables = set(source_inspector.get_table_names())
    has_items_table = bool(source_items_table and source_items_table in available_tables)

    # Build SELECT query on source
    query_parts = [f"SELECT * FROM {source_table}"]
    params = {}
    if last_synced is not None and not force_full:
        updated_col = col_map.get("updated_at", "updated_at")
        paid_col = col_map.get("paid_at", "paid_at")
        created_col = col_map.get("created_at", "created_at")
        time_check = (
            f"COALESCE({updated_col}, {paid_col}, {created_col}) >= :last_synced"
            if paid_col
            else f"COALESCE({updated_col}, {created_col}) >= :last_synced"
        )
        # Capture rows changed since last_synced, OR rows where updated_at is NULL
        query_parts.append(
            f"WHERE ({time_check} OR {updated_col} IS NULL)"
        )
        params["last_synced"] = last_synced

    order_col = col_map.get("updated_at", "updated_at")
    created_col = col_map.get("created_at", "created_at")
    query_parts.append(f"ORDER BY COALESCE({order_col}, {created_col}) ASC")
    query_str = " ".join(query_parts)

    with source_engine.connect() as source_conn:
        rows = source_conn.execute(text(query_str), params).mappings().fetchall()

        # If items table exists, pre-fetch items for retrieved invoices
        items_by_invoice_id: Dict[Any, List[Dict[str, Any]]] = {}
        if has_items_table and rows:
            src_inv_ids = [r[col_map["id"]] for r in rows if r.get(col_map["id"]) is not None]
            if src_inv_ids:
                item_inv_col = item_map.get("invoice_id", "invoice_id")
                # Format parameters safely
                items_query = text(f"SELECT * FROM {source_items_table}")
                all_items = source_conn.execute(items_query).mappings().fetchall()
                for it in all_items:
                    inv_id = it.get(item_inv_col)
                    if inv_id in src_inv_ids:
                        items_by_invoice_id.setdefault(inv_id, []).append(dict(it))

    stats = {"synced": 0, "created": 0, "updated": 0, "skipped": 0}
    seen_invoice_numbers = set()
    max_timestamp: Optional[datetime] = last_synced

    for row in rows:
        inv_num_raw = row.get(col_map["invoice_number"])
        if not inv_num_raw or not str(inv_num_raw).strip():
            logger.debug(
                "Skipping row id %s: missing or empty invoice_number",
                row.get(col_map["id"]),
            )
            stats["skipped"] += 1
            continue

        invoice_number = str(inv_num_raw).strip()

        # Duplicate invoice_number in the same batch
        if invoice_number in seen_invoice_numbers:
            logger.warning(
                "Duplicate invoice_number '%s' found in source batch. Skipping duplicate row id %s.",
                invoice_number,
                row.get(col_map["id"]),
            )
            stats["skipped"] += 1
            continue
        seen_invoice_numbers.add(invoice_number)

        # 2. Customer ID
        patient_id = row.get(col_map["customer_id"])
        customer_id = str(patient_id) if patient_id is not None else "unknown"

        # 3. Reference type (lab bills only)
        lab_order_id = row.get(col_map.get("lab_order_id", "lab_order_id"))
        if lab_order_id is not None and str(lab_order_id).strip() and str(lab_order_id).strip() != "0":
            reference_type = "lab_order"
        else:
            reference_type = None

        # 4. Total amount
        raw_amt = row.get(col_map["amount"]) or 0
        total_amount = Decimal(str(raw_amt)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # 5. Items
        src_id = row.get(col_map["id"])
        child_item_rows = items_by_invoice_id.get(src_id, [])
        formatted_items = []

        if child_item_rows:
            del_key = item_map.get("deleted_at", "deleted_at")
            for it in child_item_rows:
                if it.get(del_key) == 1 or str(it.get(del_key)) == "1":
                    continue
                desc = it.get(item_map.get("description", "description")) or "Item"
                qty = Decimal(str(it.get(item_map.get("quantity", "quantity")) or 1))
                price = Decimal(str(it.get(item_map.get("unit_price", "unit_price")) or 0)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                amt = (qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                formatted_items.append(
                    {
                        "description": str(desc),
                        "quantity": str(qty),
                        "unit_price": str(price),
                        "amount": str(amt),
                    }
                )

        if not formatted_items:
            diag = row.get(col_map.get("diagnosis", "diagnosis"))
            desc = str(diag).strip() if diag and str(diag).strip() else "Invoice"
            formatted_items = [
                {
                    "description": desc,
                    "quantity": "1.000",
                    "unit_price": str(total_amount),
                    "amount": str(total_amount),
                }
            ]

        # 6. Status
        raw_status = row.get(col_map["status"])
        deleted_val = row.get(col_map.get("deleted_at", "deleted_at"))
        status = map_status(raw_status, deleted_val)

        # 7. Dates
        raw_due = row.get(col_map.get("due_date", "due_date"))
        if isinstance(raw_due, date) and not isinstance(raw_due, datetime):
            due_date = datetime.combine(raw_due, datetime.min.time())
        elif isinstance(raw_due, datetime):
            due_date = raw_due
        else:
            due_date = None

        raw_created = row.get(col_map.get("created_at", "created_at"))
        issued_at = raw_created if isinstance(raw_created, datetime) else datetime.now(timezone.utc)

        raw_paid = row.get(col_map.get("paid_at", "paid_at"))
        paid_at = raw_paid if isinstance(raw_paid, datetime) else None

        # Track latest source timestamp
        row_ts = (
            row.get(col_map.get("updated_at", "updated_at"))
            or row.get(col_map.get("paid_at", "paid_at"))
            or raw_created
        )
        if isinstance(row_ts, datetime):
            if max_timestamp is None or row_ts > max_timestamp:
                max_timestamp = row_ts

        # Upsert into billing_db.invoices
        existing_invoice = (
            billing_session.query(Invoice)
            .filter(
                Invoice.app_id == app.id,
                Invoice.invoice_number == invoice_number,
            )
            .first()
        )

        if existing_invoice:
            changed = (
                existing_invoice.customer_id != customer_id
                or existing_invoice.reference_type != reference_type
                or existing_invoice.status != status
                or existing_invoice.total_amount != total_amount
                or existing_invoice.items != formatted_items
                or existing_invoice.due_date != due_date
                or (paid_at and existing_invoice.paid_at != paid_at)
            )
            if changed:
                existing_invoice.customer_id = customer_id
                existing_invoice.reference_type = reference_type
                existing_invoice.status = status
                existing_invoice.total_amount = total_amount
                existing_invoice.items = formatted_items
                existing_invoice.due_date = due_date
                existing_invoice.issued_at = issued_at
                if paid_at:
                    existing_invoice.paid_at = paid_at
                existing_invoice.app_name = app.name
                existing_invoice.updated_at = datetime.now(timezone.utc)
                stats["updated"] += 1
        else:
            new_inv = Invoice(
                app_id=app.id,
                app_name=app.name,
                invoice_number=invoice_number,
                customer_id=customer_id,
                reference_type=reference_type,
                status=status,
                total_amount=total_amount,
                items=formatted_items,
                due_date=due_date,
                issued_at=issued_at,
                paid_at=paid_at,
            )
            billing_session.add(new_inv)
            stats["created"] += 1

        stats["synced"] += 1

    billing_session.commit()

    if max_timestamp is not None:
        update_last_synced_timestamp(billing_engine, app.id, source_table, max_timestamp)

    logger.info(
        "Sync completed for %s: %d processed (%d created, %d updated, %d skipped).",
        app_name,
        stats["synced"],
        stats["created"],
        stats["updated"],
        stats["skipped"],
    )
    return stats


_cached_source_engines: Dict[str, Any] = {}
_last_sync_times: Dict[str, float] = {}


def get_source_engine(source_url: str):
    """Retrieve or create a cached SQLAlchemy engine for the source database."""
    if source_url not in _cached_source_engines:
        _cached_source_engines[source_url] = create_engine(source_url, pool_pre_ping=True)
    return _cached_source_engines[source_url]


def auto_sync_if_configured(
    app_name: Optional[str] = None,
    source_url: Optional[str] = None,
    min_interval_seconds: float = 0.0,
    force_full: bool = False,
) -> Optional[Dict[str, int]]:
    """Automatically run an incremental sync pass if SOURCE_DATABASE_URL is configured.

    Parameters
    ----------
    app_name:
        Target application name (defaults to current configured app or SOURCE_APP_NAME or 'preventive-care').
    source_url:
        Source database URL (defaults to SOURCE_DATABASE_URL env var).
    min_interval_seconds:
        Minimum seconds to wait between automatic sync calls (throttling). If 0, runs immediately.
    force_full:
        If True, ignores last_synced and syncs all source rows.

    Returns
    -------
    Optional[Dict[str, int]]:
        Stats dict if sync ran, None if skipped or unconfigured.
    """
    src_url = source_url or os.environ.get("SOURCE_DATABASE_URL")
    if not src_url:
        return None

    from billing_core.database import (
        SessionLocal,
        get_current_app_name,
        get_engine,
        normalize_app_name,
    )

    billing_engine = get_engine()
    if billing_engine is None:
        return None

    # Don't auto-sync into in-memory SQLite (used for isolated unit tests) unless source_url was explicitly provided
    if ":memory:" in str(billing_engine.url) and not source_url:
        return None

    resolved_app = (
        app_name
        or get_current_app_name()
        or os.environ.get("SOURCE_APP_NAME")
        or "preventive-care"
    )
    resolved_app = normalize_app_name(resolved_app)

    cache_key = f"{src_url}_{resolved_app}"
    now_mono = time.monotonic()
    if min_interval_seconds > 0 and not force_full:
        last_time = _last_sync_times.get(cache_key, 0.0)
        if now_mono - last_time < min_interval_seconds:
            return None

    try:
        ensure_sync_state_table(billing_engine)
        src_engine = get_source_engine(src_url)
        table = os.environ.get("SOURCE_TABLE", "invoices")
        items_table = os.environ.get("SOURCE_ITEMS_TABLE", "invoice_items")

        with SessionLocal() as session:
            stats = sync_invoices(
                source_engine=src_engine,
                billing_session=session,
                app_name=resolved_app,
                source_table=table,
                source_items_table=items_table,
                force_full=force_full,
            )
            _last_sync_times[cache_key] = time.monotonic()
            return stats
    except Exception as exc:
        logger.warning("auto_sync_if_configured failed: %s", exc)
        return None


def run_sync_cli():
    """CLI entrypoint for running the synchronization."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Synchronize invoices from source database into billing_db."
    )
    parser.add_argument(
        "--source-url",
        default=os.environ.get("SOURCE_DATABASE_URL"),
        help="Source database URL (defaults to SOURCE_DATABASE_URL env var)",
    )
    parser.add_argument(
        "--billing-url",
        default=os.environ.get("BILLING_DATABASE_URL"),
        help="Billing database URL (defaults to BILLING_DATABASE_URL env var)",
    )
    parser.add_argument(
        "--app-name",
        default=os.environ.get("SOURCE_APP_NAME", "preventive-care"),
        help="Target application name in billing_db (defaults to SOURCE_APP_NAME or 'preventive-care')",
    )
    parser.add_argument(
        "--table",
        default=os.environ.get("SOURCE_TABLE", "invoices"),
        help="Source invoices table name (defaults to SOURCE_TABLE or 'invoices')",
    )
    parser.add_argument(
        "--items-table",
        default=os.environ.get("SOURCE_ITEMS_TABLE", "invoice_items"),
        help="Source invoice items table name (defaults to 'invoice_items')",
    )
    parser.add_argument(
        "--every",
        type=int,
        default=None,
        help="Repeat interval in seconds (if not set, runs once and exits)",
    )

    args = parser.parse_args()

    source_url = args.source_url
    if not source_url:
        raise ValueError(
            "Source database URL is required. Provide --source-url or set SOURCE_DATABASE_URL."
        )

    # Configure billing database without auto_sync (CLI will handle its own sync loop)
    import billing_core
    billing_core.init(args.billing_url, app_name=args.app_name, auto_sync=False)
    from billing_core.database import SessionLocal

    source_engine = create_engine(source_url, pool_pre_ping=True)

    def do_sync():
        with SessionLocal() as session:
            return sync_invoices(
                source_engine=source_engine,
                billing_session=session,
                app_name=args.app_name,
                source_table=args.table,
                source_items_table=args.items_table,
            )

    if args.every is None:
        do_sync()
    else:
        logger.info(
            "Starting continuous sync every %d seconds for app '%s'...",
            args.every,
            args.app_name,
        )
        try:
            while True:
                do_sync()
                time.sleep(args.every)
        except KeyboardInterrupt:
            logger.info("Continuous sync stopped by user.")


if __name__ == "__main__":
    run_sync_cli()
