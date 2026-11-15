"""LLM trace and app log: what went to the model and came back, and what the app itself logged.

Hook points for the rest of the app (the API router is `tracing.api.router`):

    Tracing(db, config, scrubber=..., events=...)    the service the app holds
    tracing.wrap_transport(inner)                    under the LLM client's httpx transport
    trace_scope(purpose, link_kind=..., link_id=..., turn=..., step=...)    set by callers
    trace_call()                                     optional: groups the retries of one call
    tracing.start_tool(name, args).finish(ok=..., result=...)     chat tool calls
    await tracing.attach(scope, "report", report_id)               link calls to a later id
"""

from .config import TraceConfig
from .scope import TraceScope, current_scope, scoped_iter, trace_call, trace_scope
from .service import ToolTimer, Tracing

__all__ = [
    "ToolTimer",
    "TraceConfig",
    "TraceScope",
    "Tracing",
    "current_scope",
    "scoped_iter",
    "trace_call",
    "trace_scope",
]
