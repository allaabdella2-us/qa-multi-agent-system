"""Error envelope helpers.

The envelope is ``{"error": {"code": ..., "message": ...}}``. Handlers raise
:class:`ApiError` and the app-level handler renders it.
"""

from __future__ import annotations

import traceback

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """An error that should reach the client as a rendered envelope."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Render an error envelope."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def not_found(message: str) -> ApiError:
    return ApiError(404, "not_found", message)


def forbidden(message: str) -> ApiError:
    return ApiError(403, "forbidden", message)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


def internal_error_response(exc: Exception) -> JSONResponse:
    """Render an unexpected exception, with the trace that produced it."""
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": str(exc) or exc.__class__.__name__,
                "trace": traceback.format_exc(),
            }
        },
    )
