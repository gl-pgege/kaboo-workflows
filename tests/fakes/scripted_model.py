"""Deterministic scripted model for end-to-end orchestration tests.

A ``strands.models.Model`` subclass that drives the real strands event loop with
a fixed, per-agent script instead of a live LLM. Each agent references its own
scripted model, so delegate calls, swarm handoffs, and graph nodes run exactly
the same way on every request — ideal for asserting how each orchestration mode
(and their nested combinations) behaves without any network.

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
  message (parallel tool calls), each with its own tool-use id. Used to drive
  parallel gated tools / simultaneous interrupts in a single step.
- ``{"handoff": "<agent>", "message": "<text>"}`` — sugar for the swarm
  ``handoff_to_agent`` tool; fills the target/message against its live schema.
- ``{"text": "<final answer>"}`` — stream text and end the turn.
- ``{"error": "<message>"}`` — raise ``RuntimeError`` mid-turn to drive the
  error path (a failing node inside any orchestration).

When the script is exhausted the model emits ``final_text`` and stops.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from strands.models import Model


def _validate_tool_pairing(messages: Any) -> None:
    """Enforce Bedrock's toolUse/toolResult pairing on the conversation.

    Bedrock rejects a turn whose toolResult blocks don't pair one-to-one with
    the toolUse blocks of the immediately preceding assistant message (e.g. a
    replayed transcript carrying a duplicate result for an interrupted call).
    The real provider raises ValidationException and strands force-stops the
    run silently; enforcing the same contract here makes the fakes as strict
    as production so tests catch malformed conversations.
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
    def __init__(
        self,
        model_id: str = "scripted",
        *,
        script: list[dict[str, Any]] | None = None,
        final_text: str = "Done.",
        **_: Any,
    ) -> None:
        self._config: dict[str, Any] = {"model_id": model_id}
        self._script = script or []
        self._final_text = final_text
        self._step = 0

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return self._config

    async def structured_output(self, output_model: Any, prompt: Any = None, **kwargs: Any):  # ty: ignore[invalid-method-override]
        yield {"output": output_model()}

    async def stream(
        self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ):
        _validate_tool_pairing(messages)
        if _is_fresh_human_turn(messages):
            self._step = 0

        action = (
            self._script[self._step]
            if self._step < len(self._script)
            else {"text": self._final_text}
        )
        self._step += 1

        if "error" in action:
            raise RuntimeError(action["error"])

        yield {"messageStart": {"role": "assistant"}}

        if "text" in action:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": action["text"]}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        elif "tools" in action:
            for sub in action["tools"]:
                spec = _resolve_tool_spec(sub, tool_specs or [])
                tool_input = _build_tool_input(sub, spec)
                yield {
                    "contentBlockStart": {
                        "start": {
                            "toolUse": {
                                "name": spec["name"],
                                "toolUseId": f"call-{uuid.uuid4().hex[:8]}",
                            }
                        }
                    }
                }
                yield {
                    "contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}
                }
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            spec = _resolve_tool_spec(action, tool_specs or [])
            tool_input = _build_tool_input(action, spec)
            yield {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {
                            "name": spec["name"],
                            "toolUseId": f"call-{uuid.uuid4().hex[:8]}",
                        }
                    }
                }
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}

        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }


def _is_fresh_human_turn(messages: Any) -> bool:
    """True when the last message is a human text turn (not a tool result).

    strands appends tool results as user-role messages, so we distinguish a real
    new human turn (text, no toolResult) from a tool-result continuation.
    """
    if not messages:
        return True
    last = messages[-1]
    if last.get("role") != "user":
        return False
    blocks = last.get("content", []) or []
    has_text = any(isinstance(b, dict) and "text" in b for b in blocks)
    has_tool_result = any(isinstance(b, dict) and "toolResult" in b for b in blocks)
    return has_text and not has_tool_result


def _tool_name(spec: dict[str, Any]) -> str:
    return spec.get("name", "")


def _tool_schema(spec: dict[str, Any]) -> dict[str, Any]:
    schema = spec.get("inputSchema", {})
    if isinstance(schema, dict) and "json" in schema:
        schema = schema["json"]
    return schema if isinstance(schema, dict) else {}


def _resolve_tool_spec(action: dict[str, Any], tool_specs: list[dict[str, Any]]) -> dict[str, Any]:
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
