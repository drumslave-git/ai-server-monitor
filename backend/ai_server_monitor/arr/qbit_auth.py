"""What exactly happened when qBittorrent was asked to log in, and the retry back-off.

qBittorrent answers a login with `Ok.` / `Fails.` (HTTP 200), `401 Unauthorized` when its
Host-header validation or CSRF check rejects the request before looking at the password, `403`
when the IP is banned, and a reverse proxy in front can answer with an HTML page. Treating
every non-`Ok.` as "wrong password" hides the real cause, so every caller (the client, the
Settings test, the API gateway) goes through `diagnose_login` and reports its text.

The back-off is shared by everything that logs in with the same URL + user name + password
(it lives here, keyed by a hash, not on a client object): the collector, the action layer, the
API gateway and the Settings test must not take turns hammering a qBittorrent that already
refused them; repeated failed logins get the IP banned, and every retry extends the trouble.
Changing the URL or the credentials gives a new key, i.e. an immediate fresh attempt.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

BODY_EXCERPT = 200
BACKOFF_STEPS_S = (600, 1800, 3600)  # 10 min, then 30 min, then 1 h for every repeat
# Outcomes that retrying cannot fix (and that may extend a ban): back off after them.
BACKOFF_KINDS = ("bad_credentials", "host_validation", "banned")

clock: Callable[[], float] = time.time
"""Seconds since the epoch; tests replace it."""


@dataclass(frozen=True)
class LoginDiagnosis:
    kind: str
    """ok | unverified | bad_credentials | host_validation | banned | html | redirect |
    unexpected. `unverified` = a 2xx with no cookie and no body: ask for the version to know."""
    status: int
    body: str
    url: str
    headers_sent: dict[str, str]
    meaning: str

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    @property
    def accepted(self) -> bool:
        """The login may have worked: `ok`, or `unverified` (the caller must then check with
        `GET /api/v2/app/version`, see `after_probe`)."""
        return self.kind in ("ok", "unverified")

    def message(self) -> str:
        sent = ", ".join(f"{k}: {v}" for k, v in self.headers_sent.items())
        shown = self.body or "(empty)"
        return f'{self.meaning} [login answer: HTTP {self.status}, body "{shown}"; we sent {sent}]'

    def details(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "http_status": self.status,
            "body_excerpt": self.body,
            "headers_sent": self.headers_sent,
            "login_url": self.url,
            "meaning": self.meaning,
        }


def request_headers(url: str) -> dict[str, str]:
    """`Referer` and `Origin` for the WebUI at `url` (qBittorrent's CSRF check wants both to
    match its own host)."""
    base = url.strip().rstrip("/")
    parts = urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else base
    return {"Referer": base, "Origin": origin}


def host_header(url: str) -> str:
    return urlsplit(url.strip()).netloc


_SESSION_COOKIE = re.compile(r"^(?:QBT_)?SID(?:_\d+)?$", re.IGNORECASE)


def has_session_cookie(response: httpx.Response) -> bool:
    """Did the login answer set qBittorrent's session cookie (`SID`, or `QBT_SID_<port>`)?"""
    for line in response.headers.get_list("set-cookie"):
        if _SESSION_COOKIE.match(line.split("=", 1)[0].strip()):
            return True
    return False


def _excerpt(response: httpx.Response) -> str:
    text = " ".join(response.text.split())
    return text[:BODY_EXCERPT] + ("…" if len(text) > BODY_EXCERPT else "")


def diagnose_login(response: httpx.Response, url: str) -> LoginDiagnosis:
    """Interpret the answer to `POST {url}/api/v2/auth/login`."""
    base = url.strip().rstrip("/")
    host = host_header(base)
    sent = {**request_headers(base), "Host": host}
    status = response.status_code
    body = _excerpt(response)
    stripped = response.text.strip()
    content_type = response.headers.get("content-type", "").lower()

    def result(kind: str, meaning: str) -> LoginDiagnosis:
        return LoginDiagnosis(kind, status, body, f"{base}/api/v2/auth/login", sent, meaning)

    if status == 200 and stripped == "Ok.":
        return result("ok", "Login accepted.")
    if 200 <= status < 300 and has_session_cookie(response) and stripped.lower() != "fails.":
        # qBittorrent 5.2+ answers a successful login with an empty body (HTTP 204 or 200)
        # and the session cookie instead of `Ok.`.
        return result("ok", "Login accepted (session cookie set).")
    if 200 <= status < 300 and not stripped:
        return result(
            "unverified",
            f"The login answered HTTP {status} with no body and no session cookie: checking "
            "with the version call whether we are logged in.",
        )
    if status in (301, 302, 303, 307, 308):
        target = response.headers.get("location", "").split("?", 1)[0][:200]
        return result(
            "redirect",
            f"The server redirected the login to {target or 'another address'}: a proxy or an "
            "http→https redirect sits in front. Use that final address as the URL.",
        )
    if stripped == "Fails." or (status == 200 and stripped.lower().startswith("fail")):
        return result(
            "bad_credentials",
            "qBittorrent answered 'Fails.': it rejected the user name or password "
            "(WebUI → Authentication holds them; five wrong tries in a row ban the IP). "
            "Note that the password is checked as typed here, including spaces.",
        )
    if "html" in content_type or stripped[:1] == "<":
        return result(
            "html",
            "The answer is a web page, not qBittorrent's API: a reverse proxy, SSO/forward-auth "
            "login or a different application answers at this URL. Check the URL and the "
            "proxy; the API must be reachable without an extra login in front.",
        )
    if status == 401 or stripped.lower() == "unauthorized":
        return result(
            "host_validation",
            f"qBittorrent answered 401 Unauthorized. Two causes look the same from here: "
            f"(a) its Host-header validation or CSRF protection rejected the request before "
            f"looking at the password (typical behind a reverse proxy; the Host we sent is "
            f"'{host}'; a password that works in the browser points to this), or (b) in "
            f"qBittorrent 5.2+ the user name or password is wrong (older versions say 'Fails.' "
            f"with HTTP 200). The qBittorrent log tells which: 'Invalid Host header' is (a), "
            f"'WebAPI login failure' is (b). Fix for (a): use the internal URL "
            f"(http://<qbittorrent container>:8080) in Settings, or in qBittorrent → WebUI add "
            f"'{host.split(':')[0]}' to 'Server domains' (or switch off 'Enable Host header "
            f"validation' / 'CSRF protection'), and make sure the proxy passes the Host, "
            f"Origin and Referer headers unchanged.",
        )
    if status == 403:
        return result(
            "banned",
            "qBittorrent answered 403 Forbidden to the login: this server's IP address is "
            "banned after too many failed logins (qBittorrent → WebUI → 'Ban client after "
            "consecutive failures', default 5 failures, ban 3600 s). Behind a reverse proxy the "
            "banned address is the proxy's, so every client behind it is locked out. Wait for "
            "the ban to end or restart qBittorrent (that clears bans), and fix the credentials "
            "first: retrying with wrong ones extends the ban. (If no failed logins happened, a "
            "proxy/firewall in front is answering 403.)",
        )
    return result(
        "unexpected",
        f"Unexpected answer to the login (HTTP {status}): this does not look like "
        "qBittorrent's WebUI API. Check the URL and port.",
    )


def after_probe(diagnosis: LoginDiagnosis, version_status: int) -> LoginDiagnosis | None:
    """Settle an `unverified` login with the status of `GET /api/v2/app/version`: None = we
    are logged in; otherwise the failure to report ("not qBittorrent or a proxy page")."""
    if version_status == 200:
        return None
    return LoginDiagnosis(
        "unexpected",
        diagnosis.status,
        diagnosis.body,
        diagnosis.url,
        diagnosis.headers_sent,
        f"The login answered HTTP {diagnosis.status} with no body and no session cookie, and "
        f"the version call was refused (HTTP {version_status}): this does not look like "
        "qBittorrent's WebUI API (or a proxy answers instead). Check the URL and port.",
    )


# --------------------------------------------------------------------------- back-off


@dataclass
class _State:
    failures: int = 0
    until: float = 0.0
    kind: str = ""
    message: str = ""
    last_ts: float = 0.0


_states: dict[str, _State] = {}


def backoff_key(url: str, username: str, password: str) -> str:
    raw = f"{url.strip().rstrip('/')}\n{username}\n{password}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def paused_until(key: str) -> float | None:
    state = _states.get(key)
    if state and state.until > clock():
        return state.until
    return None


def clock_text(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M UTC")


def record_failure(key: str, diagnosis: LoginDiagnosis) -> float | None:
    """Note a failed login; returns the time until which retries are paused (or None for
    outcomes that deserve no pause, e.g. a proxy page or a timeout)."""
    state = _states.setdefault(key, _State())
    state.kind, state.message, state.last_ts = diagnosis.kind, diagnosis.message(), clock()
    if diagnosis.kind not in BACKOFF_KINDS:
        return None
    step = BACKOFF_STEPS_S[min(state.failures, len(BACKOFF_STEPS_S) - 1)]
    state.failures += 1
    state.until = clock() + step
    return state.until


def record_success(key: str) -> None:
    _states.pop(key, None)


def state_of(key: str) -> dict[str, object]:
    """For diagnostics: how often login failed in a row and whether retries are paused."""
    state = _states.get(key)
    if state is None:
        return {"failures_in_a_row": 0, "paused": False}
    until = paused_until(key)
    return {
        "failures_in_a_row": state.failures,
        "paused": until is not None,
        "paused_until": clock_text(until) if until else None,
        "last_failure_kind": state.kind,
        "last_failure": state.message,
        "last_failure_ago_s": int(clock() - state.last_ts),
    }


def paused_message(key: str) -> str | None:
    """The text of a refused-without-trying login, or None when not paused."""
    until = paused_until(key)
    if until is None:
        return None
    state = _states[key]
    return (
        f"qBittorrent login not retried: retries are paused until {clock_text(until)} "
        f"(failure #{state.failures}; so repeated attempts do not get this IP banned or extend "
        f"a ban). Last failure: {state.message}"
    )


def pause_note(until: float | None) -> str:
    return f" Retries are paused until {clock_text(until)}." if until else ""


def reset() -> None:
    """Forget all back-off state (tests)."""
    _states.clear()
