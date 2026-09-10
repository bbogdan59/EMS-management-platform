from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.core.csrf import finalize_csrf_cookie, prepare_csrf_token
from app.web.templating import templates

logger = structlog.get_logger(__name__)
settings = get_settings()

STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def csrf_cookie_middleware(request: Request, call_next):
        # Trebuie generat/atasat la request.state INAINTE de a apela handler-ul,
        # altfel un formular randat la prima vizita (fara cookie inca) ar
        # imbraca un camp ascuns gol -- necorelat cu cookie-ul setat abia dupa.
        pending_token = prepare_csrf_token(request)
        response = await call_next(request)
        finalize_csrf_cookie(request, response, pending_token)
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        wants_json = request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", "")
        if exc.status_code == 401 and not wants_json:
            next_path = request.url.path
            return RedirectResponse(f"/login?next={next_path}", status_code=303)
        if wants_json:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code,
        )

    from app.api.v1.router import api_v1_router
    from app.web.routes import admin as admin_routes
    from app.web.routes import auth as auth_routes
    from app.web.routes import dashboard as dashboard_routes
    from app.web.routes import organizations as organizations_routes
    from app.web.routes import sse as sse_routes
    from app.web.routes import stations as stations_routes

    app.include_router(auth_routes.router, tags=["web-auth"])
    app.include_router(dashboard_routes.router, tags=["web-dashboard"])
    app.include_router(stations_routes.router, tags=["web-stations"])
    app.include_router(organizations_routes.router, tags=["web-organizations"])
    app.include_router(admin_routes.router, prefix="/admin", tags=["web-admin"])
    app.include_router(sse_routes.router, tags=["web-sse"])
    app.include_router(api_v1_router, prefix="/api/v1", tags=["device-api"])

    @app.get("/health", tags=["ops"])
    def health():
        return {"status": "ok"}

    @app.get("/readiness", tags=["ops"])
    def readiness():
        from sqlalchemy import text

        from app.database import engine

        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)
        return {"status": "ready"}

    return app


app = create_app()
