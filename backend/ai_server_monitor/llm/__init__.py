"""OpenAI-compatible LLM access."""

from .client import (
    Completion,
    LLMClient,
    LLMConnectionError,
    LLMEmptyAnswerError,
    LLMError,
    LLMStats,
    LLMStatusError,
    StreamChunk,
    ToolCall,
    Usage,
)
from .probe import ToolModeResolver, probe_tool_mode

__all__ = [
    "Completion",
    "LLMClient",
    "LLMConnectionError",
    "LLMEmptyAnswerError",
    "LLMError",
    "LLMStats",
    "LLMStatusError",
    "StreamChunk",
    "ToolCall",
    "ToolModeResolver",
    "Usage",
    "probe_tool_mode",
]
