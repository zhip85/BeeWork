"""H5 channel (T-044): the page visitors open, and its session endpoint.

``GET /h5/{tenant}/{path}`` serves a small HTML page that loads the same ``widget.js`` as websites, in H5 mode (full screen or a bottom
drawer, mobile layout). The page is served by the API, so its calls come from the API's own origin; the channel's allowed domains are the
sites that may *embed* the page in an iframe — everyone else is refused by ``Content-Security-Policy: frame-ancestors``.
"""

import html
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.widget import _live_channel, _session, own_origin
from beework.channel_chat import enforce_visitor_limit
from beework.config import settings
from beework.db import get_db
from beework.errors import ApiError
from beework.models import Tenant
from beework.models.channels import ChannelInstance
from beework.schemas.channels import H5SessionRequest, WidgetSession

page_router = APIRouter(tags=["h5"])  # served at /h5/…, outside /v1: it is a page, not an API
api_router = APIRouter(prefix="/v1", tags=["h5"])

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>html,body{{margin:0;height:100%;background:#f6f7f6;font-family:system-ui,-apple-system,"PingFang SC","Noto Sans SC",sans-serif}}</style>
</head>
<body>
<script src="{base}/v1/widget.js" data-h5="{target}" data-api-base="{base}"></script>
</body>
</html>
"""


def channel_by_path(db: Session, tenant_slug: str, path: str) -> ChannelInstance | None:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
    if tenant is None:
        return None
    for channel in db.scalars(select(ChannelInstance).where(ChannelInstance.tenant_id == tenant.id, ChannelInstance.type == "h5")):
        snapshot = channel.published_snapshot or {}
        if (snapshot.get("config") or {}).get("h5_path") == path:
            return channel
    return None


def _normal_path(raw: str) -> str:
    return "/" + raw.strip("/") if raw.strip("/") else "/"


@page_router.get("/h5/{tenant}/{path:path}", response_class=HTMLResponse, include_in_schema=False)
def h5_page(tenant: str, path: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    channel = channel_by_path(db, tenant, _normal_path(path))
    snapshot = channel.published_snapshot if channel is not None else None
    if channel is None or not snapshot or channel.status not in ("active", "error"):
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>404</title><p>页面不存在或已停用。</p>", status_code=404)
    base = (settings.public_api_url or str(request.base_url)).rstrip("/")
    ancestors = " ".join(["'self'", *(snapshot.get("allowed_origins") or [])])
    return HTMLResponse(
        PAGE.format(title=html.escape(str(snapshot.get("name") or "")), base=html.escape(base, quote=True), target=html.escape(f"{tenant}{_normal_path(path)}", quote=True)),
        headers={"Content-Security-Policy": f"frame-ancestors {ancestors}", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@api_router.post("/h5/session", response_model=WidgetSession, summary="为 H5 页面的访客创建会话令牌")
def create_h5_session(payload: H5SessionRequest, request: Request, db: Session = Depends(get_db)) -> WidgetSession:
    ip = request.client.host if request.client else None
    enforce_visitor_limit(ip, "session", per_second=0.2, burst=10)
    origin = request.headers.get("origin", "")
    if not origin or origin.rstrip("/") != own_origin(request):
        raise ApiError(403, "origin_not_allowed", "H5 页面只能由 BeeWork 提供的地址访问")
    channel = channel_by_path(db, payload.tenant, _normal_path(payload.path))
    channel = _live_channel(db, channel, own_origin(request), request)
    visitor_id = payload.visitor_id or "v_" + secrets.token_urlsafe(18)
    return _session(channel, visitor_id, own_origin(request))
