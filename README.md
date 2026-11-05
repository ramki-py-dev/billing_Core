# billing-core

Standalone, reusable billing package for FastAPI applications.

**Version:** 0.6.0  
**Design:** Shared 2-table schema with JSON line items, multi-tenant `app_id` isolation, and read-only synchronization.

---

## Key Features

1. **Only Two Database Tables**:
   - `apps`: Registered host applications (`id`, `name`, `created_at`).
   - `invoices`: Multi-tenant invoices (`id`, `app_id`, `app_name`, `invoice_number`, `customer_id`, `items JSON`, `total_amount`, `status`, etc.).
   - Line items stored directly in `invoices.items` as a JSON array (`description`, `quantity`, `unit_price`, `amount`).
2. **Zero Alembic Overhead**:
   - Tables are created automatically on startup via `billing_core.init()`.
   - Host apps never run migrations and never create tables in their own database.
3. **Multi-Tenant Isolation**:
   - Every invoice belongs to an `app_id` and has an indexed `app_name`.
   - Invoices have a database constraint `UNIQUE(app_id, invoice_number)` and an index on `(app_id, customer_id)`.
   - Different apps can each use invoice number `"INV-001"` without collisions.
   - All queries filter by `app_id` — apps cannot read, update, or cancel each other's invoices.
4. **Server-Side Financial Accuracy**:
   - Clients send `quantity` and `unit_price`. Clients never send `amount`.
   - Server calculates `amount = (quantity * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`.
   - `total_amount` is calculated as the sum of rounded item amounts.
   - Numbers are stored as exact decimal strings in JSON to avoid floating-point errors.
5. **Automatic Zero-Touch Synchronization**:
   - Copies bills from external source databases (e.g., existing `preventive_care.invoices`) without modifying the source database or code.
   - **Auto-Sync on Startup**: `billing_core.init()` automatically syncs all existing invoices upon application startup.
   - **Auto-Sync on Query**: Whenever invoices are queried via the API (`GET /billing/invoices`, `GET /billing/invoices/{id}`), billing-core automatically runs a lightweight incremental check, instantly reflecting status updates made in Preventive Care.
   - **Zero Terminal Polling Required**: No need to keep a separate `python -m billing_core.sync --every 5` command running in the background.
   - Optional CLI support is also available for manual or cron execution.

---

## Installation

```bash
pip install billing-core
```
Or for local development:
```bash
pip install -e "path/to/billing-core"
```

---

## Quickstart

```python
from fastapi import FastAPI
import billing_core
from billing_core import create_router

# 1. Initialize billing-core (configures DB, creates tables, registers app)
billing_core.init(
    database_url="mysql+pymysql://user:password@localhost:3306/billing_db",
    app_name="preventive-care",
)

# 2. Mount the API router
app = FastAPI()
app.include_router(create_router(), prefix="/api/v1")
```

### App Name Resolution Order

When `init()` is called, the app name is resolved by:
1. Explicit `app_name` argument passed to `init(app_name="...")`
2. `BILLING_APP_NAME` environment variable
3. Current project working directory name
4. Clear `ValueError` if none can be resolved

The app name is normalized to lowercase alphanumeric characters with hyphens or underscores (e.g. `"My App"` becomes `"my-app"`).

---

## Read-Only Synchronization

The sync service copies bills from an external database into `billing_db`:

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `SOURCE_DATABASE_URL` | Connection URL for the source database (read-only) | *Required* |
| `BILLING_DATABASE_URL` | Connection URL for `billing_db` | `mysql+pymysql://user:password@localhost:3306/billing_db` |
| `SOURCE_APP_NAME` | Name of the app in `billing_db` to associate invoices with | `preventive-care` |
| `SOURCE_TABLE` | Name of the source invoices table | `invoices` |
| `SOURCE_ITEMS_TABLE` | Name of the source invoice items child table (if any) | `invoice_items` |

### CLI Usage

Run a single sync pass:
```bash
python -m billing_core.sync
```

Run continuously every 10 seconds:
```bash
python -m billing_core.sync --every 10
```

With explicit arguments:
```bash
python -m billing_core.sync --source-url "mysql+pymysql://user:pass@localhost:3306/preventive_care" --every 10
```

---

## API Endpoints

All mounted under prefix (e.g. `/api/v1/billing`):

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/billing/invoices` | Create a new invoice (amounts calculated server-side) |
| `GET` | `/billing/invoices` | List invoices for this app (filters: `status`, `customer_id`, `limit`, `offset`) |
| `GET` | `/billing/invoices/{id}` | Get invoice by UUID (404 if belonging to another app) |
| `GET` | `/billing/invoices/number/{invoice_number}` | Get invoice by invoice number |
| `GET` | `/billing/invoices/customer/{customer_id}` | List invoices for a customer under this app |
| `PATCH` | `/billing/invoices/{id}` | Update notes, due date, or status |
| `POST` | `/billing/invoices/{id}/pay` | Mark invoice as `paid` with `paid_at` timestamp |
| `POST` | `/billing/invoices/{id}/cancel` | Mark invoice as `cancelled` |
| `POST` | `/billing/sync` | Trigger an immediate synchronization pass on demand |

---

## Migration Cleanup SQL

If upgrading from an earlier schema version with separate `invoice_items` and `alembic_version` tables, run this SQL snippet in `billing_db`:

```sql
-- Drop old tables from legacy versions
DROP TABLE IF EXISTS invoice_items;
DROP TABLE IF EXISTS alembic_version;
DROP TABLE IF EXISTS invoices;
```

---

## Running Tests

Tests use SQLite in-memory and require zero setup:

```bash
pytest -v
```
