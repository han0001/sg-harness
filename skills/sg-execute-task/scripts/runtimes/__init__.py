from .base import AgentRuntime
from .claude import ClaudeRuntime
from .codex import CodexRuntime


def get_runtime(name: str = "claude") -> AgentRuntime:
    if name == "claude":
        return ClaudeRuntime()
    if name == "codex":
        return CodexRuntime()
    raise ValueError(f"unsupported runtime: {name}")


__all__ = ["AgentRuntime", "ClaudeRuntime", "CodexRuntime", "get_runtime"]
