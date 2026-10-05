"""Case 7 tool. The marker run lets verify.py find this agent's work in LangSmith."""

import langsmith as ls
from langchain.tools import tool
from langchain_core.runnables import RunnableConfig


@ls.traceable(name="mda:lookup_weather", run_type="tool")
def _lookup_weather(city: str) -> str:
    return f"MDA says: 50F and windy in {city}."


@tool
def mda_lookup_weather(city: str, config: RunnableConfig) -> str:
    """Look up the weather for a city."""
    # The harness correlation ID arrives as an x-* request header, if the server forwards it.
    harness_run = config.get("configurable", {}).get("x-harness-run")
    return _lookup_weather(city, langsmith_extra={"metadata": {"harness_run": harness_run, "service": "mda_agent"}})
