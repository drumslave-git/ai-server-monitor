"""Secret scrubbing for everything an API answer hands to the model.

The chat engine scrubs tool results again, but data must be clean *before* a JMESPath
`select` looks at it, and answers also go to the action audit log and Telegram, which never
pass through the engine. The shapes that matter here:

- `{"apiKey": "..."}` / `{"password": "..."}`: values of keys that look secret;
- *arr "fields" lists: `{"name": "apiKey", "value": "..."}` and `{"privacy": "password"}`;
- Docker `Config.Env`: `["DB_PASSWORD=hunter2", ...]`;
- every secret configured in the settings, wherever it shows up inside a string.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, SecretStr

REDACTED = "[redacted]"
_MIN_SECRET_LEN = 6
_SECRET_KEY = re.compile(
    r"(api[_-]?key|password|passwd|secret|authorization|cookie|credential|private[_-]?key"
    r"|(^|[_-])token$|^token)",
    re.IGNORECASE,
)
# Looser, for environment variable *names* (DB_PASS, AUTH_KEY, ...).
_ENV_SECRET = re.compile(r"(key|secret|token|pass|pwd|credential|auth|cert)", re.IGNORECASE)
_ENV_ITEM = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)=(.*)$", re.DOTALL)
_ENV_LISTS = {"env", "environment"}
_PRIVATE_FIELDS = {"password", "apikey"}
_PATTERNS = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot tokens
    re.compile(r"(?i)(api[_-]?key|token|password|secret)=[^\s&\"']{4,}"),
]


def secret_values(settings: Any) -> set[str]:
    """Every secret configured in the settings, as plain strings."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, SecretStr):
            raw = value.get_secret_value()
            if len(raw) >= _MIN_SECRET_LEN:
                found.add(raw)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item)
        elif isinstance(value, BaseModel):
            for name in type(value).model_fields:
                walk(getattr(value, name, None))

    walk(settings)
    return found


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()):
        # Longest first, so a secret that contains another one is replaced whole.
        self._secrets = sorted(
            {s for s in secrets if len(s) >= _MIN_SECRET_LEN}, key=len, reverse=True
        )

    @classmethod
    def from_settings(cls, settings: Any) -> Redactor:
        return cls(secret_values(settings))

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value

    def clean(self, value: Any, parent_key: str = "") -> Any:
        if isinstance(value, dict):
            private = _is_private_field(value)
            out: dict[Any, Any] = {}
            for key, item in value.items():
                if (_SECRET_KEY.search(str(key)) or (private and key == "value")) and item:
                    out[key] = REDACTED
                else:
                    out[key] = self.clean(item, str(key))
            return out
        if isinstance(value, list | tuple):
            if parent_key.lower() in _ENV_LISTS:
                return [self._env_item(v) for v in value]
            return [self.clean(v, parent_key) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value

    def _env_item(self, item: Any) -> Any:
        if isinstance(item, str):
            match = _ENV_ITEM.match(item)
            if match and _ENV_SECRET.search(match.group(1)) and match.group(2):
                return f"{match.group(1)}={REDACTED}"
        return self.clean(item)


def _is_private_field(node: dict[Any, Any]) -> bool:
    """An *arr settings field: {"name": "apiKey", "value": ..., "privacy": "apiKey"}."""
    name = node.get("name")
    if isinstance(name, str) and "value" in node and _SECRET_KEY.search(name):
        return True
    privacy = node.get("privacy")
    return isinstance(privacy, str) and privacy.lower() in _PRIVATE_FIELDS
