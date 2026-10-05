"""Google ADK agent behind FastAPI.

`TracingMiddleware` turns inbound `langsmith-trace`/`baggage` into the current
LangSmith parent; `configure_google_adk()` wraps ADK so `Runner.run_async`
opens a `google_adk.session` run under whatever parent is current.

Run a second copy with ADK_PROJECT set (case 3b) to see what happens when
configure_google_adk() is given its own project name.
"""

import os
import uuid

import langsmith as ls
import uvicorn
from fastapi import FastAPI, Request
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from langsmith.integrations.google_adk import configure_google_adk
from langsmith.middleware import TracingMiddleware
from pydantic import BaseModel

from harness import config
from harness.tracing import bind_harness_run, marker_metadata

configure_google_adk(project_name=os.environ.get("ADK_PROJECT") or None)

APP_NAME = "harness_adk"


@ls.traceable(name="adk:get_weather", run_type="tool")
def _get_weather(city: str) -> dict:
    return {"city": city, "temperature": "58F", "conditions": "Drizzle"}


def adk_get_weather(city: str) -> dict:
    """Get the current weather for a city."""
    return _get_weather(city, langsmith_extra={"metadata": marker_metadata(service="adk_service")})


agent = Agent(
    name="adk_weather_agent",
    # Any OpenAI-compatible gateway works through LiteLLM; no GOOGLE_API_KEY needed.
    model=LiteLlm(
        model=f"openai/{config.MODEL}",
        api_base=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
    ),
    instruction="Always call adk_get_weather for weather questions, then answer in one sentence.",
    tools=[adk_get_weather],
)
session_service = InMemorySessionService()
runner = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)

app = FastAPI()
app.add_middleware(TracingMiddleware)


class RunRequest(BaseModel):
    message: str


@app.post("/run")
async def run(body: RunRequest, request: Request) -> dict:
    bind_harness_run(request.headers)
    session = await session_service.create_session(app_name=APP_NAME, user_id="harness", session_id=str(uuid.uuid4()))
    output = None
    async for event in runner.run_async(
        user_id="harness",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=body.message)]),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            output = event.content.parts[0].text
    return {"output": output}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", config.PORTS["adk_service"]))
    uvicorn.run(app, host="127.0.0.1", port=port)
