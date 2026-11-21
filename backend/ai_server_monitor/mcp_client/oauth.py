"""OAuth for MCP servers (the MCP authorization spec, as far as the official SDK goes).

The SDK's `OAuthClientProvider` is an `httpx2.Auth`: it discovers the server's authorization
server, registers this app (by the client-ID metadata document `/oauth/mcp/client.json` this app
serves, or by dynamic registration), sends the browser to the authorize URL, exchanges the code
and refreshes the token. What this module adds is what a web app needs around it:

- the tokens live in the `kv` table (key `mcp_oauth:<server>`), so they survive a restart;
- *Connect* runs the SDK's flow as a background connection to the server: the SDK hands the
  authorize URL to `redirect_handler` (which returns it to the Connect request) and waits in
  `callback_handler` for the code, which the callback route (`oauth_api.py`) delivers;
- every other connection (chat, jobs, the Test button) gets the saved-token provider: it refreshes
  the token when it expires and never opens a login, because nobody is there to log in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import (
    AuthorizationCodeResult,
    OAuthClientProvider,
    OAuthFlowError,
    OAuthRegistrationError,
    OAuthTokenError,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)

from ..db import Database
from .settings import McpServerSettings
from .toolsets import HttpSeen, failure_text, server_toolset

log = logging.getLogger(__name__)

CLIENT_PATH = "/oauth/mcp/client.json"
CALLBACK_PATH = "/oauth/mcp/callback"
CLIENT_NAME = "AI Server Monitor"
SIGN_IN_WAIT = 600.0
"""How long a started sign-in waits for the owner to come back from the server's login page."""


class McpOAuthError(Exception):
    """A failure in words the owner can act on (it is shown next to the Connect button)."""


def _no_metadata(name: str, url: str) -> str:
    return (
        f"{urlparse(url).netloc} (the server {name}) does not publish OAuth sign-in information "
        "(no authorization server metadata), so it cannot be connected this way. "
        "Check the URL, or set Auth to token."
    )


def _ours(exc: BaseException) -> BaseException | None:
    """The OAuth error somewhere inside what the MCP client raised (it wraps the cause in its
    own 'failed to connect' error, and in exception groups)."""
    todo, seen = [exc], set()
    while todo:
        err = todo.pop(0)
        if id(err) in seen:
            continue
        seen.add(id(err))
        if isinstance(err, McpOAuthError | OAuthFlowError):
            return err
        if isinstance(err, BaseExceptionGroup):
            todo.extend(err.exceptions)
        todo.extend(e for e in (err.__cause__, err.__context__) if e is not None)
    return None


def _key(name: str) -> str:
    return f"mcp_oauth:{name}"


class _Store:
    """The SDK's `TokenStorage` over one `kv` record. The record belongs to the server URL it
    was made for: after the URL is edited the old tokens are not offered to the new address."""

    def __init__(self, db: Database, name: str, url: str):
        self.db, self.name, self.url = db, name, url
        self.context: Any = None
        """The provider's OAuthContext (set by the provider): where the expiry time and the
        discovered metadata, which the SDK keeps only in memory, are read from."""

    async def record(self) -> dict[str, Any]:
        rec = await self.db.kv_get(_key(self.name))
        return rec if isinstance(rec, dict) and rec.get("server_url") == self.url else {}

    async def _save(self, **changes: Any) -> None:
        rec = await self.record()
        rec.update(server_url=self.url, **changes)
        await self.db.kv_set(_key(self.name), rec)

    async def forget(self) -> None:
        await self.db.kv_set(_key(self.name), None)

    async def get_tokens(self) -> OAuthToken | None:
        tokens = (await self.record()).get("tokens")
        return OAuthToken.model_validate(tokens) if tokens else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        ctx = self.context
        meta = ctx.oauth_metadata if ctx else None
        await self._save(
            tokens=tokens.model_dump(mode="json", exclude_none=True),
            expiry=ctx.token_expiry_time if ctx else None,
            metadata=meta.model_dump(mode="json", exclude_none=True) if meta else None,
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        info = (await self.record()).get("client_info")
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._save(client_info=client_info.model_dump(mode="json", exclude_none=True))


class _Flow:
    """One sign-in that Connect started, until the callback route (or a failure) ends it."""

    def __init__(self, name: str, manager: McpOAuth):
        loop = asyncio.get_running_loop()
        self.name, self.manager = name, manager
        self.url: asyncio.Future[str] = loop.create_future()
        self.result: asyncio.Future[AuthorizationCodeResult] = loop.create_future()
        self.state: str | None = None
        self.task: asyncio.Task[None] | None = None

    async def redirect(self, authorize_url: str) -> None:
        self.state = parse_qs(urlparse(authorize_url).query)["state"][0]
        self.manager._flows[self.state] = self
        self.url.set_result(authorize_url)

    async def callback(self) -> AuthorizationCodeResult:
        try:
            return await asyncio.wait_for(self.result, SIGN_IN_WAIT)
        except TimeoutError:
            raise McpOAuthError(
                f"Nobody finished the login at the server within {int(SIGN_IN_WAIT // 60)} "
                "minutes. Press Connect to start again."
            ) from None

    def finish(self) -> None:
        if self.state is not None:
            self.manager._flows.pop(self.state, None)


class _Provider(OAuthClientProvider):
    """The SDK's provider with this app's storage and handlers. With `flow` it signs in (Connect);
    without, it only uses and refreshes what is saved."""

    def __init__(
        self,
        cfg: McpServerSettings,
        store: _Store,
        redirect_uri: str,
        *,
        flow: _Flow | None = None,
        client_metadata_url: str | None = None,
        on_lost: Callable[[str], None] | None = None,
    ):
        super().__init__(
            str(cfg.url),
            OAuthClientMetadata(
                redirect_uris=[redirect_uri],
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                client_name=CLIENT_NAME,
                application_type="web",
            ),
            store,
            redirect_handler=self._redirect,
            callback_handler=self._callback,
            client_metadata_url=client_metadata_url,
        )
        self.name, self.flow, self.store, self.on_lost = cfg.name, flow, store, on_lost
        store.context = self.context

    async def _initialize(self) -> None:
        await super()._initialize()
        if self.flow is not None:
            return
        if self.context.current_tokens is None:
            raise McpOAuthError(
                f"Not connected to {self.name} yet: open Settings → MCP and press Connect."
            )
        # what the SDK keeps only in memory: the token endpoint and when the token runs out
        rec = await self.store.record()
        if rec.get("metadata"):
            self.context.oauth_metadata = OAuthMetadata.model_validate(rec["metadata"])
        self.context.token_expiry_time = rec.get("expiry")

    async def _redirect(self, authorize_url: str) -> None:
        if self.context.oauth_metadata is None:
            raise McpOAuthError(_no_metadata(self.name, self.context.server_url))
        if self.flow is None:
            lost = (
                f"The login at {self.name} has expired or was withdrawn: "
                "open Settings → MCP and press Connect again."
            )
            if self.on_lost:
                self.on_lost(lost)
            raise McpOAuthError(lost)
        await self.flow.redirect(authorize_url)

    async def _callback(self) -> AuthorizationCodeResult:
        assert self.flow is not None
        return await self.flow.callback()


class McpOAuth:
    """All MCP OAuth state of the app: saved tokens (through `kv`), the providers that use them,
    the sign-ins in progress and the last failure per server."""

    def __init__(self, db: Database):
        self.db = db
        self._providers: dict[str, tuple[str, _Provider]] = {}
        self._flows: dict[str, _Flow] = {}
        self._errors: dict[str, str] = {}

    def _store(self, cfg: McpServerSettings) -> _Store:
        return _Store(self.db, cfg.name, str(cfg.url))

    def auth_for(self, cfg: McpServerSettings) -> _Provider | None:
        """The provider a connection to this server uses: None for a token server. One provider
        per server is shared, so two runs that find the token expired refresh it once."""
        if cfg.auth != "oauth":
            return None
        url = str(cfg.url)
        have = self._providers.get(cfg.name)
        if have is None or have[0] != url:
            provider = _Provider(
                cfg,
                self._store(cfg),
                f"http://localhost{CALLBACK_PATH}",  # (a saved login never authorizes again)
                on_lost=lambda text: self._errors.__setitem__(cfg.name, text),
            )
            have = self._providers[cfg.name] = (url, provider)
        return have[1]

    # ------------------------------------------------------------------ state

    async def states(self, servers: list[McpServerSettings]) -> dict[str, dict[str, Any]]:
        """Per OAuth server: whether a login is saved, and the last failure, if any."""
        return {
            cfg.name: {
                "connected": bool((await self._store(cfg).record()).get("tokens")),
                "error": self._errors.get(cfg.name),
            }
            for cfg in servers
            if cfg.auth == "oauth"
        }

    # ------------------------------------------------------------------ connect / disconnect

    async def prune(self, servers: list[McpServerSettings]) -> None:
        """After a settings save: forget the login of every server that is gone or no longer
        uses OAuth."""
        keep = {cfg.name for cfg in servers if cfg.auth == "oauth"}
        rows = await self.db.fetchall(
            "SELECT key FROM kv WHERE key LIKE 'mcp_oauth:%' AND value != 'null'"
        )
        for row in rows:
            name = row["key"].removeprefix("mcp_oauth:")
            if name not in keep:
                await self.forget(name)

    async def forget(self, name: str) -> None:
        """Drop the sign-in in progress and the saved login of a server (also for a server that
        was deleted or no longer uses OAuth)."""
        self._cancel(name)
        self._providers.pop(name, None)
        self._errors.pop(name, None)
        await self.db.kv_set(_key(name), None)

    def _cancel(self, name: str) -> None:
        for state, flow in list(self._flows.items()):
            if flow.name == name:
                self._flows.pop(state)
                if flow.task is not None:
                    flow.task.cancel()

    async def connect(self, cfg: McpServerSettings, origin: str) -> str:
        """Start a sign-in at the server and return the URL the browser has to open. `origin` is
        the address the owner's browser uses for this app: the server sends the browser back to
        `{origin}/oauth/mcp/callback`, and (over https) fetches `{origin}/oauth/mcp/client.json`."""
        await self.forget(cfg.name)
        flow = _Flow(cfg.name, self)
        provider = _Provider(
            cfg,
            self._store(cfg),
            f"{origin}{CALLBACK_PATH}",
            flow=flow,
            client_metadata_url=f"{origin}{CLIENT_PATH}" if origin.startswith("https://") else None,
        )
        flow.task = asyncio.create_task(self._sign_in(cfg, provider, flow))
        await asyncio.wait({flow.url, flow.task}, return_when=asyncio.FIRST_COMPLETED)
        if flow.url.done():
            return flow.url.result()
        # the connection ended without asking for a login
        error = self._errors.get(cfg.name) or (
            f"{cfg.name} let the app in without a login, so there is nothing to connect: "
            "set Auth to token."
        )
        self._errors[cfg.name] = error
        raise McpOAuthError(error)

    async def _sign_in(self, cfg: McpServerSettings, provider: _Provider, flow: _Flow) -> None:
        seen = HttpSeen()
        try:
            toolset = server_toolset(cfg, seen=seen, auth=provider)
            async with toolset:
                await toolset.list_tools()
            self._errors.pop(cfg.name, None)
            self._providers.pop(cfg.name, None)  # the next connection loads the new login
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._errors[cfg.name] = self._words(exc, cfg, seen, provider)
        finally:
            flow.finish()

    def _words(
        self, exc: BaseException, cfg: McpServerSettings, seen: HttpSeen, provider: _Provider
    ) -> str:
        leaf = _ours(exc)
        if leaf is None:
            return failure_text(exc, cfg, seen)
        text = str(leaf).strip()
        if isinstance(leaf, McpOAuthError):
            return text
        if isinstance(leaf, OAuthFlowError) and provider.context.oauth_metadata is None:
            return _no_metadata(cfg.name, str(cfg.url))
        if isinstance(leaf, OAuthRegistrationError):
            return f"The server would not register this app: {text}"
        if isinstance(leaf, OAuthTokenError):
            return f"The server refused the login: {text}"
        return f"The login could not be completed: {text}"

    async def complete(
        self, state: str, code: str | None, iss: str | None, error: str | None
    ) -> tuple[str, str | None]:
        """The callback route: the server sent the browser back. Returns the server name and the
        error to show (None: connected)."""
        flow = self._flows.get(state)
        if flow is None:
            raise McpOAuthError(
                "This login was not started here, or it has already ended. "
                "Press Connect in Settings → MCP to start again."
            )
        if error or not code:
            reason = (
                "The login at the server was denied."
                if error in (None, "access_denied")
                else f"The server answered: {error}."
            )
            flow.result.set_exception(McpOAuthError(reason))
        else:
            flow.result.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))
        assert flow.task is not None
        with contextlib.suppress(BaseException):
            await flow.task
        return flow.name, self._errors.get(flow.name)
