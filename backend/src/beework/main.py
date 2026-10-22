from typing import Any

import psycopg
import redis
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.engine import make_url
from starlette.exceptions import HTTPException as StarletteHTTPException

from beework.api import router as api_router
from beework.api.conventions import ApiError, api_error_handler
from beework.auth import get_current_user
from beework.config import settings
from beework.models import User, audit_source_ip
from beework.runtime.demo_graph import demo_graph

app = FastAPI(
    title="BeeWork API",
    version="0.1.0",
    description="BeeWork enterprise AI platform development API",
)
app.include_router(api_router)
app.exception_handler(ApiError)(api_error_handler)
app.exception_handler(StarletteHTTPException)(api_error_handler)
app.exception_handler(Exception)(api_error_handler)


@app.middleware("http")
async def set_request_audit_context(request: Request, call_next):
    token = audit_source_ip.set(request.client.host if request.client else None)
    try:
        return await call_next(request)
    finally:
        audit_source_ip.reset(token)


class DemoRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


class DemoRunResponse(BaseModel):
    result: str


def check_dependencies() -> dict[str, str]:
    results: dict[str, str] = {}
    try:
        with psycopg.connect(
            make_url(settings.database_url)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False),
            connect_timeout=3,
        ) as connection:
            connection.execute("SELECT 1")
        results["postgres"] = "ok"
    except psycopg.Error as exc:
        results["postgres"] = "unavailable"
        results["error"] = f"PostgreSQL readiness check failed: {exc}"
        return results

    try:
        client = redis.Redis.from_url(settings.valkey_url, socket_connect_timeout=3)
        client.ping()
        client.close()
        results["valkey"] = "ok"
    except redis.RedisError as exc:
        results["valkey"] = "unavailable"
        results["error"] = f"Valkey readiness check failed: {exc}"
    return results


@app.get("/health/live")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict[str, Any]:
    dependencies = check_dependencies()
    if "error" in dependencies:
        raise HTTPException(status_code=503, detail=dependencies)
    return {"status": "ok", "dependencies": dependencies}


@app.post("/api/v1/runtime/demo", response_model=DemoRunResponse)
def run_demo(
    request: DemoRunRequest,
    _user: User = Depends(get_current_user),
) -> DemoRunResponse:
    result = demo_graph.invoke({"message": request.message})
    return DemoRunResponse(result=result["message"])
