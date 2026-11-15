"""Generic HTTP API access for the chat: two tools give the model the full APIs of every
configured service (Radarr, Sonarr, Whisparr, Prowlarr, qBittorrent, Docker, Prometheus)
without a hand-written tool per question.

- `ApiGateway`: base URL, credentials, path rules, rate limit, audit log, response shaping.
- `api_tool_specs(settings)`: the `api_call` and `api_endpoints` tool specs.
- the `api_call` action pieces (writes become owner-approved requests; see `tools.py`).
- the bundled endpoint index (`index_data/endpoints.json`, from `scripts/gen_api_index.py`).
"""

from __future__ import annotations

from .errors import ApiAccessError
from .gateway import ApiGateway, ApiRequest
from .tools import (
    ACTION_DESCRIPTION,
    ACTION_NAME,
    POLICY_KEY,
    ApiCallActionArgs,
    ToolSpec,
    action_check,
    action_run,
    action_target,
    api_tool_specs,
)

__all__ = [
    "ACTION_DESCRIPTION",
    "ACTION_NAME",
    "POLICY_KEY",
    "ApiAccessError",
    "ApiCallActionArgs",
    "ApiGateway",
    "ApiRequest",
    "ToolSpec",
    "action_check",
    "action_run",
    "action_target",
    "api_tool_specs",
]
