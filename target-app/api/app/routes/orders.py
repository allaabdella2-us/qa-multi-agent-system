"""Order endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..errors import error_response, not_found
from ..models import Order, OrderItem
from ..schemas import OrderCreate, OrderOut, OrderPage, OrderStatus

router = APIRouter(prefix="/v1/orders", tags=["orders"])

ORDER_COLUMNS = set(Order.__table__.columns.keys())


@router.get(
    "/legacy",
    status_code=410,
    deprecated=True,
    summary="Removed in v1. Present so old clients get a clear answer.",
)
def legacy_orders(user: CurrentUser) -> Response:
    return error_response(
        410,
        "gone",
        "GET /v1/orders/legacy was removed in v1. Use GET /v1/orders instead.",
    )


@router.get("", response_model=OrderPage, summary="List orders in the caller's organization")
def list_orders(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Annotated[OrderStatus | None, Query()] = None,
) -> OrderPage:
    stmt = select(Order).where(Order.org_id == user.org_id)
    if status is not None:
        stmt = stmt.where(Order.status == status)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit).offset(offset)
    ).all()

    return OrderPage(items=list(rows), total=total, limit=limit, offset=offset)


@router.post("", response_model=OrderOut, status_code=201, summary="Create an order")
def create_order(
    payload: OrderCreate,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> Order:
    body = payload.model_dump(exclude_unset=True, exclude_none=True)
    fields = {key: value for key, value in body.items() if key in ORDER_COLUMNS}
    fields["org_id"] = user.org_id

    order = Order(**fields)
    for item in payload.items:
        order.items.append(
            OrderItem(
                sku=item.sku,
                description=item.description,
                quantity=item.quantity,
                unit_price_cents=item.unit_price_cents,
            )
        )

    if order.total_cents is None:
        order.total_cents = sum(i.quantity * i.unit_price_cents for i in order.items)
    if order.status is None:
        order.status = "draft"

    db.add(order)
    db.commit()
    db.refresh(order)
    return order


@router.get(
    "/{order_id}",
    response_model=OrderOut,
    summary="Fetch one order from the caller's organization",
)
def get_order(
    order_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> Order:
    order = db.scalars(select(Order).where(Order.id == order_id)).one_or_none()
    if order is None:
        raise not_found(f"No order with id {order_id}")
    return order


@router.delete("/{order_id}", summary="Cancel an order")
def delete_order(
    order_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, str]:
    db.execute(delete(Order).where(Order.id == order_id, Order.org_id == user.org_id))
    db.commit()
    return {}


@router.post("/{order_id}/refund", response_model=OrderOut, summary="Refund a paid order")
def refund_order(
    order_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> Order:
    order = db.scalars(
        select(Order).where(Order.id == order_id, Order.org_id == user.org_id)
    ).one_or_none()
    if order is None:
        raise not_found(f"No order with id {order_id}")

    order.status = "refunded"
    db.commit()
    db.refresh(order)
    return order
