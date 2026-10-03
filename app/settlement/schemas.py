from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class SettlementCreate(BaseModel):
    order_code: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
    family_name: str = Field(min_length=1, max_length=120)
    ceremony_type: Literal["wedding", "funeral"]
    ceremony_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


class FeeItemPayload(BaseModel):
    external_ref: str = Field(min_length=1, max_length=120)
    direction: Literal["receivable", "payable"]
    category: Literal["gift_money", "venue_overtime", "supplier_usage", "package_fee", "other"]
    description: str = Field(default="", max_length=500)
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=3)
    unit: str = Field(default="", max_length=20)
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    voucher_no: str | None = Field(default=None, max_length=120)


class ImportBatchRequest(BaseModel):
    batch_key: str = Field(min_length=3, max_length=160)
    items: list[FeeItemPayload] = Field(min_length=1, max_length=500)


class NoteRequest(BaseModel):
    note: str = Field(default="", max_length=1000)


class PublishRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=1000)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)


class ResolveConflictRequest(BaseModel):
    action: Literal["keep_existing", "accept_incoming"]
    reason: str = Field(min_length=2, max_length=1000)
