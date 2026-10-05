"""Service A's tools: one per downstream service and propagation strategy.

Every tool is called from inside the parent agent's tool run, so
`get_current_run_tree()` (used by `outbound_headers()`) is that tool run, and
downstream work should nest beneath it.
"""

import httpx
import langsmith as ls
from langchain_core.tools import BaseTool, tool
from langgraph.pregel.remote import RemoteGraph
from langgraph_sdk import get_client
from opentelemetry import propagate

from harness import config
from harness.tracing import HARNESS_RUN_HEADER, harness_run, outbound_headers

# Tool outputs, saved into results/runs/<case>.json for debugging.
NOTES: dict[str, list[str]] = {}


def _note(tool_name: str, value: str) -> str:
    NOTES.setdefault(tool_name, []).append(value)
    return value


# --- Case 1 and 5: child LangGraph on Agent Server ------------------------------


def child_remotegraph_tool(graph_id: str) -> BaseTool:
    """RemoteGraph(distributed_tracing=True) against one of the child's graphs."""

    @tool
    async def call_child(question: str) -> str:
        """Ask the child agent a question (it knows weather and can add numbers)."""
        # RemoteGraph builds langsmith-trace/baggage per request; they replace any
        # `baggage` passed here, so the case-6 replica workaround uses child_sdk_tool.
        headers = outbound_headers(propagate=False)
        remote = RemoteGraph(graph_id, url=config.url("child_langgraph"), distributed_tracing=True, headers=headers)
        try:
            out = await remote.ainvoke({"messages": [{"role": "user", "content": question}]})
            return _note("call_child", out["messages"][-1]["content"])
        except Exception as e:  # 1c is expected to fail server-side; keep the agent going
            return _note("call_child", f"ERROR {type(e).__name__}: {e}")

    return call_child


def child_sdk_tool(graph_id: str) -> BaseTool:
    """langgraph_sdk with headers built by hand from run_tree.to_headers()."""

    @tool
    async def call_child(question: str) -> str:
        """Ask the child agent a question (it knows weather and can add numbers)."""
        client = get_client(url=config.url("child_langgraph"))
        out = await client.runs.wait(
            None,
            graph_id,
            input={"messages": [{"role": "user", "content": question}]},
            headers=outbound_headers(),
        )
        return _note("call_child", out["messages"][-1]["content"])

    return call_child


# --- Case 2: MCP ------------------------------------------------------------------


@tool
async def call_mcp_weather(city: str) -> str:
    """Get the weather for a city from the MCP server."""
    from harness.mcp_client import call_tool_per_run

    return _note("call_mcp_weather", await call_tool_per_run("get_weather", {"city": city}))


# --- Case 3: Google ADK -----------------------------------------------------------


def adk_tool(service: str) -> BaseTool:
    @tool
    async def call_adk(question: str) -> str:
        """Ask the ADK weather agent a question."""
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{config.url(service)}/run", json={"message": question}, headers=outbound_headers())
        return _note("call_adk", resp.text)

    return call_adk


# --- Case 4: Strands --------------------------------------------------------------


@ls.traceable(name="send_to_strands", run_type="chain")
def _otel_headers() -> dict[str, str]:
    """In tracing_mode otel/hybrid, LangSmith makes this run the current OTel span,
    so `inject()` writes a W3C traceparent that points at it."""
    headers = {HARNESS_RUN_HEADER: harness_run.get() or ""}
    propagate.inject(headers)
    _note("traceparent", headers.get("traceparent", "<none: OTel context not set>"))
    return headers


_pure_otel_tracer = None


def _pure_otel_span_headers():
    """4f: a plain OpenTelemetry span (no LangSmith SDK) exported straight to
    LangSmith's OTel endpoint, as in the docs' Service A -> Service B example."""
    global _pure_otel_tracer
    import os

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    if _pure_otel_tracer is None:
        provider = TracerProvider()
        api_url = os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com").rstrip("/")
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
            endpoint=f"{api_url}/otel/v1/traces",
            headers={"x-api-key": os.environ["LANGSMITH_API_KEY"], "Langsmith-Project": config.PRIMARY_PROJECT},
        )))
        _pure_otel_tracer = provider.get_tracer("parent_pure_otel")
    return _pure_otel_tracer


async def _post_strands(route: str, question: str, headers: dict) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{config.url('strands_service')}/run/{route}", json={"message": question}, headers=headers)
    return resp.text


def strands_tool(variant: str) -> BaseTool:
    route = "otel" if variant in ("4b", "4c", "4f") else variant

    @tool
    async def call_strands(question: str) -> str:
        """Ask the Strands weather agent a question."""
        if variant == "4f":
            with _pure_otel_span_headers().start_as_current_span("otel:send_to_strands") as span:
                span.set_attribute("langsmith.metadata.harness_run", harness_run.get() or "")
                headers = {HARNESS_RUN_HEADER: harness_run.get() or ""}
                propagate.inject(headers)
                return _note("call_strands", await _post_strands(route, question, headers))
        headers = _otel_headers() if route == "otel" else outbound_headers()
        return _note("call_strands", await _post_strands(route, question, headers))

    return call_strands


# --- Case 7: Managed Deep Agents ----------------------------------------------------


def mda_tool() -> BaseTool:
    """RemoteGraph(distributed_tracing=True) against an MDA app under `mda dev`."""
    import os

    @tool
    async def call_mda(question: str) -> str:
        """Ask the managed deep agent a question (it knows the weather)."""
        remote = RemoteGraph(
            "probe",
            url=config.url("mda_agent"),
            # MDA's identity is LangSmith API-key auth (identity.py).
            api_key=os.environ["LANGSMITH_API_KEY"],
            distributed_tracing=True,
            headers=outbound_headers(propagate=False),
        )
        try:
            out = await remote.ainvoke({"messages": [{"role": "user", "content": question}]})
            return _note("call_mda", out["messages"][-1]["content"])
        except Exception as e:
            return _note("call_mda", f"ERROR {type(e).__name__}: {e}")

    return call_mda
