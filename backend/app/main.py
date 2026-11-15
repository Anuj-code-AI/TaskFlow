
from contextlib import asynccontextmanager
from collections import defaultdict
from time import monotonic
import uuid
from pathlib import Path
import logging
import json
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.core.redis import redis_client

from app.api.routes import (
    auth,
    workspaces,
    projects,
    tasks,
    notifications,
)


logger = logging.getLogger(__name__)


# --------------------------------------------------
# FRONTEND DIRECTORY
# --------------------------------------------------

# Local structure:
# taskflow/
#   backend/app/main.py
#   frontend/index.html

# Docker structure (assuming frontend is copied to /app/frontend):
# /app/app/main.py
# /app/frontend/index.html

possible_frontend_paths = [
    Path("/app/frontend"),                       # Docker
    Path(__file__).resolve().parents[2] / "frontend",  # Local
]

frontend_dir = next(
    (
        path
        for path in possible_frontend_paths
        if (path / "index.html").is_file()
    ),
    None,
)


# --------------------------------------------------
# APPLICATION LIFESPAN
# --------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("TaskFlow application starting")

    if frontend_dir:
        logger.info("Frontend directory found: %s", frontend_dir)
    else:
        logger.warning("Frontend directory not found")

    yield

    await redis_client.aclose()
    await engine.dispose()

    logger.info("TaskFlow application stopped")


# --------------------------------------------------
# FASTAPI APPLICATION
# --------------------------------------------------

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def safe_validation_error(request: Request, exc: RequestValidationError):
    # Never echo submitted values (e.g. passwords or tokens) in validation errors.
    errors = [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())),
            "message": error.get("msg", "Invalid value"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Request validation failed",
            "errors": errors,
            "request_id": getattr(request.state, "request_id", None),
        },
    )


# --------------------------------------------------
# CORS
# --------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request correlation, payload bounds, and Redis-backed rate limiting.
_rate_fallback = defaultdict(list)
_RATE_RULES = {
    "/api/v1/auth/login": (10, 60),
    "/api/v1/auth/register": (5, 3600),
    "/api/v1/auth/refresh": (20, 60),
    "/api/v1/workspaces": (30, 60),
}


@app.middleware("http")
async def request_controls(request: Request, call_next):
    supplied_request_id = request.headers.get("X-Request-ID", "")[:100]
    request_id = supplied_request_id if re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", supplied_request_id) else str(uuid.uuid4())
    request.state.request_id = request_id
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 1_048_576:
        return JSONResponse(
            status_code=413,
            content={"detail": "Request body exceeds the 1 MB limit", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    path = request.url.path
    rate_scope = path.rstrip("/")
    rule = _RATE_RULES.get(rate_scope)
    if path.startswith("/api/v1/workspaces/") and any(
        segment in path for segment in ("/invitations", "/members", "/transfer-ownership")
    ):
        rule = (20, 60)
        rate_scope = "workspace-membership-mutations"
    if path.startswith("/api/v1/workspaces/") and "/tasks" in path and request.method in {"POST", "PATCH", "DELETE"}:
        rule = (60, 60)
        rate_scope = "workspace-task-mutations"
    if rule:
        limit, window = rule
        client_ip = request.client.host if request.client else "unknown"
        key = f"taskflow:rate:{rate_scope}:{client_ip}"
        allowed = True
        try:
            count = await redis_client.incr(key)
            if count == 1:
                await redis_client.expire(key, window)
            allowed = count <= limit
        except Exception:
            # Fail open during Redis outage, with a process-local fallback limiter.
            now = monotonic()
            bucket = [t for t in _rate_fallback[key] if now - t < window]
            if len(bucket) >= limit:
                allowed = False
            else:
                bucket.append(now)
            _rate_fallback[key] = bucket
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Retry later.", "request_id": request_id},
                headers={"Retry-After": str(window), "X-Request-ID": request_id},
            )

    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled request error request_id=%s path=%s", request_id, path)
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
        )
    response.headers["X-Request-ID"] = request_id
    logger.info(json.dumps({
        "event": "request_completed",
        "request_id": request_id,
        "method": request.method,
        "path": path,
        "status_code": response.status_code,
    }))
    return response


# --------------------------------------------------
# API ROUTERS
# --------------------------------------------------

API_PREFIX = "/api/v1"

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(workspaces.router, prefix=API_PREFIX)
app.include_router(projects.router, prefix=API_PREFIX)
app.include_router(tasks.router, prefix=API_PREFIX)
app.include_router(notifications.router, prefix=API_PREFIX)


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok"
    }


# --------------------------------------------------
# READINESS CHECK
# --------------------------------------------------

@app.get("/ready")
async def ready():
    checks = {}

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

        checks["postgres"] = "ok"

    except Exception:
        logger.exception("PostgreSQL readiness check failed")
        checks["postgres"] = "unavailable"

    try:
        await redis_client.ping()

        checks["redis"] = "ok"

    except Exception:
        logger.exception("Redis readiness check failed")
        checks["redis"] = "unavailable"

    is_ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"ready": is_ready, "dependencies": checks},
    )


# --------------------------------------------------
# FRONTEND STATIC FILES
# --------------------------------------------------

if frontend_dir:

    assets_dir = frontend_dir / "assets"

    if assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=assets_dir),
            name="assets",
        )

    @app.get("/", include_in_schema=False)
    async def serve_frontend():
        index_file = frontend_dir / "index.html"

        if not index_file.is_file():
            raise HTTPException(
                status_code=404,
                detail="Frontend index.html not found",
            )

        return FileResponse(index_file)

else:

    @app.get("/", include_in_schema=False)
    async def frontend_not_found():
        raise HTTPException(
            status_code=404,
            detail="Frontend files are not available in the API container",
        )


# Dedicated page routes: landing -> authentication -> workspace application.
if frontend_dir:
    @app.get("/login", include_in_schema=False)
    async def login_page():
        return FileResponse(frontend_dir / "auth.html")

    @app.get("/register", include_in_schema=False)
    async def register_page():
        return FileResponse(frontend_dir / "auth.html")

    @app.get("/app", include_in_schema=False)
    async def application_page():
        return FileResponse(frontend_dir / "app.html")
