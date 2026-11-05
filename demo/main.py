"""Demo FastAPI application showcasing billing-core (v0.6.0).

Run with::

    cd billing-core
    uvicorn demo.main:app --reload --port 8000
"""

import os
import sys
from pathlib import Path

# Ensure root directory is in sys.path
root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI

import billing_core
from billing_core.routes.invoice_routes import create_router

# Initialize billing-core:
# Configures DB, creates tables, registers app, and automatically syncs all invoices from SOURCE_DATABASE_URL
billing_core.init(app_name="preventive-care")

app = FastAPI(
    title="billing-core demo",
    version="0.6.0",
)

# Mount the router scoped to this app (includes /billing/invoices, /billing/sync, etc.)
app.include_router(
    create_router(),
    prefix="/api/v1",
)


@app.get("/health")
def health():
    return {"status": "ok"}
