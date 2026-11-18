"""MCP client: lets the chat use tools of external MCP servers (remote, Streamable HTTP or
HTTP+SSE; tools only, no stdio). `context.py` owns one `McpManager` (rebuilt on every settings
change, servers connected lazily); writes go through the action layer as `mcp_call` (see
`actions.py`); `docs/DESIGN.md` has the decisions."""

from .connection import (
    CallOutcome,
    McpCallError,
    McpConnection,
    McpUnavailable,
    ServerInfo,
    ToolInfo,
)
from .discovery import McpCheck, check_mcp_server
from .manager import McpManager
from .settings import McpHeader, McpServerSettings, McpSettings, McpToolOverride
from .tools import build_mcp_tools, display_name

__all__ = [
    "CallOutcome",
    "McpCallError",
    "McpCheck",
    "McpConnection",
    "McpHeader",
    "McpManager",
    "McpServerSettings",
    "McpSettings",
    "McpToolOverride",
    "McpUnavailable",
    "ServerInfo",
    "ToolInfo",
    "build_mcp_tools",
    "check_mcp_server",
    "display_name",
]
