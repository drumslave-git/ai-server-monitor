"""Connection tests for the Settings page: "does what I typed actually work?".

Each test takes the *unsaved* values of one connection (a masked secret is replaced by the
stored one first), talks to the real service with a short timeout and returns a result a
person can act on: what answered (name + version), or what is wrong, in plain words.

Tests are read-only except Telegram's optional "send test message". Errors never echo
secrets: messages are built from the failure *kind* and the host, never from the request
URL (a Telegram URL contains the bot token), and every secret value that is part of the
test is scrubbed from the final text as a second line of defence.
"""

from __future__ import annotations

import errno
import os
import re
import socket
import ssl
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from .arr import qbit_auth
from .arr.base import denied_text
from .collectors.base import HostEnv
from .collectors.docker import DEFAULT_SOCKET
from .config import (
    ArrInstance,
    McpServerSettings,
    OptionalHttpUrl,
    QbitSettings,
    Secret,
    Settings,
)
from .mcp_client.discovery import check_mcp_server
from .settings_store import merge_secrets

TEST_TIMEOUT = 10.0
ARR_APPS = ("radarr", "sonarr", "whisparr", "prowlarr")
KINDS = ("prometheus", "docker", *ARR_APPS, "qbittorrent", "telegram", "mcp")
TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TEST_TEXT = (
    "✅ AI Server Monitor: test message. This chat is set up to receive reports and alerts."
)


@dataclass
class ConnResult:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class ConnFailure(Exception):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


# --------------------------------------------------------------------------- errors


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    node: BaseException | None = exc
    while node is not None and node not in seen:
        seen.append(node)
        node = node.__cause__ or node.__context__
    return seen


_REFUSED_TEXT = re.compile(r"connection refused|errno 111|winerror 10061", re.IGNORECASE)
_CERT_TEXT = re.compile(r"certificate verify failed(?::\s*([^(\]]+))?", re.IGNORECASE)
_DNS_TEXT = re.compile(
    r"name or service not known|getaddrinfo failed|temporary failure in name resolution|"
    r"nodename nor servname|no address associated",
    re.IGNORECASE,
)


def host_port(url: str) -> tuple[str, int | None]:
    parts = urlsplit(url if "://" in url else f"http://{url}")
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None and parts.hostname:
        port = 443 if parts.scheme == "https" else 80 if parts.scheme == "http" else None
    return parts.hostname or url, port


def describe_error(exc: BaseException, url: str, timeout: float = TEST_TIMEOUT) -> str:
    """A plain-words reason for a failed request. Never includes the URL or exception text
    beyond well-known kinds (the exception text can contain URLs and credentials)."""
    host, port = host_port(url)
    where = f"{host}:{port}" if port else host
    chain = _chain(exc)
    if any(isinstance(e, httpx.UnsupportedProtocol | httpx.InvalidURL) for e in chain):
        return "The URL is not valid. It must start with http:// or https://."
    if any(isinstance(e, socket.gaierror) for e in chain) or any(
        _DNS_TEXT.search(str(e)) for e in chain
    ):
        return (
            f"DNS name not found: {host}. "
            "Check the spelling, or that this container can resolve it."
        )
    for e in chain:
        if isinstance(e, ssl.SSLCertVerificationError):
            reason = (e.verify_message or e.reason or "certificate verification failed").strip()
            return (
                f"TLS certificate of {host} is not trusted ({reason}). "
                "Use a trusted certificate, or an http:// URL on a trusted network."
            )
        if isinstance(e, ssl.SSLError):
            return (
                f"TLS error talking to {where}: {(e.reason or 'handshake failed').lower()}. "
                "Is this an http:// port?"
            )
    for e in chain:  # the cause chain can be cut (some proxies/mocks); the text still tells
        if match := _CERT_TEXT.search(str(e)):
            reason = (match.group(1) or "certificate verification failed").strip()
            return (
                f"TLS certificate of {host} is not trusted ({reason}). "
                "Use a trusted certificate, or an http:// URL on a trusted network."
            )
    if any(isinstance(e, httpx.TimeoutException) for e in chain):
        return f"Timed out after {timeout:g} s: {where} did not answer."
    if any(isinstance(e, ConnectionRefusedError) or _REFUSED_TEXT.search(str(e)) for e in chain):
        return f"Connection refused by {where}. Nothing is listening there (wrong port?)."
    if any(isinstance(e, httpx.RemoteProtocolError | ConnectionResetError) for e in chain):
        return (
            f"{where} closed the connection without a proper answer. "
            "Wrong port, or http:// used for an https:// port?"
        )
    for e in chain:
        if isinstance(e, OSError) and e.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            return f"No network route to {where}."
    return f"Could not connect to {where} ({type(exc).__name__})."


def scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if len(secret) >= 8:
            text = text.replace(secret, "***")
    return text


def _scrub_any(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, dict):
        return {k: _scrub_any(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_any(v, secrets) for v in value]
    return value


# --------------------------------------------------------------------------- plumbing


def _client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(TEST_TIMEOUT, connect=min(TEST_TIMEOUT, 8.0)),
        follow_redirects=False,
        **kwargs,
    )


async def _request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise ConnFailure(describe_error(exc, url)) from None
    except (OSError, ValueError) as exc:  # e.g. a missing unix socket
        raise ConnFailure(describe_error(exc, url)) from None
    return response, (time.monotonic() - started) * 1000


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _redirect_failure(response: httpx.Response) -> ConnFailure | None:
    if response.status_code in (301, 302, 303, 307, 308):
        target = response.headers.get("location", "")
        # Show only where it goes, not what it carries.
        shown = target.split("?", 1)[0][:200]
        return ConnFailure(
            f"The server redirected to {shown or 'another address'}. "
            "Use that address as the URL (e.g. https:// instead of http://)."
        )
    return None


def _not_the_app(response: httpx.Response, what: str) -> ConnFailure:
    if response.status_code == 404:
        return ConnFailure(
            f"HTTP 404: this is not {what}. "
            "Check the URL (a URL base such as /sonarr may be needed)."
        )
    return ConnFailure(
        f"HTTP {response.status_code}: this does not look like {what}. Check the URL and port."
    )


def _latency(ms: float) -> int:
    return round(ms)


# --------------------------------------------------------------------------- *arr


async def check_arr(kind: str, cfg: ArrInstance) -> ConnResult:
    base = cfg.url.strip().rstrip("/")
    api = "/api/v1" if kind == "prowlarr" else "/api/v3"
    label = kind.capitalize()
    async with _client(headers={"X-Api-Key": cfg.api_key.get_secret_value()}) as client:
        response, ms = await _request(client, "GET", f"{base}{api}/system/status")
    if failure := _redirect_failure(response):
        raise failure
    if response.status_code in (401, 403):
        raise ConnFailure(
            denied_text(response, label),
            {"http_status": response.status_code, "url": f"{base}{api}/system/status"},
        )
    if response.status_code != 200:
        raise _not_the_app(response, label)
    body = _json(response)
    if not isinstance(body, dict):
        raise ConnFailure(
            f"The reply is not {label}'s API (it is not JSON). The URL probably points at "
            "a web page or a proxy login; check the URL and any URL base."
        )
    app_name = str(body.get("appName") or "").strip()
    version = str(body.get("version") or "").strip()
    details = {"app": app_name or label, "version": version, "latency_ms": _latency(ms)}
    if app_name and kind not in app_name.lower():
        return ConnResult(
            False,
            f"Wrong application: this URL is {app_name}{' ' + version if version else ''}, "
            f"not {label}. Check which URL goes in this slot.",
            details,
        )
    name = app_name or label
    return ConnResult(True, f"Connected: {name}{' ' + version if version else ''}.", details)


# --------------------------------------------------------------------------- qBittorrent


async def check_qbittorrent(cfg: QbitSettings) -> ConnResult:
    base = cfg.url.strip().rstrip("/")
    password = cfg.password.get_secret_value()
    key = qbit_auth.backoff_key(base, cfg.username, password)
    state = qbit_auth.state_of(key)
    if state["paused"] and state.get("last_failure_kind") in ("bad_credentials", "banned"):
        # Trying again with the same credentials would only add a failed attempt (and extend
        # a ban); changing the URL or the credentials makes a new key and a fresh test.
        raise ConnFailure(
            f"Not tried again: the last login with these credentials failed and retries are "
            f"paused until {state['paused_until']}, so this IP is not banned or kept banned. "
            f"Last failure: {state['last_failure']}",
            {"retry_state": state},
        )
    async with _client(headers=qbit_auth.request_headers(base)) as client:
        login, _ = await _request(
            client,
            "POST",
            f"{base}/api/v2/auth/login",
            data={"username": cfg.username, "password": password},
        )
        diagnosis = qbit_auth.diagnose_login(login, base)
        if not diagnosis.ok:
            until = qbit_auth.record_failure(key, diagnosis)
            raise ConnFailure(
                diagnosis.message() + qbit_auth.pause_note(until), diagnosis.details()
            )
        qbit_auth.record_success(key)
        version, ms = await _request(client, "GET", f"{base}/api/v2/app/version")
    if version.status_code != 200:
        excerpt = " ".join(version.text.split())[:200]
        raise ConnFailure(
            f"Logged in, but asking for the version was refused (HTTP {version.status_code}, "
            f'body "{excerpt}"). Check the WebUI security settings of qBittorrent '
            "(CSRF protection, host header validation).",
            {"http_status": version.status_code},
        )
    number = version.text.strip()
    return ConnResult(
        True,
        f"Connected: qBittorrent {number}, login OK.",
        {"app": "qBittorrent", "version": number, "latency_ms": _latency(ms)},
    )


# --------------------------------------------------------------------------- Prometheus


class PrometheusConn(BaseModel):
    url: OptionalHttpUrl = ""
    token: Secret
    username: str = ""
    password: Secret


async def check_prometheus(cfg: PrometheusConn) -> ConnResult:
    base = cfg.url.strip().rstrip("/")
    if not base:
        raise ConnFailure("Enter the Prometheus URL first.")
    token = cfg.token.get_secret_value()
    headers = {"Accept": "application/json"}
    auth: httpx.Auth | None = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif cfg.username:
        auth = httpx.BasicAuth(cfg.username, cfg.password.get_secret_value())
    async with _client(headers=headers, auth=auth) as client:
        info, ms_info = await _request(client, "GET", f"{base}/api/v1/status/buildinfo")
        if failure := _redirect_failure(info):
            raise failure
        if info.status_code in (401, 403):
            raise ConnFailure(
                f"Prometheus rejected the credentials (HTTP {info.status_code}). "
                "Check the bearer token, or the user name and password."
            )
        version = ""
        details: dict[str, Any] = {}
        body = _json(info)
        if info.status_code == 200 and isinstance(body, dict) and body.get("status") == "success":
            version = str((body.get("data") or {}).get("version") or "")
            details["version"] = version
        elif info.status_code != 404:  # 404: Prometheus-compatible servers without buildinfo
            raise _not_the_app(info, "Prometheus")
        probe, ms = await _request(client, "GET", f"{base}/api/v1/query", params={"query": "1+1"})
    reply = _json(probe)
    if probe.status_code in (401, 403):
        raise ConnFailure(
            f"Prometheus rejected the credentials (HTTP {probe.status_code}). "
            "Check the bearer token, or the user name and password."
        )
    if probe.status_code != 200 or not isinstance(reply, dict) or reply.get("status") != "success":
        if isinstance(reply, dict) and reply.get("error"):
            raise ConnFailure(
                f"Prometheus answered but the test query failed: {reply['error']}"[:300]
            )
        raise _not_the_app(probe, "Prometheus")
    details.update({"query": "ok", "latency_ms": _latency(ms if ms else ms_info)})
    name = f"Prometheus {version}" if version else "Prometheus"
    return ConnResult(True, f"Connected: {name}, a test query worked.", details)


# --------------------------------------------------------------------------- Docker


class DockerConn(BaseModel):
    url: str = ""


def _docker_target(cfg: DockerConn, env: HostEnv) -> tuple[str, dict[str, Any]]:
    url = cfg.url.strip() or os.environ.get("DOCKER_HOST") or DEFAULT_SOCKET
    if url.startswith("unix://"):
        path = url.removeprefix("unix://")
        if not os.path.exists(path) and env.in_container and env.path(path).exists():
            path = str(env.path(path))
        if not os.path.exists(path):
            raise ConnFailure(
                f"The Docker socket {path} does not exist here. Mount it into the container, "
                "or set the URL of a socket proxy (tcp://docker-proxy:2375)."
            )
        return "http://docker", {"transport": httpx.AsyncHTTPTransport(uds=path)}
    if url.startswith("tcp://"):
        return "http://" + url.removeprefix("tcp://"), {}
    if url.startswith(("http://", "https://")):
        return url, {}
    raise ConnFailure("Unsupported URL. Use tcp://host:port, http://host:port or unix:///path.")


async def check_docker(cfg: DockerConn, env: HostEnv) -> ConnResult:
    base, kwargs = _docker_target(cfg, env)
    base = base.rstrip("/")
    async with _client(**kwargs) as client:
        response, ms = await _request(client, "GET", f"{base}/version")
    if response.status_code == 403:
        raise ConnFailure(
            "The Docker API denied GET /version (HTTP 403). The socket proxy does not allow "
            "this request."
        )
    body = _json(response)
    if response.status_code != 200 or not isinstance(body, dict) or "ApiVersion" not in body:
        raise ConnFailure(
            f"This is not a Docker API (HTTP {response.status_code}). Check the URL and port."
        )
    version = str(body.get("Version") or "")
    api = str(body.get("ApiVersion") or "")
    return ConnResult(
        True,
        f"Connected: Docker {version} (API {api}).",
        {"version": version, "api_version": api, "os": body.get("Os"), "latency_ms": _latency(ms)},
    )


# --------------------------------------------------------------------------- Telegram


class TelegramConn(BaseModel):
    token: Secret
    allowed_chat_ids: list[int] = []
    api_url: OptionalHttpUrl = ""


def telegram_reason(description: str, code: int | None) -> str:
    text = description.lower()
    if "chat not found" in text:
        return (
            "chat not found: the user must send /start to the bot first "
            "(for a group: add the bot to it)"
        )
    if "bot was blocked" in text:
        return "the user blocked the bot (they must unblock it)"
    if "can't initiate conversation" in text:
        return "the user must send /start to the bot first"
    if "not a member" in text or "was kicked" in text:
        return "the bot is not a member of that group"
    if "user is deactivated" in text:
        return "the user's account is deactivated"
    if code == 429:
        return "Telegram says: too many requests, try again in a moment"
    return description.strip()[:200] or f"HTTP {code}"


async def _telegram_call(
    client: httpx.AsyncClient, base: str, method: str, **params: Any
) -> tuple[Any, dict[str, Any] | None]:
    """(result, error): error = {"code", "description"} for an answer from Telegram;
    a failure to reach it raises ConnFailure."""
    response, _ = await _request(client, "POST", f"{base}{method}", json=params)
    body = _json(response)
    if isinstance(body, dict) and body.get("ok"):
        return body.get("result"), None
    if isinstance(body, dict):
        return None, {
            "code": body.get("error_code") or response.status_code,
            "description": str(body.get("description") or ""),
        }
    return None, {"code": response.status_code, "description": ""}


async def check_telegram(cfg: TelegramConn, mode: str | None) -> ConnResult:
    token = cfg.token.get_secret_value().strip()
    if not token:
        raise ConnFailure("Enter the bot token first (from @BotFather).")
    base = f"{(cfg.api_url or TELEGRAM_API).rstrip('/')}/bot{token}/"
    chats = list(dict.fromkeys(cfg.allowed_chat_ids))
    async with _client() as client:
        me, error = await _telegram_call(client, base, "getMe")
        if error is not None:
            if error["code"] in (401, 404):
                raise ConnFailure(
                    "Telegram rejected the bot token. Copy it again from @BotFather "
                    "(format 123456:ABC…)."
                )
            raise ConnFailure(
                "Telegram answered with an error: "
                + telegram_reason(error["description"], error["code"])
            )
        username = f"@{me.get('username')}" if me.get("username") else str(me.get("first_name"))
        details: dict[str, Any] = {"bot": username, "bot_id": me.get("id")}
        if mode != "send":
            if chats:
                tail = f" {len(chats)} allowed chat(s); use “Send test message” to check them."
            else:
                tail = (
                    " No allowed chat ids yet: send /id to the bot in Telegram to get yours, "
                    "then add it below."
                )
            return ConnResult(True, f"Connected: bot {username} is valid.{tail}", details)
        if not chats:
            return ConnResult(
                False,
                f"Bot {username} is valid, but there are no allowed chat ids to send to. "
                "Send /id to the bot in Telegram, then add the id.",
                details,
            )
        results: dict[str, str] = {}
        for chat in chats:
            _, error = await _telegram_call(
                client, base, "sendMessage", chat_id=chat, text=TELEGRAM_TEST_TEXT
            )
            results[str(chat)] = (
                "sent" if error is None else telegram_reason(error["description"], error["code"])
            )
    details["chats"] = results
    failed = {c: r for c, r in results.items() if r != "sent"}
    if not failed:
        return ConnResult(
            True, f"Test message sent to {len(chats)} chat(s) as {username}.", details
        )
    listing = "; ".join(f"chat {c}: {r}" for c, r in failed.items())
    return ConnResult(
        False, f"Sent to {len(chats) - len(failed)} of {len(chats)} chat(s). {listing}", details
    )


# --------------------------------------------------------------------------- dispatch


def _pick(data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: data[k] for k in keys if k in data}


def _current_node(
    kind: str, stored: dict[str, Any], body: dict[str, Any], index: int | None
) -> Any:
    """The stored counterpart of the tested form node, used to un-mask secrets."""
    arr = stored["arr"]
    if kind in ("radarr", "sonarr", "whisparr"):
        items = arr[kind]
        for item in items:
            if item.get("name") == body.get("name"):
                return item
        return items[index] if index is not None and 0 <= index < len(items) else {}
    if kind in ("prowlarr", "qbittorrent"):
        return arr[kind] or {}
    if kind == "mcp":
        items = stored["mcp"]["servers"]
        for item in items:
            if item.get("name") == body.get("name"):
                return item
        return items[index] if index is not None and 0 <= index < len(items) else {}
    if kind == "prometheus":
        return stored["collectors"]["prometheus"]
    if kind == "telegram":
        return stored["outputs"]["telegram"]
    return {}


def _friendly_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        msg = str(err["msg"]).removeprefix("Value error, ")
        if err["type"] == "string_pattern_mismatch":
            msg = "must start with http:// or https://"
        elif err["type"] == "string_too_short":
            msg = "is required"
        field_name = ".".join(str(p) for p in err["loc"] if not isinstance(p, int))
        parts.append(f"{field_name} {msg}".strip() if field_name else msg)
    return "Fix this first: " + "; ".join(parts) + "."


async def _done(result: ConnResult) -> ConnResult:
    return result


async def run_test(
    kind: str,
    body: dict[str, Any],
    settings: Settings,
    env: HostEnv,
    *,
    mode: str | None = None,
    index: int | None = None,
) -> ConnResult:
    """Run the connection test `kind` against the unsaved form values `body`."""
    stored = settings.storage_dict()
    merged = merge_secrets(body, _current_node(kind, stored, body, index))
    secrets = [
        v
        for k, v in _walk_strings(merged)
        if k in ("api_key", "token", "password", "bearer_token", "value") and v
    ]
    run: Awaitable[ConnResult]
    try:
        if kind in ARR_APPS:
            arr_cfg = ArrInstance.model_validate(_pick(merged, ("name", "url", "api_key")))
            run = check_arr(kind, arr_cfg)
        elif kind == "qbittorrent":
            qbit_cfg = QbitSettings.model_validate(_pick(merged, ("url", "username", "password")))
            run = check_qbittorrent(qbit_cfg)
        elif kind == "prometheus":
            prom_cfg = PrometheusConn.model_validate(
                _pick(merged, ("url", "token", "username", "password"))
            )
            run = check_prometheus(prom_cfg)
        elif kind == "docker":
            docker_cfg = DockerConn.model_validate(_pick(merged, ("url",)))
            run = check_docker(docker_cfg, env)
        elif kind == "mcp":
            mcp_cfg = McpServerSettings.model_validate(merged)
            mcp_result = await check_mcp_server(mcp_cfg)
            run = _done(ConnResult(mcp_result.ok, mcp_result.message, mcp_result.details))
        elif kind == "telegram":
            tg_cfg = TelegramConn.model_validate(
                _pick(merged, ("token", "allowed_chat_ids", "api_url"))
            )
            run = check_telegram(tg_cfg, mode)
        else:
            raise KeyError(kind)
    except ValidationError as exc:
        return ConnResult(False, _friendly_errors(exc))
    try:
        result = await run
    except ConnFailure as failure:
        result = ConnResult(False, str(failure), failure.details)
    except Exception as exc:  # a bug must not become a 500 with a traceback in the form
        result = ConnResult(False, f"The test failed unexpectedly ({type(exc).__name__}).")
    return ConnResult(
        result.ok, scrub(result.message, secrets), _scrub_any(result.details, secrets)
    )


def _walk_strings(node: Any, key: str = "") -> list[tuple[str, str]]:
    if isinstance(node, str):
        return [(key, node)]
    if isinstance(node, dict):
        return [pair for k, v in node.items() for pair in _walk_strings(v, k)]
    if isinstance(node, list):
        return [pair for v in node for pair in _walk_strings(v, key)]
    return []
