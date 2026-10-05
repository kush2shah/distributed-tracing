"""Case 7b: the closest user-side hook MDA offers for inbound trace context.

MDA's generated graph factory never applies `langsmith-trace`, and middleware
runs inside a graph whose root run already exists, so the best middleware can
do is re-parent work started inside it. This wraps each tool call in
`tracing_context(parent=...)` using the headers Agent Server put in configurable.

It only activates when the caller's LangSmith metadata carries
`harness_mda_middleware`, so cases 7a and 7b run against the same server.
"""

import langsmith as ls
from langchain.agents.middleware import wrap_tool_call
from langgraph.config import get_config


@wrap_tool_call
async def continue_caller_trace(request, handler):
    configurable = get_config().get("configurable", {})
    metadata = configurable.get("langsmith-metadata") or {}
    parent = configurable.get("langsmith-trace")
    if not parent or not metadata.get("harness_mda_middleware"):
        return await handler(request)
    with ls.tracing_context(parent=parent, project_name=configurable.get("langsmith-project"), metadata=metadata):
        return await handler(request)
