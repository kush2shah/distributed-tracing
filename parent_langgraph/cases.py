"""Run one test case: a ReAct agent (Service A) calls a downstream service.

    uv run python cases.py 1a

Each case records its root run ID and the runs that should appear in the trace
to results/runs/<case>.json; verify.py checks them against LangSmith.
"""

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field

import langsmith as ls
from langchain.agents import create_agent
from langchain_core.tracers.langchain import wait_for_all_tracers
from langchain_openai import ChatOpenAI
from langsmith.run_trees import ApiKeyAuth, WriteReplica, get_cached_client

import tools as t
from harness import config
from harness.results import CaseRun, Check, now
from harness.tracing import harness_run, rewrite_replicas

P, R, W = config.PRIMARY_PROJECT, config.REPLICA_PROJECT, config.REPLICA_WS_PROJECT

WEATHER = "What's the weather in San Francisco?"
WEATHER_AND_ADD = "Ask the child agent two things: the weather in San Francisco, and what 2 + 3 is."


@dataclass
class Case:
    title: str
    tools: object  # callable returning the tool list (some need async setup)
    prompt: str
    checks: list[Check]
    replicas: str | None = None  # "project", "workspace"
    rewrite_replicas: bool = False
    tracing_mode: str | None = None  # must match LANGSMITH_TRACING_MODE set by the Makefile
    notes: dict = field(default_factory=dict)


def _sync(*tools):
    async def build():
        return list(tools)

    return build


def _mcp_adapter(propagate: bool, hold_session: bool = False):
    async def build():
        from langchain.mcp import MCPAdapter

        from harness.mcp_client import long_lived_client

        adapter = MCPAdapter(long_lived_client(propagate))
        if hold_session:
            # The docs' "one session per invocation" pattern: keep the adapter open so
            # tool calls reuse one connection. Opened before any run exists; left open
            # until the process exits.
            await adapter.__aenter__()
            return await adapter.list_tools()
        async with adapter:
            return await adapter.list_tools()

    return build


def _three_hop(projects: list[str]) -> list[Check]:
    return [
        Check("call_child", projects=projects),
        Check("child:lookup_weather", "call_child", projects=projects),
        Check("child:mcp_call", "call_child", projects=projects),
        Check("mcp:add", "child:mcp_call", projects=projects),
    ]


CHILD = [Check("call_child"), Check("child:lookup_weather", "call_child")]
STRANDS_NATIVE = [
    Check("call_strands"),
    Check("strands:handle", "call_strands"),
    Check("invoke_agent*", "strands:handle"),
    Check("*strands_get_weather*", "invoke_agent*"),
]
STRANDS_OTEL = [
    Check("send_to_strands", "call_strands"),
    Check("strands:handle", "send_to_strands"),
    Check("invoke_agent*", "strands:handle"),
    Check("*strands_get_weather*", "invoke_agent*"),
]

CASES: dict[str, Case] = {
    # 1: LangGraph -> LangGraph on Agent Server
    "1a": Case("RemoteGraph(distributed_tracing=True), fixed factory", _sync(t.child_remotegraph_tool("child")), WEATHER, CHILD),
    "1b": Case("langgraph_sdk runs.wait(headers=to_headers()), fixed factory", _sync(t.child_sdk_tool("child")), WEATHER, CHILD),
    "1c": Case("RemoteGraph, factory copied verbatim from docs", _sync(t.child_remotegraph_tool("child_docs")), WEATHER,
               [Check("call_child"), Check("child:lookup_weather", "call_child", expect="FAIL")]),
    "1d": Case("RemoteGraph, server exports plain graph (no factory)", _sync(t.child_remotegraph_tool("child_static")), WEATHER,
               [Check("call_child"), Check("child:lookup_weather", "call_child", expect="FAIL")]),
    # 2: LangGraph -> MCP
    "2a": Case("MCP client opened per tool call with to_headers()", _sync(t.call_mcp_weather), WEATHER,
               [Check("call_mcp_weather"), Check("mcp:get_weather", "call_mcp_weather")]),
    "2b": Case("Long-lived MCPAdapter client, httpx Auth adds headers per request", _mcp_adapter(True), WEATHER,
               [Check("get_weather"), Check("mcp:get_weather", "get_weather")]),
    "2c": Case("Long-lived MCPAdapter client, no trace headers (control)", _mcp_adapter(False), WEATHER,
               [Check("get_weather"), Check("mcp:get_weather", "get_weather", expect="FAIL")]),
    "2d": Case("Long-lived MCPAdapter, httpx Auth, session held open across the run", _mcp_adapter(True, hold_session=True), WEATHER,
               [Check("get_weather"), Check("mcp:get_weather", "get_weather", expect="?")]),
    # 3: LangGraph -> Google ADK
    "3a": Case("ADK behind TracingMiddleware", _sync(t.adk_tool("adk_service")), WEATHER,
               [Check("call_adk"), Check("google_adk.session", "call_adk"), Check("adk:get_weather", "google_adk.session")]),
    "3b": Case("ADK with configure_google_adk(project_name=<other>)", _sync(t.adk_tool("adk_service_override")), WEATHER,
               [Check("call_adk"), Check("google_adk.session", "call_adk", expect="?"), Check("adk:get_weather", "google_adk.session", expect="?")]),
    # 4: LangGraph -> Strands (question 1)
    "4a": Case("LangSmith headers + tracing_context(parent=) around Strands", _sync(t.strands_tool("4a")), WEATHER,
               STRANDS_NATIVE[:2] + [Check(c.name, c.ancestor, expect="?") for c in STRANDS_NATIVE[2:]]),
    "4b": Case("Parent tracing_mode=hybrid, W3C traceparent", _sync(t.strands_tool("4b")), WEATHER,
               [Check("call_strands")] + [Check(c.name, c.ancestor, expect="?") for c in STRANDS_OTEL], tracing_mode="hybrid"),
    "4c": Case("Parent tracing_mode=otel (all-OTel both sides), W3C traceparent", _sync(t.strands_tool("4c")), WEATHER,
               [Check("call_strands")] + [Check(c.name, c.ancestor, expect="?") for c in STRANDS_OTEL], tracing_mode="otel"),
    "4d": Case("langsmith.* attributes on one wrapper span (docs)", _sync(t.strands_tool("4d")), WEATHER,
               [Check("call_strands"), Check("strands:handle", "call_strands", expect="?")]
               + [Check(c.name, c.ancestor, expect="?") for c in STRANDS_NATIVE[2:]]),
    "4e": Case("langsmith.* attributes stamped on every Strands span", _sync(t.strands_tool("4e")), WEATHER,
               [Check("call_strands"), Check("strands:handle", "call_strands", expect="?")]
               + [Check(c.name, c.ancestor, expect="?") for c in STRANDS_NATIVE[2:]]),
    # 5: three hops, grandchildren in MCP
    "5": Case("LangGraph -> child LangGraph -> MCP", _sync(t.child_remotegraph_tool("child")), WEATHER_AND_ADD, _three_hop([P])),
    # 6: case 5 plus replicas
    "6a": Case("Project replica, child uses the docs factory pattern", _sync(t.child_remotegraph_tool("child")), WEATHER_AND_ADD,
               _three_hop([P]) + [Check(c.name, c.ancestor, projects=[R], expect="?") for c in _three_hop([R])], replicas="project"),
    "6b": Case("Project replica, child rebuilds parent from raw baggage", _sync(t.child_remotegraph_tool("child_baggage")), WEATHER_AND_ADD,
               _three_hop([P, R]), replicas="project"),
    "6c": Case("Second-workspace replica, downstream services add no credentials", _sync(t.child_remotegraph_tool("child_baggage")), WEATHER_AND_ADD,
               _three_hop([P]) + [Check("call_child", projects=[W])]
               + [Check(c.name, c.ancestor, projects=[W], expect="?") for c in _three_hop([W])[1:]], replicas="workspace"),
    "6d": Case("Second-workspace replica, downstream services attach their own key", _sync(t.child_remotegraph_tool("child_baggage")), WEATHER_AND_ADD,
               _three_hop([P, W]), replicas="workspace", rewrite_replicas=True),
}


class Skip(Exception):
    """A prerequisite (such as a second-workspace key) is missing."""


def _replicas(kind: str) -> list[WriteReplica]:
    replicas = [WriteReplica(project_name=P, primary=True)]
    if kind == "project":
        replicas.append(WriteReplica(project_name=R))
    elif kind == "workspace":
        if not config.REPLICA_API_KEY:
            raise Skip("LANGSMITH_API_KEY_REPLICA is not set")
        replicas.append(
            WriteReplica(project_name=W, api_url=config.REPLICA_API_URL, auth=ApiKeyAuth(api_key=config.REPLICA_API_KEY))
        )
    return replicas


async def run(case_id: str) -> None:
    case = CASES[case_id]
    mode = os.environ.get("LANGSMITH_TRACING_MODE")
    if case.tracing_mode != (mode or None):
        raise SystemExit(f"case {case_id} needs LANGSMITH_TRACING_MODE={case.tracing_mode}, got {mode}")

    run_id = str(uuid.uuid4())
    harness_run.set(run_id)
    rewrite_replicas.set(case.rewrite_replicas)
    record = CaseRun(case=case_id, title=case.title, harness_run=run_id, started_at=now(), checks=case.checks)

    agent = create_agent(
        ChatOpenAI(model=config.MODEL, temperature=0),
        tools=await case.tools(),
        system_prompt="You are a test agent. Always answer by calling exactly one of your tools once, then reply in one sentence.",
    )
    metadata = {"harness_run": run_id, "case": case_id}

    @ls.traceable(name="harness_root", run_type="chain")
    async def harness_root(prompt: str) -> str:
        record.root_run_id = str(ls.get_current_run_tree().id)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]}, config={"metadata": metadata})
        return result["messages"][-1].content

    try:
        replicas = _replicas(case.replicas) if case.replicas else None
    except Skip as e:
        record.notes["skipped"] = str(e)
        record.save()
        print(f"[{case_id}] SKIPPED: {e}")
        return
    try:
        with ls.tracing_context(replicas=replicas, metadata=metadata):
            answer = await harness_root(case.prompt)
        record.notes["answer"] = answer
    except Exception as e:
        record.notes["error"] = f"{type(e).__name__}: {e}"
    finally:
        record.notes["tools"] = t.NOTES
        wait_for_all_tracers()
        get_cached_client().flush()
        _flush_otel()
        record.save()
    print(f"[{case_id}] {case.title}\n  root={record.root_run_id} harness_run={run_id}\n  answer={record.notes.get('answer') or record.notes.get('error')}")


def _flush_otel() -> None:
    from opentelemetry import trace as trace_api

    provider = trace_api.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
