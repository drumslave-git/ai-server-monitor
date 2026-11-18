"""The tool spec the MCP client builds its chat tools from (`mcp_client/tools.py`), and the
context their handlers get. The agents take these as `agent.tools.mcp_agent_tools`."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from ..agent.runtime import ToolError
from ..config import Settings

if TYPE_CHECKING:
    from ..context import AppContext

__all__ = ["Tool", "ToolContext", "ToolError", "ToolKind"]

ToolKind = Literal["read", "write"]


@dataclass
class ToolContext:
    app: AppContext
    session_id: str
    source: str | None = None
    """Who asks, for the audit log of write actions. Default: chat:<session>."""
    turn: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    """Identifies this chat turn: the write requests of one turn share an approval batch."""

    @property
    def settings(self) -> Settings:
        return self.app.settings


Handler = Callable[[ToolContext, Any], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    kind: ToolKind
    handler: Handler
    kind_for: Callable[[dict[str, Any]], ToolKind] | None = None
    """For tools that read or write depending on the call (the generic MCP call): the kind
    of this call from its raw arguments. `kind` is the fallback."""

    def call_kind(self, args: dict[str, Any]) -> ToolKind:
        if self.kind_for is not None:
            try:
                return self.kind_for(args)
            except Exception:
                return self.kind
        return self.kind

    def definition(self) -> dict[str, Any]:
        """OpenAI-style function definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _clean_schema(self.args_model.model_json_schema()),
            },
        }


def _clean_schema(node: Any) -> Any:
    """Drop pydantic's `title` noise: small models read every token of the schema."""
    if isinstance(node, dict):
        return {k: _clean_schema(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node
