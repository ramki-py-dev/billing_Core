from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from billing_core.models.invoice import InvoiceStatus


class InvoiceItemCreate(BaseModel):
    description: Optional[str] = None
    name: Optional[str] = None
    quantity: Decimal = Decimal("1.000")
    unit_price: Decimal

    @model_validator(mode="after")
    def validate_description(self):
        if not self.description and self.name:
            self.description = self.name
        elif not self.description:
            raise ValueError("description is required for invoice item")
        return self


class InvoiceItemResponse(BaseModel):
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal

    model_config = ConfigDict(from_attributes=True)


class InvoiceCreate(BaseModel):
    invoice_number: str
    customer_id: str
    reference_type: Optional[str] = None
    notes: Optional[str] = None
    due_date: Optional[datetime] = None
    items: list[InvoiceItemCreate] = []


class InvoiceUpdate(BaseModel):
    notes: Optional[str] = None
    due_date: Optional[datetime] = None
    status: Optional[InvoiceStatus] = None


class InvoiceResponse(BaseModel):
    id: UUID
    app_id: UUID
    app_name: str
    invoice_number: str
    customer_id: str
    reference_type: Optional[str] = None
    status: InvoiceStatus
    total_amount: Decimal
    items: list[InvoiceItemResponse] = []
    notes: Optional[str] = None
    issued_at: datetime
    due_date: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("items", mode="before")
    @classmethod
    def coerce_items(cls, v: Any) -> list:
        """Convert JSON dicts (from ORM) to InvoiceItemResponse-compatible dicts."""
        if not v:
            return []
        result = []
        for item in v:
            if isinstance(item, dict):
                result.append(
                    {
                        "description": item["description"],
                        "quantity": Decimal(str(item["quantity"])),
                        "unit_price": Decimal(str(item["unit_price"])),
                        "amount": Decimal(str(item["amount"])),
                    }
                )
            else:
                result.append(item)
        return result
