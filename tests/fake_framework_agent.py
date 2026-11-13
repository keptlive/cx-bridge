"""Deterministic Python agent-framework fixture using cx_bridge.serve_framework_target."""
import sys

from cx_bridge import AgentRequest, AgentResponse, serve_framework_target

allow_write = "--writable" in sys.argv
clean_argv = [a for a in sys.argv[1:] if a != "--writable"]


async def async_framework_handler(req: AgentRequest) -> AgentResponse:
    # Simulate noisy third-party framework banners (CrewAI / Rich / LangChain)
    # printing directly to stdout; serve_framework_target isolates this to stderr.
    print("NOISY_FRAMEWORK_STDOUT_BANNER_DO_NOT_LEAK")
    if "RAISE_ERROR" in req.task:
        raise RuntimeError("private-framework-exception-detail")
    sid = req.session or "langgraph-session-1"
    return AgentResponse(
        response=f"framework({req.mode}):{req.task}",
        session_id=sid,
        tool_calls=["graph_retriever"],
    )


raise SystemExit(
    serve_framework_target(
        async_framework_handler,
        name="langgraph-fixture",
        version="1.0.0",
        supports_write=allow_write,
        argv=clean_argv,
    )
)
