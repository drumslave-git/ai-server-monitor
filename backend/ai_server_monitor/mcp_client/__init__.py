"""MCP client: the tools of remote MCP servers (Streamable HTTP or HTTP+SSE; tools only, no
stdio) for the agents, through Pydantic AI's `MCPToolset`. `context.py` owns one `McpServers`
per settings snapshot; every agent run builds its toolsets from it (`toolsets.py`); writes go
through the action layer as `mcp_call` (`actions.py`); `docs/DESIGN.md` has the decisions."""

from .discovery import McpCheck, check_mcp_server
from .settings import McpHeader, McpServerSettings, McpSettings, McpToolOverride
from .toolsets import McpRun, McpServers, kind_of, split_name

__all__ = [
    "McpCheck",
    "McpHeader",
    "McpRun",
    "McpServerSettings",
    "McpServers",
    "McpSettings",
    "McpToolOverride",
    "check_mcp_server",
    "kind_of",
    "split_name",
]
