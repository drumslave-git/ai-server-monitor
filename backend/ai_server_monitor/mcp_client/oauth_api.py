"""HTTP side of MCP OAuth: the app's client-ID metadata document and the callback the server
sends the browser to (both under `/oauth/mcp/`), and the Settings page's Connect / Disconnect.

`/oauth/mcp/client.json` is fetched by the MCP server itself, so a login proxy in front of the
app must let that one path through without a login. The callback is opened by the owner's own
browser, which carries the proxy's login."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Annotated
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .oauth import CALLBACK_PATH, CLIENT_NAME, CLIENT_PATH, McpOAuthError

if TYPE_CHECKING:
    from ..context import AppContext

router = APIRouter()


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def public_origin(request: Request) -> str:
    """The address the browser (or the MCP server) used to reach this app: what a reverse proxy
    forwards, else what the request itself says."""
    headers = request.headers
    scheme = headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = headers.get("x-forwarded-host", headers.get("host", request.url.netloc))
    return f"{scheme}://{host.split(',')[0].strip()}"


class McpOAuthState(BaseModel):
    name: str
    connected: bool
    error: str | None


class McpOAuthStart(BaseModel):
    authorize_url: str


@router.get("/oauth/mcp/client.json", include_in_schema=False)
async def client_document(request: Request) -> JSONResponse:
    origin = public_origin(request)
    return JSONResponse(
        {
            "client_id": f"{origin}{CLIENT_PATH}",
            "client_name": CLIENT_NAME,
            "redirect_uris": [f"{origin}{CALLBACK_PATH}"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
    )


@router.get("/oauth/mcp/callback", include_in_schema=False, response_model=None)
async def callback(
    request: Request,
    state: Annotated[str, Query(max_length=500)] = "",
    code: Annotated[str | None, Query(max_length=5000)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
    iss: Annotated[str | None, Query(max_length=2000)] = None,
) -> RedirectResponse | HTMLResponse:
    try:
        name, _ = await _ctx(request).oauth.complete(state, code, iss, error)
    except McpOAuthError as exc:
        page = f"<p>{escape(str(exc))}</p><p><a href='/#/settings/mcp'>Back to Settings</a></p>"
        return HTMLResponse(page, status_code=400)
    return RedirectResponse(f"/#/settings/mcp/{quote(name, safe='')}", status_code=303)


@router.get("/api/mcp/oauth")
async def oauth_states(request: Request) -> list[McpOAuthState]:
    """Per server that uses OAuth: is a login saved, and why the last attempt failed."""
    ctx = _ctx(request)
    states = await ctx.oauth.states(ctx.settings.mcp.servers)
    return [McpOAuthState(name=name, **state) for name, state in states.items()]


@router.post("/api/mcp/oauth/{server}/connect")
async def connect(server: str, request: Request) -> McpOAuthStart:
    """Start the login at the server; the page sends the browser to `authorize_url`."""
    ctx = _ctx(request)
    cfg = ctx.settings.mcp.server(server)
    if cfg is None:
        raise HTTPException(404, f"There is no saved MCP server named {server!r}: save it first.")
    if cfg.auth != "oauth":
        raise HTTPException(400, f"{server} uses a token: set Auth to oauth and save first.")
    try:
        return McpOAuthStart(authorize_url=await ctx.oauth.connect(cfg, public_origin(request)))
    except McpOAuthError as exc:
        raise HTTPException(400, str(exc)) from None


@router.delete("/api/mcp/oauth/{server}", status_code=204)
async def disconnect(server: str, request: Request) -> None:
    """Forget the saved login of the server."""
    await _ctx(request).oauth.forget(server)
