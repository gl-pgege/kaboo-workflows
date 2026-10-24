"""Browser tools for any agent, and the run's context for every agent.

A browser tool is one the client declares in ``RunAgentInput.tools`` and runs
itself: a question panel, a file request, opening a canvas. An agent may call
the ones its ``client_tools`` lists. Every call waits: the run ends with the
call in ``pendingToolCallIds``, the browser answers it with a tool message, and
the next run hands the answer to the agent that asked. The entry agent waits on
ag-ui-strands' own proxies; a delegated agent on the same proxies, whose wait
the delegate wrapper forwards to the entry (see
:func:`~kaboo_workflows.tools.wrappers.node_as_tool`).

The run's context (``RunAgentInput.context``) reaches every agent's model calls
through :class:`~kaboo_workflows.hooks.ContextHook`. An item whose description
starts with ``tool:<name>`` belongs to that browser tool and reaches only the
agents allowed it.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any

from ag_ui.core import RunAgentInput
from ag_ui_a2ui_toolkit import A2UI_SCHEMA_CONTEXT_DESCRIPTION
from ag_ui_strands import ToolBehavior
from ag_ui_strands.client_proxy_tool import sync_proxy_tools
from strands import Agent

from .._context import get_run_context, set_run_context
from ..config.schema import AgentDef, AppConfig
from ..hooks._run_context import TOOL_SCOPE_PREFIX
from ._display_tools import DISPLAY_TOOLS, RENDER_A2UI, a2ui_catalog, offer_display_tools
from ._strands_bridge import ensure_thread_agent

_TRACKED = "_kaboo_client_tools"
OPEN_GENERATIVE_UI = "generateSandboxedUi"


def waiting(names: Iterable[str]) -> dict[str, ToolBehavior]:
    """ag-ui-strands tool behaviours that make each named browser tool wait."""
    return {name: ToolBehavior(continue_after_frontend_call=False) for name in names}


def entry_agent_def(app_config: AppConfig) -> AgentDef | None:
    """The definition of the agent that answers the chat."""
    if app_config.entry in app_config.agents:
        return app_config.agents[app_config.entry]
    orchestration = app_config.orchestrations.get(app_config.entry)
    if orchestration is not None and orchestration.mode == "delegate":
        return app_config.agents.get(orchestration.entry_name)
    return None


def running_agents(app_config: AppConfig, resolved: Any) -> list[tuple[Agent, AgentDef]]:
    """Every agent object a run can execute, with the definition it was built from.

    A delegate orchestration runs a copy of its entry agent under the
    orchestration's name, so that copy carries its entry agent's definition.
    """
    pairs: list[tuple[Agent, AgentDef]] = []
    for name, agent in resolved.agents.items():
        if isinstance(agent, Agent) and name in app_config.agents:
            pairs.append((agent, app_config.agents[name]))
    for name, node in resolved.orchestrators.items():
        definition = app_config.orchestrations.get(name)
        if isinstance(node, Agent) and definition is not None and definition.mode == "delegate":
            agent_def = app_config.agents.get(definition.entry_name)
            if agent_def is not None:
                pairs.append((node, agent_def))
    return pairs


def offer_client_tools(agent: Agent, tools: list[Any]) -> None:
    """Give a delegated agent waiting stand-ins for these browser tools, and only these."""
    tracked = getattr(agent, _TRACKED, set())
    names = [tool.name for tool in tools]
    current = sync_proxy_tools(agent.tool_registry, tools, tracked, tool_behaviors=waiting(names))
    setattr(agent, _TRACKED, current)


def allowed(tools: Iterable[Any], client_tools: Collection[str]) -> list[Any]:
    """The browser tools an allow-list admits."""
    return [tool for tool in tools if tool.name in client_tools]


def _owning_tool(description: str) -> str | None:
    if description == A2UI_SCHEMA_CONTEXT_DESCRIPTION or description.startswith(
        "A2UI render tool usage guide"
    ):
        return RENDER_A2UI
    if OPEN_GENERATIVE_UI in description or "Websandbox" in description:
        return OPEN_GENERATIVE_UI
    return None


def split_context(context: Iterable[Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """Separate A2UI's catalog entries, which ag-ui-strands reads, from the context agents read.

    Items the client sends for one of its tools (A2UI's catalog, Open
    Generative UI's design guide and sandbox functions) are scoped to that
    tool, so they reach only the agents allowed it.
    """
    a2ui: list[Any] = []
    regular: list[dict[str, Any]] = []
    for item in context:
        data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        description = str(data.get("description") or "")
        if description == A2UI_SCHEMA_CONTEXT_DESCRIPTION:
            a2ui.append(item)
        owner = _owning_tool(description)
        if owner is not None:
            data = {**data, "description": f"{TOOL_SCOPE_PREFIX}{owner} {description}"}
        regular.append(data)
    return a2ui, regular


def prepare_run(input_data: RunAgentInput, session: Any) -> list[str]:
    """Hand every agent the browser tools and context it may use in this run.

    The context goes to every agent's model calls (bound for
    :class:`~kaboo_workflows.hooks.ContextHook`); ag-ui-strands keeps only
    A2UI's catalog entries, which it reads itself. The entry agent receives the
    browser tools its allow-list admits; each delegated agent, waiting
    stand-ins for its own. Call before the run task is created.

    Returns:
        The names of every browser tool the client offered.
    """
    a2ui, regular = split_context(input_data.context or [])
    set_run_context(regular)
    input_data.context = a2ui
    catalog = a2ui_catalog(regular)
    if (
        isinstance(input_data.forwarded_props, dict)
        and "injectA2UITool" in input_data.forwarded_props
    ):
        input_data.forwarded_props = {
            key: value
            for key, value in input_data.forwarded_props.items()
            if key != "injectA2UITool"
        }
    offered = [tool for tool in input_data.tools or [] if tool.name not in DISPLAY_TOOLS]
    resolved = session.resolved
    entry_def = entry_agent_def(resolved.app_config)
    input_data.tools = allowed(offered, entry_def.client_tools) if entry_def else []
    for agent, agent_def in running_agents(resolved.app_config, resolved):
        if agent is not resolved.entry:
            offer_display_tools(agent, agent_def.client_tools, catalog)
            offer_client_tools(agent, allowed(offered, agent_def.client_tools))
    return [tool.name for tool in offered]


async def equip_entry_agent(input_data: RunAgentInput, session: Any) -> None:
    """Give the thread's entry agent the display tools its allow-list names.

    ag-ui-strands runs the entry agent as a copy per thread, built from the
    template's tools as they were at start-up, so the tools are offered to that
    copy. Call after :func:`prepare_run`.
    """
    entry_def = entry_agent_def(session.resolved.app_config)
    if session.orchestrated or entry_def is None:
        return
    agent = await ensure_thread_agent(session.agui_agent, input_data)
    offer_display_tools(agent, entry_def.client_tools, a2ui_catalog(get_run_context()))


def take_browser_answers(
    input_data: RunAgentInput, carried: list[Any]
) -> dict[str, tuple[str, bool]]:
    """Take the browser's answers to delegated agents' calls out of a run's input.

    They answer calls the entry agent never made, so they resume the delegate
    waiting on each instead of joining the entry's conversation. Returns
    ``(content, is_error)`` by call id.
    """
    delegated = {
        call.id for message in carried for call in getattr(message, "tool_calls", None) or []
    }
    answers: dict[str, tuple[str, bool]] = {}
    kept: list[Any] = []
    for message in input_data.messages or []:
        call = (
            getattr(message, "tool_call_id", None)
            if getattr(message, "role", None) == "tool"
            else None
        )
        if isinstance(call, str) and call in delegated:
            content = message.content if isinstance(message.content, str) else str(message.content)
            answers[call] = (content, getattr(message, "error", None) is not None)
        else:
            kept.append(message)
    if answers:
        input_data.messages = kept
    return answers
