"""The fake MCP server of `mcp_fake` behind a real OAuth 2.1 authorization server (invented data,
all on one local port): protected-resource and authorization-server metadata, dynamic client
registration, the authorize and token endpoints with PKCE, refresh-token rotation.

The tests play the browser themselves: `authorize()` sends the authorize URL the app gave and
returns where the server would send the browser back (the app's callback URL)."""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .mcp_fake import FakeMcp

AUTHORIZE, TOKEN, REGISTER = "/oauth2/authorize", "/oauth2/token", "/oauth2/register"
RESOURCE = "/.well-known/oauth-protected-resource/mcp"
"""(not the SDK's fallback paths /authorize, /token, /register: the client must have read them
from the metadata)"""


class FakeOAuthMcp(FakeMcp):
    def __init__(
        self,
        *,
        metadata: bool = True,
        login: bool = True,
        dcr: bool = True,
        cimd: bool = False,
        lifetime: int = 3600,
    ):
        super().__init__("streamable_http")
        self.metadata, self.login, self.dcr, self.cimd = metadata, login, dcr, cimd
        self.lifetime = lifetime
        self.access: set[str] = set()
        self.refresh: dict[str, str] = {}
        self.codes: dict[str, dict[str, str]] = {}
        self.registered: list[dict[str, Any]] = []
        self.authorized: list[dict[str, str]] = []
        self.token_posts: list[dict[str, str]] = []
        self.deny = False
        self.mcp_bearers: list[str | None] = []

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------ the server

    def revoke_all(self) -> None:
        self.access.clear()
        self.refresh.clear()

    def _app(self) -> ASGIApp:
        inner = super()._app()
        oauth = Starlette(
            routes=[
                Route(RESOURCE, self._resource),
                Route("/.well-known/oauth-authorization-server", self._as_metadata),
                Route(REGISTER, self._register_client, methods=["POST"]),
                Route(AUTHORIZE, self._authorize),
                Route(TOKEN, self._token, methods=["POST"]),
            ]
        )

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "lifespan":  # the MCP app's session manager lives in it
                await inner(scope, receive, send)
                return
            if scope["type"] != "http" or scope["path"] != "/mcp":
                await oauth(scope, receive, send)
                return
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            bearer = auth.removeprefix("Bearer ") if auth else None
            self.mcp_bearers.append(bearer)
            if self.login and bearer not in self.access:
                hint = f'Bearer resource_metadata="{self.base}{RESOURCE}"'
                await PlainTextResponse(
                    "login", status_code=401, headers={"WWW-Authenticate": hint}
                )(scope, receive, send)
                return
            await inner(scope, receive, send)

        return app

    async def _resource(self, request: Request) -> Response:
        if not self.metadata:
            return Response(status_code=404)
        return JSONResponse({"resource": f"{self.base}/mcp", "authorization_servers": [self.base]})

    async def _as_metadata(self, request: Request) -> Response:
        if not self.metadata:
            return Response(status_code=404)
        doc: dict[str, Any] = {
            "issuer": self.base,
            "authorization_endpoint": f"{self.base}{AUTHORIZE}",
            "token_endpoint": f"{self.base}{TOKEN}",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": self.cimd,
        }
        if self.dcr:
            doc["registration_endpoint"] = f"{self.base}{REGISTER}"
        return JSONResponse(doc)

    async def _register_client(self, request: Request) -> Response:
        body = await request.json()
        self.registered.append(body)
        client_id = f"client-{len(self.registered)}"
        return JSONResponse({**body, "client_id": client_id}, status_code=201)

    async def _authorize(self, request: Request) -> Response:
        q = dict(request.query_params)
        self.authorized.append(q)
        back = {"state": q["state"]}
        if self.deny:
            back["error"] = "access_denied"
        else:
            code = secrets.token_urlsafe(12)
            self.codes[code] = q
            back["code"] = code
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode(back)}", status_code=302)

    async def _token(self, request: Request) -> Response:
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        self.token_posts.append(form)
        grant = form.get("grant_type")
        if grant == "authorization_code":
            asked = self.codes.pop(form.get("code", ""), None)
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                )
                .decode()
                .rstrip("=")
            )
            if (
                asked is None
                or asked["redirect_uri"] != form.get("redirect_uri")
                or asked["client_id"] != form.get("client_id")
                or asked["code_challenge"] != challenge
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return self._tokens(form["client_id"])
        if grant == "refresh_token" and self.refresh.pop(form.get("refresh_token", ""), None):
            return self._tokens(form["client_id"])
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    def _tokens(self, client_id: str) -> Response:
        access, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.access.add(access)
        self.refresh[refresh] = client_id
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": self.lifetime,
                "refresh_token": refresh,
            }
        )


async def authorize(authorize_url: str) -> str:
    """The owner's browser at the server's login page, consenting: where the server sends it
    back (the app's callback URL with the code, or with the error)."""
    async with httpx.AsyncClient() as http:
        response = await http.get(authorize_url)
    assert response.status_code == 302, response.text
    return response.headers["location"]


def path_of(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.path}?{parts.query}"


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
