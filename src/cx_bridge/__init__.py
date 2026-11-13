"""Portable Claude Code, Codex, Antigravity, ACP, and multi-framework agent bridge."""
__version__ = "0.3.0"


def __getattr__(name):
    if name in ("AgentRequest", "AgentResponse", "BridgeConnector", "BridgeError", "serve_framework_target"):
        from . import connector

        return getattr(connector, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    "AgentRequest",
    "AgentResponse",
    "BridgeConnector",
    "BridgeError",
    "serve_framework_target",
]
