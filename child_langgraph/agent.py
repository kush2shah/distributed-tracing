"""Service B: a LangGraph agent served by Agent Server (`langgraph dev`).

langgraph.json exports four graphs over the same agent so case 1 can compare
server-side setups:

* `child`        the docs' opt-in factory with the decorator fixed (asynccontextmanager)
* `child_docs`   the docs' factory verbatim (@contextmanager on an async def)
* `child_static` the compiled agent, no factory (does the server continue traces by itself?)
* `child_baggage` passes the raw `baggage` header so replicas survive (case 6)
"""

import contextlib

import langsmith as ls
from langchain.agents import create_agent
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from harness import config
from harness.mcp_client import call_tool_per_run
from harness.tracing import REWRITE_REPLICAS_HEADER, bind_harness_run, marker_metadata, parent_from_headers


def _bind(run_config: RunnableConfig) -> None:
    # Agent Server copies x-harness-* request headers into configurable (see langgraph.json).
    bind_harness_run(run_config.get("configurable", {}))


@ls.traceable(name="child:lookup_weather", run_type="tool")
def _lookup_weather(city: str) -> str:
    return f"Child service says: 65F and foggy in {city}."


@ls.traceable(name="child:mcp_call", run_type="chain")
async def _add_via_mcp(a: float, b: float) -> str:
    # Called inside a traced run, so outbound headers point at this run (grandchild hop).
    return await call_tool_per_run("add", {"a": a, "b": b})


@tool
def child_lookup_weather(city: str, config: RunnableConfig) -> str:
    """Look up the weather for a city."""
    _bind(config)
    return _lookup_weather(city, langsmith_extra={"metadata": marker_metadata(service="child_langgraph")})


@tool
async def child_add_via_mcp(a: float, b: float, config: RunnableConfig) -> str:
    """Add two numbers using the MCP math server."""
    _bind(config)
    return await _add_via_mcp(a, b, langsmith_extra={"metadata": marker_metadata(service="child_langgraph")})


agent = create_agent(
    ChatOpenAI(model=config.MODEL, temperature=0),
    tools=[child_lookup_weather, child_add_via_mcp],
    system_prompt=(
        "You are a test agent. Always use your tools: call child_lookup_weather for any "
        "weather question and child_add_via_mcp for any addition. Then answer in one sentence."
    ),
)

# Named alias for the no-factory control, so the server sees a compiled graph.
child_static = agent


@contextlib.asynccontextmanager
async def graph(config: RunnableConfig):
    """The docs' opt-in factory, with @asynccontextmanager instead of @contextmanager."""
    configurable = config.get("configurable", {})
    with ls.tracing_context(
        parent=configurable.get("langsmith-trace"),
        project_name=configurable.get("langsmith-project"),
        metadata=configurable.get("langsmith-metadata"),
        tags=configurable.get("langsmith-tags"),
    ):
        yield agent


@contextlib.contextmanager
async def graph_docs(config):
    """Copied from /langsmith/agent-server-distributed-tracing, decorator included."""
    configurable = config.get("configurable", {})
    parent_trace = configurable.get("langsmith-trace")
    parent_project = configurable.get("langsmith-project")
    metadata = configurable.get("langsmith-metadata")
    tags = configurable.get("langsmith-tags")
    with ls.tracing_context(parent=parent_trace, project_name=parent_project, metadata=metadata, tags=tags):
        yield agent


@contextlib.asynccontextmanager
async def graph_baggage(config: RunnableConfig):
    """Rebuild the parent from the raw headers so `langsmith-replicas` is kept.

    Agent Server extracts only metadata, tags, and project from `baggage`. This
    graph needs `baggage` allowed in langgraph.json `http.configurable_headers`.
    """
    configurable = config.get("configurable", {})
    headers = {
        key: configurable[key]
        for key in ("langsmith-trace", "baggage", REWRITE_REPLICAS_HEADER)
        if configurable.get(key)
    }
    with ls.tracing_context(parent=parent_from_headers(headers)):
        yield agent
