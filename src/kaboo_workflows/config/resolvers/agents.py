"""Resolve AgentDef -> strands Agent instances and orchestration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from strands import Agent

from ...exceptions import ConfigurationError
from ...hooks.context_hook import ContextHook
from ...hooks.interrupt_hook import GateRule, InterruptHook
from ...hooks.reference_hook import ReferenceHook
from ...hooks.state_effects import StateEffectsHook
from ...hooks.tool_approval_hook import ToolApprovalHook
from ...hooks.tool_attachments_hook import ToolAttachmentsHook
from ...tools.references import fetch_attachment, list_references
from ...utils import load_object
from .conversation_manager import resolve_conversation_manager
from .hooks import resolve_hook_entry
from .mcp import resolve_tools
from .models import resolve_model, with_thinking
from .session_manager import resolve_leaf_session_manager

if TYPE_CHECKING:
    from strands.agent.conversation_manager import ConversationManager
    from strands.models import Model
    from strands.session.session_manager import SessionManager
    from strands.tools.mcp import MCPClient as StrandsMCPClient

    from ..schema import AgentDef, AttachmentsDef, InterruptToolDef, SessionManagerDef

logger = logging.getLogger(__name__)


def _resolve_gate_rule(agent_name: str, tool_def: InterruptToolDef) -> GateRule:
    """Turn one ``interrupt.tools`` entry into a :class:`GateRule`.

    Raises:
        TypeError: If ``subject`` does not resolve to a callable.
    """
    subject = None
    if tool_def.subject is not None:
        subject = load_object(tool_def.subject, target="approval subject")
        if not callable(subject):
            raise TypeError(
                f"Agent '{agent_name}': approval subject {tool_def.subject!r} for "
                f"'{tool_def.name}' must be a callable (tool_name, tool_input), "
                f"got {type(subject).__name__}."
            )
    return GateRule(pattern=tool_def.name, remember=tool_def.remember, subject=subject)


def build_agent_from_def(
    name: str,
    agent_def: AgentDef,
    models: dict[str, Model],
    mcp_clients: dict[str, StrandsMCPClient],
    *,
    global_session_manager_def: SessionManagerDef | None = None,
    session_id: str | None = None,
    extra_tools: list[Any] | None = None,
    extra_hooks: list[Any] | None = None,
    orchestration_agent_names: set[str] | None = None,
    attachments: AttachmentsDef | None = None,
) -> Agent:
    """Build a single Agent from an AgentDef blueprint.

    This is the canonical way to construct an agent from its YAML definition.
    Used by both :func:`resolve_agents` (to build all declared agents) and
    by :func:`~kaboo_workflows.config.resolvers.orchestrations.builders.build_delegate`
    (to fork an agent with delegate tools).

    Args:
        name: Agent name / agent_id.
        agent_def: The agent's schema definition.
        models: Resolved model objects keyed by name.
        mcp_clients: Resolved MCP client objects keyed by name.
        global_session_manager_def: Global session manager def from
            ``AppConfig.session_manager``, used as a fallback when the agent
            declares no ``session_manager:`` of its own and has not explicitly
            opted out (``session_manager: ~``).
        session_id: Effective session id threaded down from ``load_session``.
            Passed as ``session_id_override`` to every
            ``resolve_session_manager`` call made in this function.
        extra_tools: Additional tools to append (e.g. delegate tools).
        extra_hooks: Additional hooks to append (e.g. orchestration-level hooks).
        orchestration_agent_names: Agent names in swarm/graph orchestrations (fail-fast on SM).
        attachments: Global reference/attachment policy from ``AppConfig``. When
            the agent is in scope (its own ``attachments:`` or the global
            ``default``), a :class:`~kaboo_workflows.hooks.ReferenceHook` is
            attached (so the manifest reaches this agent AND, via hook
            forwarding, the entry clone) and — when the global ``tool`` flag is
            set — the built-in ``list_references`` / ``fetch_attachment`` tools
            are registered.

    Returns:
        A freshly constructed strands Agent.

    Raises:
        ConfigurationError: If a swarm or graph agent has a session manager.
        TypeError: If a custom factory doesn't return an Agent.
    """
    # 1. Resolve model — inline ModelDef or global name ref, with the agent's thinking
    model: Model | None = None
    if agent_def.model is not None:
        if isinstance(agent_def.model, str):
            model = models[agent_def.model]
            if agent_def.thinking is not None:
                model = with_thinking(model, agent_def.thinking)
        else:
            model = resolve_model(agent_def.model, agent_def.thinking)

    # 2. Resolve tool specs
    tools = resolve_tools(agent_def.tools)

    # 3. Resolve hooks — module:ClassName or inline HookDef
    hooks = [resolve_hook_entry(h) for h in agent_def.hooks]

    # 4. MCP clients as tool providers
    tool_providers: list[Any] = [mcp_clients[n] for n in agent_def.mcp]

    # 5. Resolve session manager (uniform leaf chain).
    #   1. agent_def.session_manager set        → resolve it (per-agent override)
    #   2. agent_def explicit opt-out (None)    → None
    #   3. global_session_manager_def set       → resolve it (inherited global)
    #   4. otherwise                            → None
    agent_session: SessionManager | None = resolve_leaf_session_manager(
        leaf_def=agent_def.session_manager,
        leaf_is_set="session_manager" in agent_def.model_fields_set,
        global_def=global_session_manager_def,
        session_id=session_id,
    )

    # Fail fast: swarm/graph node agents cannot carry a session manager.
    if (
        orchestration_agent_names
        and name in orchestration_agent_names
        and agent_session is not None
    ):
        source = (
            "per-agent 'session_manager:' field"
            if agent_def.session_manager is not None
            else "global 'session_manager:' in config"
        )
        raise ConfigurationError(
            f"Agent '{name}' is in a swarm or graph orchestration and cannot have a session manager "
            f"(source: {source}).\n"
            "Strands does not yet support session persistence for Swarm or Graph node agents.\n"
            f"Fix: Add 'session_manager: ~' to agent '{name}' to opt out of the global default."
        )

    # 6. Resolve conversation manager
    conversation_manager: ConversationManager | None = None
    if agent_def.conversation_manager is not None:
        conversation_manager = resolve_conversation_manager(agent_def.conversation_manager)

    # 6b. Resolve interrupt configuration
    agent_kwargs = dict(agent_def.agent_kwargs)
    interrupt = agent_def.interrupt
    if interrupt is not None:
        if interrupt.tools:
            hooks.append(
                InterruptHook(
                    tools=[_resolve_gate_rule(name, t) for t in interrupt.tools],
                    agent_name=name,
                    ttl_seconds=interrupt.ttl_seconds,
                    skip_read_only=interrupt.skip_read_only,
                    remember=interrupt.remember,
                )
            )
        if interrupt.tool_approvals:
            hooks.append(ToolApprovalHook(agent_name=name))

    # 6bb. Resolve reference/attachment policy. Attach the ReferenceHook at
    # build time (not wire time) so it rides `_kaboo_hook_providers`: the
    # blueprint fires it for sub-agents, and the AG-UI adapter forwards it to
    # the per-thread entry clone — the same forwarding path used for HITL hooks.
    ref_default = attachments.default if attachments is not None else "reference"
    ref_tool = attachments.tool if attachments is not None else True
    agent_attachments = agent_def.attachments
    if agent_attachments is not None and not isinstance(agent_attachments, (bool, str)):
        ref_enabled = agent_attachments.enabled
        ref_inline = agent_attachments.inline
    else:
        ref_enabled = ref_default != "none"
        ref_inline = False
    if ref_enabled:
        hooks.append(ReferenceHook(enabled=True, inline=ref_inline, tool_enabled=ref_tool))
        if ref_tool:
            tools.extend([list_references, fetch_attachment])

    # 6bbb. Attached at build time for the same reason as the ReferenceHook: it
    # must reach the per-thread clone the AG-UI adapter runs for the entry.
    clearing = agent_def.context.clear_tool_results if agent_def.context is not None else None
    if clearing is None:
        hooks.append(
            ContextHook(
                client_tools=agent_def.client_tools,
                workspace=agent_def.workspace,
                views=agent_def.views,
            )
        )
    else:
        hooks.append(
            ContextHook(
                clear_trigger=clearing.trigger,
                keep_last=clearing.keep_last,
                min_chars=clearing.min_chars,
                keep_tools=clearing.keep_tools,
                client_tools=agent_def.client_tools,
                workspace=agent_def.workspace,
                views=agent_def.views,
            )
        )
    hooks.append(StateEffectsHook(agent_name=name))

    # 6bbbb. Media a tool returned as data becomes media blocks; attached at
    # build time for the same reason as the hooks above.
    hooks.append(ToolAttachmentsHook())

    # 6c. Resolve output_schema
    if agent_def.output_schema is not None:
        schema_cls = load_object(agent_def.output_schema, target="output schema")
        agent_kwargs["structured_output_model"] = schema_cls

    # 7. Build the agent
    all_tools = tools + tool_providers + (extra_tools or [])
    all_hooks = hooks + (extra_hooks or [])

    if agent_def.type is not None:
        factory = load_object(agent_def.type, target="agent factory")
        agent = factory(
            name=name,
            agent_id=name,
            model=model,
            system_prompt=agent_def.system_prompt,
            description=agent_def.description,
            tools=all_tools,
            hooks=all_hooks,
            conversation_manager=conversation_manager,
            session_manager=agent_session,
            **agent_kwargs,
        )
    else:
        agent = Agent(
            name=name,
            agent_id=name,
            model=model,
            system_prompt=agent_def.system_prompt,
            description=agent_def.description,
            tools=all_tools,
            hooks=all_hooks,
            conversation_manager=conversation_manager,
            session_manager=agent_session,
            load_tools_from_directory=False,
            **agent_kwargs,
        )

    if not isinstance(agent, Agent):
        raise TypeError(
            f"Agent factory for '{name}' returned {type(agent).__name__}, expected strands.Agent."
        )

    # Retain the kaboo HookProviders on the agent. strands stores hooks as a
    # HookRegistry of bound callbacks and drops the original provider objects,
    # but ag-ui-strands forks a fresh per-thread Agent for the entry node and
    # only wires providers passed via ``StrandsAgent(hooks=...)``. Stashing them
    # here lets the AG-UI adapter forward them so interrupt/HITL hooks fire on
    # the executing per-thread clone, not just this blueprint. See
    # ``get_agent_hook_providers`` and ``create_agui_app``.
    setattr(agent, "_kaboo_hook_providers", list(all_hooks))

    return agent


def get_agent_hook_providers(agent: Agent) -> list[Any]:
    """Return the kaboo HookProviders retained on *agent* (empty if none).

    These are the providers declared for the agent at build time (YAML
    ``hooks:`` + interrupt hooks + orchestration hooks) — deliberately *not*
    the runtime-attached :class:`EventPublisher` (which is stateful and scoped
    to a single template, and whose events for the entry node are not rendered).
    """
    return list(getattr(agent, "_kaboo_hook_providers", []) or [])


def resolve_agents(
    agent_defs: dict[str, AgentDef],
    models: dict[str, Model],
    mcp_clients: dict[str, StrandsMCPClient],
    *,
    global_session_manager_def: SessionManagerDef | None = None,
    session_id: str | None = None,
    orchestration_agent_names: set[str] | None = None,
    attachments: AttachmentsDef | None = None,
) -> dict[str, Agent]:
    """Resolve all agents -- flat, no dependencies between them.

    Each agent is independent.  They reference models, MCP clients, and
    sessions -- but NOT other agents.  Agent-to-agent wiring is handled by
    the orchestration step.

    ``hooks`` entries are either import-path strings
    (``module.path:ClassName`` or ``./file.py:ClassName``) or inline HookDef
    objects -- resolved per-agent so each agent gets fresh hook instances.

    Args:
        agent_defs: Agent definitions keyed by name.
        models: Resolved model instances keyed by name.
        mcp_clients: Resolved MCP clients keyed by name.
        global_session_manager_def: Global session manager def from
            ``AppConfig.session_manager``, used as a fallback when an agent
            declares no ``session_manager:`` of its own and has not explicitly
            opted out (``session_manager: ~``).
        session_id: Effective session id threaded down from ``load_session``.
            Passed as ``session_id_override`` to every
            ``resolve_session_manager`` call made in this function.
        orchestration_agent_names: Names of agents in swarm or graph orchestrations
            (these agents must not have a session manager).

    Returns:
        Resolved agents dict keyed by name.

    Raises:
        ConfigurationError if an agent used in a Swarm or Graph orchestration has a session manager assigned.
        TypeError if agent factory does not return a strands Agent instance.
    """
    resolved: dict[str, Agent] = {}

    for name, agent_def in agent_defs.items():
        agent = build_agent_from_def(
            name=name,
            agent_def=agent_def,
            models=models,
            mcp_clients=mcp_clients,
            global_session_manager_def=global_session_manager_def,
            session_id=session_id,
            orchestration_agent_names=orchestration_agent_names,
            attachments=attachments,
        )
        resolved[name] = agent
        logger.info("agent=<%s>, type=<%s> | resolved agent", name, agent_def.type or "Agent")

    return resolved
