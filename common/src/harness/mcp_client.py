"""Three ways to call the MCP server, one per case-2 variant.

MCP transports take headers when the client is constructed, so tracing headers
captured at startup go stale after the first run. The two propagating variants
work around that differently:

* `call_tool_per_run`: build a client inside the tool call, with headers from
  the run that is current at that moment (2a).
* `TraceHeadersAuth`: keep one long-lived client and add headers from the
  current run to every HTTP request it sends (2b).
"""

from typing import Any

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from harness import config
from harness.tracing import HARNESS_RUN_HEADER, harness_run, outbound_headers

MCP_URL = f"{config.url('mcp_server')}/mcp"


async def call_tool_per_run(tool: str, arguments: dict[str, Any]) -> str:
    transport = StreamableHttpTransport(MCP_URL, headers=outbound_headers())
    async with Client(transport) as client:
        result = await client.call_tool(tool, arguments)
    return str(result.data if result.data is not None else result.content)


class TraceHeadersAuth(httpx2.Auth):
    """Adds the current run's tracing headers to each outgoing MCP request."""

    def auth_flow(self, request: httpx2.Request):
        request.headers.update(outbound_headers())
        yield request


def long_lived_client(propagate: bool) -> Client:
    """A client for MCPAdapter. Without propagation it sends only the correlation ID."""
    if propagate:
        return Client(StreamableHttpTransport(MCP_URL, auth=TraceHeadersAuth()))
    return Client(StreamableHttpTransport(MCP_URL, headers={HARNESS_RUN_HEADER: harness_run.get() or ""}))
