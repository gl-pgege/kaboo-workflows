"""State effects: tool results written into the shared state as they happen.

``state_effects`` in the config name a tool (or a glob) and either a mapper or a
path. After a matching call succeeds, the effect's JSON Patch operations are
applied to the run's shared state and sent to the client as a ``STATE_DELTA``,
so the canvas follows the agent and every later model call sees the result in
its workspace view (see :class:`~kaboo_workflows.hooks.ContextHook`).
"""

from __future__ import annotations

import copy
import fnmatch
import json
import logging
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import jsonpatch
import jsonpointer
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent

from .._context import StateChannel, get_run_id, get_state_channel
from ..utils import load_object

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StateEffectCall:
    """What a state effect's mapper receives about the call that succeeded.

    Attributes:
        tool: The tool's name.
        input: The call's arguments.
        result: The tool's result: its text parsed as JSON when it is JSON,
            otherwise the text.
        agent: The agent that made the call.
        run_id: The run the call belongs to.
        state: The shared state before this effect, to read and never modify.
    """

    tool: str
    input: Any
    result: Any
    agent: str
    run_id: str
    state: dict[str, Any]


Effect = Callable[[StateEffectCall], Iterable[dict[str, Any]]]


def compile_effects(defs: Iterable[Any]) -> list[tuple[str, Effect]]:
    """Turn ``state_effects`` definitions into ``(tool glob, effect)`` pairs."""
    effects: list[tuple[str, Effect]] = []
    for definition in defs:
        if definition.mapper is not None:
            mapper = load_object(definition.mapper, target="state effect mapper")
            effects.append((definition.tool, mapper))
        else:
            path = definition.path
            effects.append(
                (
                    definition.tool,
                    lambda call, path=path: [{"op": "add", "path": path, "value": call.result}],
                )
            )
    return effects


def _result_value(result: Any) -> Any:
    structured = (result or {}).get("structuredContent")
    if structured is not None:
        return structured
    for block in (result or {}).get("content") or []:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str):
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


def apply_effect(channel: StateChannel, operations: list[dict[str, Any]]) -> bool:
    """Apply operations to the run's shared state and send them to the client.

    Returns:
        Whether they applied. Operations that do not fit the state are dropped.
    """
    if not operations:
        return False
    try:
        jsonpatch.apply_patch(channel.state, operations, in_place=True)
    except (jsonpatch.JsonPatchException, jsonpointer.JsonPointerException):
        logger.warning(
            "operations=<%s> | state effect does not fit the state; dropped",
            operations,
            exc_info=True,
        )
        return False
    for operation in operations:
        key = jsonpointer.JsonPointer(operation["path"]).parts[:1]
        if key:
            channel.touched.add(key[0])
    if channel.emit is not None:
        channel.emit(operations)
    return True


def fold_effects(snapshot: dict[str, Any]) -> None:
    """Put the keys this run's effects changed into a state snapshot passed on."""
    channel = get_state_channel()
    if channel is None:
        return
    for key in channel.touched:
        if key in channel.state:
            snapshot[key] = copy.deepcopy(channel.state[key])


class StateEffectsHook(HookProvider):
    """Apply the run's state effects after each successful tool call."""

    def __init__(self, *, agent_name: str = "") -> None:
        """Initialize the hook.

        Args:
            agent_name: The agent's name, handed to mappers.
        """
        self._agent_name = agent_name

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._on_after_tool)

    def _on_after_tool(self, event: AfterToolCallEvent) -> None:
        channel = get_state_channel()
        if channel is None or not channel.effects or event.exception is not None:
            return
        if (event.result or {}).get("status") != "success":
            return
        tool = event.tool_use.get("name", "")
        call: StateEffectCall | None = None
        for pattern, effect in channel.effects:
            if not fnmatch.fnmatchcase(tool, pattern):
                continue
            if call is None:
                call = StateEffectCall(
                    tool=tool,
                    input=event.tool_use.get("input"),
                    result=_result_value(event.result),
                    agent=self._agent_name,
                    run_id=get_run_id() or "",
                    state=copy.deepcopy(channel.state),
                )
            try:
                operations = list(effect(call))
            except Exception:
                logger.warning("tool=<%s> | state effect failed", tool, exc_info=True)
                continue
            apply_effect(channel, operations)


def workspace_view(pointers: Iterable[str]) -> str | None:
    """The parts of the shared state an agent sees, as text for its model call."""
    channel = get_state_channel()
    if channel is None:
        return None
    parts: list[str] = []
    for pointer in pointers:
        try:
            value = jsonpointer.resolve_pointer(channel.state, pointer)
        except jsonpointer.JsonPointerException:
            continue
        parts.append(f"{pointer}: {json.dumps(value, separators=(',', ':'), default=str)}")
    if not parts:
        return None
    return "The workspace — what this conversation has built, current as of now:\n" + "\n".join(
        parts
    )
