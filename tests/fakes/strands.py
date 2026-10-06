"""Owned fakes standing in for the strands runtime.

These let tests drive real ``strands.Agent`` / ``Swarm`` / ``Graph`` objects and
the real event loop without a provider network call or an MCP subprocess. They
are the single fake boundary — see the ``library-testing`` skill's Mocking
Policy. Never fabricate strands hook events with ``MagicMock``; build a real
agent around one of these models instead.
"""

from __future__ import annotations

import copy
from typing import Any

from strands.models import Model
from strands.models.bedrock import BedrockModel

from kaboo_workflows.mcp.server import MCPServer


class FakeModel(Model):
    """A strands ``Model`` that streams a fixed text response, no network.

    Drives the real strands event loop, so a real ``Agent`` built with this
    model emits genuine AGENT_START / TOKEN / AGENT_COMPLETE activity through
    ``EventPublisher``.
    """

    def __init__(
        self, text_chunks: list[str] | None = None, *, model_id: str = "fake-model"
    ) -> None:
        """Store the text chunks to stream and a reportable model id."""
        self._text_chunks = text_chunks if text_chunks is not None else ["Hello", " world"]
        self._config: dict[str, Any] = {"model_id": model_id}

    def update_config(self, **model_config: Any) -> None:
        """Merge overrides into the reported config."""
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        """Return the reported config (used by the manifest model descriptor)."""
        return self._config

    async def structured_output(self, output_model: Any, prompt: Any = None, **kwargs: Any):  # ty: ignore[invalid-method-override]
        """Yield a single empty structured-output instance (unused by most tests)."""
        yield {"output": output_model()}

    async def stream(
        self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ):
        """Stream a minimal text completion in the raw strands chunk protocol."""
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        for chunk in self._text_chunks:
            yield {"contentBlockDelta": {"delta": {"text": chunk}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 5, "outputTokens": 3, "totalTokens": 8},
                "metrics": {"latencyMs": 1},
            }
        }


class ToolThenTextModel(Model):
    """Streams one tool call on the first turn, then a text answer on the second."""

    def __init__(self, *, tool_name: str, tool_input: str = '{"name": "Bob"}') -> None:
        """Store the tool to call on the first turn."""
        self._config: dict[str, Any] = {"model_id": "fake-tool-model"}
        self._tool_name = tool_name
        self._tool_input = tool_input
        self._turn = 0

    def update_config(self, **model_config: Any) -> None:
        """Merge overrides into the reported config."""
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        """Return the reported config."""
        return self._config

    async def structured_output(self, output_model: Any, prompt: Any = None, **kwargs: Any):  # ty: ignore[invalid-method-override]
        """Yield a single empty structured-output instance (unused)."""
        yield {"output": output_model()}

    async def stream(
        self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ):
        """First turn: call the tool. Second turn: emit text and stop."""
        self._turn += 1
        yield {"messageStart": {"role": "assistant"}}
        if self._turn == 1:
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"name": self._tool_name, "toolUseId": "call-1"}}
                }
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": self._tool_input}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "Done"}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }


class BoomModel(Model):
    """A model whose stream raises, to drive the ERROR event path."""

    def __init__(self, *, message: str = "credentials expired") -> None:
        """Store the failure message the model call raises with."""
        self._message = message

    def update_config(self, **model_config: Any) -> None:
        """No-op — this model never succeeds."""

    def get_config(self) -> dict[str, Any]:
        """Return a minimal config."""
        return {"model_id": "boom-model"}

    async def structured_output(self, output_model: Any, prompt: Any = None, **kwargs: Any):  # ty: ignore[invalid-method-override]
        """Raise on structured output too."""
        raise RuntimeError(self._message)
        yield  # pragma: no cover — makes this an async generator

    async def stream(
        self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ):
        """Raise a provider-style error mid-call."""
        raise RuntimeError(self._message)
        yield  # pragma: no cover — makes this an async generator


class LocalBedrockModel(BedrockModel):
    """A ``BedrockModel`` that answers locally and records what it was sent.

    Skips ``BedrockModel.__init__``, so no boto client is created and nothing
    reaches AWS, while code that checks for a Bedrock model still sees one.
    With ``tool`` it calls that tool on its first turn, so one invocation makes
    two model calls.
    """

    def __init__(
        self,
        model_id: str = "us.anthropic.claude-sonnet-4-6-v1:0",
        *,
        tool: str | None = None,
        cache_config: Any = None,
    ) -> None:
        """Store the reported config and the optional first-turn tool."""
        self.config = {"model_id": model_id}
        if cache_config is not None:
            self.config["cache_config"] = cache_config
        self._tool = tool
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(
        self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ):
        """Record the messages, then call ``tool`` once or answer with text."""
        self.calls.append(copy.deepcopy(messages))
        yield {"messageStart": {"role": "assistant"}}
        if self._tool is not None and len(self.calls) == 1:
            start = {"toolUse": {"name": self._tool, "toolUseId": "bedrock-call-1"}}
            yield {"contentBlockStart": {"start": start}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"key": "k"}'}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "Done."}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }


class FakeMCPServer(MCPServer):
    """A real ``MCPServer`` subtype that records lifecycle calls, no uvicorn thread.

    Subclasses the ABC so it is accepted by ``MCPLifecycle.add_server`` while
    overriding every runtime method to be inert and observable.
    """

    def __init__(
        self,
        *,
        url: str = "http://localhost:0/mcp",
        ready: bool = True,
        record: list[str] | None = None,
        label: str = "server",
    ) -> None:
        """Store the reported URL, readiness result, and optional shared order log."""
        super().__init__(name=label)
        self.calls: list[str] = []
        self._url = url
        self._will_be_ready: bool = ready
        self._record = record
        self._label = label

    def _register_tools(self, mcp: Any) -> None:
        """No tools to register on the fake."""

    def start(self) -> None:
        """Record a start."""
        self.calls.append("start")

    def wait_ready(self, timeout: float = 30) -> bool:
        """Record a readiness probe and return the configured result."""
        self.calls.append("wait_ready")
        return self._will_be_ready

    def stop(self) -> None:
        """Record a stop (and its order relative to clients, when a log is shared)."""
        self.calls.append("stop")
        if self._record is not None:
            self._record.append(self._label)

    @property
    def url(self) -> str:
        """Return the reported URL."""
        return self._url


class FakeMCPClient:
    """Minimal MCP client stand-in for lifecycle ordering tests."""

    def __init__(self, *, record: list[str] | None = None, label: str = "client") -> None:
        """Initialise an empty call log and optional shared order log."""
        self.calls: list[str] = []
        self._record = record
        self._label = label

    def start(self) -> None:
        """Record a start."""
        self.calls.append("start")

    def stop(self, exc_type: Any = None, exc_val: Any = None, exc_tb: Any = None) -> None:
        """Record a stop (matches the strands MCPClient stop signature)."""
        self.calls.append("stop")
        if self._record is not None:
            self._record.append(self._label)
