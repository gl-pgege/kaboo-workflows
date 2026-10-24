"""Deterministic scripted model for testing workflows without an LLM.

``ScriptedModel`` is a ``strands.models.Model`` that drives the real strands
event loop from a fixed, per-agent script instead of a provider call. Reference
it from a config like any custom model, one per agent, so delegate calls, swarm
handoffs and graph nodes run the same way on every test run::

    agents:
      assistant:
        model:
          provider: kaboo_workflows.testing:ScriptedModel
          model_id: scripted
          params:
            script:
              - {tool: lookup, input: "order 42"}
            final_text: "Order 42 shipped yesterday."

A ``step`` is one model turn. The step index resets at the start of each fresh
human turn (a user message with text and no tool result) and advances on every
subsequent model call within that turn, so multi-tool turns and follow-ups both
stay deterministic. Each step is one of:

- ``{"tool": "<name>", "input": <str|dict>}`` — call a tool. The name is matched
  against the tools strands actually exposes this turn (exact, then substring),
  so sanitized/prefixed names still resolve. A string input is placed into the
  tool's first schema property; a dict is passed through. Delegate connections
  surface as tools named after the connection target, so ``{"tool": "analyst"}``
  triggers a delegation.
- ``{"tools": [<tool-action>, ...]}`` — call several tools in ONE assistant
  message (parallel tool calls), each with its own tool-use id.
- ``{"handoff": "<agent>", "message": "<text>"}`` — sugar for the swarm
  ``handoff_to_agent`` tool; fills the target/message against its live schema.
- ``{"text": "<final answer>"}`` — stream text and end the turn.
- ``{"error": "<message>"}`` — raise ``RuntimeError`` mid-turn to drive the
  error path (a failing node inside any orchestration).

When the script is exhausted the model emits ``final_text`` and stops.

The step is read from the conversation, not counted on the instance: it is the
number of assistant messages since the last human turn. A run that builds fresh
agents, a resume after a pause and a follow-up after a browser tool therefore
all land on the right step. ``turns`` gives each human turn its own script (the
first human turn plays ``turns[0]``, the second ``turns[1]``, and the last one
repeats); without it every turn plays ``script``.

``stream_chunk`` splits text and tool arguments into deltas of that many
characters, and ``stream_delay_ms`` waits between them, so a UI can be watched
rendering arguments as they stream.

Every call's messages are kept on ``calls`` and its system prompt on
``system_prompts``, so a test can assert exactly what the model was sent — which
is not always what the agent holds.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from strands.models import Model


def _validate_tool_pairing(messages: Any) -> None:
    """Enforce Bedrock's toolUse/toolResult pairing on the conversation.

    Bedrock rejects a turn whose toolResult blocks don't pair one-to-one with
    the toolUse blocks of the immediately preceding assistant message (e.g. a
    replayed transcript carrying a duplicate result for an interrupted call).
    The real provider raises ValidationException and strands force-stops the
    run silently; enforcing the same contract here makes the scripted model as
    strict as production so tests catch malformed conversations.

    Args:
        messages: The conversation strands is about to send to the model.

    Raises:
        RuntimeError: A toolResult block has no matching toolUse block in the
            preceding assistant message.
    """
    prev_use_ids: list[str] = []
    for i, msg in enumerate(messages or []):
        content = msg.get("content") or []
        result_ids = [
            b["toolResult"].get("toolUseId")
            for b in content
            if isinstance(b, dict) and "toolResult" in b
        ]
        if result_ids and (
            len(result_ids) > len(prev_use_ids) or any(r not in prev_use_ids for r in result_ids)
        ):
            raise RuntimeError(
                f"ValidationException: The number of toolResult blocks at "
                f"messages.{i}.content exceeds the number of toolUse blocks "
                "of previous turn."
            )
        prev_use_ids = [
            b["toolUse"].get("toolUseId") for b in content if isinstance(b, dict) and "toolUse" in b
        ]


class ScriptedModel(Model):
    """A strands model that replays a fixed script instead of calling a provider."""

    def __init__(
        self,
        model_id: str = "scripted",
        *,
        script: list[dict[str, Any]] | None = None,
        turns: list[list[dict[str, Any]]] | None = None,
        final_text: str = "Done.",
        stream_chunk: int = 0,
        stream_delay_ms: int = 0,
        **_: Any,
    ) -> None:
        """Initialize the scripted model.

        Args:
            model_id: Reported as the model id; any value works.
            script: The steps to replay, one per model turn. See the module
                docstring for the step shapes.
            turns: One script per human turn; the last repeats. Overrides
                ``script`` when given.
            final_text: The reply once the script is exhausted.
            stream_chunk: Characters per streamed delta; 0 sends each text and
                each tool's arguments as one delta.
            stream_delay_ms: Pause between deltas, for watching a UI render.
            **_: Extra model params from the config, accepted and ignored so a
                config written for a real provider still loads.
        """
        self._config: dict[str, Any] = {"model_id": model_id}
        self._script = script or []
        self._turns = [list(turn) for turn in turns] if turns else None
        self._final_text = final_text
        self._stream_chunk = max(0, int(stream_chunk))
        self._stream_delay = max(0, int(stream_delay_ms)) / 1000
        self._step = 0
        self.calls: list[list[dict[str, Any]]] = []
        self.system_prompts: list[Any] = []

    def update_config(self, **model_config: Any) -> None:
        """Merge ``model_config`` into the model's config.

        Args:
            **model_config: Config keys to set.
        """
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        """Return the model's config.

        Returns:
            The config dict, including ``model_id``.
        """
        return self._config

    async def structured_output(  # ty: ignore[invalid-method-override]
        self, output_model: Any, prompt: Any = None, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield a default instance of ``output_model``.

        Args:
            output_model: The Pydantic model to instantiate.
            prompt: Ignored.
            **kwargs: Ignored.

        Yields:
            One ``{"output": instance}`` event.
        """
        yield {"output": output_model()}

    async def stream(  # ty: ignore[invalid-method-override]
        self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        """Replay the next script step as a strands model event stream.

        Args:
            messages: The conversation strands sends; recorded on ``calls``.
            tool_specs: The tools available this turn, used to resolve tool
                names and fill tool inputs.
            system_prompt: Recorded on ``system_prompts``.
            **kwargs: Ignored.

        Yields:
            strands model stream events for one assistant message.

        Raises:
            RuntimeError: The step is an ``error`` step, or the conversation
                breaks Bedrock's tool-use pairing rule.
            ValueError: A tool step names a tool that is not available.
        """
        self.calls.append(copy.deepcopy(messages))
        self.system_prompts.append(system_prompt)
        _validate_tool_pairing(messages)

        script = self._script_for(messages)
        step = _steps_since_human_turn(messages)
        action = script[step] if step < len(script) else {"text": self._final_text}
        self._step = step + 1

        if "error" in action:
            raise RuntimeError(action["error"])

        yield {"messageStart": {"role": "assistant"}}

        if "text" in action:
            yield {"contentBlockStart": {"start": {}}}
            for piece in _pieces(action["text"], self._stream_chunk):
                yield {"contentBlockDelta": {"delta": {"text": piece}}}
                await self._pause()
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        elif "tools" in action:
            for sub in action["tools"]:
                spec = _resolve_tool_spec(sub, tool_specs or [])
                for event in _tool_use_events(
                    spec, _build_tool_input(sub, spec), self._stream_chunk
                ):
                    yield event
                    await self._pause()
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            spec = _resolve_tool_spec(action, tool_specs or [])
            for event in _tool_use_events(
                spec, _build_tool_input(action, spec), self._stream_chunk
            ):
                yield event
                await self._pause()
            yield {"messageStop": {"stopReason": "tool_use"}}

        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }

    def _script_for(self, messages: Any) -> list[dict[str, Any]]:
        """The script for the current human turn."""
        if not self._turns:
            return self._script
        index = max(0, _human_turns(messages) - 1)
        return self._turns[min(index, len(self._turns) - 1)]

    async def _pause(self) -> None:
        if self._stream_delay:
            await asyncio.sleep(self._stream_delay)


def _pieces(text: str, size: int) -> list[str]:
    """Split ``text`` into deltas of ``size`` characters; one delta when ``size`` is 0."""
    if size <= 0 or len(text) <= size:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]


def _tool_use_events(
    spec: dict[str, Any], tool_input: dict[str, Any], chunk: int = 0
) -> list[dict[str, Any]]:
    """Build the content-block events for one tool call.

    Args:
        spec: The tool's spec, as strands exposes it this turn.
        tool_input: The input to call the tool with.
        chunk: Characters per argument delta; 0 sends the arguments at once.

    Returns:
        The start, delta and stop events of one ``toolUse`` block.
    """
    start = {
        "contentBlockStart": {
            "start": {
                "toolUse": {
                    "name": spec["name"],
                    "toolUseId": f"call-{uuid.uuid4().hex[:8]}",
                }
            }
        }
    }
    deltas = [
        {"contentBlockDelta": {"delta": {"toolUse": {"input": piece}}}}
        for piece in _pieces(json.dumps(tool_input), chunk)
    ]
    return [start, *deltas, {"contentBlockStop": {}}]


def _is_human_text(message: Any) -> bool:
    """Whether a message is a person's turn: user text with no tool result."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    blocks = message.get("content", []) or []
    has_text = any(isinstance(b, dict) and "text" in b for b in blocks)
    has_tool_result = any(isinstance(b, dict) and "toolResult" in b for b in blocks)
    return has_text and not has_tool_result


def _human_turns(messages: Any) -> int:
    """How many human turns the conversation holds."""
    return sum(1 for message in messages or [] if _is_human_text(message))


def _steps_since_human_turn(messages: Any) -> int:
    """How many assistant messages follow the last human turn: the step to play."""
    steps = 0
    for message in reversed(messages or []):
        if _is_human_text(message):
            break
        if isinstance(message, dict) and message.get("role") == "assistant":
            steps += 1
    return steps


def _tool_name(spec: dict[str, Any]) -> str:
    """Return a tool spec's name.

    Args:
        spec: A strands tool spec.

    Returns:
        The tool name, or an empty string.
    """
    return spec.get("name", "")


def _tool_schema(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a tool spec's JSON input schema.

    Args:
        spec: A strands tool spec.

    Returns:
        The input schema, or an empty dict.
    """
    schema = spec.get("inputSchema", {})
    if isinstance(schema, dict) and "json" in schema:
        schema = schema["json"]
    return schema if isinstance(schema, dict) else {}


def _resolve_tool_spec(action: dict[str, Any], tool_specs: list[dict[str, Any]]) -> dict[str, Any]:
    """Find the tool a step names among the tools available this turn.

    Args:
        action: The script step.
        tool_specs: The tools strands exposes this turn.

    Returns:
        The matching tool spec: an exact name match, then a substring match,
        then (for a handoff step) any handoff tool.

    Raises:
        ValueError: No available tool matches.
    """
    wanted = action.get("tool") or ("handoff_to_agent" if "handoff" in action else "")
    exact = [s for s in tool_specs if _tool_name(s) == wanted]
    if exact:
        return exact[0]
    contains = [s for s in tool_specs if wanted and wanted in _tool_name(s)]
    if contains:
        return contains[0]
    if "handoff" in action:
        handoffs = [s for s in tool_specs if "handoff" in _tool_name(s)]
        if handoffs:
            return handoffs[0]
    raise ValueError(
        f"ScriptedModel: no tool matching {wanted!r}; available: {[_tool_name(s) for s in tool_specs]}"
    )


def _build_tool_input(action: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Build the input a step calls its tool with.

    Args:
        action: The script step.
        spec: The resolved tool spec.

    Returns:
        A handoff step's target and message mapped onto the handoff tool's
        schema, a dict input as given, or a string input placed in the tool's
        first schema property.
    """
    schema = _tool_schema(spec)
    props: dict[str, Any] = schema.get("properties", {}) if isinstance(schema, dict) else {}

    if "handoff" in action:
        target = action["handoff"]
        message = action.get("message", f"Handing off to {target}.")
        built: dict[str, Any] = {}
        for key, prop in props.items():
            low = key.lower()
            is_string = isinstance(prop, dict) and prop.get("type") == "string"
            if is_string and ("agent" in low or low in {"to", "handoff_to", "target"}):
                built[key] = target
            elif is_string and ("message" in low or "reason" in low):
                built[key] = message
        if "agent_name" not in built and not any("agent" in k.lower() for k in built):
            built["agent_name"] = target
        if not any("message" in k.lower() for k in built):
            built["message"] = message
        return built

    raw = action.get("input", "")
    if isinstance(raw, dict):
        return raw
    first_prop = next(iter(props), None)
    return {first_prop: raw} if first_prop else {}
