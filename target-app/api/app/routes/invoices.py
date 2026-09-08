"""Invoice endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..models import Invoice, Order
from ..schemas import InvoicePage

router = APIRouter(prefix="/v1/invoices", tags=["invoices"])


@router.get("", response_model=InvoicePage, summary="List invoices in the caller's organization")
def list_invoices(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InvoicePage:
    stmt = (
        select(Invoice)
        .join(Order, Order.id == Invoice.order_id)
        .where(Order.org_id == user.org_id)
    )

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Invoice.issued_at.desc(), Invoice.id.desc()).limit(limit).offset(offset)
    ).all()

    return InvoicePage(items=list(rows), total=total, limit=limit, offset=offset)
