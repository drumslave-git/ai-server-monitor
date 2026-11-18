"""What the chat's own tool handlers get (`ToolContext`), and the schema cleaner the job tools
use."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ..agent.runtime import ToolError
from ..config import Settings

if TYPE_CHECKING:
    from ..context import AppContext

__all__ = ["ToolContext", "ToolError", "ToolKind"]

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


def _clean_schema(node: Any) -> Any:
    """Drop pydantic's `title` noise: small models read every token of the schema."""
    if isinstance(node, dict):
        return {k: _clean_schema(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node
