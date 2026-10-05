# distributed-tracing

A test harness that answers, with traces you can open in LangSmith, which
service and framework combinations produce **one trace** (parent → children →
grandchildren in one project) and which ones split, drop runs, or lose replicas.

Every case runs a real agent, then `verify.py` reads the trace back from
LangSmith and checks trace membership, ancestry, and project. Results are in
[`results/matrix.md`](results/matrix.md); the write-up is in [FINDINGS.md](FINDINGS.md).

## Layout

| Path | What it is | Port |
|---|---|---|
| `parent_langgraph/` | Service A. A ReAct agent (`create_agent`) whose tools call each downstream service. `cases.py` defines and runs every case. | (client) |
| `child_langgraph/` | Service B. A LangGraph agent on Agent Server (`langgraph dev`). `langgraph.json` exports four graph entries for case 1 and 6. | 2024 |
| `mcp_server/` | FastMCP server (`get_weather`, `add`). Each tool continues the caller's trace from the request headers. | 8001 |
| `adk_service/` | Google ADK agent behind FastAPI with `TracingMiddleware` and `configure_google_adk()`. A second copy on 8004 sets its own project name (case 3b). | 8002, 8004 |
| `strands_service/` | AWS Strands agent behind FastAPI with LangSmith's OTel exporter. One route per bridging strategy (case 4). | 8003 |
| `mda_agent/` | A minimal Managed Deep Agent under `mda dev` (case 7), plus a patched copy of its build on 2026 (case 7c). | 2025, 2026 |
| `common/` | Shared package: settings, trace-header helpers, MCP client variants, run records. | |
| `verify.py` | Polls LangSmith for each case's runs and writes `results/verify/<case>.json` and `results/matrix.md`. | |
| `scripts/services.sh` | Starts and stops all services (logs in `.logs/`). | |

Each service is its own uv project with its own virtualenv, so ADK, Strands,
`langgraph-api`, and MDA dependencies never have to resolve together.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/), Python 3.11+, and a LangSmith account.

```bash
cp .env.example .env   # then fill it in
make install           # uv sync in every service
```

`.env` (gitignored) needs:

| Variable | Used for |
|---|---|
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | Every agent's model. Any OpenAI-compatible gateway works; ADK reaches it through LiteLLM and Strands through its OpenAI provider, so no Google, Anthropic, or AWS key is needed. |
| `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_TRACING=true` | Primary workspace and project. |
| `HARNESS_MODEL` (optional) | Model name on the gateway, default `gpt-4.1-mini`. It must support tool calling. |
| `LANGSMITH_API_KEY_REPLICA` (optional) | A key for a **different** workspace. Cases 6e and 6f are skipped without it. |

The MDA case reads `.env` through a symlink, `mda_agent/.env -> ../.env`
(create it with `ln -s ../.env mda_agent/.env`). `mda dev` copies it into
`mda_agent/.mda/build/`, which is gitignored.

## Run

```bash
make services-up       # start all services (about 30s; MDA compiles first)
make all               # run and verify every case
make matrix            # rebuild results/matrix.md from results/verify/
make services-down
```

Run one case or one group:

```bash
make case-4e           # one case
make case-4            # every Strands variant
uv run python verify.py 4e   # re-verify without re-running
```

Each `case-*` target runs the agent, then verifies. A case's runner records
its root run ID and the runs it expects in `results/runs/<case>.json`.

## Cases

| Case | Question | Setup |
|---|---|---|
| **1a–1d** | LangGraph → LangGraph on Agent Server | RemoteGraph or SDK headers; correct factory, the docs' factory verbatim, and no factory |
| **2a–2d** | LangGraph → MCP | Client per tool call, long-lived client with an httpx auth hook (with and without a held session), and no headers |
| **3a–3b** | LangGraph → Google ADK | `TracingMiddleware`, and ADK configured with its own project |
| **4a–4f** | LangGraph → Strands (OTel) | `tracing_context` only, `hybrid` and `otel` tracing modes, the docs' `langsmith.*` attributes, attributes on every span, and a pure-OTel parent span |
| **5** | Three hops | LangGraph → child LangGraph → MCP |
| **6a–6f** | Case 5 + replicas | Second project, then second workspace, adding one fix per step |
| **7a–7c** | LangGraph → Managed Deep Agents | As shipped, with tool-call middleware, and with the generated factory patched |

## How verification works

Agent run names vary, so each service emits a **marker run** with a fixed name
(`child:lookup_weather`, `mcp:add`, `adk:get_weather`, and so on). Markers carry
a per-run correlation ID, `harness_run`, which travels in an `x-harness-run`
header, separate from the tracing headers under test, so a run can be found
even when its trace context was lost.

For each expected run, `verify.py` reports:

| Verdict | Meaning |
|---|---|
| `PASS` | In the root's trace, under the expected ancestor, in the expected project |
| `WRONG_PARENT` | In the root's trace, under the wrong ancestor |
| `SPLIT` | Exists, but in a separate trace (the "starts a new trace" symptom) |
| `NOT_REPLICATED` | In the primary project but missing from the replica project |
| `MISROUTED` | Found in a different project than expected |
| `MISSING` | Not found anywhere: never ran, errored, or was dropped during ingestion |

It polls until every check passes or the verdicts stop changing (at least 30s,
at most 120s). Cases with `expected ?` are exploratory: the matrix records what
happened without a prior expectation.

## Results

Latest full run (2026-10-05, `make all`). "One trace?" is the raw result;
"As expected" compares it with the case's expectation, so a negative control
that splits is `FAIL` / `yes`. What each result means, and how to work around
each failure, is in [FINDINGS.md](FINDINGS.md).

| Case | Setup | One trace? | As expected | Non-passing runs |
|---|---|---|---|---|
| 1a | RemoteGraph(distributed_tracing=True), fixed factory | **PASS** | yes | - |
| 1b | langgraph_sdk runs.wait(headers=to_headers()), fixed factory | **PASS** | yes | - |
| 1c | RemoteGraph, factory copied verbatim from docs | **FAIL** | yes | child:lookup_weather @ distributed-tracing: MISSING |
| 1d | RemoteGraph, server exports plain graph (no factory) | **FAIL** | yes | child:lookup_weather @ distributed-tracing: SPLIT |
| 2a | MCP client opened per tool call with to_headers() | **PASS** | yes | - |
| 2b | Long-lived MCPAdapter client, httpx Auth adds headers per request | **PASS** | yes | - |
| 2c | Long-lived MCPAdapter client, no trace headers (control) | **FAIL** | yes | mcp:get_weather @ distributed-tracing: SPLIT |
| 2d | Long-lived MCPAdapter, httpx Auth, session held open across the run | **PASS** | yes | - |
| 3a | ADK behind TracingMiddleware | **PASS** | yes | - |
| 3b | ADK with configure_google_adk(project_name=<other>) | **PASS** | yes | - |
| 4a | LangSmith headers + tracing_context(parent=) around Strands | **FAIL** | yes | invoke_agent* @ distributed-tracing: SPLIT; *strands_get_weather* @ distributed-tracing: SPLIT |
| 4b | Parent tracing_mode=hybrid, W3C traceparent | **FAIL** | yes | strands:handle @ distributed-tracing: MISSING; invoke_agent* @ distributed-tracing: MISSING; *strands_get_weather* @ distributed-tracing: MISSING |
| 4c | Parent tracing_mode=otel (all-OTel both sides), W3C traceparent | **FAIL** | yes | strands:handle @ distributed-tracing: MISSING; invoke_agent* @ distributed-tracing: MISSING; *strands_get_weather* @ distributed-tracing: MISSING |
| 4d | langsmith.* attributes on one wrapper span (docs) | **FAIL** | yes | invoke_agent* @ distributed-tracing: MISSING; *strands_get_weather* @ distributed-tracing: MISSING |
| 4e | langsmith.* attributes stamped on every Strands span | **PASS** | yes | - |
| 4f | Pure-OTel span on the parent side (no LangSmith SDK for this hop), W3C traceparent | **FAIL** | yes | otel:send_to_strands @ distributed-tracing: SPLIT; strands:handle @ distributed-tracing: SPLIT; invoke_agent* @ distributed-tracing: SPLIT; *strands_get_weather* @ distributed-tracing: SPLIT |
| 5 | LangGraph -> child LangGraph -> MCP | **PASS** | yes | - |
| 6a | Project replica, SDK defaults (RemoteGraph + docs factory) | **FAIL** | yes | child:lookup_weather @ distributed-tracing-replica: NOT_REPLICATED; child:mcp_call @ distributed-tracing-replica: NOT_REPLICATED; mcp:add @ distributed-tracing-replica: NOT_REPLICATED |
| 6b | Project replica, child rebuilds parent from raw baggage | **FAIL** | yes | child:lookup_weather @ distributed-tracing-replica: NOT_REPLICATED; child:mcp_call @ distributed-tracing-replica: NOT_REPLICATED; mcp:add @ distributed-tracing-replica: NOT_REPLICATED |
| 6c | Project replica, SDK headers + replicas re-added to baggage, docs factory | **FAIL** | yes | child:lookup_weather @ distributed-tracing-replica: NOT_REPLICATED; child:mcp_call @ distributed-tracing-replica: NOT_REPLICATED; mcp:add @ distributed-tracing-replica: NOT_REPLICATED |
| 6d | Project replica, SDK headers + replicas re-added to baggage, raw-baggage factory | **PASS** | yes | - |
| 6e | Second-workspace replica, downstream adds no credentials | **SKIPPED** | yes | LANGSMITH_API_KEY_REPLICA is not set |
| 6f | Second-workspace replica, downstream attaches its own key | **SKIPPED** | yes | LANGSMITH_API_KEY_REPLICA is not set |
| 7a | RemoteGraph(distributed_tracing=True) -> Managed Deep Agent (mda dev) | **FAIL** | yes | mda:lookup_weather @ distributed-tracing: SPLIT |
| 7b | Same, plus MDA tool-call middleware wrapping tracing_context(parent=) | **FAIL** | yes | mda:lookup_weather @ distributed-tracing: SPLIT; mda_lookup_weather @ distributed-tracing: SPLIT |
| 7c | Same MDA build with its generated factory patched to apply tracing_context | **PASS** | yes | - |

## Notes

- Cases run one at a time. `verify.py` scopes each case by start time and its
  correlation ID, so running cases in parallel can mix up untagged runs (OTel spans).
- The child runs under `langgraph dev --allow-blocking`, because FastMCP's client
  makes a blocking filesystem call that `langgraph dev`'s blocking-call guard
  rejects inside an async tool. Deployed Agent Server does not run that guard.
- Distributed tracing headers are trusted input. Everything here is
  service-to-service on localhost; don't accept `langsmith-trace` or `baggage`
  from the public internet.
