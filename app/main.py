import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import get_settings
from app.rate_limit import limiter
from app.routes.assets import router as assets_router
from app.routes.auth import router as auth_router
from app.routes.sites import router as sites_router

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    client = AsyncMongoClient(
        settings.mongodb_url,
        serverSelectionTimeoutMS=3000,
    )
    app.state.mongo_client = client
    app.state.database = client[settings.mongodb_database]
    try:
        yield
    finally:
        await client.close()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="REST API foundation for the WebBuilder platform.",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

app.include_router(auth_router, prefix="/api/auth", tags=["authentication"])
app.include_router(assets_router, prefix="/api/assets", tags=["assets"])
app.include_router(sites_router, prefix="/api/sites", tags=["websites"])


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready", tags=["health"])
async def readiness(request: Request) -> JSONResponse:
    try:
        await request.app.state.mongo_client.admin.command("ping")
    except PyMongoError:
        logger.exception("MongoDB readiness check failed")
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "dependencies": {"mongodb": "unavailable"},
            },
        )

    return JSONResponse(
        content={
            "status": "ready",
            "dependencies": {"mongodb": "available"},
        }
    )
