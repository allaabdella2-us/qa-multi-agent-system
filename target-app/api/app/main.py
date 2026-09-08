"""Corvid Orders API application."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import settings
from .errors import ApiError, api_error_handler, internal_error_response
from .routes import auth, invoices, orders, stream
from .schemas import HealthResponse

logger = logging.getLogger("corvid.api")

app = FastAPI(
    title="Corvid Orders API",
    version="1.0.0",
    description=(
        "Order and invoice management for small organizations. "
        "See openapi.yaml for the published contract."
    ),
    debug=settings.debug,
)

app.add_exception_handler(ApiError, api_error_handler)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error serving %s %s", request.method, request.url.path)
    return internal_error_response(exc)


app.include_router(auth.router)
app.include_router(orders.router)
app.include_router(invoices.router)
app.include_router(stream.router)


@app.get("/v1/health", response_model=HealthResponse, tags=["ops"], summary="Liveness probe")
def health() -> HealthResponse:
    return HealthResponse(status="ok")
