"""Settings shared by every service. Values come from the repo-root .env."""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = REPO_ROOT / "results"

# Real environment variables win over .env so the Makefile can override per case.
load_dotenv(REPO_ROOT / ".env", override=False)

# Model served by the OpenAI-compatible gateway (OPENAI_BASE_URL / OPENAI_API_KEY).
MODEL = os.environ.get("HARNESS_MODEL", "gpt-4.1-mini")

PRIMARY_PROJECT = os.environ.get("LANGSMITH_PROJECT", "distributed-tracing-harness")
# Case 6: a second project in the same workspace, and a project in a second workspace.
REPLICA_PROJECT = os.environ.get("HARNESS_REPLICA_PROJECT", f"{PRIMARY_PROJECT}-replica")
REPLICA_WS_PROJECT = os.environ.get("HARNESS_REPLICA_WS_PROJECT", f"{PRIMARY_PROJECT}-ws2")
REPLICA_API_KEY = os.environ.get("LANGSMITH_API_KEY_REPLICA")
REPLICA_API_URL = os.environ.get("LANGSMITH_REPLICA_ENDPOINT", "https://api.smith.langchain.com")
# Case 3b: a project name configure_google_adk() is told to use instead of the caller's.
ADK_OVERRIDE_PROJECT = f"{PRIMARY_PROJECT}-adk-override"

PORTS = {
    "child_langgraph": 2024,
    "mda_agent": 2025,
    "mda_agent_patched": 2026,  # case 7c: MDA build with a patched graph factory
    "mcp_server": 8001,
    "adk_service": 8002,
    "strands_service": 8003,
    "adk_service_override": 8004,
}


def url(service: str) -> str:
    return f"http://127.0.0.1:{PORTS[service]}"
