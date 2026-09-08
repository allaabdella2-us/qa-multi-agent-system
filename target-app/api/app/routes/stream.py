"""WebSocket feed of order activity for the caller's organization."""

from __future__ import annotations

import asyncio

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from ..auth import decode_access_token
from ..db import SessionLocal
from ..models import Order

router = APIRouter(prefix="/v1/orders", tags=["orders"])

POLL_SECONDS = 2.0
SNAPSHOT_SIZE = 25

POLICY_VIOLATION = 1008


def _recent_orders(org_id: int) -> list[dict[str, object]]:
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(Order)
            .where(Order.org_id == org_id)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(SNAPSHOT_SIZE)
        ).all()
        return [
            {
                "id": order.id,
                "reference": order.reference,
                "status": order.status,
                "total_cents": order.total_cents,
                "currency": order.currency,
                "created_at": order.created_at.isoformat(),
            }
            for order in rows
        ]
    finally:
        db.close()


@router.websocket("/stream")
async def orders_stream(websocket: WebSocket, token: str | None = None) -> None:
    if not token:
        await websocket.close(code=POLICY_VIOLATION, reason="Missing token")
        return

    try:
        claims = decode_access_token(token)
        org_id = int(claims["org_id"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        await websocket.close(code=POLICY_VIOLATION, reason="Invalid token")
        return

    await websocket.accept()

    try:
        previous: list[dict[str, object]] = []
        while True:
            orders = await run_in_threadpool(_recent_orders, org_id)
            if orders != previous:
                await websocket.send_json({"type": "orders", "orders": orders})
                previous = orders
            await asyncio.sleep(POLL_SECONDS)
    except WebSocketDisconnect:
        return
