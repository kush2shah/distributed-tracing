"""Trace-context helpers used on both sides of every service boundary.

Two independent channels cross each boundary:

* LangSmith tracing headers (`langsmith-trace`, `baggage`), which are what we test.
* `x-harness-run`, a correlation ID that lets verify.py find a service's runs even
  when tracing propagation failed and they landed in a separate trace.
"""

import contextvars
import json
import urllib.parse
from collections.abc import Mapping
from typing import Any

import langsmith as ls
from langsmith import get_current_run_tree
from langsmith.run_trees import ApiKeyAuth, RunTree

from harness import config

HARNESS_RUN_HEADER = "x-harness-run"
# Case 6c: ask a downstream service to attach its own credentials to replicas it
# inherits, since API keys and api_url never travel in `baggage`.
REWRITE_REPLICAS_HEADER = "x-harness-rewrite-replicas"
# Case 6 workaround: langsmith>=0.6.3 no longer writes replicas into `baggage`
# (receivers still parse `langsmith-replicas`), so add them back on outbound calls.
FORWARD_REPLICAS_HEADER = "x-harness-forward-replicas"
# The fields the SDK's receiving side accepts from headers; credentials stay out.
_HEADER_SAFE_REPLICA_FIELDS = ("project_name", "primary", "updates")

harness_run: contextvars.ContextVar[str | None] = contextvars.ContextVar("harness_run", default=None)
rewrite_replicas: contextvars.ContextVar[bool] = contextvars.ContextVar("rewrite_replicas", default=False)
forward_replicas: contextvars.ContextVar[bool] = contextvars.ContextVar("forward_replicas", default=False)


def marker_metadata(**extra: Any) -> dict[str, Any]:
    """Metadata every marker run carries so verify.py can find it."""
    return {"harness_run": harness_run.get(), **extra}


def outbound_headers(propagate: bool = True) -> dict[str, str]:
    """Headers for a call to another service, built from the current run."""
    headers: dict[str, str] = {}
    if run_id := harness_run.get():
        headers[HARNESS_RUN_HEADER] = run_id
    if rewrite_replicas.get():
        headers[REWRITE_REPLICAS_HEADER] = "1"
    if forward_replicas.get():
        headers[FORWARD_REPLICAS_HEADER] = "1"
    if propagate and (run_tree := get_current_run_tree()):
        headers.update(run_tree.to_headers())
        if item := replica_baggage_item():
            headers["baggage"] = f"{headers['baggage']},{item}" if headers.get("baggage") else item
    return headers


def replica_baggage_item() -> str | None:
    """`langsmith-replicas=...` for the current run, without credentials or URLs.

    Only emitted when the case opts in, so the matrix shows SDK-default behavior too.
    """
    run_tree = get_current_run_tree()
    if not forward_replicas.get() or run_tree is None or not run_tree.replicas:
        return None
    safe = [{k: r[k] for k in _HEADER_SAFE_REPLICA_FIELDS if r.get(k) is not None} for r in run_tree.replicas]
    return f"langsmith-replicas={urllib.parse.quote(json.dumps(safe))}"


def lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def bind_harness_run(headers: Mapping[str, str]) -> None:
    """Adopt the caller's correlation ID (and case-6c flag) so outbound calls forward them."""
    headers = lower_headers(headers)
    if run_id := headers.get(HARNESS_RUN_HEADER):
        harness_run.set(run_id)
    if headers.get(REWRITE_REPLICAS_HEADER) == "1":
        rewrite_replicas.set(True)
    if headers.get(FORWARD_REPLICAS_HEADER) == "1":
        forward_replicas.set(True)


def parent_from_headers(headers: Mapping[str, str]) -> RunTree | None:
    """Build the distributed parent, optionally re-attaching replica credentials.

    `RunTree.from_headers` takes replicas from `baggage` and they override any
    `replicas=` passed alongside, so credentials must be added to the parsed
    RunTree rather than to `tracing_context(replicas=...)`.
    """
    headers = lower_headers(headers)
    if "langsmith-trace" not in headers:
        return None
    parent = RunTree.from_headers(headers)
    if headers.get(REWRITE_REPLICAS_HEADER) == "1":
        parent.replicas = attach_replica_credentials(parent.replicas)
    return parent


def attach_replica_credentials(replicas: list | None) -> list:
    """Give the second-workspace replica this service's own key and URL."""
    rewritten = []
    for replica in replicas or []:
        replica = dict(replica)
        if replica.get("project_name") == config.REPLICA_WS_PROJECT and config.REPLICA_API_KEY:
            replica["api_url"] = config.REPLICA_API_URL
            replica["auth"] = ApiKeyAuth(api_key=config.REPLICA_API_KEY)
        rewritten.append(replica)
    return rewritten


def continue_trace(headers: Mapping[str, str]):
    """`tracing_context` that continues the caller's trace, replicas included.

    LangChain/LangGraph runs started under a distributed RunTree parent do not
    inherit the parent's replicas, so pass them explicitly as well.
    """
    parent = parent_from_headers(headers)
    return ls.tracing_context(parent=parent, replicas=(parent.replicas or None) if parent else None)
