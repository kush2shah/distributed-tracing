"""FastMCP server whose tools continue the caller's LangSmith trace.

MCP has no tracing hook of its own, so each tool reads the HTTP request headers
for the current call and runs inside `tracing_context(parent=...)`. When the
caller sent no `langsmith-trace` header (case 2c), the tool starts a new trace.
"""

import langsmith as ls
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from harness import config
from harness.tracing import bind_harness_run, continue_trace, marker_metadata

mcp = FastMCP("harness-mcp")


@ls.traceable(name="mcp:get_weather", run_type="tool")
def _get_weather(city: str) -> str:
    return f"It is 72F and sunny in {city}."


@ls.traceable(name="mcp:add", run_type="tool")
def _add(a: float, b: float) -> float:
    return a + b


def _trace_context():
    # get_http_headers() drops credential and hop-by-hop headers but keeps
    # `langsmith-trace`, `baggage`, and `x-*`.
    headers = get_http_headers()
    bind_harness_run(headers)
    return continue_trace(headers)


@mcp.tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    with _trace_context():
        return _get_weather(city, langsmith_extra={"metadata": marker_metadata(service="mcp_server")})


@mcp.tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    with _trace_context():
        return _add(a, b, langsmith_extra={"metadata": marker_metadata(service="mcp_server")})


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=config.PORTS["mcp_server"], show_banner=False)
