"""Request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OrderStatus = Literal["draft", "placed", "paid", "refunded", "cancelled"]
InvoiceStatus = Literal["open", "paid", "void"]
Role = Literal["admin", "member", "viewer"]


class OrderItemIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=255)
    quantity: int = Field(ge=1)
    unit_price_cents: int = Field(ge=0)


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sku: str
    description: str
    quantity: int
    unit_price_cents: int


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    reference: str = Field(min_length=1, max_length=64)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    items: list[OrderItemIn] = Field(min_length=1)


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    reference: str
    status: OrderStatus
    total_cents: int
    currency: str
    created_at: datetime
    items: list[OrderItemOut] = []


class OrderPage(BaseModel):
    items: list[OrderOut]
    total: int
    limit: int
    offset: int


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    number: str
    amount_cents: int
    issued_at: datetime
    status: InvoiceStatus


class InvoicePage(BaseModel):
    items: list[InvoiceOut]
    total: int
    limit: int
    offset: int


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    role: Role


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
