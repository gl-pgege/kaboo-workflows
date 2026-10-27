"""Tools the client renders from their arguments, so no call waits for it.

``render_a2ui`` is the A2UI middleware's render tool: the middleware paints the
surface from the call's streamed arguments, wherever on the wire the call
appears, delegates' calls included. An agent whose ``client_tools`` names it
gets a real tool that checks the components against the client's catalog and
answers at once, so the agent goes on with its turn; a malformed surface comes
back as an error the model corrects.

A browser action (``browser_actions`` in the config) works the same way: the
client declares the tool, the agent gets a real tool with that name and schema
that answers at once, and the client carries the call out as it streams.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from typing import Any

from ag_ui_a2ui_toolkit import (
    A2UI_SCHEMA_CONTEXT_DESCRIPTION,
    DEFAULT_GENERATION_GUIDELINES,
    RENDER_A2UI_TOOL_DEF,
    format_validation_errors,
    validate_a2ui_components,
)
from strands import Agent, tool
from strands.tools.tools import PythonAgentTool

RENDER_A2UI: str = RENDER_A2UI_TOOL_DEF["function"]["name"]
DISPLAY_TOOLS = frozenset({RENDER_A2UI})
ACTION_RESULT = "Done: the browser carried out this call as it arrived."

_TRACKED = "_kaboo_display_tools"


def a2ui_catalog(context: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    for item in context:
        if str(item.get("description") or "").endswith(A2UI_SCHEMA_CONTEXT_DESCRIPTION):
            value = item.get("value")
            try:
                parsed = json.loads(value) if isinstance(value, str) else value
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return None


def render_a2ui_tool(catalog: dict[str, Any]) -> Any:
    components = catalog.get("components")
    check = {"components": components} if isinstance(components, dict) and components else None
    description = (
        f"{RENDER_A2UI_TOOL_DEF['function']['description']}\n\n{DEFAULT_GENERATION_GUIDELINES}"
    )

    @tool(name=RENDER_A2UI, description=description)
    def render_a2ui(
        surfaceId: str, components: list[dict[str, Any]], data: dict[str, Any] | None = None
    ) -> str:  # noqa: N803
        """Render an A2UI surface in the chat.

        Args:
            surfaceId: Unique surface identifier.
            components: The A2UI v0.9 component array, flat; the root component has id "root".
            data: Optional initial data model for the surface.
        """
        result = validate_a2ui_components(
            components=components, data=data, catalog=check, validate_bindings=False
        )
        if not result["valid"]:
            problems = format_validation_errors(result["errors"])
            return f"Error: the surface was not rendered. Fix these and call {RENDER_A2UI} again.\n{problems}"
        return f"Rendered surface {surfaceId} in the chat."

    return render_a2ui


def browser_action_tool(declared: Any) -> Any:
    data = declared.model_dump() if hasattr(declared, "model_dump") else dict(declared)
    name = str(data["name"])
    spec = {
        "name": name,
        "description": str(data.get("description") or ""),
        "inputSchema": {"json": data.get("parameters") or {"type": "object", "properties": {}}},
    }

    def answer(tool_use: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "toolUseId": tool_use["toolUseId"],
            "status": "success",
            "content": [{"text": ACTION_RESULT}],
        }

    answer.__name__ = name
    return PythonAgentTool(tool_name=name, tool_spec=spec, tool_func=answer)  # ty: ignore[invalid-argument-type]


def offer_display_tools(
    agent: Agent,
    client_tools: Collection[str],
    catalog: dict[str, Any] | None,
    actions: Iterable[Any] = (),
) -> None:
    registry = agent.tool_registry
    wanted = (
        {RENDER_A2UI: render_a2ui_tool(catalog)}
        if catalog is not None and RENDER_A2UI in client_tools
        else {}
    )
    for declared in actions:
        if declared.name in client_tools:
            wanted[declared.name] = browser_action_tool(declared)
    for name in getattr(agent, _TRACKED, set()) | set(wanted):
        registry.registry.pop(name, None)
        registry.dynamic_tools.pop(name, None)
    for display in wanted.values():
        registry.register_tool(display)
    setattr(agent, _TRACKED, set(wanted))
