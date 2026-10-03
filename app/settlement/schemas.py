from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EntryInput(BaseModel):
    entry_type: Literal["payable", "receipt"]
    line_key: str = Field(default="", max_length=120)
    category: str = Field(min_length=1, max_length=80)
    counterparty: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=500)
    amount: Any = Field(...)
    quantity: Any = Field(default=None)
    unit_price: Any = Field(default=None)
    source_ref: str = Field(default="", max_length=120)
    voucher_no: str = Field(default="", max_length=80)
    voucher_required: bool = True


class BatchImport(BaseModel):
    import_batch: str = Field(min_length=6, max_length=160)
    entries: list[EntryInput] = Field(min_length=1, max_length=2000)
    replace: bool = False
    note: str = Field(default="", max_length=1000)


class CaseCreate(BaseModel):
    case_code: str = Field(min_length=2, max_length=64)
    ceremony_type: str = Field(default="", max_length=80)
    family_contact: str = Field(default="", max_length=120)


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)


class NewVersionRequest(BaseModel):
    basis: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=1000)
