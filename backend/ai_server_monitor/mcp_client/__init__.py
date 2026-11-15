"""MCP client: lets the chat use tools of external MCP servers (remote, Streamable HTTP or
HTTP+SSE; tools only, no stdio). See `docs/handover-mcp.md` for how it plugs in."""

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
