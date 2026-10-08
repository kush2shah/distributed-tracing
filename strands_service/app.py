"""AWS Strands agent behind FastAPI, with one route per bridging variant (question 1).

Strands emits OpenTelemetry spans, which reach LangSmith through its OTel
endpoint, not through the LangSmith SDK. Each route tries a different way of
making those spans join the caller's trace:

  /run/4a     LangSmith-native headers + tracing_context(parent=headers) only
  /run/otel   W3C `traceparent` extracted with opentelemetry.propagate (4b, 4c)
  /run/4d     LangSmith-native headers -> langsmith.* attributes on one wrapper span (docs)
  /run/4e     same, but a span processor stamps langsmith.* on every Strands span
"""

import os
import threading
from datetime import datetime, timezone

import langsmith as ls
import uvicorn
from fastapi import FastAPI, Request
from langsmith.integrations.strands_agents import create_langsmith_exporter
from langsmith.run_trees import RunTree, uuid7_from_datetime
from opentelemetry import propagate
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic import BaseModel
from strands import Agent, tool
from strands.models.openai import OpenAIModel
from strands.telemetry import StrandsTelemetry

from harness import config
from harness.tracing import bind_harness_run, harness_run, lower_headers, marker_metadata, parent_from_headers

LANGSMITH_API_URL = os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com").rstrip("/")


class LangSmithParentStamper(SpanProcessor):
    """Gives every descendant of a registered span explicit langsmith.* IDs.

    OTLP span IDs are 8 bytes and cannot name a LangSmith run, so a span's
    native parentSpanId cannot point at a run created by the LangSmith SDK.
    The docs' workaround sets langsmith.* attributes on one span; this applies
    it to the whole subtree so children do not fall back to native IDs.
    """

    def __init__(self) -> None:
        # OTel span ID -> (trace_id, run_id, dotted_order, project)
        self._runs: dict[int, tuple[str, str, str, str]] = {}
        self._lock = threading.Lock()

    def register(self, span, trace_id: str, run_id: str, dotted_order: str, project: str) -> None:
        with self._lock:
            self._runs[span.get_span_context().span_id] = (trace_id, run_id, dotted_order, project)

    def on_start(self, span, parent_context=None) -> None:
        parent_span_id = trace_api.get_current_span(parent_context).get_span_context().span_id
        with self._lock:
            parent = self._runs.get(parent_span_id)
        if parent is None:
            return
        trace_id, parent_run_id, parent_dotted, project = parent
        start = datetime.fromtimestamp(span.start_time / 1e9, tz=timezone.utc)
        run_id = str(uuid7_from_datetime(start))
        dotted = f"{parent_dotted}.{start.strftime('%Y%m%dT%H%M%S%fZ')}{run_id}"
        _set_langsmith_ids(span, trace_id, parent_run_id, run_id, dotted, project)
        self.register(span, trace_id, run_id, dotted, project)

    def on_end(self, span) -> None:
        with self._lock:
            self._runs.pop(span.get_span_context().span_id, None)


def _set_langsmith_ids(span, trace_id: str, parent_run_id: str, run_id: str, dotted: str, project: str) -> None:
    span.set_attribute("langsmith.trace.id", trace_id)
    span.set_attribute("langsmith.span.parent_id", parent_run_id)
    span.set_attribute("langsmith.span.id", run_id)
    span.set_attribute("langsmith.span.dotted_order", dotted)
    span.set_attribute("langsmith.trace.session_name", project)


telemetry = StrandsTelemetry()  # sets the global tracer provider and W3C propagators
stamper = LangSmithParentStamper()
telemetry.tracer_provider.add_span_processor(stamper)
telemetry.tracer_provider.add_span_processor(
    BatchSpanProcessor(
        create_langsmith_exporter(
            endpoint=f"{LANGSMITH_API_URL}/otel/v1/traces",
            headers={"x-api-key": os.environ["LANGSMITH_API_KEY"], "Langsmith-Project": config.PRIMARY_PROJECT},
        )
    )
)
tracer = trace_api.get_tracer("strands_service")


@tool
def strands_get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Strands service says: 80F and humid in {city}."


def _agent() -> Agent:
    return Agent(
        model=OpenAIModel(
            client_args={"base_url": os.environ["OPENAI_BASE_URL"], "api_key": os.environ["OPENAI_API_KEY"]},
            model_id=config.MODEL,
            params={"temperature": 0},
        ),
        tools=[strands_get_weather],
        system_prompt="Always call strands_get_weather for weather questions, then answer in one sentence.",
        callback_handler=None,
        trace_attributes={"langsmith.metadata.harness_run": harness_run.get() or ""},
    )


async def _invoke(message: str) -> str:
    return str(await _agent().invoke_async(message))


app = FastAPI()


class RunRequest(BaseModel):
    message: str


@app.post("/run/{variant}")
async def run(variant: str, body: RunRequest, request: Request) -> dict:
    headers = lower_headers(request.headers)
    bind_harness_run(headers)
    try:
        if variant == "4a":
            output = await _run_4a(body.message, headers)
        elif variant == "otel":
            output = await _run_otel(body.message, headers)
        elif variant in ("4d", "4e"):
            output = await _run_attributes(body.message, headers, stamp_subtree=variant == "4e")
        else:
            return {"error": f"unknown variant {variant}"}
    finally:
        # Ship spans before the caller starts verifying.
        telemetry.tracer_provider.force_flush()
    return {"output": output, "variant": variant}


@ls.traceable(name="strands:handle", run_type="chain")
async def _handle_native(message: str) -> str:
    return await _invoke(message)


async def _run_4a(message: str, headers: dict) -> str:
    # The LangSmith marker run nests under the caller; the question is whether
    # Strands' OTel spans follow it.
    with ls.tracing_context(parent=parent_from_headers(headers)):
        return await _handle_native(message, langsmith_extra={"metadata": marker_metadata(service="strands_service")})


async def _run_otel(message: str, headers: dict) -> str:
    context = propagate.extract(headers)
    with tracer.start_as_current_span("strands:handle", context=context) as span:
        span.set_attribute("langsmith.metadata.harness_run", harness_run.get() or "")
        return await _invoke(message)


async def _run_attributes(message: str, headers: dict, stamp_subtree: bool) -> str:
    parent: RunTree | None = parent_from_headers(headers)
    if parent is None:
        return await _invoke(message)
    with tracer.start_as_current_span("strands:handle") as span:
        start = datetime.fromtimestamp(span.start_time / 1e9, tz=timezone.utc)
        run_id = str(uuid7_from_datetime(start))
        dotted = f"{parent.dotted_order}.{start.strftime('%Y%m%dT%H%M%S%fZ')}{run_id}"
        project = parent.session_name or config.PRIMARY_PROJECT
        _set_langsmith_ids(span, str(parent.trace_id), str(parent.id), run_id, dotted, project)
        span.set_attribute("langsmith.metadata.harness_run", harness_run.get() or "")
        if stamp_subtree:
            stamper.register(span, str(parent.trace_id), run_id, dotted, project)
        return await _invoke(message)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=config.PORTS["strands_service"])
