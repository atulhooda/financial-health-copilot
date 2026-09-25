"""FastAPI app (docs/API.md). `uvicorn app.api.main:app`; the contract is frozen in docs/openapi.json."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import ApiError, Services
from app.api.routes import router

DESCRIPTION = """Hisaab: AI Financial Health Copilot, API v1.

Every number is labelled FACT, PREDICTION (with confidence) or RECOMMENDATION (with its simulated impact and
assumptions). A what-if the user asks about is a conditional PREDICTION. Money is a Quantity: integer paise plus a
display string. **Auth is dev-only** (`X-User-Id`)."""


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": details or {}}}, status_code=status)


def create_app(services: Services | None = None) -> FastAPI:
    app = FastAPI(title="Hisaab API", version="1.0.0", description=DESCRIPTION)
    app.state.services = services or Services()
    app.include_router(router)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return _error(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return _error(422, "VALIDATION_ERROR", "the request doesn't match the schema",
                      {"errors": jsonable_encoder(exc.errors(), custom_encoder={Exception: str})})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return _error(exc.status_code, code, str(exc.detail))

    return app


app = create_app()
